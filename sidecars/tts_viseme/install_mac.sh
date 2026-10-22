#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew not found."
  echo "Install Python 3.11 using python.org or install Homebrew first."
  exit 2
fi

brew install python@3.11

PY="$(brew --prefix python@3.11)/bin/python3.11"
echo "Using: $PY"
"$PY" --version

rm -rf .venv311
"$PY" -m venv .venv311
source .venv311/bin/activate
python -m pip install -U pip setuptools wheel
python -m pip install -r requirements-mac.txt

echo
echo "Install complete."
echo "Next:"
echo "  source .venv311/bin/activate"
echo "  python qwen_tts_viseme_poc.py --no-ws"
