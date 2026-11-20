"""微信支付账单解析器。

微信对账单典型列：交易时间 / 交易类型 / 收/支 / 交易对方 /
商品名称 / 收/支金额 / 当前状态 / 支付方式 / 备注。

注意：首次无模板时列探测会给建议，实际解析时按手动校准后的
col_map 读取。bill_type 判定依据方向（收/支）+ 关键词。
"""
from decimal import Decimal
from typing import Any

from app.parsers.base import BaseParser, norm_direction

_TRANSFER_KW = ("转账", "红包", "转给")
_REFUND_KW = ("退款",)


class WechatParser(BaseParser):
    source = "wechat"
    default_col_map = {
        "tx_date": "交易时间",
        "amount": "收/支金额",
        "direction": "收/支",
        "merchant": "商品名称",
        "counterparty": "交易对方",
        "description": "备注",
        "payment_method": "支付方式",
    }

    def classify(self, amount: Decimal, view: dict[str, Any]) -> str:
        direction = norm_direction(view.get("direction"))
        merchant = str(view.get("merchant") or "")
        counterparty = str(view.get("counterparty") or "")
        desc = str(view.get("description") or "")
        blob = f"{merchant}|{counterparty}|{desc}"

        if any(k in blob for k in _REFUND_KW):
            return "refund"
        if direction == "income":
            return "income"
        if direction == "expense":
            # 转给自己或他人的资金挪移记为 transfer（聚合过滤）
            if any(k in blob for k in _TRANSFER_KW):
                return "transfer"
            return "consume"
        if direction == "transfer":
            # 中性交易（提现/零钱通存取/信用卡还款等）：不计入收支
            return "transfer"
        # 无方向列时的兜底：负金额=退款
        return "refund" if amount < 0 else "consume"
