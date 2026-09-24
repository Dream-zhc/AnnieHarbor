#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "已创建 .env。请填写 DASHSCOPE_API_KEY，然后再次运行 ./run.sh"
  exit 0
fi

if ! grep -Eq '^DASHSCOPE_API_KEY=sk-[A-Za-z0-9_-]{8,}' .env || grep -q 'sk-xxxx' .env; then
  echo "请先编辑 .env，填写有效的 DASHSCOPE_API_KEY。"
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ ! -d .venv ]; then
  "$PYTHON_BIN" -m venv .venv
fi
source .venv/bin/activate
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
python smoke_test.py
exec python -m uvicorn app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8765}"
