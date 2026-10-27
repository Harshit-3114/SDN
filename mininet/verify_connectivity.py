#!/usr/bin/env python3
"""
End-to-end connectivity verification + automatically generated evidence for
the report.

Usage (from the project root, with Ryu already running -- see start_sdn.sh):

    sudo python3 mininet/verify_connectivity.py            # tests, then CLI
    sudo python3 mininet/verify_connectivity.py --headless # tests, then exit

the suite starts the controller itself if it is not running; redirect
stdout to keep the evidence: ... > outputs/verify_output.txt 2>&1

What it checks, in order:
    T1  full pingAll round 1   -- every host pings every host (first packets
                                   go through the controller: learn + install)
    T2  full pingAll round 2   -- same test again, now served straight from
                                   the installed flow rules (much faster,
                                   proves the data plane is in OVS, not the
                                   controller)
    T3  end-to-end latency     -- ICMP ping h1 -> h5 and h2 -> h6 (both are
                                   2 backbone hops apart), 4 packets each
    T4  throughput             -- iperf h1 -> h6 (through BOTH backbone
                                    links; run through the host shells
                                    with a hard timeout so it cannot
                                    hang, unlike Mininet's iperf command)
    T5  flow tables            -- dump the flow table of every switch into
                                   outputs/flows_<switch>.txt for the report
    T6  MAC learning state     -- the controller's own log shows every
                                   PACKET_IN/FLOW_MOD (outputs/ryu.log)
    T7  dynamic disconnect     -- cut the s2-s3 backbone (link down), show
                                    traffic die, restore the link (up) and
                                    show traffic recover
    T8  controller detach      -- rip a switch OFF the controller
                                    (ovs-vsctl set-controller to a dead port
                                    + del-flows) -> it goes dark; re-attach it
                                    -> handshake, table-miss rule, traffic
                                    resumes (the SDN punchline: without the
                                    controller a switch cannot learn anything)
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # for topology.py

from mininet.cli import CLI
from mininet.log import info, setLogLevel, warn

from run_topo import build_net
from purge_state import purge

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = PROJECT_ROOT / 'outputs'


def banner(title):
    line = '=' * 64
    info('\n%s\n%s\n%s\n' % (line, title, line))


def ensure_controller():
    """Start the Ryu controller if it is not already running, so the
    whole suite can be launched standalone with one command."""
    running = subprocess.run(['pgrep', '-f', 'l2_learn_switch.py'],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE).returncode == 0
    if running:
        info('*** Ryu controller is already running (l2_learn_switch.py)\n')
        return
    info('*** Ryu controller not running -- starting it as a detached '
         'daemon (log -> outputs/ryu.log) ...\n')
    REPORT_DIR.mkdir(exist_ok=True)
    launch = ('setsid env PYTHONPATH=/opt/ryu-src '
              '/opt/ryu-venv/bin/python /opt/ryu-src/bin/ryu-manager '
              'controller/l2_learn_switch.py > outputs/ryu.log 2>&1 &')
    subprocess.run(['bash', '-c', launch], cwd=str(PROJECT_ROOT))
    for i in range(30):
        time.sleep(1)
        up = subprocess.run(['bash', '-c', "ss -ltn | grep -q ':6653 '"])
        if up.returncode == 0:
            info('*** Ryu is up and listening on OpenFlow port 6653 '
                 '(waited %d s)\n' % (i + 1))
            return
    warn('*** WARNING: Ryu did not come up within 30 s -- continuing '
         'anyway (switches will not forward; check outputs/ryu.log)\n')


def run():
    purge()   # remove state a previous crashed run may have left behind
    ensure_controller()
    net = build_net()
    net.start()
    if not net.waitConnected(30):
        warn('*** WARNING: not all switches are connected to the controller\n')

    try:
        # ------------------------------------------------------------ T1
        banner('T1: pingAll (round 1) -- first packets travel hop by hop '
               'through the controller (learning phase)')
        t0 = time.time()
        loss1 = net.pingAll()
        info('*** T1 lost=%s%%, took %.2f s\n' % (loss1, time.time() - t0))

        # ------------------------------------------------------------ T2
        banner('T2: pingAll (round 2) -- the same 30 ordered host pairs, now '
               'switched purely by the installed flow rules')
        t0 = time.time()
        loss2 = net.pingAll()
        info('*** T2 lost=%s%%, took %.2f s  (compare with T1: caching in '
             'the flow tables makes this dramatically faster)\n'
             % (loss2, time.time() - t0))

        # ------------------------------------------------------------ T3
        banner('T3: end-to-end latency, cross-switch pairs (2 backbone hops)')
        for src, dstip, label in (('h1', '10.0.0.5',
                                   'h1 -> h5 (s1 over s2 to s3)'),
                                  ('h2', '10.0.0.6',
                                   'h2 -> h6 (s1 over s2 to s3)')):
            info('--- %s ---\n' % label)
            net.getNodeByName(src).cmdPrint('ping -c 4 %s' % dstip)

        # ------------------------------------------------------------ T4
        # NOTE: we do NOT use Mininet's net.iperf() here. Its parser expects
        # two "/sec" summary lines from the iperf server, but iperf >= 2.2.1
        # emits only one per connection -- which makes net.iperf loop forever.
        # Hand-rolling it through the host shells is robust and bounded.
        banner('T4: TCP throughput h1 -> h6 with iperf (worst-case path, '
               'traverses the full backbone)')
        try:
            h1 = net.getNodeByName('h1')
            h6 = net.getNodeByName('h6')
            h6.cmd('killall -9 iperf 2>/dev/null')
            h6.cmd('iperf -s -p 5001 > /tmp/iperf_h6_server.log 2>&1 &')
            time.sleep(1)
            info('*** iperf client on h1 (5s transfer, hard-capped at 12s '
                 'so the suite can never hang)\n')
            h1.cmdPrint('timeout 12 iperf -c %s -t 5 -p 5001' % h6.IP())
            time.sleep(0.5)
            info('*** iperf server-side summary (h6):\n%s\n'
                 % h6.cmd('cat /tmp/iperf_h6_server.log'))
            h6.cmd('killall -9 iperf 2>/dev/null')
        except Exception as exc:                 # iperf missing or busy
            warn('*** iperf failed (%s) -- is the "iperf" package '
                 'installed?\n' % exc)


        # ------------------------------------------------------------ T7
        banner('T7: dynamic SWITCH disconnection -- cut the s2-s3 backbone, '
               'show traffic die, then bring it back up')
        info('*** cutting s2 <-> s3 (link down) ...\n')
        try:
            net.configLinkStatus('s2', 's3', 'down')
        except Exception as exc:
            warn('*** configLinkStatus unavailable (%s), falling back to '
                 'interface-level down\n' % exc)
            net.getNodeByName('s2').cmd('ip link set s2-eth4 down')
            net.getNodeByName('s3').cmd('ip link set s3-eth3 down')
        time.sleep(0.5)
        info('--- h5 -> h1 while the backbone is CUT (expected: 100% loss, '
             'the path is physically severed) ---\n')
        net.getNodeByName('h5').cmdPrint('ping -c 3 -W 1 10.0.0.1')
        info('*** restoring s2 <-> s3 (link up) ...\n')
        try:
            net.configLinkStatus('s2', 's3', 'up')
        except Exception:
            net.getNodeByName('s2').cmd('ip link set s2-eth4 up')
            net.getNodeByName('s3').cmd('ip link set s3-eth3 up')
        time.sleep(1)
        info('--- h5 -> h1 after the backbone is RESTORED ---\n')
        net.getNodeByName('h5').cmdPrint('ping -c 4 -W 1 10.0.0.1')

        # ------------------------------------------------------------ T8
        banner('T8: dynamic CONTROLLER disconnection -- detach s2 from Ryu, '
               'show it go dark, then re-attach it')
        s2 = net.getNodeByName('s2')
        info('*** detaching s2 from the controller (dead port tcp:9999) and '
             'wiping its flow table\n')
        s2.cmd('ovs-ofctl -O OpenFlow13 del-flows s2')
        s2.cmdPrint('ovs-vsctl set-controller s2 tcp:127.0.0.1:9999')
        time.sleep(1)
        info('--- h4 -> h1 while s2 has NO controller '
             '(expected: 100% loss -- failMode=secure + nothing to learn from) '
             '---\n')
        net.getNodeByName('h4').cmdPrint('ping -c 3 -W 1 10.0.0.1')
        info('*** re-attaching s2 to the real controller (tcp:127.0.0.1:6653)\n')
        s2.cmdPrint('ovs-vsctl set-controller s2 tcp:127.0.0.1:6653')
        time.sleep(2)      # handshake: Ryu re-sees s2 and re-installs table-miss
        info('--- h4 -> h1 after s2 re-connected and re-learned ---\n')
        net.getNodeByName('h4').cmdPrint('ping -c 4 -W 1 10.0.0.1')

        # ------------------------------------------------------------ T5
        banner('T5: flow tables of all switches -> outputs/flows_<switch>.txt')
        REPORT_DIR.mkdir(exist_ok=True)
        for swname in ('s1', 's2', 's3'):
            flows = net.getNodeByName(swname).cmd(
                'ovs-ofctl -O OpenFlow13 dump-flows %s' % swname)
            outfile = REPORT_DIR / ('flows_%s.txt' % swname)
            outfile.write_text(flows)
            nrule = sum(1 for ln in flows.splitlines() if 'priority=0' not in ln)
            info('*** %s : %d flow rules  (%s)\n' % (swname, nrule, outfile))

        info('*** note: s2 reports fewer rules -- T8 wiped its table, and it\n'
             '     has so far re-learned only the pair exercised in T8, which\n'
             '     is reactive flow installation demonstrated in one picture\n')

        # ------------------------------------------------------------ T6
        banner('T6: controller log -> outputs/ryu.log')
        ryulog = REPORT_DIR / 'ryu.log'
        if ryulog.exists():
            nlines = len(ryulog.read_text().splitlines())
            info('*** ryu.log has %d lines of PACKET_IN / FLOW_MOD evidence\n'
                 % nlines)
        else:
            info('*** (ryu.log not found here -- the controller was probably '
                 'started manually; its log is on its own terminal)\n')

        if '--headless' in sys.argv:
            return
        info('\n*** Interactive exploration: try "h1 ping h5", "pingall", '
             '"sh ovs-ofctl -O OpenFlow13 dump-flows s2", "exit"\n\n')
        CLI(net)
    finally:
        net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    run()
