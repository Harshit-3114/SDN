#!/usr/bin/env python3
r"""
Minimal topology for the SDN project: a LINEAR topology of 3 OpenFlow
switches in a line, with 2 hosts hanging from each switch (6 hosts total),
all pointed at a remote SDN controller (Ryu).

            h1          h2          h3          h4          h5          h6
        10.0.0.1    10.0.0.2    10.0.0.3    10.0.0.4    10.0.0.5    10.0.0.6
             \          /             \          /            \         /
              \        /               \        /              \       /
            [s1-eth1][s1-eth2]    [s2-eth1][s2-eth2]    [s3-eth1][s3-eth2]
                 [        s1  ]          [        s2        ]  [   s3   ]
                    s1-eth3 \             s2-eth3 \   s2-eth4 /    s3-eth3 /
                             \                     \            /
                              +---------- s1 >-----< s2 >-----< s3 ----------+
                                          (linear backbone, 1 Gb/s links)

Links (created in build order, which fixes the port numbering):
    s1-eth1 <-> h1        s2-eth1 <-> h3        s3-eth1 <-> h5
    s1-eth2 <-> h2        s2-eth2 <-> h4        s3-eth2 <-> h6
    s1-eth3 <-> s2-eth3                 (s1 -- s2 backbone)
                          s2-eth4 <-> s3-eth3   (s2 -- s3 backbone)

Everything lives under one /24 (10.0.0.0/24, a single L2 domain), as is
natively typical of Mininet: any host can reach any other through the
switches as long as the SDN controller installs the right flow rules.
"""

from mininet.topo import Topo


class LinearTopo(Topo):
    """3 switches in a line; 2 hosts per switch; remote controller."""

    # Mnemonic shifts: host i is attached to switch s(1 + (i-1)//2)
    HOSTS_PER_SWITCH = 2

    def build(self):
        # --- switches, in linear order ---------------------------------
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        s3 = self.addSwitch('s3')
        switches = (s1, s2, s3)

        # --- hosts: exactly two behind every switch --------------------
        # h1,h2 -> s1 ;  h3,h4 -> s2 ;  h5,h6 -> s3.
        # Adding all host links BEFORE the backbone links means every
        # host-facing interface gets a low number (eth1, eth2) and the
        # inter-switch links get eth3 / eth4 -- this keeps flow-table
        # dumps easy to read (port 1 == first host, port 3 == toward the
        # neighbour switch).
        n = 0
        for sw in switches:
            for _ in range(self.HOSTS_PER_SWITCH):
                n = n + 1
                self.addLink(self.addHost('h%d' % n), sw)

        # --- the line itself -------------------------------------------
        # s1 -- s2 and s2 -- s3. Default Mininet link = veth pair at
        # 1000 Mb/s with a 15 us queue delay, i.e. ~a realistic LAN hop.
        self.addLink(s1, s2)
        self.addLink(s2, s3)


# Module-level hook that lets you also run it through the classic `mn`
# CLI:   sudo mn --custom topology.py --topo linear --controller=remote,ip=127.0.0.1 ...
topos = {'linear': LinearTopo}

if __name__ == '__main__':
    # Small self-test: instantiate the topology and print it.
    topo = LinearTopo()
    print('hosts    :', sorted(topo.hosts()))
    print('switches :', sorted(topo.switches()))
    for src, dst in sorted(topo.links()):
        print('link     : %-4s <-> %-4s' % (src, dst))
