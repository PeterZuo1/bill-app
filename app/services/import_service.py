"""导入编排：上传 -> 列探测/模板 -> 解析 -> 落库 -> 去重。

支持多格式账单：Excel(.xlsx/.xls)、CSV、PDF。按文件扩展名选择读取器，
无扩展名时按 Excel -> CSV -> PDF 顺序兜底尝试，保证不同平台导出的账单都能导入。
"""
import csv
import io
import re
import uuid

import pandas as pd
from sqlalchemy.orm import Session

from app.config import UPLOAD_MAX_MB
from app.models import BillFile, Transaction, ImportTemplate
from app.parsers import get_parser
from app.parsers.base import find_header_row
from app.services.file_hash import sha256_of_bytes
from app.services.dedup_engine import run_dedup


def _read_excel(data: bytes) -> pd.DataFrame:
    # 先不带表头读取，再定位真实表头行（跳过前导标题行/空行），
    # 让表头不在第一行的账单也能正确解析列名。
    df = pd.read_excel(io.BytesIO(data), sheet_name=0, header=None, dtype=str).fillna("")
    hr = find_header_row(df)
    if hr is None:
        hr = 0  # 兜底：未探测到表头时按第一行当表头
    cols = df.iloc[hr].tolist()
    df = df.iloc[hr + 1:].copy()
    df.columns = [str(c).strip() for c in cols]
    df = df.reset_index(drop=True)
    return df


def _decode_bytes(data: bytes) -> str:
    """按常见编码依次尝试解码字节，返回文本。

    支付宝等国内平台导出的 CSV 多为 GBK/GB18030，部分带 BOM 用 utf-8-sig。
    """
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    # 兜底：gb18030 容错解码，尽量不丢数据
    return data.decode("gb18030", errors="ignore")


def _df_from_rows(rows: list[list]) -> pd.DataFrame:
    """把二维行列表整理为 DataFrame：定位表头行、设列名、去前导行。

    Excel/CSV/PDF 读取后都走此函数，统一表头定位逻辑，
    让前导说明行/标题行的账单也能正确解析。
    """
    df_raw = pd.DataFrame(rows, dtype=str).fillna("")
    hr = find_header_row(df_raw)
    if hr is None:
        hr = 0  # 兜底：未探测到表头时按第一行当表头
    cols = df_raw.iloc[hr].tolist()
    df = df_raw.iloc[hr + 1:].copy()
    df.columns = [str(c).strip() for c in cols]
    df = df.reset_index(drop=True)
    return df


def _read_csv(data: bytes) -> pd.DataFrame:
    """读取 CSV：先解码文本，再用 csv 模块安全逐行解析（兼容引号内逗号）。

    支付宝等 CSV 前导有标题/说明行，列数与数据行不一致，直接 read_csv 会报错，
    故先解析为行列表，再交由 _df_from_rows 定位表头行。
    """
    text = _decode_bytes(data)
    rows = list(csv.reader(io.StringIO(text)))
    return _df_from_rows(rows)


# 银行/信用卡 PDF 明细行：交易日 记账日 摘要 金额 卡号末四位 原币金额
# 部分行（如还款）只有交易日没有记账日，单独匹配
_PDF_ROW_TWO_DATES = re.compile(
    r"^(\d{1,2}/\d{1,2})\s+(\d{1,2}/\d{1,2})\s+(.+?)\s+(-?[\d,]+\.\d{2})\s+(\d{4})\s+(\S.*)$")
_PDF_ROW_ONE_DATE = re.compile(
    r"^(\d{1,2}/\d{1,2})\s+(.+?)\s+(-?[\d,]+\.\d{2})\s+(\d{4})\s+(\S.*)$")

_PDF_CARD_HEADER = ["交易日", "记账日", "交易摘要", "人民币金额", "卡号末四位", "交易地金额"]


def _pdf_card_rows_from_text(text: str) -> list[list[str]]:
    """从 PDF 文本层逐行还原信用卡明细。

    pdfplumber 的 extract_tables 对无完整表格线的银行账单会漏行（实测招行账单
    只能抽到约一半），而文本层是完整的，故按行正则还原标准列结构。
    """
    rows: list[list[str]] = []
    for raw in (text or "").splitlines():
        s = " ".join(raw.split())
        m = _PDF_ROW_TWO_DATES.match(s)
        if m:
            rows.append(list(m.groups()))
            continue
        m = _PDF_ROW_ONE_DATE.match(s)
        if m:
            # 仅一个日期（如「掌上生活还款」）：交易日取该日期，记账日留空
            rows.append([m.group(1), "", *m.groups()[1:]])
    return rows


