"""数据库引擎与会话：SQLAlchemy 2.0 风格。

- engine: 全局引擎（SQLite 启用 WAL 提升并发读）
- SessionLocal: 每请求会话工厂
- Base: ORM 声明基类
"""
from collections.abc import Generator
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL

# SQLite 需要 check_same_thread=False 才能在多线程（FastAPI worker）中使用
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)

# SQLite 启用 WAL 模式，减少读写锁争用
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, conn_record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """ORM 声明基类。"""


def get_db() -> Generator["SessionLocal"]:
    """FastAPI 依赖：每请求提供独立会话，用完关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 已有 SQLite 库的轻量迁移：create_all 不会给旧表补列，这里按需 ALTER TABLE
_ADDED_COLUMNS = {
    "transaction": {
        "tx_datetime": "DATETIME",
        "payment_method": "VARCHAR(64) DEFAULT ''",
    },
}


def ensure_schema() -> None:
    """启动时补齐新增列，兼容已导入过的旧数据库。"""
    if not DATABASE_URL.startswith("sqlite"):
        return
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in tables:
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}'))
