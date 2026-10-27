@echo off
title SDN Lab - Mininet + Ryu (OpenFlow 1.3)
REM Double-click to bring the whole lab up in one WSL window:
REM   [1] Open vSwitch service   [2] leftover-state cleanup
REM   [3] Ryu controller (l2_learn_switch.py, log: outputs/ryu.log)
REM   [4] Mininet topology  ->  this same window becomes the 'mininet>' prompt
REM Location-independent: derives the project dir from this file's location.
setlocal
cd /d "%~dp0"
set "NIX=%CD:\=/%"
for /f "usebackq delims=" %%P in (`wsl.exe -d Ubuntu -u root -- bash -c "wslpath '%NIX%'"`) do set "WSLP=%%P"
if not defined WSLP (echo Could not map the project path into WSL & pause & exit /b 1)
echo Starting the SDN lab in WSL Ubuntu (OVS + Ryu + Mininet)...
echo When the mininet$ prompt appears you are up. Type 'exit' to leave it.
echo.
wsl.exe -d Ubuntu -u root -- bash -c "cd '%WSLP%' && bash start_sdn.sh; echo; read -r -p '[info] mininet closed. Press Enter to stop the Ryu controller too...' __hold__; bash stop_sdn.sh; echo '[info] lab stopped.'"
echo.
echo SDN lab session ended (you can close this window).
pause
