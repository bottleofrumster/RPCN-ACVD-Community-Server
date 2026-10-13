@echo off
setlocal
where wsl.exe >nul 2>&1
if errorlevel 1 goto missing_wsl

wsl.exe --cd "%~dp0." --exec bash "./Launch Server UI.sh"
if errorlevel 1 goto launch_failed
exit /b 0

:missing_wsl
echo Install WSL 2 with Ubuntu and Linux GUI support first.
echo See README.txt under SERVER UI.
pause
exit /b 1

:launch_failed
echo.
echo The UI did not start. Read the error above.
echo Check that your default WSL distribution has Python 3.11+ and venv support.
echo See README.txt under SERVER UI.
pause
exit /b 1
