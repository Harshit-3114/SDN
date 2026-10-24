# SDN Course Project — Linear Topology (3 switches · 6 hosts · 1 controller)

A Software-Defined Networking lab built with **Mininet** (network emulation),
**Open vSwitch** (the switches), and a custom **Ryu** controller application
(the single SDN controller) speaking **OpenFlow 1.3**.

```
        ONE SDN controller (Ryu, custom L2-learning app)
             │
             │  control channel: OpenFlow 1.3 over TCP 6653
             │  (every OVS opens its own switch↔controller connection)
             ▼
      ┌───────────────────────────────────────────────────────┐
      │  data plane (veth links)                              │
      │                                                       │
      │     h1  h2         h3  h4         h5  h6              │
      │      \  /           \  /           \  /               │
      │       s1 ──eth3── s2 ──eth4── s3                     │
      └───────────────────────────────────────────────────────┘
```

Final map (addresses are auto-assigned by Mininet, MACs by `autoSetMacs`):

```
      h1        h2          h3        h4          h5        h6
  10.0.0.1  10.0.0.2    10.0.0.3  10.0.0.4    10.0.0.5  10.0.0.6
   MAC 01    MAC 02      MAC 03    MAC 04      MAC 05    MAC 06
      │         │           │         │           │         │
      │eth1   eth2│         │eth1   eth2│         │eth1   eth2│
   ┌─────────────────┐  ┌──────────────────┐  ┌─────────────────┐
   │        s1       │  │        s2        │  │        s3       │
   │ (Open vSwitch)  │  │ (Open vSwitch)   │  │ (Open vSwitch)  │
   └─────────────────┘  └──────────────────┘  └─────────────────┘
         │ eth3              │eth3        eth4│             │ eth3
          └───────────────┘                    └────────────┘
             (s1–s2 backbone)                   (s2–s3 backbone)
```

| Device | Role | Interfaces / addresses |
|--------|------|-----------------------|
| s1 | OVS bridge, datapath | eth1→h1, eth2→h2, eth3→s2 |
| s2 | OVS bridge, datapath | eth1→h3, eth2→h4, eth3→s1, eth4→s3 |
| s3 | OVS bridge, datapath | eth1→h5, eth2→h6, eth3→s2 |
| h1..h6 | end hosts (Linux netns) | 10.0.0.1 … 10.0.0.6 /24 |
| c0 | Ryu `l2_learn_switch.py` | listens on 127.0.0.1:6653 |

---

## 1. Repository layout

```
sdn/
├── controller/
│   └── l2_learn_switch.py        # the SDN controller app (Ryu, OF 1.3)
├── gui/
│   └── sdn_gui.py                # web control panel (Flask): the lab's GUI
├── mininet/
│   ├── topology.py               # LinearTopo: 3 switches, 6 hosts
│   ├── run_topo.py               # start topology, drop into 'mininet>' CLI
│   └── verify_connectivity.py    # automated tests T1–T8 + evidence gathering
├── outputs/                      # created at run time (not committed):
│   ├── ryu.log                   #   controller log (PACKET_IN / FLOW_MOD)
│   ├── verify_output.txt         #   verify suite results
│   ├── history.jsonl             #   every GUI operation
│   └── flows_s{1,2,3}.txt        #   dumped flow tables
├── start_sdn.sh                  # (WSL) OVS + Ryu + topology, one command
├── stop_sdn.sh                   # (WSL) cleanup
├── start.bat                     # Windows: double-click -> full lab in console
├── stop.bat                      # Windows: double-click -> console teardown
├── gui.bat                       # Windows: double-click -> web panel + browser
├── stop-gui.bat                  # Windows: double-click -> stop panel + lab
├── .gitignore / .gitattributes
└── README.md
```

## 2. One-time setup (already done on this machine)

Inside WSL Ubuntu (or any Ubuntu):

```bash
# Mininet + Open vSwitch + test tools
sudo apt-get update
sudo apt-get install -y mininet openvswitch-switch iperf tcpdump net-tools
```

**Ryu on Ubuntu 26.04 (Python 3.14).** The PyPI `ryu` release (4.34, last one)
cannot even *build* on Python ≥ 3.12 (`distutils` removed, new setuptools
incompatible). A maintained source install does work:

