@echo off
rem Hourly watchlist snapshot - registered in Windows Task Scheduler.
rem %~dp0 = folder of this .bat, so the task works regardless of cwd.
cd /d "%~dp0"
echo [%date% %time%] trade collect >> "data\trade_watch.log"
"C:\Python310\python.exe" -m poe2tool trade collect >> "data\trade_watch.log" 2>&1
