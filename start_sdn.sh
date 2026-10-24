#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# start_sdn.sh -- bring the whole SDN lab up in one shot:
#   [1] Open vSwitch service (the switch software Mininet controls)
#   [2] cleanup of leftover Mininet state (harmless when nothing is running)
#   [3] Ryu SDN controller (background, detached, log -> outputs/ryu.log);
#       we WAIT until it is really listening on 6653, not merely spawned
#   [4] the Mininet topology (interactive 'mininet>' prompt, needs sudo)
#
# Run from the project root inside WSL Ubuntu:
#     cd /mnt/c/Users/harsh/sdn
#     bash start_sdn.sh
#
# NOTE: if WSL was shut down (reboot, idle timeout, `wsl --shutdown`), this
# script BOOT-STARTS the VM. The very first Python run afterwards warms the
# Windows file system (/mnt/c) and can sit silent for a minute -- that is
# normal, not a hang. Every later start is instant.
# ---------------------------------------------------------------------------
set -e
cd "$(dirname "$0")"

# Cold-boots take patience on the first Python/9P touch; be quiet about it.
export PYTHONDONTWRITEBYTECODE=1

# ---- Ryu launcher (source install that works on Ubuntu 26.04 / Python 3.14) ----
RYU_PY=/opt/ryu-venv/bin/python
RYU_APP=controller/l2_learn_switch.py
if [ ! -x "$RYU_PY" ]; then
    echo "!! /opt/ryu-venv not found -- see README.md section 2 (Ryu on Python 3.14)"
    exit 1
fi

echo "[1/4] Open vSwitch service"
if [ -d /run/systemd/system ]; then
    sudo systemctl start openvswitch-switch
else
    sudo service openvswitch-switch start
fi
sudo ovs-vsctl show > /dev/null 2>&1 \
    && echo "      OVS ok" \
    || { echo "!! OVS not reachable -- see README troubleshooting"; exit 1; }

echo "[2/4] Cleanup of leftover Mininet state"
sudo mn -c > /dev/null 2>&1 || true
for LINK in $(ip -o link show | awk -F': ' '{print $2}' | sed 's/@.*//' | grep -E '^((h|s)[0-9]+)-eth'); do
    sudo ip link del "$LINK" 2>/dev/null || true
done

mkdir -p outputs

echo "[3/4] Ryu SDN controller (l2_learn_switch.py)"
if pgrep -f "l2_learn_switch" > /dev/null 2>&1; then
    echo "      already running (pid $(pgrep -f l2_learn_switch | head -n1))"
else
    # Detached so it survives this script; logs land in outputs/ryu.log.
    setsid env PYTHONPATH=/opt/ryu-src \
        "$RYU_PY" /opt/ryu-src/bin/ryu-manager $RYU_APP \
        > outputs/ryu.log 2>&1 < /dev/null &
    # Wait until the controller is actually LISTENING for OpenFlow.
    # (pgrep alone is not enough: the process exists while it is still
    # importing, and the switches would try to connect to a dead port.)
    RYU_UP=no
    for _ in $(seq 1 60); do
        if ss -ltn 2>/dev/null | grep -q ':6653 '; then RYU_UP=yes; break; fi
        sleep 0.5
    done
    if [ "$RYU_UP" = yes ]; then
        echo "      running (pid $(pgrep -f l2_learn_switch | head -n1)), listening on 6653, log: outputs/ryu.log"
    else
        echo "!! Ryu never started listening within 30 s -- outputs/ryu.log contains details:"
        tail -n 10 outputs/ryu.log
        exit 1
    fi
fi

echo "[4/4] Mininet topology (type 'mininet> exit' when done)"
echo "      NOTE: on the FIRST start after a WSL reboot this can sit silent"
echo "            for up to a minute while Windows warms /mnt/c -- normal!"
echo "      quick demo: pingall | h1 ping h5 | link s2 s3 down |"
echo "                  h6 iperf -s > /dev/null 2>&1 & ; h1 iperf -c 10.0.0.6 -t 3"
# -u = unbuffered: every print shows IMMEDIATELY on the console
sudo python3 -u mininet/run_topo.py

# Controller keeps running after the network goes down; stop_sdn.sh stops it.
echo "Network stopped. (Ryu may still be running; './stop_sdn.sh' closes everything.)"
