"""聚合引擎：按月统计净额。

口径（见 PRD 2.1）：
- 收入 = 当月 income 之和
- 支出 = 当月 consume + fee（refund 以负数存储，相加即自动冲减）
- 净额 = 收入 − 支出
- 只计 is_primary 代表行，被合并从记录不计入（避免重复计数）
- 转账(transfer) 过滤
"""
from datetime import date, timedelta
from decimal import Decimal
from collections import defaultdict

from sqlalchemy.orm import Session

from app.models import Transaction, DedupCandidate


def month_range(month: str) -> tuple[date, date]:
    """'YYYY-MM' -> (月初date, 月末date)。"""
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y, m, 28)
    if m == 12:
        end = date(y, 12, 31)
    else:
        end = date(y, m + 1, 1) - timedelta(days=1)
    return start, end


def get_monthly(db: Session, month: str) -> dict:
    """月度净额 + 渠道占比 + 待确认数。"""
    start, end = month_range(month)
    rows = (
        db.query(Transaction)
        .filter(Transaction.tx_date >= start, Transaction.tx_date <= end)
        .filter(Transaction.is_primary.is_(True))
        .filter(Transaction.bill_type != "transfer")
        .all()
    )

    income = sum((r.amount for r in rows if r.bill_type == "income"), Decimal("0"))
    consume = sum((r.amount for r in rows if r.bill_type == "consume"), Decimal("0"))
    fee = sum((r.amount for r in rows if r.bill_type == "fee"), Decimal("0"))
    refund = sum((r.amount for r in rows if r.bill_type == "refund"), Decimal("0"))

    expense = consume + fee + refund  # refund 负 => 减
    net = income - expense

    by_channel: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for r in rows:
        if r.bill_type in ("consume", "fee", "refund"):
            by_channel[r.source] = by_channel[r.source] + r.amount

    pending = (
        db.query(DedupCandidate)
        .filter(DedupCandidate.status == "pending")
        .count()
    )

    return {
        "month": month,
        "income": income,
        "expense": expense,
        "net": net,
        "by_channel": {k: float(v) for k, v in by_channel.items()},
        "by_category": {},  # 分类报表扩展期再做
        "pending_candidates": pending,
    }
