#!/usr/bin/env python3
"""
sdn_gui.py -- Web control panel for the SDN lab (Ryu + Mininet).

Run from the project root inside WSL Ubuntu (this process owns the lab):

    sudo /opt/ryu-venv/bin/python gui/sdn_gui.py                  (manually)
    gui.bat                                (from Windows: double-click)

Then open http://localhost:5000 -- the panel lets you start/stop the lab
and run every demonstration (pingall, link cut/restore, controller
detach/attach, iperf, flow tables, live controller log) with one click.
Operations are appended to outputs/history.jsonl for the report.

Design notes:
  * The panel and the lab share ONE process, so no console-readline quirks
    can freeze interaction: everything is HTTP + JSON.
  * All destructive/slow operations run under a global lock, one at a time.
  * No external web assets: the page works fully offline (viva-safe).
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, Response

# --- project layout ----------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]      # .../sdn
sys.path.insert(0, str(ROOT / 'mininet'))       # build_net / purge / topology
# The Ryu venv cannot see the apt-installed mininet package; append the
# Debian/Ubuntu dist-packages path so this process has BOTH flask (venv)
# and mininet (system).
_DIST = '/usr/lib/python3/dist-packages'
if os.path.isdir(_DIST) and _DIST not in sys.path:
    sys.path.append(_DIST)
sys.dont_write_bytecode = True
os.chdir(str(ROOT))

from run_topo import build_net            # noqa: E402  (project's builder)
from purge_state import purge             # noqa: E402
from mininet.log import setLogLevel        # noqa: E402

setLogLevel('info')

RYU_PY = '/opt/ryu-venv/bin/python'
RYU_RUN = ('/opt/ryu-src/bin/ryu-manager', 'controller/l2_learn_switch.py')
RYU_ENV = {'PYTHONPATH': '/opt/ryu-src'}
OF_PORT = 6653
SWITCHES = ('s1', 's2', 's3')
HOSTS = ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')
CONTROL_ADDR = 'tcp:127.0.0.1:%d' % OF_PORT
DEAD_ADDR = 'tcp:127.0.0.1:9999'

HISTORY = ROOT / 'outputs' / 'history.jsonl'

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False


class Lab:
    """Everything the panel drives, guarded by one lock."""

    def __init__(self):
        self.lock = threading.RLock()
        self.net = None
        self.state = 'idle'          # idle|starting|running|stopping|error
        self.detail = ''
        self.started_at = None

    # -- helpers -------------------------------------------------------------
    def _run(self, cmd, shell=True):
        return subprocess.run(cmd, shell=shell, capture_output=True,
                              text=True)

    def _ryu_listening(self):
        return self._run("ss -ltn | grep -q ':%d '" % OF_PORT).returncode == 0

    def _ryu_running(self):
        return self._run("pgrep -f l2_learn_switch").returncode == 0

    def _ovs_ok(self):
        return self._run('ovs-vsctl show').returncode == 0

    def _connected_list(self):
        """['s1', ...] of switches whose OpenFlow channel is up."""
        if not self.net:
            return []
        out = []
        for sw in self.net.switches:
            try:
                out.append(sw.name) if sw.connected() else None
            except Exception:
                pass
        return out

    def _history(self, op, ok, detail=''):
        rec = {'time': datetime.now().strftime('%H:%M:%S'),
               'op': op, 'ok': bool(ok), 'detail': detail}
        try:
            HISTORY.parent.mkdir(exist_ok=True)
            with open(HISTORY, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(rec) + '\n')
        except Exception:
            pass
        return rec

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        with self.lock:
            if self.state == 'running':
                return {'ok': False, 'log': ['lab already running']}
            self.state, self.detail = 'starting', ''
            steps = []
            try:
                steps.append('[1/4] Open vSwitch service')
                if not self._ovs_ok():
                    self._run('systemctl start openvswitch-switch || '
                              'service openvswitch-switch start')
                steps.append('       ovs: %s' %
                             ('ok' if self._ovs_ok() else 'STILL DOWN'))
                steps.append('[2/4] purge leftover state')
                purge(verbose=False)
                steps.append('       clean')
                steps.append('[3/4] Ryu controller (l2_learn_switch.py)')
                if self._ryu_running() and self._ryu_listening():
                    steps.append('       already running')
                elif self._ryu_running():
                    steps.append('       process alive but not listening yet'
                                 ' -- waiting')
                    self._wait_ryu_up(steps)
                else:
                    sub = subprocess.Popen(
                        ['setsid', 'env',
                         'PYTHONPATH=' + RYU_ENV['PYTHONPATH'],
                         RYU_PY, *RYU_RUN],
                        stdout=open(ROOT / 'outputs' / 'ryu.log', 'w'),
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, start_new_session=True)
                    steps.append('       launched pid %d' % sub.pid)
                    if not self._wait_ryu_up(steps):
                        raise RuntimeError('ryu did not listen within 30 s; '
                                           'see outputs/ryu.log')
                steps.append('[4/4] Mininet topology (3 switches, 6 hosts)')
                self.net = build_net()
                self.net.start()
                if not self.net.waitConnected(45):
                    steps.append('       !! switches not all connected '
                                 '(check ryu.log)')
                else:
                    steps.append('       all switches connected to Ryu')
                self.started_at = time.time()
                self.state = 'running' if self._connected_list() else 'error'
                for sw in SWITCHES:
                    steps.append('* switch %s ready' % sw) \
                        if sw in self._connected_list() else None
            except Exception as exc:                    # keep the server alive
                self.state = 'error'
                steps.append('!! start failed: %s' % exc)
            return {'ok': self.state == 'running', 'log': steps}

    def _wait_ryu_up(self, steps):
        for _ in range(60):
            if self._ryu_listening():
                steps.append('       OpenFlow port %d listening' % OF_PORT)
                return True
            time.sleep(0.5)
        return False

    def stop(self, full=False):
        with self.lock:
            steps = []
            try:
                if self.net:
                    self.net.stop()
                    self.net = None
                steps.append('mininet topology down')
                self._run('mn -c')
                for br in SWITCHES:
                    self._run('ovs-vsctl --if-exists del-br %s' % br)
                steps.append('ovs state purged')
                if full:
                    self._run('pkill -f l2_learn_switch')
                    steps.append('ryu controller stopped')
                else:
                    steps.append('ryu left running (click Full stop to kill it)')
            except Exception as exc:
                steps.append('!! stop issue: %s' % exc)
            self.state = 'idle'
            self.started_at = None
            return {'ok': True, 'log': steps}

    # -- demos ----------------------------------------------------------------
    def ping_all(self):
        if not self._require():
            return
        t0 = time.time()
        loss1 = self.net.pingAll()
        r1 = {'loss': loss1, 'took': round(time.time() - t0, 2)}
        t0 = time.time()
        loss2 = self.net.pingAll()
        r2 = {'loss': loss2, 'took': round(time.time() - t0, 2)}
        txt = ('round 1 (through the controller):  %s%% dropped, %.2fs\n'
               'round 2 (from the flow tables):     %s%% dropped, %.2fs'
               % (r1['loss'], r1['took'], r2['loss'], r2['took']))
        self._history('pingall', loss1 == 0 and loss2 == 0, txt)
        return {'ok': True, 'text': txt, 'rounds': [r1, r2]}

    def ping_pair(self, src, dst, count=2, wait=1):
        if not self._require():
            return
        s = self.net.get(src)
        d = self.net.get(dst)
        out = s.cmd('ping -c %d -W %d %s' % (count, wait, d.IP()))
        m = re.search(r'(\d+) packets transmitted\S+\s*(\d+) received', out)
        loss = 100
        if m:
            tx, rx = int(m.group(1)), int(m.group(2))
            loss = 100 - int(100.0 * rx / max(tx, 1))
        txt = '%s -> %s (%s): %d%% loss\n' % (src, dst, d.IP(), loss) + out
        self._history('ping %s->%s' % (src, dst), loss == 0,
                      '%d%% loss' % loss)
        return {'ok': True, 'text': txt, 'loss': loss}

    def matrix(self):
        """Sequential per-pair ping matrix (fast warm, slower first time)."""
        if not self._require():
            return
        rows = []
        all_ok = True
        for src in HOSTS:
            s = self.net.get(src)
            cells, statuses = [], []
            for dst in HOSTS:
                if dst == src:
                    cells.append('--')
                    statuses.append(None)
                    continue
                d = self.net.get(dst)
                out = s.cmd('ping -c 1 -W 1 %s' % d.IP())
                okk = bool(re.search(r'1 received', out))
                all_ok &= okk
                cells.append('OK' if okk else 'FAIL')
                statuses.append(okk)
            rows.append({'src': src, 'cells': cells,
                         'statuses': statuses})
        txt = ('%s: all %d pairs replied through switched paths'
               % ('PASS' if all_ok else 'FAIL', 30))
        self._history('ping matrix', all_ok, txt)
        return {'ok': all_ok, 'rows': rows, 'text': txt}

    def iperf(self, seconds=5):
        if not self._require():
            return
        h1, h6n = self.net['h1'], self.net['h6']
        h6n.cmd('killall -9 iperf 2>/dev/null')
        h6n.cmd('iperf -s -p 5001 > /tmp/iperf_gui_server.log 2>&1 &')
        time.sleep(1)
        out = h1.cmd('timeout %d iperf -c %s -t %d -p 5001'
                     % (seconds + 7, h6n.IP(), seconds))
        srv = h6n.cmd('cat /tmp/iperf_gui_server.log')
        h6n.cmd('killall -9 iperf 2>/dev/null')
        b = re.findall(r'([\d.]+) ([KMG])bits/sec', out)
        m = re.search(r'([\d.]+) ([KMG])bits/sec\s*$', out, re.M)
        rate = '%s %sbits/sec' % (m.group(1), m.group(2)) if m else 'n/a'
        txt = 'TCP throughput h1 -> h6 (both backbone links): %s\n' % rate + out \
            + '\n--- iperf server (h6) saw:\n' + srv
        self._history('iperf h1->h6', bool(m), rate)
        return {'ok': bool(m), 'text': txt, 'rate': rate}

    def set_link(self, left, right, state):
        if not self._require():
            return
        if state not in ('up', 'down'):
            return {'ok': False, 'text': 'state must be up|down'}
        self.net.configLinkStatus(left, right, state)
        msg = 'link %s <-> %s is %%s' % (left, right) % \
            ('DOWN' if state == 'down' else 'up')
        self._history('link %s %s' % (left, right), True, msg)
        return {'ok': True, 'text': msg}

    def link_states(self):
        """Enumerate every mininet link: name pairs + interface up/down."""
        if not self.net:
            return {'ok': False, 'links': []}
        out = []
        for l in getattr(self.net, 'links', []):
            try:
                i1, i2 = l.intf1, l.intf2
                out.append('%s:%s <-> %s:%s  [%s | %s]' %
                           (i1.node.name, i1.name, i2.node.name, i2.name,
                            i1.status(), i2.status()))
            except Exception:
                continue
        return {'ok': True, 'links': out}

    def controller_op(self, switch, action):
        if not self._require():
            return
        sw = self.net.get(switch)
        if action == 'detach':
            sw.cmd('ovs-ofctl -O OpenFlow13 del-flows %s' % switch)
            sw.cmdPrint('ovs-vsctl set-controller %s %s' % (switch, DEAD_ADDR))
            txt = ('%s detRacted from the controller (dead port %s) and its '
                   'flow table was wiped.\nAny new traffic forwarding through '
                   'it now requires a controller: expect 100%% loss '
                   '(failMode=secure).' % (switch, DEAD_ADDR))
            self._history('%s detach' % switch, True, txt)
            return {'ok': True, 'text': txt}
        elif action == 'attach':
            sw.cmdPrint('ovs-vsctl set-controller %s %s' %
                        (switch, CONTROL_ADDR))
            time.sleep(2)
            txt = ('%s re-attached to Ryu at %s: the handshake reinstalls the '
                   'table-miss rule; next unknown traffic re-learns through '
                   'the controller.' % (switch, CONTROL_ADDR))
            self._history('%s attach' % switch, True, txt)
            return {'ok': True, 'text': txt}
        return {'ok': False, 'text': 'action must be attach|detach'}

    def flows(self):
        if not self._require():
            return
        tables = {}
        for swname in SWITCHES:
            tables[swname] = self.net[swname].cmd(
                'ovs-ofctl -O OpenFlow13 dump-flows %s' % swname)
        n = sum(len(t.splitlines()) for t in tables.values())
        txt = 'flow rules dumped for %s (%d lines total)' % (
            ', '.join(SWITCHES), n)
        self._history('flow tables', True, txt)
        return {'ok': True, 'tables': tables, 'text': txt}

    def ryu_log(self, lines=40):
        p = ROOT / 'outputs' / 'ryu.log'
        try:
            data = p.read_text(errors='replace').splitlines()[-lines:]
            return {'ok': True, 'lines': data}
        except FileNotFoundError:
            return {'ok': False, 'lines': ['(outputs/ryu.log not found)']}

    def cmd(self, text):
        """Tiny console grammar mirroring the mininet CLI."""
        text = (text or '').strip()
        parts = text.split()
        if not parts:
            return {'ok': False, 'text': 'empty command (try: help)'}
        head = parts[0].lower()
        if head == 'help':
            return {'ok': True, 'text': HELP}
        if head in ('pingall',):
            return self.ping_all()
        if head in ('iperf',):
            return self.iperf()
        if head == 'flows' and len(parts) == 1:
            return self.flows()
        if head == 'ping' and len(parts) == 3 and \
                parts[1] in HOSTS and parts[2] in HOSTS:
            return self.ping_pair(parts[1], parts[2])
        if head == 'link' and len(parts) == 3 and \
                parts[2] in ('up', 'down') and \
                parts[0][0:1] == 's' and parts[1][0:1] == 's' and \
                parts[0] in SWITCHES and parts[1] in SWITCHES:
            return self.set_link(parts[0], parts[1], parts[2])
        if head in ('detach', 'attach') and len(parts) == 2 and \
                parts[1] in SWITCHES:
            return self.controller_op(parts[1], head)
        if head == 'log':
            r = self.ryu_log(200)
            return {'ok': True, 'text': '\n'.join(r['lines'][-200:])}
        if head in ('hosts', 'switches', 'links') and self.net:
            if head == 'hosts':
                return {'ok': True, 'text': ' '.join(
                    h.name for h in self.net.hosts)}
            if head == 'switches':
                return {'ok': True, 'text': ' '.join(
                    s.name for s in self.net.switches)}
            if head == 'links':
                r = self.link_states()
                return {'ok': True, 'text': '\n'.join(r['links']) or '(none)'}
        return {'ok': False, 'text': 'unknown command (try: help)'}

    def _require(self):
        if self.state != 'running' or not self.net:
            return False, {'ok': False,
                           'text': 'lab is not running -- click Start first'}
        return True, None


HELP = """Console commands (same ideas as the mininet> CLI):
  pingall                two full 6x6 rounds (controller vs flow tables)
  ping h1 h5             ICMP ping between two hosts
  iperf                  TCP throughput h1 -> h6
  flows                  dump flow tables of s1, s2, s3
  link s2 s3 down        cut the backbone between two switches (up to restore)
  detach s2              switch stops talking to the controller (goes dark)
  attach s2              switch re-joins the controller (re-learns)
  hosts | switches | links | log
