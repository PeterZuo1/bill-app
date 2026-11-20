"""路由：Jinja2 页面渲染。"""
from pathlib import Path
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import aggregator

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["pages"])


@router.get("/")
def index(request: Request, month: str | None = None, db: Session = Depends(get_db)):
    m = month or date.today().strftime("%Y-%m")
    try:
        report = aggregator.get_monthly(db, m)
    except Exception:
        report = {"month": m, "income": 0, "expense": 0, "net": 0, "by_channel": {}, "pending_candidates": 0}
    return templates.TemplateResponse(request, "report.html", {"report": report, "month": m})


@router.get("/upload")
def upload_page(request: Request):
    return templates.TemplateResponse(request, "upload.html", {})


@router.get("/dedup")
def dedup_page(request: Request):
    return templates.TemplateResponse(request, "dedup_queue.html", {})


@router.get("/transactions")
def transactions_page(request: Request):
    return templates.TemplateResponse(request, "transactions.html", {})


@router.get("/game")
def game_page(request: Request):
    return templates.TemplateResponse(request, "game.html", {})
