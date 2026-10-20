@echo off
REM ============================================================
REM  Daily Job Logs - runs daily_job_logs.py and keeps a log file.
REM
REM  Daily run (what Task Scheduler calls):   run_daily_job_logs.bat
REM  One-time 90-day rebuild:                 run_daily_job_logs.bat rebuild 90
REM
REM  Output goes to the "logs" folder next to this file, one file per day.
REM ============================================================

cd /d "%~dp0"
if not exist logs mkdir logs

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
set LOGFILE=logs\djl_%TODAY%.log

echo. >> "%LOGFILE%"
echo ===== %DATE% %TIME%  args: %* ===== >> "%LOGFILE%"

REM Windows' console can't print emoji by default; this keeps the script from crashing on them.
set PYTHONIOENCODING=utf-8

python daily_job_logs.py %* >> "%LOGFILE%" 2>&1
set RESULT=%ERRORLEVEL%

echo ===== finished with code %RESULT% ===== >> "%LOGFILE%"
exit /b %RESULT%
