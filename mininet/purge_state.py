#!/usr/bin/env python3
"""
purge_state.py -- purge leftover Mininet/Open vSwitch state before a new run.

Mininet keeps some of its virtual network state outside its own processes
(kernel veth devices, OVS bridge definitions, switch pid files). If a
previous run crashed or was killed without net.stop() running, a new run
fails with RTNETLINK "File exists" while creating its links. Every script
that starts the topology calls purge() first, so a crashed run can never
poison the next one.
"""

import re
import subprocess

# Mininet names its interfaces hN-ethX / sN-ethX (veth pairs may show "@peer")
MN_LINK_RE = re.compile(r'^(h|s)\d+-eth\d+$')
IFACE_LINE_RE = re.compile(r'^\s*\d+:\s+(\S+?)(?:@\S+)?:')


def _run(cmd, quiet_fail=True):
    """Run a shell command; return (rc, combined_output)."""
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if rc := proc.returncode:
        return rc, (proc.stderr or proc.stdout)
    return 0, proc.stdout


def purge(verbose=True):
    """Delete all topology state a Mininet run might have left behind."""
    steps = (
        # remove OVS bridge definitions + controller junk (official cleaner).
        # MUST be a plain string: with subprocess.run(..., shell=True) a list's
        # items past the first become arguments to the SHELL and are dropped,
        # so ['mn', '-c'] actually executed a bare `mn` -- an interactive
        # Mininet CLI that silently sat waiting on the console for input.
        'mn -c',
    )
    for cmd in steps:
        _run(cmd)

    # belt & braces: drop our bridges directly, ignore errors
    for br in ('s1', 's2', 's3'):
        _run('ovs-vsctl --if-exists del-br %s' % br)

    # delete any remaining Mininet-named veth devices from /sys
    rc, out = _run('ip -o link show')
    deleted = []
    if rc == 0:
        for line in out.splitlines():
            m = IFACE_LINE_RE.match(line)
            if m and MN_LINK_RE.match(m.group(1)):
                dev = m.group(1).split('@')[0]
                _run('ip link del %s' % dev)
                deleted.append(dev)

    if verbose:
        if deleted:
            print('*** purged leftover devices: %s' % ' '.join(deleted))
        else:
            print('*** no leftover state found (clean start)')
