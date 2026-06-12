@echo off
:: xauusd-scalper wrapper — launched by Task Scheduler (interactive session).
:: Appends timestamped start/exit lines to scalper-wrapper.log.

cd /d C:\Users\BotVm\Desktop\xauusd-scalper
if not exist logs mkdir logs

powershell -NoProfile -Command "(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' [WRAPPER] scalper starting'" >> logs\scalper-wrapper.log

%~dp0..\.venv\Scripts\python.exe main.py >> logs\scalper-wrapper.log 2>&1

powershell -NoProfile -Command "(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' [WRAPPER] scalper exited (code %ERRORLEVEL%)'" >> logs\scalper-wrapper.log
