"""路由：去重确认队列。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DedupCandidate, Transaction
from app.schemas import DedupCandidateOut, TxBrief
from app.services import dedup_engine

router = APIRouter(prefix="/api/dedup", tags=["dedup"])


def _tx_brief(t: Transaction) -> TxBrief:
    return TxBrief(
        id=t.id,
        source=t.source,
        date=t.tx_date,
        amount=t.amount,
        merchant=t.merchant,
        description=t.description,
        confidence=t.confidence,
    )


def _serialize(c: DedupCandidate, ta: Transaction, tb: Transaction) -> DedupCandidateOut:
    return DedupCandidateOut(
        id=c.id,
        tx_a=_tx_brief(ta),
        tx_b=_tx_brief(tb),
        score=c.score,
        reason=c.reason,
        status=c.status,
    )


@router.get("/queue", response_model=list[DedupCandidateOut])
def dedup_queue(db: Session = Depends(get_db)):
    items = (
        db.query(DedupCandidate)
        .filter(DedupCandidate.status == "pending")
        .order_by(DedupCandidate.id.desc())
        .all()
    )
    out = []
    for c in items:
        ta = db.get(Transaction, c.tx_a_id)
        tb = db.get(Transaction, c.tx_b_id)
        if ta is None or tb is None:
            continue
        out.append(_serialize(c, ta, tb))
    return out


@router.post("/{candidate_id}/merge")
def merge_candidate(candidate_id: int, db: Session = Depends(get_db)):
    c = db.get(DedupCandidate, candidate_id)
    if c is None or c.status != "pending":
        raise HTTPException(404, "待确认项不存在或已处理")
    try:
        g = dedup_engine.merge_candidate(db, c.tx_a_id, c.tx_b_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return {"group_id": g.id, "status": "merged"}


@router.post("/{candidate_id}/discard")
def discard_candidate(candidate_id: int, db: Session = Depends(get_db)):
    c = db.get(DedupCandidate, candidate_id)
    if c is None or c.status != "pending":
        raise HTTPException(404, "待确认项不存在或已处理")
    dedup_engine.discard_candidate(db, candidate_id)
    db.commit()
    return {"status": "discarded"}
