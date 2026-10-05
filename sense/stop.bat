@echo off
rem Stops Resilience Sense. A session that was recording is resumed if you start again within 15 min.
cd /d "%~dp0"
echo.> data\stop.flag
if exist data\supervisor.pid (
  set /p SUP=<data\supervisor.pid
  call taskkill /PID %%SUP%% /T /F >nul 2>&1
)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8765 " ^| findstr LISTENING') do taskkill /PID %%p /F >nul 2>&1
del data\supervisor.pid >nul 2>&1
del data\stop.flag >nul 2>&1
echo Resilience Sense stopped.
