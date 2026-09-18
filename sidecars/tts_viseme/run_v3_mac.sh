#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv311" ]; then
  echo "ERROR: .venv311 not found in this directory."
  echo "Create it with Python 3.11 first."
  exit 2
fi

PYTHON="$PWD/.venv311/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "ERROR: $PYTHON is not executable."
  exit 2
fi

# venv 跟随 project/ 一起迁过目录，activate 里可能还残留旧绝对路径。
# 直接使用当前目录的解释器，避免激活后 PATH 指回旧工程导致 python 找不到。
export VIRTUAL_ENV="$PWD/.venv311"
export PATH="$VIRTUAL_ENV/bin:$PATH"

echo "[PYTHON] $($PYTHON --version)"
echo "[ENV]    $VIRTUAL_ENV"

"$PYTHON" - <<'PY'
import importlib.util
missing = []
for name in ("dotenv", "websocket", "pypinyin", "g2pw", "numpy", "mlx_qwen3_asr"):
    if importlib.util.find_spec(name) is None:
        missing.append(name)
if missing:
    print("MISSING:", ",".join(missing))
    raise SystemExit(3)
print("Dependencies OK")
PY

exec "$PYTHON" qwen_stream_room_g2pw.py "$@"
