"""解析基类：列映射探测 + 单位/日期归一化 + 统一 parse 脚手架。

每个渠道解析器（wechat / alipay / creditcard）继承 BaseParser，提供：
- default_col_map：该渠道常见列名默认映射（首次无模板时使用）
- classify(amount, view)：该渠道特有的交易类型判定

parse() 统一完成：按 col_map 读取列 -> 归一化 -> 分类 -> 产出
标准化 transaction 字典列表。商户名模糊、转出转入识别、退款识别等
归类逻辑在此或渠道 classify 中处理。
"""
from __future__ import annotations
import re
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import pandas as pd

# ── 标准字段契约（每个渠道解析器产出） ──
STANDARD_FIELDS = (
    "tx_date", "amount", "merchant", "counterparty",
    "description", "direction", "currency", "entry_date", "payment_method",
)

# 各渠道列名探测候选（按优先级）。归一化后做包含匹配。
# 已扩充各银行/各平台常见列名变体，提升不同格式账单的探测命中率。
_COLUMN_HINTS: dict[str, list[str]] = {
    "tx_date": ["交易时间", "交易日期", "交易创建时间", "消费时间",
                "支付时间", "记账时间", "入账时间", "交易日", "时间", "日期"],
    "amount": ["收/支金额", "交易金额", "支出金额", "收入金额", "金额",
               "交易金额(人民币)", "交易金额(元)", "交易金额（人民币）", "实付金额",
               "实收金额", "支付金额", "交易金额(人民币元)"],
    "direction": ["收/支", "收支类型", "收支", "交易类型", "收支方向"],
    "merchant": ["商品名称", "商品", "商户名称", "商户", "交易摘要", "摘要",
                 "交易描述", "交易内容", "交易说明", "商品说明", "商户信息"],
    "counterparty": ["交易对方", "对方", "转入", "转出", "交易对方名称",
                     "对方账户", "收方", "付方", "对方户名"],
    "description": ["备注", "商品说明", "摘要", "交易备注", "说明",
                    "商品描述", "交易详情"],
    "currency": ["币种", "交易币种", "货币", "交易货币"],
    "entry_date": ["记账日期", "记账日", "入账日期", "入账时间", "还款入账日",
                   "记账时间", "入账日"],
    "payment_method": ["支付方式", "收/付款方式", "付款方式", "收付款方式", "收/付方式"],
}


def _norm(s: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(s))


def find_header_row(df: pd.DataFrame, max_scan: int = 40) -> int | None:
    """在前 max_scan 行中定位表头行：命中标准字段 hint 最多的行。

    用于跳过账单前导的标题行/空行/汇总行/注释，让表头不在第一行也能正确解析。
    微信/支付宝真实导出的账单前导说明文字较多（标题、汇总、注释），表头常在第
    15~20 行，故默认扫描 40 行以覆盖此类格式。
    返回行索引；命中字段不足 2 个则返回 None（交由调用方按原表头处理）。
    """
    hints_all = [_norm(h) for hints in _COLUMN_HINTS.values() for h in hints]
    best_row, best_hits = None, 0
    for i in range(min(max_scan, len(df))):
        row_vals = [_norm(v) for v in df.iloc[i].tolist()]
        hits = sum(1 for nv in row_vals if nv and any(nh in nv for nh in hints_all))
        if hits > best_hits:
            best_row, best_hits = i, hits
    if best_hits >= 2:
        return best_row
    return None


def detect_columns(df: pd.DataFrame, source: str | None = None) -> dict[str, str]:
    """试探列映射 -> {标准字段: 实际列名}。仅作建议，可手动校准。"""
    cols = list(df.columns)
    out: dict[str, str] = {}
    for field, hints in _COLUMN_HINTS.items():
        # 精确匹配优先
        for h in hints:
            nh = _norm(h)
            for c in cols:
                if _norm(c) == nh:
                    out[field] = c
                    break
            if field in out:
                break
        # 宽松包含匹配
        if field not in out:
            for h in hints:
                nh = _norm(h)
                for c in cols:
                    if nh and nh in _norm(c):
                        out[field] = c
                        break
                if field in out:
                    break
    return out


def to_decimal(v: Any) -> Decimal:
    """把各种金额写法规整为 Decimal。支持 ¥/￥、千分位、括号负数。"""
    if v is None:
        return Decimal("0")
    s = str(v).replace("¥", "").replace("￥", "").replace(",", "").replace(" ", "").strip()
    if not s or s in ("-", "—", "N/A", "nan", "NaN", "None", ""):
        return Decimal("0")
    # 括号负数，如 (123.45) 表示 -123.45
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()
    try:
        d = Decimal(s).quantize(Decimal("0.01"))
        return -d if neg else d
    except InvalidOperation:
        return Decimal("0")


