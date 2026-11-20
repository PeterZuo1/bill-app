# -*- coding: utf-8 -*-
"""斗地主记分业务逻辑：固定一桌三人，按标准规则计算每局得分。"""
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import GameSession, GameRound

PLAYER_COUNT = 3
SPRING_FACTOR = 2
MAX_NAME_LEN = 20


def get_session(db: Session) -> GameSession | None:
    """一桌只会有一条牌桌记录。"""
    return db.query(GameSession).order_by(GameSession.id).first()


def _validate_players(names: list[str]) -> list[str]:
    if len(names) != PLAYER_COUNT:
        raise ValueError("必须设置三个玩家")
    cleaned = [str(n or "").strip() for n in names]
    for n in cleaned:
        if not n:
            raise ValueError("玩家名字不能为空")
        if len(n) > MAX_NAME_LEN:
            raise ValueError(f"玩家名字不能超过 {MAX_NAME_LEN} 个字")
    if len(set(cleaned)) != PLAYER_COUNT:
        raise ValueError("三个玩家名字不能重复")
    return cleaned


def setup_players(db: Session, names: list[str]) -> GameSession:
    """首次设置或修改玩家名字；修改名字不会清空历史成绩。"""
    cleaned = _validate_players(names)
    session = get_session(db)
    if session is None:
        session = GameSession(
            player1=cleaned[0], player2=cleaned[1], player3=cleaned[2])
        db.add(session)
    else:
        session.player1, session.player2, session.player3 = cleaned
    db.commit()
    db.refresh(session)
    return session


def calculate_scores(landlord: int, base_score: int, multiplier: int,
                     landlord_win: bool, spring: bool = False) -> list[int]:
    """标准规则：地主 ±2×底分×倍数，两个农民 ∓1×底分×倍数；春天再 ×2。"""
    if landlord not in (0, 1, 2):
        raise ValueError("地主必须是 0/1/2 中的一个")
    if base_score < 1:
        raise ValueError("底分必须大于等于 1")
    if multiplier < 1:
        raise ValueError("倍数必须大于等于 1")
    unit = base_score * multiplier * (SPRING_FACTOR if spring else 1)
    scores = [-unit, -unit, -unit]
    if landlord_win:
        scores[landlord] = unit * 2
    else:
        scores = [unit, unit, unit]
        scores[landlord] = -unit * 2
    return scores


def add_round(db: Session, landlord: int, base_score: int, multiplier: int,
              landlord_win: bool, spring: bool = False) -> GameRound:
    session = get_session(db)
    if session is None:
        raise ValueError("请先设置三个玩家")
    scores = calculate_scores(landlord, base_score, multiplier, landlord_win, spring)
    max_no = db.query(func.max(GameRound.round_no)).filter(
        GameRound.session_id == session.id).scalar() or 0
    rnd = GameRound(
        session_id=session.id,
        round_no=max_no + 1,
        landlord=landlord,
        base_score=base_score,
        multiplier=multiplier,
        spring=bool(spring),
        landlord_win=bool(landlord_win),
        score1=scores[0],
        score2=scores[1],
        score3=scores[2],
    )
    db.add(rnd)
    db.commit()
    db.refresh(rnd)
    return rnd


def delete_round(db: Session, round_id: int) -> bool:
    """删除一局，并把剩余局号重排为 1..n。"""
    rnd = db.get(GameRound, round_id)
    if rnd is None:
        raise ValueError("该局记录不存在")
    session_id = rnd.session_id
    db.delete(rnd)
    db.flush()
    remaining = (
        db.query(GameRound)
        .filter(GameRound.session_id == session_id)
        .order_by(GameRound.id)
        .all()
    )
    for i, r in enumerate(remaining, start=1):
        r.round_no = i
    db.commit()
    return True


def clear_rounds(db: Session) -> int:
    """清空本桌成绩，保留玩家名字。"""
    deleted = db.query(GameRound).delete(synchronize_session=False)
    db.commit()
    return deleted


def get_state(db: Session) -> dict:
    session = get_session(db)
    if session is None:
        return {
            "configured": False, "players": [], "totals": [0, 0, 0],
            "round_count": 0, "leader": None, "rounds": [],
        }

    players = [session.player1, session.player2, session.player3]
    rounds = (
        db.query(GameRound)
        .filter(GameRound.session_id == session.id)
        .order_by(GameRound.round_no)
        .all()
    )
    totals = [0, 0, 0]
    items = []
    for r in rounds:
        scores = [r.score1, r.score2, r.score3]
        for i in range(PLAYER_COUNT):
            totals[i] += scores[i]
        items.append({
            "id": r.id,
            "round_no": r.round_no,
            "landlord": r.landlord,
            "landlord_name": players[r.landlord],
            "base_score": r.base_score,
            "multiplier": r.multiplier,
            "spring": r.spring,
            "landlord_win": r.landlord_win,
            "scores": scores,
        })

    leader = None
    if rounds:
        leader = max(range(PLAYER_COUNT), key=lambda i: (totals[i], -i))
    return {
        "configured": True,
        "players": players,
        "totals": totals,
        "round_count": len(rounds),
        "leader": leader,
        "rounds": items,
    }
