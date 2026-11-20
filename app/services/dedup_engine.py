"""去重引擎（系统灵魂）。

判重规则：

1. 同文件内去重
   仅当两行「交易时间相差 ≤60 秒」且类型、金额、日期、商户一致时合并。
   信用卡账单只有日期、没有时分秒，且账单会逐笔列出相同交易，
   因此信用卡文件不做同文件去重。

2. 跨渠道去重
   只把「信用卡账单」与「用信用卡支付的微信/支付宝流水」配对：
   - 付款只配付款，退款只配退款（类型必须一致）；
   - 金额一致（误差 ≤0.01）；
   - 双方都有精确时间：时间差 ≤60 秒才算重复；
   - 任一方无时间（信用卡账单）：交易日必须相同；
   - 商户名 >=0.6 或该金额/日期只存在唯一匹配 -> 自动合并，
     否则进入待人工确认。
   花呗、余额、储蓄卡支付的流水不会与信用卡账单配对。

3. 还款/转账等中性流水不参与去重。

商户名模糊匹配：字符级 Jaccard + 包含关系。
"""
from decimal import Decimal
import re

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models import (
    Transaction, MergeGroup, DedupCandidate,
)

AMOUNT_TOL = Decimal("0.01")
DEDUP_TIME_WINDOW_SECONDS = 60
_CARD_METHOD_KW = ("信用卡", "贷记卡")

# 支付渠道前缀：不同来源对同一商户的写法不同（如「财付通-肯德基」），
# 比对前统一剥离，避免仅因前缀差异而漏判重复。
_PAY_PREFIXES = (
    "财付通-", "财付通", "微信支付-", "支付宝-", "支付宝",
    "京东支付-", "京东支付", "抖音支付-", "抖音支付",
    "拼多多支付-", "拼多多支付", "掌上生活优惠商户-", "掌上生活-",
    "美团支付-", "银联-",
)
# 订单号/商品编码等长串字母数字噪声（如微信商品名 KFC_PRE_PLUSWX100126...）
_NOISE_TOKEN = re.compile(r"[a-z0-9]{6,}")


# ── 商户名模糊匹配 ──
def _clean_name(s: str) -> str:
    """归一化商户名：去渠道前缀、订单号噪声、标点与空白，转小写。"""
    t = str(s or "").lower()
    for p in _PAY_PREFIXES:
        t = t.replace(p, "")
    t = _NOISE_TOKEN.sub("", t)
    t = re.sub(r"[\s_\-·/\\（）()【】\[\]{}:：,，.。|＋+]+", "", t)
    return t


def _name_score(na: str, nb: str) -> float:
    """已归一化的两个名称相似度 [0,1]。"""
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.8
    sa, sb = set(na), set(nb)
    return len(sa & sb) / len(sa | sb)


def merchant_score(a: str, b: str) -> float:
    """商户名相似度 [0,1]（字符串级，保留供外部/测试使用）。"""
    return _name_score(_clean_name(a), _clean_name(b))


def tx_merchant_score(tx_a: Transaction, tx_b: Transaction) -> float:
    """两笔流水的商户相似度：merchant / counterparty 两两交叉取最高分。

    微信的 merchant 常是商品编码（KFC_PRE_PLUSWX...），真实商户名在
    counterparty；信用卡的商户名在 merchant 且带「财付通-」前缀。
    单字段比对会漏配，故双字段交叉比对。
    """
    names_a = [n for n in (_clean_name(tx_a.merchant), _clean_name(tx_a.counterparty)) if n]
    names_b = [n for n in (_clean_name(tx_b.merchant), _clean_name(tx_b.counterparty)) if n]
    best = 0.0
    for na in names_a:
        for nb in names_b:
            best = max(best, _name_score(na, nb))
    return best


