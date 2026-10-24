@echo off
title SDN lab - stop GUI panel and everything
wsl.exe -d Ubuntu -u root -- bash -c "cd /mnt/c/Users/harsh/sdn && pkill -9 -f sdn_gui.py 2>/dev/null; sleep 1; pkill -9 -f l2_learn_switch 2>/dev/null; pkill -9 -f ryu-manager 2>/dev/null; bash stop_sdn.sh"
echo.
echo Panel and lab stopped.
pause