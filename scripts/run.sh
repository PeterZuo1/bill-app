#!/usr/bin/env bash
# Linux/macOS 本地启动（项目根目录）
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then python3 -m venv .venv; fi
source .venv/bin/activate
pip install -q -r requirements.txt
mkdir -p data/tmp
export DATABASE_URL=sqlite:///./data/bill.db
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
