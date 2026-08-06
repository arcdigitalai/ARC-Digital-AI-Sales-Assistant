@echo off
rem Start ARC AI Sales Assistant and switch ON auto-restart.
rem Safe to run any time - if the app is already running, nothing changes.
echo Enabling auto-restart and starting the app in the background...
schtasks /Change /TN "ARC AI Sales Assistant (port 9000)" /ENABLE
schtasks /Run /TN "ARC AI Sales Assistant (port 9000)"
echo.
echo Give it ~10 seconds, then open:  http://127.0.0.1:9000/
echo (You can close this window - the app keeps running in the background.)
pause
