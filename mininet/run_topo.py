#!/usr/bin/env python3
"""
Interactive run: build the linear SDN topology and drop into the Mininet
command-line interface.

Usage (from the project root, inside WSL Ubuntu):
    sudo python3 mininet/run_topo.py

Terminal 1 (or the background step done by start_sdn.sh) must be running the
Ryu controller on 127.0.0.1:6653 first -- see start_sdn.sh.

Inside the CLI, things worth trying:
    mininet> pingall                     # full 6x6 connectivity test
    mininet> pingall                     # ...again: now ~10x faster (flows)
    mininet> h1 ping -c 3 h5             # one cross-switch ping by name
    mininet> sh ovs-ofctl --version      # (sanity)
    mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
    mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s2
    mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s3
    mininet> h6 iperf -s > /dev/null 2>&1 &   # start an iperf server on h6
    mininet> h1 iperf -c 10.0.0.6 -t 3        # throughput h1 -> h6
    mininet> h6 killall iperf                 # cleanup
    (avoid the built-in `iperf h1 h6` command: with iperf >= 2.2.1 it can
     spin forever -- its parser waits for a server line that never comes)
    mininet> exit
"""

from functools import partial
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))   # import topology.py

from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.cli import CLI
from mininet.log import info, setLogLevel

from topology import LinearTopo
from purge_state import purge


def build_net():
    setLogLevel('info')
    net = Mininet(
        topo=LinearTopo(),
        # our one SDN controller (Ryu) listens for OpenFlow on port 6653
        controller=partial(RemoteController, ip='127.0.0.1', port=6653),
        # explicit OVS settings for an SDN setup:
        #   protocols=OpenFlow13 -> speak OpenFlow 1.3 to Ryu
        #   failMode='secure'    -> with no controller and no flows, DROP
        #                           (never fall back to dumb standalone MAC
        #                            learning, we want to show SDN behaviour)
        switch=partial(OVSSwitch, protocols='OpenFlow13', failMode='secure'),
        autoSetMacs=True,     # MACs 00:00:00:00:00:01 .. 00:00:00:00:00:06
    )
    return net


def main():
    purge()   # remove state a previous crashed run may have left behind
    net = build_net()
    net.start()
    # never wait forever: if the controller is unreachable, say so plainly
    # instead of silently hanging the CLI
    if not net.waitConnected(30):
        warn('*** WARNING: not all switches connected to the controller.\n')
        warn('*** The mininet prompt will still start (local commands work),\n')
        warn('*** but pings will fail. Check outputs/ryu.log; then run: bash stop_sdn.sh\n')

    try:
        info('\n*** Map: h1,h2->s1  |  h3,h4->s2  |  h5,h6->s3  (backbone s1--s2--s3)\n')
        info('*** Try: pingall ; h1 ping h5 ; iperf h1 h6 ; '
             'sh ovs-ofctl -O OpenFlow13 dump-flows s1 ; exit\n\n')
        CLI(net)
    finally:
        net.stop()


if __name__ == '__main__':
    main()
