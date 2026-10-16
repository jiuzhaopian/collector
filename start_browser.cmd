@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -X utf8 browser_collect.py %*
) else (
    py -3 -X utf8 browser_collect.py %*
)
set "COLLECT_EXIT=%ERRORLEVEL%"
echo.
echo Collector finished. Exit code: %COLLECT_EXIT%
echo Logs: data\logs    Reports: data\reports    Index: data\exports
pause
exit /b %COLLECT_EXIT%
