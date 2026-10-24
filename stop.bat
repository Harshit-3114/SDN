@echo off
title SDN Lab - stop everything
wsl.exe -d Ubuntu -u root -- bash -c "cd /mnt/c/Users/harsh/sdn && bash stop_sdn.sh"
echo.
pause
