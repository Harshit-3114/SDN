@echo off
title SDN lab - stop GUI panel and everything
setlocal
cd /d "%~dp0"
set "NIX=%CD:\=/%"
for /f "usebackq delims=" %%P in (`wsl.exe -d Ubuntu -u root -- bash -c "wslpath '%NIX%'"`) do set "WSLP=%%P"
if not defined WSLP (echo Could not map the project path into WSL & pause & exit /b 1)
wsl.exe -d Ubuntu -u root -- bash -c "cd '%WSLP%' && pkill -9 -f sdn_gui.py 2>/dev/null; sleep 1; pkill -9 -f l2_learn_switch 2>/dev/null; pkill -9 -f ryu-manager 2>/dev/null; bash stop_sdn.sh"
echo.
echo Panel and lab stopped.
pause
