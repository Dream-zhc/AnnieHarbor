@echo off
setlocal
cd /d "%~dp0"
set "UV_CACHE_DIR=%CD%\var\uv-cache"
set "UV_PYTHON_INSTALL_DIR=%CD%\var\uv-python"
set "TEMP=%CD%\var\tmp"
set "TMP=%TEMP%"
if not exist "%TEMP%" mkdir "%TEMP%"
uv run --locked python -m annie_runtime --replace-existing %*
exit /b %ERRORLEVEL%