def _read_pdf(data: bytes, source: str = "") -> pd.DataFrame:
    """读取 PDF：优先表格提取，信用卡账单改用文本层还原完整明细。"""
    import pdfplumber

    table_rows: list[list] = []
    text_rows: list[list[str]] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for table in (page.extract_tables() or []):
                for row in table:
                    table_rows.append(["" if c is None else str(c) for c in row])
            if source == "card":
                text_rows.extend(_pdf_card_rows_from_text(page.extract_text() or ""))

    # 信用卡账单：文本层行数更多时采用文本还原结果，避免表格漏行
    if source == "card" and len(text_rows) > len(table_rows):
        return _df_from_rows([_PDF_CARD_HEADER, *text_rows])
    if not table_rows:
        return pd.DataFrame()
    return _df_from_rows(table_rows)


def read_bill_table(data: bytes, filename: str = "", source: str = "") -> pd.DataFrame:
    """按文件扩展名选择读取器，统一多格式账单入口。

    无扩展名或未知扩展名时，按 Excel -> CSV -> PDF 顺序兜底尝试，
    首个能产出非空表格的读取器即采用，保证不同平台账单都能导入。
    """
    name = (filename or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext in ("xlsx", "xls"):
        return _read_excel(data)
    if ext == "csv":
        return _read_csv(data)
    if ext == "pdf":
        return _read_pdf(data, source)
    # 无扩展名/未知扩展名：按顺序兜底尝试
    for reader in (_read_excel, _read_csv, lambda d: _read_pdf(d, source)):
        try:
            df = reader(data)
            if df is not None and not df.empty:
                return df
        except Exception:
            continue
    return _read_csv(data)


def detect_template(source: str, data: bytes, filename: str = "") -> dict:
    """探测列映射建议（仅返回建议，不落库）。"""
    df = read_bill_table(data, filename, source)
    parser = get_parser(source)
    return parser.detect(df)


def _resolve_col_map(db: Session, source: str, df: pd.DataFrame) -> dict:
    """取当前渠道 active 模板；列名对不上当前账单时改用探测结果。

    模板列名全部存在于当前 df 才信任模板；否则本次用探测结果，
    但不覆盖用户手动校准的模板，保证不同格式账单都能正确导入。
    """
    tmpl = (
        db.query(ImportTemplate)
        .filter(ImportTemplate.source == source, ImportTemplate.active.is_(True))
        .first()
    )
    if tmpl is not None and tmpl.col_map:
        cm = dict(tmpl.col_map)
        if all(v in df.columns for v in cm.values() if v):
            # 旧模板可能缺少后来新增的字段（如支付方式），用探测结果补齐
            probed = get_parser(source).detect(df)
            missing = {k: v for k, v in probed.items() if k not in cm}
            if missing:
                cm.update(missing)
                tmpl.col_map = cm
                db.commit()
            return cm
        # 模板列名对不上当前账单：本次用探测结果，保留用户校准的模板
        return get_parser(source).detect(df)
    parser = get_parser(source)
    col_map = parser.detect(df)
    tmpl = ImportTemplate(
        source=source, name=f"{source}_default",
        col_map=col_map, active=True,
    )
    db.add(tmpl)
    db.commit()
    return col_map


def upload_and_import(
    db: Session, source: str, data: bytes, filename: str, force: bool = False,
) -> dict:
    """导入一个账单文件，返回结果 dict。

    - 同一文件（sha256 相同）默认跳过，返回 status=duplicate，避免重复记账；
    - force=True 时强制再次导入（用于修正后重导），指纹追加后缀规避唯一约束。
    """
    if source not in ("wechat", "alipay", "card"):
        raise ValueError(f"不支持的渠道 source={source}")
    if len(data) > UPLOAD_MAX_MB * 1024 * 1024:
        raise ValueError("文件超过限制")

    file_hash = sha256_of_bytes(data)

    existing = db.query(BillFile).filter(BillFile.file_hash == file_hash).first()
    if existing is not None:
        if not force:
            return {
                "status": "duplicate",
                "file_id": existing.id,
                "source": existing.source,
                "file_name": existing.file_name,
                "rows_imported": 0,
                "dedup_candidates": 0,
                "file_hash": file_hash,
                "duplicate": True,
            }
        file_hash = file_hash + "-" + uuid.uuid4().hex[:8]

    # 解析（按文件格式选择 Excel/CSV/PDF 读取器）
    df = read_bill_table(data, filename, source)
    parser = get_parser(source)
    col_map = _resolve_col_map(db, source, df)
    txns: list[dict] = parser.parse(df, col_map)

    # 落库
    bill_file = BillFile(
        source=source, file_name=filename or "upload",
        file_hash=file_hash, row_count=len(txns), status="parsed",
    )
    db.add(bill_file)
    db.flush()

    for t in txns:
        t["file_id"] = bill_file.id
        db.add(Transaction(**t))
    db.commit()

    # 去重
    candidates = run_dedup(db, bill_file.id)
    bill_file.status = "deduped"
    db.commit()

    return {
        "status": "ok",
        "file_id": bill_file.id,
        "source": source,
        "file_name": filename or "upload",
        "rows_imported": len(txns),
        "dedup_candidates": candidates,
        "file_hash": file_hash,
        "duplicate": False,
    }
