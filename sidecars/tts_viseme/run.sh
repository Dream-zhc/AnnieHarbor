#!/usr/bin/env bash
set -euo pipefail
if [ ! -d .venv ]; then
  python3.10 -m venv .venv
fi
source .venv/bin/activate
python -m pip install -U pip
pip install -r requirements.txt
python qwen_tts_viseme_poc.py "$@"
