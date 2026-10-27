@echo off
title SDN Lab - stop everything
setlocal
cd /d "%~dp0"
set "NIX=%CD:\=/%"
for /f "usebackq delims=" %%P in (`wsl.exe -d Ubuntu -u root -- bash -c "wslpath '%NIX%'"`) do set "WSLP=%%P"
if not defined WSLP (echo Could not map the project path into WSL & pause & exit /b 1)
wsl.exe -d Ubuntu -u root -- bash -c "cd '%WSLP%' && bash stop_sdn.sh"
echo.
echo SDN lab stopped.
pause
