@echo off
setlocal
cd /d "%~dp0"
set "CELMA_RUNTIME=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\node"
if not exist "%CELMA_RUNTIME%\bin\node.exe" (
  echo Codex bundled Node runtime unavailable. CSV indexes remain usable in Excel.
  exit /b 2
)
if not exist node_modules mklink /J node_modules "%CELMA_RUNTIME%\node_modules"
"%CELMA_RUNTIME%\bin\node.exe" export_excel.mjs %*