def _amount_match(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= AMOUNT_TOL


def _is_card_funded(tx: Transaction) -> bool:
    """钱包流水是否用信用卡支付（才可能与信用卡账单重复）。"""
    if tx.source == "card":
        return True
    method = tx.payment_method or ""
    return any(k in method for k in _CARD_METHOD_KW)


def _time_match(a: Transaction, b: Transaction) -> bool:
    """判重的时间条件：有精确时间按 60 秒，无时间则必须同一交易日。"""
    if a.tx_datetime is not None and b.tx_datetime is not None:
        gap = abs((a.tx_datetime - b.tx_datetime).total_seconds())
        return gap <= DEDUP_TIME_WINDOW_SECONDS
    return a.tx_date == b.tx_date


def _auto_merge_pair(db: Session, tx_a: Transaction, tx_b: Transaction) -> None:
    """把具备 high 置信匹配的两张流水自动并入同一 merge_group。"""
    rep = tx_a if abs(tx_a.amount) >= abs(tx_b.amount) else tx_b
    other = tx_b if rep is tx_a else tx_a
    group = MergeGroup(
        representative_tx_id=rep.id,
        merged_count=2,
        auto_merged=True,
        reason="跨渠道金额+日期+商户名一致",
    )
    db.add(group)
    db.flush()
    rep.merged_group_id = group.id
    rep.confidence = "high"
    other.merged_group_id = group.id
    other.is_primary = False
    other.confidence = "high"


def _create_candidate(db: Session, tx_a: Transaction, tx_b: Transaction,
                      score: float | None = None) -> DedupCandidate:
    """金额+日期匹配但商户名对不上 -> 待人工确认候选（同一对不重复入队）。"""
    existed = (
        db.query(DedupCandidate)
        .filter(
            DedupCandidate.status == "pending",
            or_(
                and_(DedupCandidate.tx_a_id == tx_a.id, DedupCandidate.tx_b_id == tx_b.id),
                and_(DedupCandidate.tx_a_id == tx_b.id, DedupCandidate.tx_b_id == tx_a.id),
            ),
        )
        .first()
    )
    if existed is not None:
        return existed

    if score is None:
        score = tx_merchant_score(tx_a, tx_b)
    candidate = DedupCandidate(
        tx_a_id=tx_a.id,
        tx_b_id=tx_b.id,
        score=Decimal(str(round(score, 1))),
        reason="金额+日期一致但商户名匹配度低，需人工确认",
        status="pending",
    )
    db.add(candidate)
    tx_a.confidence = "medium"
    tx_b.confidence = "medium"
    return candidate


def _same_file_dedup(db: Session, file_id: int) -> int:
    """同一文件内：仅合并交易时间相差 ≤60 秒的相同流水。

    信用卡账单没有时分秒，账单本身会逐笔列出同日同额同商户的多笔交易，
    无法用时间区分，故不参与同文件去重。
    """
    txs = (
        db.query(Transaction)
        .filter(
            Transaction.file_id == file_id,
            Transaction.is_primary.is_(True),
            Transaction.tx_datetime.isnot(None),
        )
        .order_by(Transaction.tx_datetime)
        .all()
    )
    groups: dict[tuple, list[Transaction]] = {}
    for t in txs:
        key = (t.bill_type, t.amount, t.tx_date, t.merchant, t.counterparty)
        groups.setdefault(key, []).append(t)

    merged = 0
    for members in groups.values():
        if len(members) <= 1:
            continue
        clusters: list[list[Transaction]] = [[members[0]]]
        for t in members[1:]:
            gap = (t.tx_datetime - clusters[-1][-1].tx_datetime).total_seconds()
            if gap <= DEDUP_TIME_WINDOW_SECONDS:
                clusters[-1].append(t)
            else:
                clusters.append([t])
        for cluster in clusters:
            if len(cluster) <= 1:
                continue
            rep = cluster[0]
            group = MergeGroup(
                representative_tx_id=rep.id,
                merged_count=len(cluster),
                auto_merged=True,
                reason=f"同文件内 {DEDUP_TIME_WINDOW_SECONDS} 秒内的相同流水",
            )
            db.add(group)
            db.flush()
            rep.merged_group_id = group.id
            rep.confidence = "high"
            for m in cluster[1:]:
                m.merged_group_id = group.id
                m.is_primary = False
                m.confidence = "high"
            merged += len(cluster) - 1
    return merged


def _cross_source_dedup(db: Session, file_id: int) -> int:
    """新增文件流水与已有信用卡账单流水做跨渠道匹配。

    只处理消费/退款；付款只配付款，退款只配退款；
    钱包流水必须是用信用卡支付的（花呗/余额/储蓄卡不参与）。
    """
    new_txs = db.query(Transaction).filter_by(
        file_id=file_id, is_primary=True, merged_group_id=None).all()
    if not new_txs:
        return 0

    existing = (
        db.query(Transaction)
        .filter(Transaction.file_id != file_id)
        .filter(Transaction.merged_group_id.is_(None))
        .filter(Transaction.is_primary.is_(True))
        .all()
    )

    candidates_created = 0
    for tx_a in new_txs:
        if tx_a.bill_type not in ("consume", "refund"):
            continue  # 中性/收入流水不参与跨渠道去重
        best_match: Transaction | None = None
        best_ms = -1.0
        match_count = 0
        for tx_b in existing:
            if tx_a.source == tx_b.source:
                continue
            # 只允许「信用卡账单 ↔ 信用卡支付的钱包流水」配对
            if tx_a.source == "card":
                if not _is_card_funded(tx_b):
                    continue
            elif tx_b.source == "card":
                if not _is_card_funded(tx_a):
                    continue
            else:
                continue
            # 付款只配付款，退款只配退款
            if tx_a.bill_type != tx_b.bill_type:
                continue
            if not _amount_match(abs(tx_a.amount), abs(tx_b.amount)):
                continue
            if not _time_match(tx_a, tx_b):
                continue
            match_count += 1
            ms = tx_merchant_score(tx_a, tx_b)
            if ms > best_ms:
                best_ms = ms
                best_match = tx_b

        if best_match is None:
            continue
        # 商户名高度相似，或该金额/时间只有唯一候选 -> 自动合并；否则人工确认
        if best_ms >= 0.6 or match_count == 1:
            _auto_merge_pair(db, tx_a, best_match)
            existing = [e for e in existing if e.id != best_match.id]
        else:
            _create_candidate(db, tx_a, best_match, best_ms)
            candidates_created += 1
    return candidates_created


def run_dedup(db: Session, file_id: int) -> int:
    """针对新导入文件执行完整去重，返回产生的待确认候选数。"""
    run = _same_file_dedup(db, file_id)
    db.commit()
    run += _cross_source_dedup(db, file_id)
    db.commit()
    return run


def merge_candidate(db: Session, tx_a_id: int, tx_b_id: int) -> MergeGroup:
    """人工确认合并：把两个待确认流水并入一组。"""
    tx_a = db.get(Transaction, tx_a_id)
    tx_b = db.get(Transaction, tx_b_id)
    if tx_a is None or tx_b is None:
        raise ValueError("交易不存在或已被清空")
    if tx_a.merged_group_id or tx_b.merged_group_id:
        raise ValueError("该交易已合并，无需重复操作")
    rep = tx_a if abs(tx_a.amount) >= abs(tx_b.amount) else tx_b
    other = tx_b if rep is tx_a else tx_a
    group = MergeGroup(
        representative_tx_id=rep.id, merged_count=2,
        auto_merged=False, reason="人工确认合并",
    )
    db.add(group)
    db.flush()
    rep.merged_group_id = group.id
    rep.confidence = "high"
    other.merged_group_id = group.id
    other.is_primary = False
    other.confidence = "high"
    # 标注对应候选已处理
    for c in db.query(DedupCandidate).filter(
        DedupCandidate.status == "pending",
        ((DedupCandidate.tx_a_id == tx_a_id) & (DedupCandidate.tx_b_id == tx_b_id))
        | ((DedupCandidate.tx_a_id == tx_b_id) & (DedupCandidate.tx_b_id == tx_a_id)),
    ):
        c.status = "merged"
    return group


def discard_candidate(db: Session, candidate_id: int) -> None:
    """人工放弃当前候选，标记为 discarded。"""
    c = db.get(DedupCandidate, candidate_id)
    if c is not None and c.status == "pending":
        c.status = "discarded"
