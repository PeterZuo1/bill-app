@echo off
REM 本地一键启动：创建 venv、装依赖、跑服务（项目根目录）
cd /d "%~dp0.."

if not exist .venv (
  echo [venv] 首次运行，创建虚拟环境...
  python -m venv .venv
)
call .venv\Scripts\activate.bat

echo [deps] 检查/安装依赖...
python -m pip install -q -r requirements.txt

if not exist data\tmp mkdir data\tmp

set DATABASE_URL=sqlite:///./data/bill.db
echo [run] 启动 http://127.0.0.1:8000 ...
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
