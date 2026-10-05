@echo off
rem Starts Resilience Sense in the background (no window), this PC only.
rem It restarts itself if it ever stops. Use stop.bat to stop it. Log: data\server.log
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" supervise.py
timeout /t 4 /nobreak >nul
start "" http://127.0.0.1:8765
