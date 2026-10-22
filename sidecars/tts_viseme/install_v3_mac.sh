#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ -d ".venv311" ]; then
  source .venv311/bin/activate
elif command -v python3.11 >/dev/null 2>&1; then
  python3.11 -m venv .venv311
  source .venv311/bin/activate
else
  echo "Python 3.11 not found. Run: brew install python@3.11"
  exit 2
fi

python -m pip install -U pip setuptools wheel
python -m pip install -r requirements-v3.txt

echo
echo "V3 dependencies installed."
echo "First test:"
echo "  ./run_v3_mac.sh --map-only"