```bash
sudo apt-get install -y python3-venv python3-pip git
sudo python3 -m venv /opt/ryu-venv
sudo /opt/ryu-venv/bin/pip install eventlet lxml netaddr six oslo.config \
     routes tinyrpc webob setuptools
sudo git clone --depth 1 https://github.com/faucetsdn/ryu /opt/ryu-src

# smoke test  (expect:  ryu-manager 4.34)
PYTHONPATH=/opt/ryu-src /opt/ryu-venv/bin/python /opt/ryu-src/bin/ryu-manager --version
```

On older Pythons (e.g. Ubuntu 22.04/24.04) a plain `pip install ryu` is
enough instead.

## 3. Running the lab

### Option 1 — Web control panel (recommended)

From Windows: **double-click `gui.bat`** (project root). It starts the
control panel inside WSL and opens `http://localhost:5000` in your browser.

The panel owns the whole lab (OVS + Ryu + Mininet) in one process:

* **Start lab / Stop lab / Full stop** buttons — *Start* self-heals leftover
  state (a stale or half-dead controller process is detected, killed and
  restarted automatically); *Full stop* stops the lab **and** the controller
  (the panel itself stays open)
* **Live topology diagram** — dashed Ryu control lines and the s1–s2–s3
  backbone update in real time: a cut link turns red with a flowing-dash
  animation, a switch detached from the controller goes dark
* **pingall** — two rounds (controller-learned vs flow-cached) with timings
* **Detailed ping matrix** — all 30 host pairs at once, per-pair latency in
  every cell, PASS badge when the board is clean
* **ping** between any two hosts (with avg RTT), **iperf** h1→h6 throughput
* **Dynamic connect/disconnect demos** (the rubric row): cut/restore either
  backbone (`link s2 s3 down/up`), and **detach/attach** a switch from the
  controller (s2 *and* s3) — each with a companion test button showing the
  100% loss → recovery story live
* **Flow tables** viewer (`ovs-ofctl -O OpenFlow13 dump-flows s1/s2/s3`)
* A console mirroring the CLI (with ↑/↓ command history): `pingall | ping h1
  h5 | link s2 s3 down | detach s2 | flows | log | help`
* **History table** — every operation is recorded to
  `outputs/history.jsonl` for the report

`stop-gui.bat` stops the panel and tears the whole lab down.

### Option 2 — Interactive CLI

From Windows: double-click **`start.bat`** — everything (OVS + Ryu +
topology) comes up in one console window, which becomes the `mininet>`
prompt. `stop.bat` tears it down. On the **first start after a WSL reboot**
the window may sit silent for a few seconds while the Windows file system
under `/mnt/c` warms up — that is normal, not a hang.

> Tip: if your particular Windows console goes silent *after* `[4/4]` with
> no `mininet>` prompt, that is a WSL console/readline quirk this specific
> machine can show — use the panel (Option 1), which cannot be affected,
> or run from the Windows Terminal WSL tab.

From a WSL shell:

```bash
cd /mnt/c/Users/harsh/sdn
bash start_sdn.sh
```

This starts OVS, launches the controller detached (log: `outputs/ryu.log`),
waits until the controller really listens on 6653, and drops you into the
interactive Mininet prompt.

The classic one-liner equivalent (after `ryu-manager` is running separately):

```bash
sudo mn --custom mininet/topology.py --topo linear \
        --controller=remote,ip=127.0.0.1,port=6653 \
        --switch=ovs,protocols=OpenFlow13,failMode=secure --mac
```

Then, from `mininet>`:

```
pingall                      # every host pings every host
pingall                      # ... again: now served by the flow tables
h1 ping -c 3 h5              # a plain cross-switch ping
h6 iperf -s > /dev/null 2>&1 &               # iperf server on h6
h1 iperf -c 10.0.0.6 -t 3                    # throughput h1 -> h6
h6 killall iperf                             # (avoid the built-in `iperf`
sh ovs-ofctl -O OpenFlow13 dump-flows s1     #  command: it can hang; and
sh ovs-ofctl -O OpenFlow13 dump-flows s2     #  these are the flow tables)
exit                         # tears the topology down
```

Full automated verification + report evidence:

```bash
sudo python3 mininet/verify_connectivity.py                 # tests, then CLI
sudo python3 mininet/verify_connectivity.py --headless > outputs/verify_output.txt 2>&1
#   ^ the suite starts the controller itself if it is not running;
#     the redirected file is the report evidence (T1..T8)
```

