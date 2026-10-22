@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_win.ps1"
    if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" model_assets.py --download
if errorlevel 1 goto fail
echo [MODEL] Local ForcedAligner is ready for offline operation.
pause
exit /b 0
:fail
echo [ERROR] Model download failed. Check network/proxy, then retry.
pause
exit /b 1
