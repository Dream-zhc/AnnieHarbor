@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
rem 独立扬声器（可选麦克风）。改下面三行即可换房间。A=roomv2，B=roomv3。
rem 同时开麦克风：set ROOM_MIC=1 后运行，或 start.bat --mic；选设备 --mic-device 名称片段。
rem 一个 Room 只留一个麦克风、一个扬声器：同时开网页麦克风或网页调试扬声器会在大屏提醒。
if not defined SPEAKER_HOST set "SPEAKER_HOST=192.168.1.47"
if not defined SPEAKER_PORT set "SPEAKER_PORT=8090"
if not defined SPEAKER_ROOM set "SPEAKER_ROOM=roomv2"

set "PY=python"
where py >nul 2>&1 && set "PY=py -3"
%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo 需要 Python 3.10 或更新版本。
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv
  if errorlevel 1 (
    echo 无法创建虚拟环境。请安装 Python 3.10 或更新版本。
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -c "import sounddevice,websockets,numpy" >nul 2>&1
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
  if errorlevel 1 (
    echo 依赖安装失败。请确认这台 Windows 能访问 PyPI。
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" "%~dp0speaker.py" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
exit /b %CODE%
