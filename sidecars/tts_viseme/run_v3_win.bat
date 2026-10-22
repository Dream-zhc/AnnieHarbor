@echo off
rem Legacy shortcut retained; all Windows starts use the portable entrypoint.
call "%~dp0start_win.bat" %*
exit /b %ERRORLEVEL%