Cleanup: `bash stop_sdn.sh`.

## 4. Verification results (what you should see)

`verify_connectivity.py` prints:

* **T1/T2** — `pingAll` twice. Round 1 succeeds already (the controller
  learns+installs fast enough that every ARP/ping still gets through),
  but is slow; round 2 is much faster because identical flows are now
  switched *entirely within OVS*. Both rounds must report `0% packet loss`
  — **that is your end-to-end connectivity proof.**
* **T3** — RTTs of `h1→h5` and `h2→h6`: first packet a few ms (ARP + flow
  installation), the remaining packets ~0.1–0.3 ms (pure data-plane flow
  matches).
* **T4** — iperf h1→h6 throughput across both backbone links. Measured here:
  45.0 Gbit/s (iperf 2.2.1 inside WSL2 loops through the host kernel, so
  numbers are generous and vary with kernel/iperf version/hardware; the
  classical single-digit-Gbit/s figures of older stacks are equally
  plausible — quote whatever YOUR run prints).
* **T5** — flow tables saved to `outputs/flows_s1.txt` etc. Each switch
  shows the `priority=0` table-miss rule plus a pair of `priority=1` unicast
  rules per host pair it has relayed.
* **T6** — `outputs/ryu.log` records every `PACKET_IN`, `FLOW_MOD`,
`FLOOD` — the control-plane evidence that packets really passed through the
controller.
* **T7** — dynamic disconnection: the s2–s3 backbone is cut with
  `link s2 s3 down` (h5→h1 = 100% loss — no physical path) and restored with
  `up` (traffic resumes).
* **T8** — dynamic controller detachment: s2 is pointed at a dead controller
  address and its flows wiped (100% loss: a failMode=secure switch with no
  controller forwards nothing new); re-attaching it to Ryu restores the
  handshake, installs the table-miss rule, and traffic resumes — the SDN
  punchline in one ping.

## 5. How packets travel between hosts on different switches
   (h1 @ s1  →  h5 @ s3: the full story — report-ready)

**Nothing in the data plane knows anything in the beginning.** Every OVS
bridge starts with exactly ONE flow rule (the table-miss rule the controller
installed at handshake time):

```
priority=0, match=*,            actions=CONTROLLER:65535
```

and, because we run `failMode=secure`, an OVS with no controller reachable
forwards *nothing*. All knowledge lives in one place: the controller's
`mac_to_port` table per switch (and, after learning, the switches' own flow
tables).

### Phase A — the first packet is an ARP *broadcast* (controller in the loop)

`h1` wants to ping `10.0.0.5` but its ARP cache is empty, so the kernel
emits an **ARP request** — a broadcast frame:

```
eth_src=00:00:00:00:00:01, eth_dst=ff:ff:ff:ff:ff:ff
"Who has 10.0.0.5? Tell 10.0.0.1"
```

1. **s1, port eth1 → PACKET_IN.** No flow matches → table-miss → the frame
   goes to Ryu. Ryu remembers: *s1 sees `MAC(h1)` on port 1*. It looks up
   `ff:ff:ff:ff:ff:ff` — not in the table → **FLOOD**: a `PACKET_OUT` sends
   the frame out eth2 (h2) and eth3 (s2). (No flow rule is installed for
   broadcasts — that would be defying how broadcasts behave.)
2. **h2 drops it** (it is not 10.0.0.5). The copy that went into the
   **s1–s2 backbone** arrives at s2, port eth3 → PACKET_IN: Ryu records
   *s2 sees `MAC(h1)` on port 3* and floods it out eth1, eth2, eth4.
3. **h3 and h4 drop it.** The next copy crosses the **s2–s3 backbone**,
   arrives s3, port eth3 → PACKET_IN: Ryu records *s3 sees `MAC(h1)` on
   port 3* and floods out eth1 and eth2.
4. **h5 answers** — "10.0.0.5 is at `MAC(h5)`" — as a *unicast* ARP reply:
   `eth_src=…:05, eth_dst=…:01`, entering s3, port eth1.
5. **The payoff of step 3:** Ryu already knows at s3 that `MAC(h1)` lives
   behind **port 3**. So it can do more than replay the frame: it
   simultaneously installs a **flow rule** on s3
   (`match in_port=1, eth_src=…:05, eth_dst=…:01 → output:3`, priority 1)
   and sends this one reply (`PACKET_OUT` port 3 → backbone → s2:eth4).
