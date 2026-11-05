@echo off
cd /d "%~dp0"
python -c "import websocket" >nul 2>&1
if errorlevel 1 (
  echo Install dependencies first: python -m pip install -r requirements.txt
  pause
  exit /b 1
)
python service.py start
pause
