#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "== Qwen TTS Viseme POC / macOS =="
echo

PYTHON_BIN=""

if command -v python3.11 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3.11)"
elif [ -x "/opt/homebrew/bin/python3.11" ]; then
  PYTHON_BIN="/opt/homebrew/bin/python3.11"
elif [ -x "/usr/local/bin/python3.11" ]; then
  PYTHON_BIN="/usr/local/bin/python3.11"
fi

if [ -z "$PYTHON_BIN" ]; then
  echo "ERROR: Python 3.11 was not found."
  echo
  echo "Install it first:"
  echo "  brew install python@3.11"
  echo
  echo "Then run:"
  echo "  ./run_mac.sh --no-ws"
  exit 2
fi

echo "[PYTHON] $PYTHON_BIN"
"$PYTHON_BIN" --version

VENV=".venv311"

if [ ! -x "$VENV/bin/python" ]; then
  echo "[VENV] creating $VENV"
  "$PYTHON_BIN" -m venv "$VENV"
fi

source "$VENV/bin/activate"

echo "[VENV] $(python --version)"
echo "[PIP] upgrading pip/setuptools/wheel"
python -m pip install -U pip setuptools wheel

echo "[PIP] installing macOS POC dependencies"
python -m pip install -r requirements-mac.txt

echo
echo "[CHECK] versions"
python - <<'PY'
import sys
print("python:", sys.version)
import numpy
print("numpy:", numpy.__version__)
import pypinyin
print("pypinyin:", getattr(pypinyin, "__version__", "ok"))
import allosaurus
print("allosaurus: imported")
import dashscope
print("dashscope: imported")
import websocket
print("websocket-client: imported")
PY

echo
echo "[RUN] python qwen_tts_viseme_poc.py $*"
exec python qwen_tts_viseme_poc.py "$@"