6. **s2 (eth4 → PACKET_IN):** Ryu adds `MAC(h5) → port 4` to s2's table…
   `MAC(h1)` is known there too (port 3, from step 2) → installs
   `match in_port=4 … eth_dst=…:01 → output:3` and relays the reply into
   the **s1–s2 backbone** (s2:eth3 → s1:eth3).
7. **s1 (eth3 → PACKET_IN):** Ryu adds `MAC(h5) → port 3`, installs
   `match in_port=3 … eth_dst=…:01 → output:1`, and delivers the ARP reply
   to **h1**. h1 caches `10.0.0.5 ⇔ MAC(h5)` and the ICMP ping can start.

So one ARP exchange cost ~2 controller round-trips per switch in each
direction — but it also **provisioned every switch along the path** with
rules for h1→h5 traffic. Broadcast handling and unicast path installation
are different jobs in an SDN: broadcasts are *relayed* by the controller's
explicit `PACKET_OUT`s, unicast paths are *made permanent* via `FLOW_MOD`s.

### Phase B — ICMP echo, then: pure data plane, controller not involved

8. `h1` builds the ICMP echo (dst `MAC(h5)` from its ARP cache) and hands it
   to s1. No matching rule yet for that direction → PACKET_IN (last one!).
   Ryu: `MAC(h5)` is known at s1 on **port 3** → installs
   `match in_port=1, eth_src=…:01, eth_dst=…:05 → output:3` + `PACKET_OUT`.
9. **s2 and s3 handle the same frame the same way** (one PACKET_IN each,
   one rule each, one PACKET_OUT each — the frame is carried by the
   controller exactly once through each hop on its first trip).
10. `h5` echoes back. This response however **never reaches the controller
    again**: every hop already has a matching rule from step 5–7 (`h5→h1`
    direction was installed during the ARP reply). The frame is matched in
    the OVS pipeline at s3 (`output:3`) → backbone → s2 (`output:3`,
    entering eth4) → backbone → s1 (`output:1`) → h1. *Every subsequent echo
    request/reply pair now flows exclusively through the flow tables* —
    microseconds, no controller, no PACKET_IN. This is why round-2 `pingall`
    is dramatically faster than round-1.

### Why this is *SDN*, not a normal switch network

* The intelligence (MAC learning, path decision) is **not** in the switches:
  they only execute flow tables. The knowledge lives centrally in
  `l2_learn_switch.py` (`self.mac_to_port`); the switches become "dumb,
  consistent forwarding boxes" (the SDN design principle of *separation of
  control plane and data plane*).
* The switches learn topology **one packet at a time, wherever packets
  actually flow** — notice how nothing needs to know that the three
  switches form a line; the controller only ever sees ports and MACs
  (per-switch behaviour is topology-independent).
* New flows are provisioned *lazily, reactively*: first packet of a flow
  pays the control-plane tax, all later packets ride in the data plane.

### Concrete evidence to cite in your report

* `outputs/flows_s1.txt` — contains only `MATCH all → CONTROLLER` at first
  (before any traffic) and the learned rules afterwards, e.g.:
  ```
  priority=1,match=in_port=1,eth_src=00:00:00:00:00:01,eth_dst=00:00:00:00:00:05 actions=output:3   <- h1->h5 out the s2 side
  priority=1,match=in_port=3,eth_src=00:00:00:00:00:05,eth_dst=00:00:00:00:00:01 actions=output:1   <- h5->h1 back in
  priority=0,match=*                                     actions=CONTROLLER:65535
  ```
* `s2` shows the same pairs twice (once per direction, watch `in_port`
  change): 3 = towards s1, 4 = towards s3.
* `outputs/ryu.log` — the chronological `PACKET_IN → FLOW_MOD / FLOOD`
  narrative of both phases.
* Optional Wireshark proof that the frame physically crosses the backbone:
  `sudo tcpdump -i s1-eth3 -w s1-eth3.pcap` while `h1 ping h5` runs
  (Mininet switch interface `s1-eth3` is visible to the host kernel).

## 6. Viva cheat-sheet (maps to the marking rubric)

