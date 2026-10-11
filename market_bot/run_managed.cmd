@echo off
setlocal

set "PROJECT_ROOT=%~dp0.."
set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
set "LOG_DIR=%~dp0logs"
set "LOG_FILE=%LOG_DIR%\market_bot.log"

if not exist "%PYTHON%" exit /b 2
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
cd /d "%PROJECT_ROOT%" || exit /b 3
set "DATA_PROVIDER=router"

:restart
echo [%date% %time%] Starting market bot.>>"%LOG_FILE%"
"%PYTHON%" -u -m market_bot run >>"%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] Market bot exited with code %EXIT_CODE%; retrying in 60 seconds.>>"%LOG_FILE%"
timeout /t 60 /nobreak >nul
goto restart
