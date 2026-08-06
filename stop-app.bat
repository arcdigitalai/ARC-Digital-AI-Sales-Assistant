@echo off
rem Stop ARC AI Sales Assistant and switch OFF auto-restart.
rem (Without disabling the task, the self-heal timer would restart the app
rem within 5 minutes.)
echo Disabling auto-restart...
schtasks /Change /TN "ARC AI Sales Assistant (port 9000)" /DISABLE
schtasks /End /TN "ARC AI Sales Assistant (port 9000)" >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :9000 ^| findstr LISTENING') do (
    echo Stopping app process %%p ...
    taskkill /PID %%p /F
)
echo.
echo Done. The app is OFF and will STAY off (even after reboot)
echo until you run start-app.bat again.
pause
