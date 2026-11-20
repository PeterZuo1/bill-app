"""Pydantic 模型：API 请求/响应。与 ORM models.py 分离。"""
from datetime import date
from decimal import Decimal
from typing import Optional, List

from pydantic import BaseModel, Field


# ── 上传 ──
class UploadResult(BaseModel):
    status: str = "ok"          # ok / duplicate
    file_id: int
    source: str
    file_name: str
    rows_imported: int
    dedup_candidates: int = 0
    file_hash: str
    duplicate: bool = False


class TemplateOut(BaseModel):
    id: int
    source: str
    name: str
    col_map: dict
    active: bool


class TemplateDetectOut(BaseModel):
    source: str
    suggested_col_map: dict


# ── 去重确认队列 ──
class TxBrief(BaseModel):
    id: int
    source: str
    date: date
    amount: Decimal
    merchant: str
    description: str
    confidence: str


class DedupCandidateOut(BaseModel):
    id: int
    tx_a: TxBrief
    tx_b: TxBrief
    score: Decimal
    reason: str
    status: str


# ── 报表 ──
class MonthlyReport(BaseModel):
    month: str
    income: Decimal
    expense: Decimal
    net: Decimal
    by_channel: dict
    by_category: dict
    pending_candidates: int


# ── 手动录入 ──
class ManualTxCreate(BaseModel):
    source: str
    bill_type: str
    amount: Decimal
    tx_date: date
    merchant: str = ""
    counterparty: str = ""
    description: str = ""
    currency: str = "CNY"


class TransactionOut(BaseModel):
    id: int
    source: str
    bill_type: str
    amount: Decimal
    tx_date: date
    merchant: str
    counterparty: str
    description: str
    confidence: str
    is_primary: bool
    merged_group_id: Optional[int] = None


class TransactionPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[TransactionOut]
    income_total: Decimal = Decimal("0")
    expense_total: Decimal = Decimal("0")


# ── 斗地主记分 ──
class GameRoundOut(BaseModel):
    id: int
    round_no: int
    landlord: int
    landlord_name: str
    base_score: int
    multiplier: int
    spring: bool
    landlord_win: bool
    scores: List[int]


class GameStateOut(BaseModel):
    configured: bool
    players: List[str]
    totals: List[int]
    round_count: int
    leader: Optional[int] = None
    rounds: List[GameRoundOut]


class GamePlayersIn(BaseModel):
    players: List[str] = Field(..., min_length=3, max_length=3)


class GameRoundIn(BaseModel):
    landlord: int = Field(..., ge=0, le=2)
    base_score: int = Field(1, ge=1)
    multiplier: int = Field(1, ge=1)
    landlord_win: bool
    spring: bool = False