def to_datetime(v: Any) -> Optional[datetime]:
    """时间字符串 -> datetime（仅保留带时分秒的值）；无时间部分返回 None。

    微信/支付宝账单的「交易时间」精确到秒，是判断同一笔订单是否重复的关键；
    信用卡账单通常只有「月/日」，此时返回 None，由去重引擎退回按日期比对。
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("nan", "None", "NaT", "", "-"):
        return None
    # Excel 日期序列号（整数部分是日期，小数部分是一天内的时刻）
    try:
        num = float(s)
        if s.replace(".", "", 1).isdigit() and 1 <= num <= 80000:
            dt = datetime(1899, 12, 30) + timedelta(days=num)
            return dt if num % 1 else None
    except ValueError:
        pass
    if ":" not in s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def to_date(v: Any) -> Optional[date]:
    """时间字符串 -> date，兼容常见格式与 Excel 日期序列号；无日期返回 None。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("nan", "None", "NaT", "", "-"):
        return None
    # Excel 日期序列号（纯数字，从 1899-12-30 起算的天数）
    if s.isdigit():
        n = int(s)
        if 1 <= n <= 80000:
            try:
                return date(1899, 12, 30) + timedelta(days=n)
            except (ValueError, OverflowError):
                pass
    # 仅取前 19 字符作时间解析（兼容带毫秒/时区的字符串）
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M:%S", "%Y%m%d", "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    # 信用卡账单等场景仅有"月/日"（如 10/21），无年份，补当前年份
    try:
        md = datetime.strptime(s, "%m/%d")
        return date(datetime.now().year, md.month, md.day)
    except ValueError:
        pass
    return None


def norm_direction(v: Any) -> str:
    """方向值归一化，返回 income/expense/transfer/空串。

    兼容各平台方向列的常见写法（收/支、收支、交易类型、+/- 等）。
    支付宝「不计收支」、微信「中性交易」均为不影响收支的中性流水
    （还款/提现/理财申赎等），统一映射为 transfer，聚合时过滤。
    """
    s = str(v or "").strip()
    if s in ("收入", "+", "收", "转入", "收款"):
        return "income"
    if s in ("支出", "-", "付", "转出", "付款", "消费"):
        return "expense"
    if s in ("转账", "转给", "资金挪移", "不计收支", "中性交易", "/"):
        return "transfer"
    return ""


class BaseParser:
    """渠道解析器基类。"""

    source: str = ""
    default_col_map: dict[str, str] = {}

    def detect(self, df: pd.DataFrame) -> dict[str, str]:
        """探测列映射：默认列名确实存在于 df 时信任默认，否则用探测结果覆盖。

        这样当账单实际列名与默认不同（如微信用"交易日期"而非"交易时间"）时，
        探测结果能正确顶替默认，避免按不存在的列名取值落空。
        """
        m = dict(self.default_col_map)
        probed = detect_columns(df, self.source)
        for k, v in probed.items():
            # 默认列名确实存在于 df 时保留默认，避免探测误匹配覆盖正确默认
            if k in m and m[k] in df.columns:
                continue
            m[k] = v
        return m

    def classify(self, amount: Decimal, view: dict[str, Any]) -> str:
        """交易类型判定钩子，渠道可覆盖。默认：正=消费，负=退款。"""
        return "refund" if amount < 0 else "consume"

    def parse(self, df: pd.DataFrame, col_map: dict[str, str]) -> list[dict]:
        """按 col_map 把 DataFrame 转为标准化 transaction 字典列表。"""
        txns: list[dict] = []
        for i, row in df.iterrows():
            def g(field: str) -> Any:
                if field not in col_map:
                    return None
                return row.get(col_map[field], None)

            tx_date = to_date(g("tx_date"))
            if tx_date is None:
                continue  # 无日期行（如汇总行、空行）跳过
            tx_dt = to_datetime(g("tx_date"))

            raw = to_decimal(g("amount"))
            view = {f: g(f) for f in STANDARD_FIELDS}

            bill_type = self.classify(raw, view)
            absamt = abs(raw)
            stored = -absamt if bill_type == "refund" else absamt

            txns.append({
                "source": self.source,
                "bill_type": bill_type,
                "amount": stored,
                "currency": "CNY",
                "tx_date": tx_date,
                "tx_datetime": tx_dt,
                "entry_date": to_date(g("entry_date")),
                "payment_method": str(g("payment_method") or ""),
                "merchant": str(g("merchant") or ""),
                "counterparty": str(g("counterparty") or ""),
                "description": str(g("description") or ""),
                "source_row": int(i) + 2,  # 含表头行号，便于排查
            })
        return txns
