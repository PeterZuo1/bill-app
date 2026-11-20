"""应用配置：从 .env 读取，集中管理。"""
import os
from pathlib import Path
from dotenv import load_dotenv

# 加载项目根目录下的 .env（不存在则忽略）
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# 运行时数据目录（SQLite 文件、上传暂存）
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
(DATA_DIR / "tmp").mkdir(exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/bill.db")
# Render/Heroku 等平台提供的连接串可能是 postgres://，SQLAlchemy 2.0 需要 postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
UPLOAD_MAX_MB = int(os.getenv("UPLOAD_MAX_MB", "10"))
SECRET_KEY = os.getenv("SECRET_KEY", "change-me")

# Alembic 配置（供 migrations/env.py 使用）
ALEMBIC_INI = BASE_DIR / "alembic.ini"
