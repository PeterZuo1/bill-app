"""FastAPI 入口。

启动时幂等建表（create_all），使本地即可跑通；
正式部署仍可配合 alembic 迁移管理版本。
"""
from fastapi import FastAPI

from app.database import Base, engine, ensure_schema
import app.models  # 确保模型注册到 Base.metadata

Base.metadata.create_all(bind=engine)
ensure_schema()

from app.routers import pages, bills, dedup, reports, game  # noqa: E402

app = FastAPI(title="个人记账系统", version="0.1.0")

app.include_router(pages.router)
app.include_router(bills.router)
app.include_router(dedup.router)
app.include_router(reports.router)
app.include_router(game.router)
