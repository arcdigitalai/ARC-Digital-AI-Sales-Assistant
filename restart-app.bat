@echo off
rem Restart ARC AI Sales Assistant (auto-restart stays ON).
schtasks /Change /TN "ARC AI Sales Assistant (port 9000)" /ENABLE
schtasks /End /TN "ARC AI Sales Assistant (port 9000)" >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :9000 ^| findstr LISTENING') do (
    echo Stopping app process %%p ...
    taskkill /PID %%p /F
)
schtasks /Run /TN "ARC AI Sales Assistant (port 9000)"
echo.
echo Restarted. Give it ~10 seconds, then open:  http://127.0.0.1:9000/
pause
