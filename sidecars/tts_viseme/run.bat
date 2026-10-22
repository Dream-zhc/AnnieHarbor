@echo off
setlocal
if not exist .venv (
  py -3.10 -m venv .venv
)
call .venv\Scripts\activate
python -m pip install -U pip
pip install -r requirements.txt
python qwen_tts_viseme_poc.py %*