"""

LAB = Lab()


# ---------------------------------------------------------------- endpoints --
def _ok(fn):
    """wrap: lab._require guard handled per op; catch ALL exceptions."""
    try:
        return fn()
    except Exception as exc:
        return jsonify({'ok': False, 'text': 'error: %r' % exc}), 200


@app.route('/')
def index():
    return Response(PAGE, mimetype='text/html')


@app.route('/api/status')
def status():
    ryu = LAB._ryu_listening() or LAB._ryu_running()
    return jsonify({'state': LAB.state, 'detail': LAB.detail,
                    'ovs': LAB._ovs_ok(), 'ryu': ryu,
                    'mininet': LAB.net is not None and LAB.state == 'running',
                    'connected': LAB._connected_list(),
                    'uptime': int(time.time() - LAB.started_at) if
                    LAB.started_at else 0})


@app.route('/api/start', methods=['POST'])
def start():
    return jsonify(LAB.start())


@app.route('/api/stop', methods=['POST'])
def stop():
    full = request.form.get('full') == '1' or \
        request.get_json(silent=True) == {'full': True} or \
        request.args.get('full') == '1'
    rec = LAB.stop(full=bool(full))
    LAB._history('stop' + (' (full)' if full else ''), True, ' '.join(rec['log']))
    return jsonify(rec)


@app.route('/api/pingall', methods=['POST'])
def pingall():
    return _ok(lambda: jsonify(LAB.ping_all()))


@app.route('/api/ping', methods=['POST'])
def ping():
    src = request.form.get('src', '')
    dst = request.form.get('dst', '')
    if src not in HOSTS or dst not in HOSTS:
        return jsonify({'ok': False, 'text': 'src/dst must be h1..h6'})
    return _ok(lambda: jsonify(LAB.ping_pair(src, dst)))


@app.route('/api/matrix', methods=['POST'])
def matrix():
    return _ok(lambda: jsonify(LAB.matrix()))


@app.route('/api/iperf', methods=['POST'])
def iperf():
    return _ok(lambda: jsonify(LAB.iperf()))


@app.route('/api/link', methods=['POST'])
def link():
    left, right, state = (request.form.get(k) for k in ('left', 'right', 'state'))
    if left not in SWITCHES or right not in SWITCHES or left == right:
        return jsonify({'ok': False, 'text': 'left/right must be two '
                        'different switches'})
    return _ok(lambda: jsonify(LAB.set_link(left, right, state)))


@app.route('/api/controller', methods=['POST'])
def controller():
    sw, action = request.form.get('switch'), request.form.get('action')
    if sw not in SWITCHES or action not in ('attach', 'detach'):
        return jsonify({'ok': False, 'text': 'switch/action invalid'})
    return _ok(lambda: jsonify(LAB.controller_op(sw, action)))


@app.route('/api/flows')
def flows():
    return _ok(lambda: jsonify(LAB.flows()))


@app.route('/api/ryulog')
def ryulog():
    n = int(request.args.get('lines', 50))
    return jsonify(LAB.ryu_log(n))


@app.route('/api/history')
def history():
    rows = []
    try:
        with open(HISTORY, encoding='utf-8') as fh:
            rows = [json.loads(x) for x in fh if x.strip()][::-1][:40]
    except FileNotFoundError:
        pass
    return jsonify({'rows': rows})


@app.route('/api/cmd', methods=['POST'])
def cmd_():
    text = request.form.get('cmd', '')
    return _ok(lambda: jsonify(LAB.cmd(text)))


@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    LAB.stop(full=True)
    def bye():
        time.sleep(1)
        os._exit(0)
    threading.Thread(target=bye, daemon=True).start()
    return jsonify({'ok': True, 'text': 'lab stopped, panel closing'})


# ------------------------------------------------------------ the web page --
PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SDN Lab — Ryu + Mininet control panel</title>
<style>
:root{--bg:#0d1117;--pane:#161b22;--line:#30363d;--fg:#c9d1d9;--dim:#8b949e;
--ok:#3fb950;--bad:#f85149;--warn:#d29922;--acc:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 "Segoe UI",Roboto,Arial,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:10px 18px;
background:var(--pane);border-bottom:1px solid var(--line)}
header h1{font-size:17px;margin:0;font-weight:600}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;
border:1px solid var(--line);border-radius:20px;font-size:12px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--bad)}
.dot.on{background:var(--ok)}
main{display:grid;grid-template-columns:minmax(340px,380px) 1fr;
gap:14px;padding:14px;max-width:1400px;margin:0 auto}
section{background:var(--pane);border:1px solid var(--line);
border-radius:10px;padding:14px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;
color:var(--dim);margin:0 0 10px}
button{background:#21262d;color:var(--fg);border:1px solid var(--line);
padding:7px 10px;border-radius:8px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--acc)}
button.primary{background:#1f6feb;border-color:#1f6feb;color:#fff}
button.danger{background:#8b1c1c;border-color:#a40404;color:#fff}
button:disabled{opacity:.45;cursor:default}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
svg .sw{fill:#21262d;stroke:var(--acc);stroke-width:1.5}
svg .host{fill:#0d1117;stroke:var(--ok);stroke-width:1.5}
svg .link{stroke:#57606a;stroke-width:5;stroke-linecap:round}
svg text{fill:var(--fg);font:11px monospace;text-anchor:middle}
svg .t{fill:var(--dim);font:10px sans-serif}
#out{height:330px;overflow:auto;background:#0d1117;border:1px solid var(--line);
border-radius:8px;padding:10px;white-space:pre-wrap;
font:12px/1.5 Consolas,monospace}
table{width:100%;border-collapse:collapse;font-size:12px}
td,th{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left}
th{color:var(--dim);font-weight:500}
tbody.m td.ok{color:var(--ok)} tbody.m td.fail{color:var(--bad)}
input{background:#0d1117;border:1px solid var(--line);color:var(--fg);
padding:7px 10px;border-radius:8px;width:100%;font:13px monospace}
.row{display:flex;gap:8px;align-items:center;margin-top:8px}
#busy{color:var(--warn);font-size:12px;min-height:16px;white-space:pre-line}
.foot{padding:6px 18px;color:var(--dim);font-size:12px}
a{color:var(--acc)}
</style></head>
<body>
<header>
 <h1>SDN Lab control panel</h1>
 <span class="chip"><span class="dot" id="d_ovs"></span> Open vSwitch</span>
 <span class="chip"><span class="dot" id="d_ryu"></span> Ryu controller</span>
 <span class="chip"><span class="dot" id="d_mn"></span> Mininet</span>
 <span class="chip" id="chip_state">idle</span>
 <span style="flex:1"></span>
 <button class="primary" id="b_start">Start lab</button>
 <button id="b_stop">Stop lab</button>
 <button class="danger" id="b_full">Full stop</button>
</header>
<main>
<section>
 <h2>Topology — linear: s1–s2–s3, 2 hosts each</h2>
 <svg viewBox="0 0 340 240" width="100%" height="250">
  <line class="link" x1="100" y1="120" x2="170" y2="120"/>
  <line class="link" x1="170" y1="120" x2="240" y2="120"/>
  <line class="link" x1="60" y1="70" x2="100" y2="115"/>
  <line class="link" x1="100" y1="125" x2="60" y2="175"/>
  <line class="link" x1="130" y1="70" x2="170" y2="112"/>
  <line class="link" x1="170" y1="128" x2="130" y2="175"/>
  <line class="link" x1="200" y1="70" x2="240" y2="112"/>
  <line class="link" x1="240" y1="128" x2="200" y2="175"/>
  <rect class="sw" x="80" y="105" width="40" height="30" rx="6"/>
  <text x="100" y="124">s1</text>
  <rect class="sw" x="150" y="105" width="40" height="30" rx="6"/>
  <text x="170" y="124">s2</text>
  <rect class="sw" x="220" y="105" width="40" height="30" rx="6"/>
  <text x="240" y="124">s3</text>
  <circle class="host" cx="60" cy="65" r="14"/><text x="60" y="69">h1</text>
  <circle class="host" cx="60" cy="180" r="14"/><text x="60" y="184">h2</text>
  <circle class="host" cx="130" cy="65" r="14"/><text x="130" y="69">h3</text>
  <circle class="host" cx="130" cy="180" r="14"/><text x="130" y="184">h4</text>
  <circle class="host" cx="200" cy="65" r="14"/><text x="200" y="69">h5</text>
  <circle class="host" cx="200" cy="180" r="14"/><text x="200" y="184">h6</text>
  <text class="t" x="135" y="142">backbone s1–s2–s3</text>
 </svg>
 <h2 style="margin-top:12px">Ping matrix</h2>
 <div id="matrix" style="overflow:auto"></div>
 <div class="row"><button id="b_matrix">Detailed ping matrix (h1..h6)</button></div>
</section>
<section>
 <h2>Tests &amp; the rubric demos — one click each</h2>
 <div class="grid">
  <button id="b_pingall">pingall (2 rounds)</button>
  <button id="b_ping">ping h1 → h5</button>
  <button id="b_iperf">iperf h1 → h6</button>
  <button id="b_flows">dump flow tables</button>
 </div>
 <h2 style="margin-top:14px">Dynamic connect / disconnect (the SDN punchline)</h2>
 <div class="grid4" id="linkrow">
  <button onclick="doLink('s2','s3','down')">cut s2–s3</button>
  <button onclick="doLink('s2','s3','up')">restore s2–s3</button>
  <button onclick="doCtl('s2','detach')">detach s2</button>
  <button onclick="doCtl('s2','attach')">attach s2</button>
 </div>
 <div class="grid4" style="margin-top:8px">
  <button onclick="doLink('s1','s2','down')">cut s1–s2</button>
  <button onclick="doLink('s1','s2','up')">restore s1–s2</button>
  <button onclick="doCtl('s2','detach');setTimeout(()=>{post('/api/ping',
   {src:'h4',dst:'h1'});},900)">detach + test h4→h1</button>
  <button onclick="doCtl('s2','attach');setTimeout(()=>{post('/api/ping',
   {src:'h4',dst:'h1'});},2700)">attach + test h4→h1</button>
 </div>
 <div class="row">
  <input id="cmd" placeholder="console: pingall | ping h1 h5 | link s2 s3 down |
   detach s2 | flows | log | help" spellcheck="false">
  <button onclick="runCmd()">run</button>
 </div>
 <div id="out">[panel output appears here]</div>
 <div id="busy"></div>
</section>
</main>
<section style="max-width:1400px;margin:0 auto 20px">
 <h2>History (also saved to outputs/history.jsonl)</h2>
 <table><thead><tr><th style="width:70px">time</th><th>operation</th>
 <th style="width:60px">result</th><th>detail</th></tr></thead>
 <tbody id="hist"></tbody></table>
</section>
<div class="foot">Panel owns Ryu + Mininet in this WSL session.
Deployed for the SDN course project — see README.md for the CLI route.</div>
<script>
const $=id=>document.getElementById(id);
const out=$('out');let busy=0;
function print(t){out.textContent=t??'';out.scrollTop=out.scrollHeight;}
function note(t){$('busy').textContent=t;}
function lock(b,name){busy+=b?1:-1;if(busy<0)busy=0;note(busy?name+' …':'');
 document.querySelectorAll('main button, header button').forEach
 (x=>x.disabled=busy>0);}
async function post(url,data){lock(true,url);
 try{const r=await fetch(url,{method:'POST',body:new URLSearchParams(data)});
  const j=await r.json();print(j.text||(['ok','log'].map(k=>j[k]).join('\n')));
  if(j.log)print(j.log.join('\n'));if(j.rows)matrixView(j);
  hist();return j;}
 catch(e){print('request failed: '+e);return {ok:false}}
 finally{lock(false,'')}}
$('b_start').onclick=()=>post('/api/start');
$('b_stop').onclick=()=>post('/api/stop',{full:0});
$('b_full').onclick=()=>confirm('Also kill the Ryu controller?')&&post('/api/shutdown');
$('b_pingall').onclick=()=>post('/api/pingall');
$('b_ping').onclick=()=>post('/api/ping',{src:'h1',dst:'h5'});
$('b_iperf').onclick=()=>post('/api/iperf');
$('b_flows').onclick=()=>post('/api/flows');
$('b_matrix').onclick=()=>post('/api/matrix');
function matrixView(j){let h='<table class="m"><tr><th>from＼to</th>'+
 ['h1','h2','h3','h4','h5','h6'].map(x=>'<th>'+x+'</th>').join('')+'</tr>';
 j.rows.forEach(r=>{h+='<tr><td><b>'+r.src+'</b></td>'+
  r.cells.map((c,i)=>'<td class="'+(c=='OK'?'ok':c=='FAIL'?'fail':'')+'">'+c+
  '</td>').join('')+'</tr>'});
 h+='</table>';$('matrix').innerHTML=h;}
function doLink(l,r,s){post('/api/link',{left:l,right:r,state:s});}
function doCtl(s,a){post('/api/controller',{switch:s,action:a});}
function runCmd(){const v=$('cmd').value;$('cmd').value='';post('/api/cmd',{cmd:v});}
$('cmd').addEventListener('keydown',e=>{if(e.key==='Enter')runCmd()});
async function hist(){try{const r=await fetch('/api/history');const j=await r.json();
 $('hist').innerHTML=j.rows.map(x=>'<tr><td>'+x.time+'</td><td>'+x.op+
 '</td><td>'+(x.ok?'ok':'FAIL')+'</td><td>'+(x.detail||'').slice(0,300)+
 '</td></tr>').join('');}catch(e){}}
async function poll(){try{const r=await fetch('/api/status');const s=await r.json();
 $('d_ovs').className='dot'+(s.ovs?' on':'');
 $('d_ryu').className='dot'+(s.ryu?' on':'');
 $('d_mn').className='dot'+(s.mininet?' on':'');
 $('chip_state').textContent=s.state+(s.mininet?(' · '+s.connected.length+
  '/3 switches connected'):'')
 for(const b of document.querySelectorAll('main button'))
  b.disabled=(busy>0)||s.state==='starting';
 if(s.state==='starting')note('starting the lab (up to ~45 s)…');
 else if(!busy)note('');
 }catch(e){}}
setInterval(poll,2000);poll();hist();
setInterval(hist,15000);
</script></body></html>"""


if __name__ == '__main__':
    HOST, PORT = '127.0.0.1', 5000
    print('* SDN lab control panel  ->  http://localhost:%d  (Ctrl-C stops it)'
          % PORT, flush=True)
    app.run(host=HOST, port=PORT, threaded=True)