| Rubric item | How you demonstrate it |
|-------------|------------------------|
| Topology creation & size (2) | show `mininet/topology.py` (`LinearTopo`: 3 switches s1–s3, hosts h1–h6, 2 per switch, 2 backbone links) and, in the CLI, `net` / `hosts` / `switches` / `links` counts; `py len(net.hosts), len(net.switches)` prints `6 3` |
| Network configuration (2) | the topology is built against a **remote controller** (`RemoteController ip=127.0.0.1 port=6653` — in `run_topo.py`), i.e. an external SDN controller, not the mn-default; `sh ovs-vsctl show` lists every bridge with `Controller "tcp:127.0.0.1:6653"`. Full 2 marks: Open vSwitch bridges are pointed at Ryu as a *remote* OpenFlow controller |
| Connectivity testing (ping/pingall) (2a) | `pingall` twice: first 0% loss, second round near-instant (flow tables) — see §4 |
| Connecting & disconnecting switches dynamically (2b) | inside `mininet>`: `link s2 s3 down` → `h5 ping -c3 h1` (100% loss: path cut) → `link s2 s3 up` → traffic recovers; and to yank the CONTROL channel: `sh ovs-vsctl set-controller s2 tcp:127.0.0.1:9999` + `sh ovs-ofctl -O OpenFlow13 del-flows s2` → `h4 ping -c3 h1` (100% loss: switch is dark) → `sh ovs-vsctl set-controller s2 tcp:127.0.0.1:6653` → ping works again after s2 re-handshakes with Ryu (log shows the new table-miss install). T7/T8 in `verify_connectivity.py` run both demos automatically |
| Mininet commands (viva) | `pingall`, `pingpair`, `h1 ping h5`, `net`, `hosts`, `switches`, `links`, `dump`, `py net.get('s1').cmd(...)`, `sh ovs-ofctl -O OpenFlow13 dump-flows s2`; for throughput use the iperf recipe below (safer than Mininet's `iperf` command — see the iperf note) |
| SDN concepts (viva) | control plane (Ryu: learning, MAC table, FLOW_MOD) vs data plane (OVS pipeline: matches packets per flow rule); PACKET_IN on table-miss; PACKET_OUT replay; failMode=secure explains the dark switch in the dynamic demo; reactive flow installation; per-switch `mac_to_port` |
| Interpretation of results (viva) | round-2 pingall is faster with identical 0% loss → answered straight from the data plane (flow tables), not the controller; first-packet latency ms then 0.1 ms → one controller round-trip then flow match; 100% loss in T7/T8 exactly when either the *path* (link down) or the *brain* (controller detach) is cut for switch s2/s3-side hosts, recovery after restoration |

**iperf note (throughput during the viva).** Mininet's built-in
`iperf` command can spin forever with iperf 2.2.1+ (its parser waits
for a second server summary line that iperf never sends). Use the
host-shell form instead — valid inside `mininet>`:

```
mininet> h6 iperf -s > /dev/null 2>&1 &        # iperf server on h6
mininet> h1 iperf -c 10.0.0.6 -t 3             # 3 s transfer h1 -> h6
mininet> h6 killall iperf                      # clean up
```

The verify suite's T4 runs exactly this (with a hard `timeout 12`
cap), so the report evidence is unaffected.

## 7. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ovs-vsctl show` fails after reboot/start | `sudo service openvswitch-switch start` again (WSL2 shuts WSL daemons down when idle) |
| pingAll 100% loss, ryu.log empty | switches not connected: check `outputs/ryu.log` exists and shows 3 switches; try `bash stop_sdn.sh` then `bash start_sdn.sh` |
| `ImportError: No module named distutils` from ryu | install latest `setuptools` into the venv (provides the distutils shim) — see section 2 |
| `ping works but new ping needs new rules` | normal: ARP cache expiry re-triggers ARP; controller handles it |
| `mn: invoking 'sudo mn -c' does not clean` prints errors | harmless leftovers of an already-clean state |
| Need to re-run after `wsl --shutdown` | just `bash start_sdn.sh` again |

## 8. Ideas for extra credit

* Add `idle_timeout=30` to the `OFPFlowMod` in `l2_learn_switch.add_flow`
  and re-run: ping pairs go stale after 30 s of inactivity and the next ping
  does a fresh PACKET_IN round — good demonstration of flow aging.
* Block a specific host with a `match=eth_src` drop rule and show the
  controller enforcing policy (that's the classic SDN "centralized policy"
  argument).
* Add a second controller (e.g., Ryu's `simple_switch_13.py` from the same
  source tree) and compare `pingall` behaviour to your custom app.
