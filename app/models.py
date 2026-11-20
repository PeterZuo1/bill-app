"""ORM 模型：7 张表。

- bill_file      账单文件元信息（防重指纹）
- transaction    单笔流水（去重主体）
- merge_group    去重合并组（代表一条真实消费）
- dedup_candidate 待人工确认的疑似重复对
- import_template 渠道 Excel 列映射模板缓存
- game_session   斗地主记分牌桌（固定一桌三人）
- game_round     斗地主每局得分

C 模型不建 account / balance 表。
"""
from datetime import datetime, date, timezone
from decimal import Decimal
from sqlalchemy import (
    Integer, String, Numeric, Date, DateTime, Boolean,
    ForeignKey, Text, JSON, Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ──────────────────────────────────────────────
# 枚举常量（用 String 存储，跨 SQLite/PG 一致）
# ──────────────────────────────────────────────
SOURCES = ("wechat", "alipay", "card")
BILL_TYPES = ("consume", "income", "transfer", "refund", "fee", "other")
CONFIDENCES = ("high", "medium", "low", "independent")
FILE_STATUSES = ("parsed", "deduped", "failed")
CANDIDATE_STATUSES = ("pending", "merged", "discarded")


# ──────────────────────────────────────────────
# 1. bill_file
# ──────────────────────────────────────────────
class BillFile(Base):
    __tablename__ = "bill_file"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)          # wechat/alipay/card
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)      # 原始文件名
    file_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)  # sha256 防重
    row_count: Mapped[int] = mapped_column(Integer, default=0)               # 解析行数
    status: Mapped[str] = mapped_column(String(16), default="parsed")        # parsed/deduped/failed
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="file")


# ──────────────────────────────────────────────
# 2. transaction
# ──────────────────────────────────────────────
class Transaction(Base):
    __tablename__ = "transaction"
    __table_args__ = (
        Index("ix_tx_source_date", "source", "tx_date"),
        Index("ix_tx_source_type", "source", "bill_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    file_id: Mapped[int] = mapped_column(ForeignKey("bill_file.id"), nullable=False)
    bill_type: Mapped[str] = mapped_column(String(16), nullable=False)       # consume/income/transfer/refund/fee/other
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)  # 消费正 / 退款负
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    tx_date: Mapped[date] = mapped_column(Date, nullable=False)              # 交易日（聚合按此）
    tx_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=True)   # 精确交易时间（账单无时分秒时为空）
    entry_date: Mapped[date] = mapped_column(Date, nullable=True)            # 信用卡入账日（存不聚合）
    payment_method: Mapped[str] = mapped_column(String(64), default="")      # 付款方式（如：招商银行信用卡(2782)/花呗/零钱）
    merchant: Mapped[str] = mapped_column(String(255), default="")           # 商户名
    counterparty: Mapped[str] = mapped_column(String(255), default="")       # 交易对方
    description: Mapped[str] = mapped_column(Text, default="")               # 原始摘要
    source_row: Mapped[int] = mapped_column(Integer, nullable=True)          # 源文件行号
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)          # 是否代表行（聚合只计代表）
    merged_group_id: Mapped[int] = mapped_column(
        ForeignKey("merge_group.id"), nullable=True, index=True)
    confidence: Mapped[str] = mapped_column(String(16), default="independent")  # high/medium/low/independent
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    file: Mapped["BillFile"] = relationship(back_populates="transactions")
    merge_group: Mapped["MergeGroup | None"] = relationship(
        back_populates="members", foreign_keys=[merged_group_id])


# ──────────────────────────────────────────────
# 3. merge_group
# ──────────────────────────────────────────────
class MergeGroup(Base):
    __tablename__ = "merge_group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    representative_tx_id: Mapped[int] = mapped_column(
        ForeignKey("transaction.id"), nullable=False, unique=True)
    merged_count: Mapped[int] = mapped_column(Integer, default=1)
    auto_merged: Mapped[bool] = mapped_column(Boolean, default=False)        # high 置信自动合并
    reason: Mapped[str] = mapped_column(Text, default="")                     # 合并依据说明
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    members: Mapped[list["Transaction"]] = relationship(
        back_populates="merge_group", foreign_keys=[Transaction.merged_group_id])
    representative = relationship("Transaction", foreign_keys=[representative_tx_id])


# ──────────────────────────────────────────────
# 4. dedup_candidate
# ──────────────────────────────────────────────
class DedupCandidate(Base):
    __tablename__ = "dedup_candidate"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tx_a_id: Mapped[int] = mapped_column(ForeignKey("transaction.id"), nullable=False)
    tx_b_id: Mapped[int] = mapped_column(ForeignKey("transaction.id"), nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(3, 1), nullable=False)    # 匹配得分
    reason: Mapped[str] = mapped_column(Text, default="")                    # 匹配理由
    status: Mapped[str] = mapped_column(String(16), default="pending")       # pending/merged/discarded
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("ix_dedup_tx_a", "tx_a_id"),
        Index("ix_dedup_tx_b", "tx_b_id"),
    )


# ──────────────────────────────────────────────
# 5. import_template
# ──────────────────────────────────────────────
class ImportTemplate(Base):
    __tablename__ = "import_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    col_map: Mapped[dict] = mapped_column(JSON, default=dict)                # {原列名: 标准字段}
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ──────────────────────────────────────────────
# 6. game_session（斗地主：只保留一桌）
# ──────────────────────────────────────────────
class GameSession(Base):
    __tablename__ = "game_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player1: Mapped[str] = mapped_column(String(32), nullable=False)
    player2: Mapped[str] = mapped_column(String(32), nullable=False)
    player3: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now)

    rounds: Mapped[list["GameRound"]] = relationship(
        back_populates="session", cascade="all, delete-orphan",
        order_by="GameRound.round_no")


# ──────────────────────────────────────────────
# 7. game_round（每局得分）
# ──────────────────────────────────────────────
class GameRound(Base):
    __tablename__ = "game_round"
    __table_args__ = (Index("ix_game_round_session", "session_id", "round_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("game_session.id"), nullable=False, index=True)
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)           # 局号，删除后重排
    landlord: Mapped[int] = mapped_column(Integer, nullable=False)           # 0/1/2
    base_score: Mapped[int] = mapped_column(Integer, nullable=False)         # 底分
    multiplier: Mapped[int] = mapped_column(Integer, nullable=False)         # 倍数
    spring: Mapped[bool] = mapped_column(Boolean, default=False)             # 春天，自动再 ×2
    landlord_win: Mapped[bool] = mapped_column(Boolean, nullable=False)
    score1: Mapped[int] = mapped_column(Integer, nullable=False)
    score2: Mapped[int] = mapped_column(Integer, nullable=False)
    score3: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    session: Mapped["GameSession"] = relationship(back_populates="rounds")
