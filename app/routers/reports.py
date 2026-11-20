"""路由：月度报表 / 交易明细 / 手动录入。"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Transaction
from app.schemas import MonthlyReport, TransactionOut, TransactionPage, ManualTxCreate
from app.services import aggregator, bill_ops

router = APIRouter(prefix="/api", tags=["reports"])


@router.get("/reports/monthly", response_model=MonthlyReport)
def monthly(
    month: str = Query(..., pattern="^\\d{4}-\\d{2}$"),
    db: Session = Depends(get_db),
):
    return aggregator.get_monthly(db, month)


@router.get("/transactions", response_model=TransactionPage)
def list_transactions(
    month: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    bill_type: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None, max_length=50),
    primary_only: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    q = db.query(Transaction)
    if month:
        start, end = aggregator.month_range(month)
        q = q.filter(Transaction.tx_date >= start, Transaction.tx_date <= end)
    if source:
        q = q.filter(Transaction.source == source)
    if bill_type:
        q = q.filter(Transaction.bill_type == bill_type)
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        q = q.filter(or_(
            Transaction.merchant.like(like),
            Transaction.counterparty.like(like),
            Transaction.description.like(like),
        ))
    if primary_only:
        q = q.filter(Transaction.is_primary.is_(True))

    total = q.count()
    # 汇总只统计代表行，避免已合并从记录被重复计算
    income_sum, expense_sum = (
        q.with_entities(
            func.coalesce(func.sum(case(
                (Transaction.bill_type == "income", Transaction.amount), else_=0)), 0),
            func.coalesce(func.sum(case(
                (Transaction.bill_type.in_(("consume", "fee", "refund")), Transaction.amount),
                else_=0)), 0),
        )
        .filter(Transaction.is_primary.is_(True))
        .one()
    )

    items = (
        q.order_by(Transaction.tx_date.desc(), Transaction.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return TransactionPage(
        total=total,
        page=page,
        page_size=page_size,
        income_total=income_sum,
        expense_total=expense_sum,
        items=[
            TransactionOut(
                id=i.id, source=i.source, bill_type=i.bill_type, amount=i.amount,
                tx_date=i.tx_date, merchant=i.merchant, counterparty=i.counterparty,
                description=i.description, confidence=i.confidence,
                is_primary=i.is_primary, merged_group_id=i.merged_group_id,
            )
            for i in items
        ],
    )


@router.post("/transactions/manual")
def manual_create(data: ManualTxCreate, db: Session = Depends(get_db)):
    """手动录入一笔交易，自动参与去重。"""
    return bill_ops.manual_create(db, data.model_dump())
