# -*- coding: utf-8 -*-
"""斗地主记分 API。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import GamePlayersIn, GameRoundIn, GameStateOut
from app.services import game_service

router = APIRouter(prefix="/api/game", tags=["game"])


@router.get("/state", response_model=GameStateOut)
def get_state(db: Session = Depends(get_db)):
    return game_service.get_state(db)


@router.put("/players", response_model=GameStateOut)
def set_players(payload: GamePlayersIn, db: Session = Depends(get_db)):
    try:
        game_service.setup_players(db, payload.players)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return game_service.get_state(db)


@router.post("/rounds", response_model=GameStateOut)
def add_round(payload: GameRoundIn, db: Session = Depends(get_db)):
    try:
        game_service.add_round(
            db, payload.landlord, payload.base_score, payload.multiplier,
            payload.landlord_win, payload.spring)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return game_service.get_state(db)


@router.delete("/rounds/{round_id}", response_model=GameStateOut)
def delete_round(round_id: int, db: Session = Depends(get_db)):
    try:
        game_service.delete_round(db, round_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return game_service.get_state(db)


@router.post("/clear", response_model=GameStateOut)
def clear_rounds(db: Session = Depends(get_db)):
    game_service.clear_rounds(db)
    return game_service.get_state(db)
