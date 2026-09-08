@echo off
cd /d "%~dp0"
echo Starting P-Card Audit Console...
echo Server running at: http://localhost:5000
echo Press Ctrl+C to stop the server.
echo.
.venv\Scripts\python.exe app.py
pause
