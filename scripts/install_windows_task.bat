@echo off
REM Register daily EstateMonitor crawl in Windows Task Scheduler (07:00).
REM Run once as Administrator from project root if needed, or normally.

set ROOT=%~dp0..
set PYTHON=%ROOT%\.venv\Scripts\python.exe
set TASK_NAME=EstateMonitorDailyCrawl

if not exist "%PYTHON%" (
  echo venv python not found: %PYTHON%
  exit /b 1
)

schtasks /Create /F /TN "%TASK_NAME%" /SC DAILY /ST 07:00 /TR "\"%PYTHON%\" -m scripts.cli crawl --max-pages 8 --max-details 80"
echo Created/updated task %TASK_NAME%
schtasks /Query /TN "%TASK_NAME%" /V /FO LIST
