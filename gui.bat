@echo off
title SDN lab control panel - Ryu + Mininet
REM Double-click: starts the web panel inside WSL (detached) and opens
REM the browser. The panel owns OVS + Ryu + Mininet; its Start button
REM brings the whole lab up.
cd /d C:\Users\harsh\sdn
echo Starting the SDN control panel (web GUI)...
wsl.exe -d Ubuntu -u root -- bash -c "cd /mnt/c/Users/harsh/sdn && if ss -ltn | grep -q ':5000 '; then echo 'panel already running'; else setsid /opt/ryu-venv/bin/python gui/sdn_gui.py > outputs/gui.log 2>&1 & echo 'panel starting...'; fi; for i in 1 2 3 4 5 6 7 8 9 10 11 12; do ss -ltn | grep -q ':5000 ' && break; sleep 1; done; cmd.exe /c start http://localhost:5000"
echo.
echo If http://localhost:5000 did not open, open it yourself in a browser.
echo (You can close this window; the panel keeps running.
echo  stop-gui.bat stops the panel and the whole lab.)
pause