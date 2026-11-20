"""账单操作：清空当月、手动录入。"""
from datetime import date
from decimal import Decimal
import uuid

from sqlalchemy.orm import Session
from sqlalchemy import or_, update

from app.models import BillFile, Transaction, MergeGroup, DedupCandidate
from app.services import aggregator, dedup_engine


def clear_month(db: Session, month: str) -> dict:
    """清空指定月份所有交易及关联数据，返回删除计数。

    删除顺序（避免外键冲突）：
    1. 引用该月交易的去重候选
    2. 代表或成员在该月的合并组 —— 先把这些组的所有成员交易
       的 merged_group_id 置空（含跨月成员），再删组
    3. 该月交易
    4. 已无交易的账单文件
    """
    start, end = aggregator.month_range(month)
    txs = db.query(Transaction).filter(
        Transaction.tx_date >= start, Transaction.tx_date <= end).all()
    if not txs:
        return {"deleted_tx": 0, "deleted_files": 0}
    tx_ids = [t.id for t in txs]
    file_ids = {t.file_id for t in txs}

    # 1. 删除引用该月交易的去重候选
    db.query(DedupCandidate).filter(
        or_(DedupCandidate.tx_a_id.in_(tx_ids),
            DedupCandidate.tx_b_id.in_(tx_ids))).delete(synchronize_session=False)

    # 2. 找出要删的合并组：成员在当月，或代表在当月
    member_group_ids = {t.merged_group_id for t in txs if t.merged_group_id}
    rep_group_ids = {g.id for g in db.query(MergeGroup)
                     .filter(MergeGroup.representative_tx_id.in_(tx_ids)).all()}
    all_group_ids = member_group_ids | rep_group_ids
    if all_group_ids:
        # 先把这些组的所有成员交易的 merged_group_id 置空（含跨月成员，避免外键冲突）
        db.execute(update(Transaction)
                   .filter(Transaction.merged_group_id.in_(all_group_ids))
                   .values(merged_group_id=None))
        # 再删合并组本身
        db.query(MergeGroup).filter(
            MergeGroup.id.in_(all_group_ids)).delete(synchronize_session=False)

    # 3. 删除该月交易
    db.query(Transaction).filter(Transaction.id.in_(tx_ids)).delete(synchronize_session=False)

    # 4. 删除已无交易的账单文件
    deleted_files = 0
    for fid in file_ids:
        remaining = db.query(Transaction).filter(Transaction.file_id == fid).count()
        if remaining == 0:
            db.query(BillFile).filter(BillFile.id == fid).delete(synchronize_session=False)
            deleted_files += 1

    db.commit()
    return {"deleted_tx": len(tx_ids), "deleted_files": deleted_files}


def manual_create(db: Session, data: dict) -> dict:
    """手动录入一笔交易，自动参与跨渠道去重。"""
    source = data["source"]
    bill_type = data["bill_type"]
    amount = Decimal(str(data["amount"]))
    tx_date = data["tx_date"]
    if isinstance(tx_date, str):
        tx_date = date.fromisoformat(tx_date)
    merchant = data.get("merchant") or ""
    counterparty = data.get("counterparty") or ""
    description = data.get("description") or ""
    currency = data.get("currency") or "CNY"

    # 退款存负，其余存正（与解析器一致）
    stored = -abs(amount) if bill_type == "refund" else abs(amount)

    # 创建虚拟账单文件（手动录入无原始文件）
    file = BillFile(
        source=source, file_name="手动录入",
        file_hash="manual-" + uuid.uuid4().hex,
        row_count=1, status="parsed",
    )
    db.add(file)
    db.flush()

    tx = Transaction(
        file_id=file.id, source=source, bill_type=bill_type,
        amount=stored, currency=currency,
        tx_date=tx_date, merchant=merchant, counterparty=counterparty,
        description=description, is_primary=True, confidence="independent",
    )
    db.add(tx)
    db.commit()

    # 手动录入同样参与去重，发现与已有账单的重复
    candidates = dedup_engine.run_dedup(db, file.id)
    db.commit()
    return {"id": tx.id, "file_id": file.id, "dedup_candidates": candidates}
