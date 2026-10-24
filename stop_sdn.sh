#!/usr/bin/env bash
# stop_sdn.sh -- clean up everything the lab created:
#   * remove any leftover Mininet/OVS state (mn -c)
#   * brute-force delete leftover Mininet veth devices (s* / h* -eth*)
#   * shut down the Ryu controller
# Run:  bash stop_sdn.sh
cd "$(dirname "$0")"

echo "Cleaning Mininet/OVS leftovers (mn -c)"
sudo mn -c > /dev/null 2>&1 || true

echo "Deleting any stray Mininet veth devices"
for LINK in $(ip -o link show | awk -F': ' '{print $2}' | sed 's/@.*//' | grep -E '^((h|s)[0-9]+)-eth'); do
    sudo ip link del "$LINK" 2>/dev/null || true
done

echo "Stopping Ryu controller"
pkill -f l2_learn_switch 2>/dev/null || true

echo "Done."
