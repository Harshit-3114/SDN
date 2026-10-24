@echo off
title SDN lab - stop GUI panel and everything
wsl.exe -d Ubuntu -u root -- bash -c "cd /mnt/c/Users/harsh/sdn && pkill -f sdn_gui.py 2>/dev/null; sleep 1; pkill -f l2_learn_switch 2>/dev/null; bash stop_sdn.sh"
echo.
echo Panel and lab stopped.
pause