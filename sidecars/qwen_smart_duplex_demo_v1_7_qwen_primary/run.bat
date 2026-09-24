@echo off
cd /d %~dp0
if not exist .env (
  copy .env.example .env >nul
  echo 已创建 .env。请填写 DASHSCOPE_API_KEY，然后再次双击 run.bat
  pause
  exit /b 0
)
findstr /R /C:"^DASHSCOPE_API_KEY=sk-[A-Za-z0-9_-][A-Za-z0-9_-]*" .env >nul
if errorlevel 1 (
  echo 请先编辑 .env，填写有效的 DASHSCOPE_API_KEY。
  pause
  exit /b 1
)
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
python smoke_test.py
python -m uvicorn app:app --host 127.0.0.1 --port 8765
pause
