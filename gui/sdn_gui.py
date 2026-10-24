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


def _labop(fn):
    """Decorate a lab operation: take the lab lock and refuse to run when
    the lab is not actually up (before: the guard tuple was always truthy,
    so ops ran against a broken lab and hung)."""
    def wrap(self, *args, **kw):
        with self.lock:
            guard = self._require()
            if guard:
                return guard
            return fn(self, *args, **kw)
    wrap.__name__ = fn.__name__
    wrap.__doc__ = fn.__doc__
    return wrap


class Lab:
    """Everything the panel drives, guarded by one lock."""

    def __init__(self):
        self.lock = threading.RLock()
        self.net = None
        self._links = {}             # backbone state: 's1-s2' -> True|False
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
    def _ryu_logtail(self, n=4):
        try:
            return ' | '.join(
                (ROOT / 'outputs' / 'ryu.log').read_text(errors='replace')
                .splitlines()[-n:]).strip() or '(log empty)'
        except Exception:
            return '(no ryu.log)'

    def _clear_port(self):
        """Kill whatever process holds the OpenFlow port (stale controller)."""
        out = self._run("ss -ltnp | grep ':%d '" % OF_PORT).stdout
        for pid in sorted(set(re.findall(r'pid=(\d+)', out))):
            self._run('kill -9 %s' % pid)

    def _spawn_ryu(self, steps):
        sub = subprocess.Popen(
            ['setsid', 'env', 'PYTHONPATH=' + RYU_ENV['PYTHONPATH'],
             RYU_PY, *RYU_RUN],
            stdout=open(ROOT / 'outputs' / 'ryu.log', 'w'),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
        steps.append('       launched fresh controller (pid %d)' % sub.pid)
        if not self._wait_ryu_up(steps):
            raise RuntimeError('controller never listened on %d after 30 s -- '
                               'outputs/ryu.log tail: %s'
                               % (OF_PORT, self._ryu_logtail()))

    def _wait_ryu_up(self, steps, max_secs=30):
        """Poll for the OpenFlow port; bail early if the process vanished."""
        for i in range(max_secs * 2):
            if self._ryu_listening():
                steps.append('       OpenFlow port %d listening' % OF_PORT)
                return True
            if not self._ryu_running() and i >= 4:
                steps.append('       controller process DIED -- ryu.log tail: '
                             '%s' % self._ryu_logtail())
                return False
            time.sleep(0.5)
        steps.append('       not listening after %d s -- ryu.log tail: %s'
                     % (max_secs, self._ryu_logtail()))
        return False

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
                if self._ryu_listening() and self._ryu_running():
                    steps.append('       already listening on %d' % OF_PORT)
                elif self._ryu_listening():
                    # port held by some other process -- take it back
                    steps.append('       port %d is held by a foreign process '
                                 '-- clearing it first' % OF_PORT)
                    self._clear_port()
                    time.sleep(0.5)
                    self._spawn_ryu(steps)
                elif self._ryu_running():
                    # maybe it is genuinely still booting; give it a moment,
                    # then self-heal a wedged/stale controller
                    if not self._wait_ryu_up(steps, max_secs=6):
                        steps.append('       stale controller -- killing it '
                                     'and starting fresh')
                        self._run('pkill -9 -f l2_learn_switch')
                        self._clear_port()
                        time.sleep(0.5)
                        self._spawn_ryu(steps)
                else:
                    self._spawn_ryu(steps)
                steps.append('[4/4] Mininet topology (3 switches, 6 hosts)')
                self.net = build_net()
                self.net.start()
                if self.net.waitConnected(45):
                    steps.append('       all switches connected to Ryu')
                else:
                    steps.append('       !! switches did not all connect '
                                 'within 45 s (see ryu.log)')
                self.started_at = time.time()
                self._links = {'s1-s2': True, 's2-s3': True}
                ready = self._connected_list()
                for sw in sorted(ready):
                    steps.append('* switch %s ready (%s)' % (sw, CONTROL_ADDR))
                self.state = ('running' if len(ready) == len(SWITCHES)
                              else 'error')
            except Exception as exc:                    # keep the server alive
                self.state = 'error'
                if self.net:                             # clean the half-built lab
                    try:
                        self.net.stop()
                    except Exception:
                        pass
                    self.net = None
                self._run('mn -c')
                steps.append('!! start failed: %r' % exc)
            return {'ok': self.state == 'running', 'log': steps}

    def stop(self, full=False):
        with self.lock:
            steps = []
            try:
                if self.net:
                    self.net.stop()
                    self.net = None
                self._links = {}
                steps.append('mininet topology down')
                self._run('mn -c')
                for br in SWITCHES:
                    self._run('ovs-vsctl --if-exists del-br %s' % br)
                steps.append('ovs state purged')
                if full:
                    self._run('pkill -9 -f l2_learn_switch')
                    steps.append('ryu controller stopped')
                else:
                    steps.append('ryu left running (click Full stop to kill it)')
            except Exception as exc:
                steps.append('!! stop issue: %s' % exc)
            self.state = 'idle'
            self.started_at = None
            return {'ok': True, 'log': steps}

    # -- demos ----------------------------------------------------------------
    @_labop
    def ping_all(self):
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

    @_labop
    def ping_pair(self, src, dst, count=2, wait=1):
        s = self.net.get(src)
        d = self.net.get(dst)
        out = s.cmd('ping -c %d -W %d %s' % (count, wait, d.IP()))
        m = re.search(r'(\d+) packets transmitted\S+\s*(\d+) received', out)
        loss = 100
        if m:
            tx, rx = int(m.group(1)), int(m.group(2))
            loss = 100 - int(100.0 * rx / max(tx, 1))
        mavg = re.search(r'= [\d.]+/([\d.]+)/', out)
        ms = round(float(mavg.group(1)), 2) if mavg else None
        mstxt = (' %.2f ms avg' % ms) if ms is not None else ''
        txt = ('%s -> %s (%s): %d%% loss%s\n' %
               (src, dst, d.IP(), loss, mstxt)) + out
        self._history('ping %s->%s' % (src, dst), loss == 0,
                      '%d%% loss%s' % (loss, mstxt))
        return {'ok': True, 'text': txt, 'loss': loss, 'ms': ms}

    @_labop
    def matrix(self):
        """Sequential per-pair ping matrix (fast warm, slower first time)."""
        rows = []
        all_ok = True
        for src in HOSTS:
            s = self.net.get(src)
            cells, statuses = [], []
            for dst in HOSTS:
                if dst == src:
                    cells.append({'txt': '--', 'ms': None})
                    statuses.append(None)
                    continue
                d = self.net.get(dst)
                out = s.cmd('ping -c 1 -W 1 %s' % d.IP())
                okk = bool(re.search(r'1 received', out))
                ms = None
                if okk:
                    t = re.search(r'= [\d.]+/([\d.]+)/', out)
                    if t:
                        ms = round(float(t.group(1)), 2)
                all_ok &= okk
                cells.append({'txt': 'OK' if okk else 'FAIL', 'ms': ms})
                statuses.append(okk)
            rows.append({'src': src, 'cells': cells,
                         'statuses': statuses})
        txt = ('%s: all %d pairs replied through switched paths'
               % ('PASS' if all_ok else 'FAIL', 30))
        self._history('ping matrix', all_ok, txt)
        return {'ok': all_ok, 'rows': rows, 'text': txt}

    @_labop
    def iperf(self, seconds=5):
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

    @_labop
    def set_link(self, left, right, state):
        if state not in ('up', 'down'):
            return {'ok': False, 'text': 'state must be up|down'}
        self.net.configLinkStatus(left, right, state)
        up = state == 'up'
        self._links['-'.join(sorted((left, right)))] = up
        msg = 'link %s <-> %s is %s' % (left, right,
                                        'up' if up else 'DOWN')
        self._history('link %s %s' % (left, right), True, msg)
        return {'ok': True, 'text': msg, 'links': self._links}

    @_labop
    def controller_op(self, switch, action):
        sw = self.net.get(switch)
        if action == 'detach':
            sw.cmd('ovs-ofctl -O OpenFlow13 del-flows %s' % switch)
            sw.cmdPrint('ovs-vsctl set-controller %s %s' % (switch, DEAD_ADDR))
            txt = ('%s detached from the controller (dead port %s) and its '
                   'flow table was wiped.\nAny new traffic forwarding through '
                   'it now requires a controller: expect 100%% loss '
                   '(failMode=secure).' % (switch, DEAD_ADDR))
            self._history('%s detach' % switch, True, txt)
            return {'ok': True, 'text': txt}
        elif action == 'attach':
            sw.cmdPrint('ovs-vsctl set-controller %s %s' %
                        (switch, CONTROL_ADDR))
            for _ in range(12):                    # wait for the OFP channel
                if sw.connected():
                    break
                time.sleep(0.5)
            state_now = 're-connected to Ryu' if sw.connected() \
                else 'still not connected (check ryu.log)'
            txt = ('%s re-attached to %s: %s\nNext unknown traffic re-learns '
                   'through the controller.' % (switch, CONTROL_ADDR,
                                                state_now))
            self._history('%s attach' % switch, True, txt)
            return {'ok': True, 'text': txt}
        return {'ok': False, 'text': 'action must be attach|detach'}

    @_labop
    def flows(self):
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
        if head == 'link' and len(parts) == 4 and \
                parts[3] in ('up', 'down') and \
                parts[1] in SWITCHES and parts[2] in SWITCHES and \
                parts[1] != parts[2]:
            return self.set_link(parts[1], parts[2], parts[3])
        if head in ('detach', 'attach') and len(parts) == 2 and \
                parts[1] in SWITCHES:
            return self.controller_op(parts[1], head)
        if head == 'log':
            r = self.ryu_log(200)
            return {'ok': True, 'text': '\n'.join(r['lines'][-200:])}
        if head == 'hosts':
            return {'ok': True, 'text': ' '.join(
                h.name for h in self.net.hosts)} if self.net else \
                {'ok': False, 'text': '(lab not started)'}
        if head == 'switches':
            return {'ok': True, 'text': ' '.join(
                s.name for s in self.net.switches)} if self.net else \
                {'ok': False, 'text': '(lab not started)'}
        if head == 'links':
            return {'ok': True, 'text': ' | '.join(
                '%s: %s' % (k, 'up' if v else 'DOWN')
                for k, v in sorted(self._links.items())) or '(lab not started)'}
        return {'ok': False, 'text': 'unknown command (try: help)'}

    def _require(self):
        """Return None when the lab may run operations, else the error dict
        the op should return (the old truthy-tuple guard never blocked)."""
        if self.state != 'running' or not self.net:
            return {'ok': False,
                    'text': 'lab is not running -- click Start lab first '
                            '(state: %s)' % self.state}
        return None


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
    # the chip means "controller ACCEPTING OpenFlow connections", not
    # merely "a process with that name exists" (a dying/stale ryu is red)
    ryu = LAB._ryu_listening()
    return jsonify({'state': LAB.state, 'detail': LAB.detail,
                    'ovs': LAB._ovs_ok(), 'ryu': ryu,
                    'mininet': LAB.net is not None and LAB.state == 'running',
                    'connected': LAB._connected_list(),
                    'links': (LAB._links if LAB.net and
                              LAB.state == 'running' else {}),
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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SDN Lab — Control Deck</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect x='2' y='5' width='12' height='6' rx='2' fill='%2358a6ff'/%3E%3C/svg%3E">
<style>
:root{
 --bg:#05070c; --core:#0d1017; --core2:#131a23; --card:#8ea0b51a;
 --line:rgba(255,255,255,.08); --line2:rgba(255,255,255,.14);
 --fg:#dbe3ee; --dim:#8a93a2; --dim2:#5c6572;
 --ok:#3fb950; --bad:#f85149; --warn:#d29922; --acc:#58a6ff;
 --mono:ui-monospace,"Cascadia Code","JetBrains Mono",Consolas,monospace;
 --ez:cubic-bezier(.32,.72,0,1);
}
*{box-sizing:border-box}
html{scrollbar-color:#2c333d transparent}
body{margin:0;background:var(--bg);color:var(--fg);min-height:100dvh;
 font:14px/1.55 -apple-system,"Segoe UI",system-ui,sans-serif;
 -webkit-font-smoothing:antialiased}
.orb{position:fixed;width:560px;height:560px;border-radius:50%;
 pointer-events:none;z-index:0;filter:blur(110px)}
.orb-a{background:radial-gradient(circle,#0d2f5c 0,transparent 62%);
 top:-190px;left:-130px}
.orb-b{background:radial-gradient(circle,#0a3d31 0,transparent 62%);
 bottom:-200px;right:-120px;opacity:.85}
main,header,section.wide,.foot{position:relative;z-index:1}

/* ---------- floating glass header ---------- */
header{position:sticky;top:10px;z-index:10;margin:12px auto 0;
 width:min(1400px,calc(100% - 28px));display:flex;gap:16px;align-items:center;
 padding:12px 18px;border-radius:20px;flex-wrap:wrap;
 background:rgba(8,12,19,.72);backdrop-filter:blur(14px);
 border:1px solid var(--line);box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
.brand{display:flex;align-items:center;gap:12px;margin-right:6px}
.mark{width:38px;height:38px;border-radius:12px;flex:none;
 background:linear-gradient(135deg,#1f6feb,#0a2038 65%);
 border:1px solid rgba(255,255,255,.12);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.15);display:grid;place-items:center}
.mark svg{display:block}
h1{font-size:16px;font-weight:700;letter-spacing:-.01em;margin:0}
.sub{color:var(--dim);font-size:11.5px;margin-top:2px}
.pills{display:flex;gap:8px;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:7px;padding:6px 12px;
 border-radius:999px;font-size:12px;color:var(--dim);
 background:rgba(255,255,255,.03);border:1px solid var(--line);
 transition:border-color .3s var(--ez),color .3s var(--ez)}
.dot{width:8px;height:8px;border-radius:50%;background:#5c2a28;
 transition:background .35s var(--ez),box-shadow .35s var(--ez)}
.dot.on{background:var(--ok);box-shadow:0 0 10px rgba(63,185,80,.55)}
body[data-state=starting] .dot{animation:pulse 1.05s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.35}50%{opacity:1}}
.pill.state{color:var(--acc);font-family:var(--mono);font-size:11.5px}
.pill.state[data-s=idle]{color:var(--dim)}
.pill.state[data-s=starting]{color:var(--warn);border-color:rgba(210,153,34,.35)}
.pill.state[data-s=error]{color:var(--bad);border-color:rgba(248,81,73,.35)}
.actions{margin-left:auto;display:flex;gap:8px}

/* ---------- buttons ---------- */
button{appearance:none;cursor:pointer;color:var(--fg);font:600 13px/1 inherit;
 padding:10px 16px;border-radius:999px;user-select:none;
 background:linear-gradient(180deg,rgba(255,255,255,.07),rgba(255,255,255,.02));
 border:1px solid var(--line2);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.07);
 transition:transform .26s var(--ez),border-color .26s var(--ez),
            background .26s var(--ez),opacity .3s}
button:hover:enabled{border-color:rgba(88,166,255,.55);
 background:linear-gradient(180deg,rgba(88,166,255,.12),rgba(255,255,255,.02))}
button:active:enabled{transform:scale(.96)}
button:disabled{opacity:.35;cursor:default}
button.primary{background:linear-gradient(180deg,#3e8bff,#1f6feb);
 border-color:rgba(255,255,255,.25);color:#fff;
 box-shadow:inset 0 1px 0 rgba(255,255,255,.3),0 6px 22px rgba(31,111,235,.35)}
button.primary:hover:enabled{background:linear-gradient(180deg,#5599ff,#2a7bff)}
button.danger{background:linear-gradient(180deg,rgba(248,81,73,.28),rgba(248,81,73,.08));
 border-color:rgba(248,81,73,.45);color:#ffb2ac}
button.danger:hover:enabled{border-color:rgba(248,81,73,.8)}
button.ghost{color:var(--dim);padding:9px 14px}

/* ---------- layout ---------- */
main{display:grid;grid-template-columns:minmax(380px,440px) 1fr;
 gap:18px;padding:18px;max-width:1400px;margin:0 auto}
@media (max-width:1020px){main{grid-template-columns:1fr}}
.rail{display:flex;flex-direction:column;gap:18px}
section.shell{background:var(--card);border:1px solid var(--line);
 border-radius:22px;padding:3px}
.core{background:linear-gradient(180deg,var(--core2),var(--core));
 border-radius:19px;padding:20px 20px 18px;
 box-shadow:inset 0 1px 0 rgba(255,255,255,.055)}
.eyebrow{display:inline-flex;align-items:center;gap:8px;font-size:10.5px;
 letter-spacing:.18em;text-transform:uppercase;color:var(--dim);
 border:1px solid var(--line);border-radius:999px;padding:5px 12px;
 background:rgba(255,255,255,.02)}
.headrow{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}

/* ---------- topology ---------- */
#topo{display:block;width:100%;height:auto}
#topo .swbox{fill:url(#gsw);stroke:rgba(88,166,255,.75);stroke-width:1.4;
 transition:stroke .5s var(--ez),fill .5s var(--ez)}
#topo .swbox.dark{stroke:#f85149;fill:url(#gswd)}
body[data-state=running] #topo .swbox:not(.dark){
 filter:drop-shadow(0 0 7px rgba(88,166,255,.35))}
#topo .ctrl{fill:url(#grayu);stroke:rgba(210,153,34,.8);stroke-width:1.4}
#topo .edg{stroke:#4c545e;stroke-width:5.5;stroke-linecap:round;
 transition:stroke .5s var(--ez)}
#topo .edg.cut{stroke:#f85149;stroke-dasharray:7 8;animation:cutflow 1.15s linear infinite}
@keyframes cutflow{to{stroke-dashoffset:-15}}
#topo .ctl{stroke:#98a1b0;stroke-width:1.4;stroke-dasharray:3 5;opacity:.5;
 fill:none;transition:opacity .5s var(--ez)}
#topo .ctl.off{opacity:.08}
#topo .hlink{stroke:#39414c;stroke-width:3;stroke-linecap:round}
#topo text{fill:#d7dee8;font:600 12px var(--mono);text-anchor:middle}
#topo .tiny{fill:var(--dim);font:500 9.5px -apple-system,"Segoe UI",sans-serif}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--dim);font-size:11.5px;
 margin-top:10px;padding-top:12px;border-top:1px solid var(--line)}
.legend .swatch{display:inline-block;width:18px;height:3px;border-radius:2px;
 vertical-align:middle;margin-right:7px}
.sw-up{background:#4c545e}
.sw-cut{background:#f85149;box-shadow:0 0 8px rgba(248,81,73,.5)}
.sw-ctl{background:repeating-linear-gradient(90deg,#98a1b0 0 3px,transparent 3px 8px)}

/* ---------- matrix ---------- */
.mwrap{overflow:auto;max-height:260px;border:1px solid var(--line);
 border-radius:14px;background:rgba(5,7,12,.55)}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:12.5px}
th,td{padding:6px 10px;text-align:left;border-bottom:1px solid rgba(255,255,255,.05)}
th{color:var(--dim);font-weight:500;font-size:11.5px;position:sticky;top:0;
 background:rgba(13,16,23,.9)}
tbody.minc td:first-child{font-family:var(--mono);color:var(--fg)}
tbody.minc td.ok{color:var(--ok);font-family:var(--mono)}
tbody.minc td.ok small{display:block;color:var(--dim);font-size:9.5px;line-height:1}
tbody.minc td.fail{color:var(--bad);font-family:var(--mono)}
tbody.minc td.diag{color:var(--dim2)}
.badge{padding:3px 10px;border-radius:999px;font-size:11px;
 font-family:var(--mono)}
.badge.ok{background:rgba(63,185,80,.12);color:var(--ok);
 border:1px solid rgba(63,185,80,.3)}
.badge.bad{background:rgba(248,81,73,.12);color:var(--bad);
 border:1px solid rgba(248,81,73,.3)}

/* ---------- console ---------- */
.intro{color:var(--dim);font-size:12.5px;margin:0 0 12px}
.demos{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.demos button{padding:11px 6px;font-size:12.5px;white-space:nowrap}
@media (max-width:700px){.demos{grid-template-columns:repeat(2,1fr)}}
.row{display:flex;gap:8px;align-items:center;margin-top:12px}
input{flex:1;background:rgba(5,7,12,.7);border:1px solid var(--line);
 color:var(--fg);padding:11px 14px;border-radius:999px;
 font:13px var(--mono);transition:border-color .3s var(--ez)}
input::placeholder{color:var(--dim2)}
input:focus{outline:none;border-color:rgba(88,166,255,.6);
 box-shadow:0 0 0 3px rgba(31,111,235,.2)}
#out{height:400px;overflow:auto;margin-top:12px;white-space:pre-wrap;
 font:12.5px/1.6 var(--mono);color:#c4cddb;
 background:rgba(5,7,12,.6);border:1px solid var(--line);border-radius:16px;
 padding:14px 16px;box-shadow:inset 0 2px 14px rgba(0,0,0,.35)}
#out .err{color:#ff8b83}
#out .good{color:#5dc96b}
#out .dim{color:var(--dim)}
#busy{color:var(--warn);font-size:12px;min-height:18px;margin-top:8px;
 font-family:var(--mono)}

/* ---------- history ---------- */
section.wide{max-width:1400px;margin:4px auto 8px}
.hwrap{overflow:auto;max-height:300px}
tbody.h td:first-child{color:var(--dim);font-family:var(--mono);font-size:11.5px}
tbody.h td:nth-child(2){font-family:var(--mono);font-size:12px}
tbody.h td:nth-child(4){color:var(--dim);font-size:12px}

.foot{text-align:center;color:var(--dim2);font-size:12px;padding:14px 18px 26px}
.foot b{color:var(--dim);font-weight:600}
</style>
</head>
<body data-state="idle">
<div class="orb orb-a"></div><div class="orb orb-b"></div>

<header>
 <div class="brand">
  <div class="mark"><svg width="20" height="20" viewBox="0 0 20 20">
   <rect x="2" y="7" width="16" height="6" rx="3" fill="none" stroke="#9ecbff" stroke-width="1.4"/>
   <circle cx="5.5" cy="10" r="1.2" fill="#9ecbff"/>
   <circle cx="10" cy="10" r="1.2" fill="#9ecbff"/>
   <circle cx="14.5" cy="10" r="1.2" fill="#9ecbff"/>
   <line x1="10" y1="1" x2="10" y2="7" stroke="#d29922" stroke-width="1.2" stroke-dasharray="1.5 2"/>
   <line x1="4" y1="13" x2="4" y2="19" stroke="currentColor" opacity=".35" stroke-width="1.2"/>
   <line x1="16" y1="13" x2="16" y2="19" stroke="currentColor" opacity=".35" stroke-width="1.2"/>
  </svg></div>
  <div>
   <h1>SDN Lab — Control Deck</h1>
   <div class="sub">linear s1–s2–s3 &nbsp;·&nbsp; 6 hosts &nbsp;·&nbsp; Ryu / OpenFlow 1.3 @ 6653</div>
  </div>
 </div>
 <div class="pills">
  <span class="pill"><span class="dot" id="d_ovs"></span>Open vSwitch</span>
  <span class="pill"><span class="dot" id="d_ryu"></span>Ryu :6653</span>
  <span class="pill"><span class="dot" id="d_mn"></span>Mininet</span>
  <span class="pill state" id="chip_state" data-s="idle">idle</span>
 </div>
 <div class="actions">
  <button class="primary" id="b_start">Start lab</button>
  <button id="b_stop">Stop</button>
  <button class="danger" id="b_full">Full stop</button>
 </div>
</header>

<main>
 <div class="rail">
  <section class="shell">
   <div class="core">
    <div class="headrow"><span class="eyebrow">Live topology</span>
     <span style="flex:1"></span><span class="eyebrow" style="border:none;background:none;color:var(--dim2)">red = cut / dark</span></div>
    <svg id="topo" viewBox="0 0 320 214">
     <defs>
      <linearGradient id="gsw" x1="0" y1="0" x2="0" y2="1">
       <stop offset="0" stop-color="#1c2633"/><stop offset="1" stop-color="#0e131b"/>
      </linearGradient>
      <linearGradient id="gswd" x1="0" y1="0" x2="0" y2="1">
       <stop offset="0" stop-color="#33232a"/><stop offset="1" stop-color="#180d10"/>
      </linearGradient>
      <linearGradient id="grayu" x1="0" y1="0" x2="0" y2="1">
       <stop offset="0" stop-color="#242c37"/><stop offset="1" stop-color="#151a22"/>
      </linearGradient>
     </defs>
     <line class="ctl" id="c_s1" x1="180" y1="36" x2="100" y2="112"/>
     <line class="ctl" id="c_s2" x1="180" y1="36" x2="180" y2="112"/>
     <line class="ctl" id="c_s3" x1="180" y1="36" x2="260" y2="112"/>
     <rect class="ctrl" x="140" y="8" width="80" height="28" rx="9"/>
     <text x="180" y="26">Ryu · 6653</text>
     <line class="edg" id="e_s1s2" x1="122" y1="126" x2="158" y2="126"/>
     <line class="edg" id="e_s2s3" x1="202" y1="126" x2="238" y2="126"/>
     <path class="hlink" d="M66 76 Q 88 94 78 112" fill="none"/>
     <path class="hlink" d="M66 184 Q 88 166 78 142" fill="none"/>
     <path class="hlink" d="M146 68 Q 152 96 160 112" fill="none"/>
     <path class="hlink" d="M146 186 Q 152 160 160 140" fill="none"/>
     <path class="hlink" d="M228 66 Q 234 94 242 112" fill="none"/>
     <path class="hlink" d="M228 186 Q 234 160 242 140" fill="none"/>
     <rect class="swbox" id="q_s1" x="78" y="112" width="44" height="28" rx="9"/>
     <text x="100" y="130">s1</text>
     <rect class="swbox" id="q_s2" x="158" y="112" width="44" height="28" rx="9"/>
     <text x="180" y="130">s2</text>
     <rect class="swbox" id="q_s3" x="238" y="112" width="44" height="28" rx="9"/>
     <text x="260" y="130">s3</text>
     <circle cx="54" cy="66" r="13" stroke="#3fb950" fill="#0b1116" stroke-width="1.4" opacity=".9"/>
     <text x="54" y="70">h1</text>
     <circle cx="54" cy="194" r="13" stroke="#3fb950" fill="#0b1116" stroke-width="1.4" opacity=".9"/>
     <text x="54" y="198">h2</text>
     <circle cx="146" cy="56" r="13" stroke="#3fb950" fill="#0b1116" stroke-width="1.4" opacity=".9"/>
     <text x="146" y="60">h3</text>
     <circle cx="146" cy="198" r="13" stroke="#3fb950" fill="#0b1116" stroke-width="1.4" opacity=".9"/>
     <text x="146" y="202">h4</text>
     <circle cx="228" cy="56" r="13" stroke="#3fb950" fill="#0b1116" stroke-width="1.4" opacity=".9"/>
     <text x="228" y="60">h5</text>
     <circle cx="228" cy="198" r="13" stroke="#3fb950" fill="#0b1116" stroke-width="1.4" opacity=".9"/>
     <text x="228" y="202">h6</text>
    </svg>
    <div class="legend">
     <span><span class="swatch sw-ctl"></span>OpenFlow control line</span>
     <span><span class="swatch sw-up"></span>link up</span>
     <span><span class="swatch sw-cut"></span>cut / switch dark</span>
    </div>
   </div>
  </section>

  <section class="shell">
   <div class="core">
    <div class="headrow">
     <span class="eyebrow">Ping matrix</span><span style="flex:1"></span>
     <span id="mres"></span>
    </div>
    <div class="mwrap"><table class="m" id="mtable"></table></div>
    <div class="row"><button class="ghost" id="b_matrix" style="width:100%">Run the full 30-pair matrix · shows latency per pair</button></div>
   </div>
  </section>
 </div>

 <div class="rail">
  <section class="shell">
   <div class="core">
    <div class="headrow"><span class="eyebrow">Rubric demos — one click</span></div>
    <p class="intro">Cut a backbone or detach a switch → watch the topology go red,
     ping dies with <b style="color:var(--bad)">100% loss</b> (failMode=secure),
     then restore and see the controller re-program the path back to 0%.</p>
    <div class="demos">
     <button onclick="doLink('s1','s2','down')">cut s1–s2</button>
     <button onclick="doLink('s1','s2','up')">restore s1–s2</button>
     <button onclick="doLink('s2','s3','down')">cut s2–s3</button>
     <button onclick="doLink('s2','s3','up')">restore s2–s3</button>
     <button id="b_dt2">detach s3 · test h6→h3</button>
     <button id="b_at2">attach s3 · re-test</button>
     <button id="b_dt">detach s2 · test h4→h1</button>
     <button id="b_at">attach s2 · re-test</button>
    </div>
   </div>
  </section>

  <section class="shell">
   <div class="core">
    <div class="headrow"><span class="eyebrow">Console</span></div>
    <div class="grid" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px">
     <button id="b_pingall">pingall · 2 rounds</button>
     <button id="b_ping">ping h1 → h5</button>
     <button id="b_iperf">iperf h1 → h6</button>
     <button id="b_flows">dump flow tables</button>
    </div>
    <div class="row" style="margin-top:0">
     <input id="cmd" placeholder="try: pingall · ping h1 h5 · link s2 s3 down · detach s2 · flows · log · help  (↑/↓ history)" spellcheck="false" autocapitalize="off">
     <button onclick="runCmd()">run</button>
    </div>
    <div id="out">[ panel output appears here — start the lab to begin ]</div>
    <div id="busy"></div>
   </div>
  </section>
 </div>
</main>

<section class="shell wide">
 <div class="core">
  <div class="headrow"><span class="eyebrow">History</span>
   <span style="flex:1"></span>
   <span class="eyebrow" style="border:none;background:none;color:var(--dim2)">appended to outputs/history.jsonl</span></div>
  <div class="mwrap"><table><thead><tr>
   <th style="width:70px">time</th><th style="width:200px">operation</th>
   <th style="width:80px">result</th><th>detail</th></tr></thead>
   <tbody class="h" id="hist"></tbody></table></div>
 </div>
</section>

<div class="foot">The <b>panel owns the lab</b> (OVS + Ryu + Mininet) inside WSL — works fully offline.<br>
CLI route &amp; the automated T1–T8 verify suite: see <b>README.md</b> · one-click launch: <b>gui.bat</b></div>

<script>
const $=id=>document.getElementById(id);
const HOSTS=['h1','h2','h3','h4','h5','h6'];
const out=$('out');let busy=0;const cmdHist=[];let hi=0;

function esc(t){return String(t??'').replace(/&/g,'&amp;')
 .replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function fmt(t){return esc(t).split('\n').map(l=>{
 if(/^\s*!!/.test(l))return '<span class="err">'+l+'</span>';
 if(/^\s*\* switch|listening|all 30/.test(l))
  return '<span class="good">'+l+'</span>';
 if(/^\s*\[|stale|clean|Open vSwitch|launched|already/.test(l))
  return '<span class="dim">'+l+'</span>';
 return l;}).join('\n');}
function print(t){out.innerHTML=fmt(t);out.scrollTop=out.scrollHeight;}
function note(t){$('busy').textContent=t;}
function lock(b){busy=Math.max(0,busy+(b?1:-1));note(busy?'working …':'');
 document.querySelectorAll('main button, header button')
  .forEach(x=>x.disabled=busy>0);}
async function post(url,data){lock(true);
 try{const r=await fetch(url,{method:'POST',
   body:new URLSearchParams(data||{})});
  const j=await r.json();
  if(j.log)print(j.log.join('\n'));else print(j.text||String(j['ok']??''));
  if(j.rows)matrixView(j);
  hist();poll();return j;}
 catch(e){print('!! request failed: '+e);return{ok:false}}
 finally{lock(false)}}

$('b_start').onclick=()=>post('/api/start');
$('b_stop').onclick=()=>post('/api/stop',{full:0});
$('b_full').onclick=()=>confirm('Stop the lab AND kill the controller?')&&
 post('/api/stop',{full:1});
$('b_pingall').onclick=()=>post('/api/pingall');
$('b_ping').onclick=()=>post('/api/ping',{src:'h1',dst:'h5'});
$('b_iperf').onclick=()=>post('/api/iperf');
$('b_flows').onclick=()=>post('/api/flows');
$('b_matrix').onclick=()=>post('/api/matrix');
async function doCtl(s,a){return post('/api/controller',{switch:s,action:a});}
function doLink(l,r,s){post('/api/link',{left:l,right:r,state:s});}
$('b_dt').onclick=async()=>{await doCtl('s2','detach');
 await post('/api/ping',{src:'h4',dst:'h1'});};
$('b_at').onclick=async()=>{await doCtl('s2','attach');
 await post('/api/ping',{src:'h4',dst:'h1'});};
$('b_dt2').onclick=async()=>{await doCtl('s3','detach');
 await post('/api/ping',{src:'h6',dst:'h3'});};
$('b_at2').onclick=async()=>{await doCtl('s3','attach');
 await post('/api/ping',{src:'h6',dst:'h3'});};
function runCmd(){const v=$('cmd').value.trim();$('cmd').value='';
 if(v){if(cmdHist[cmdHist.length-1]!==v)cmdHist.push(v);
  hi=cmdHist.length;post('/api/cmd',{cmd:v});}}
$('cmd').addEventListener('keydown',e=>{
 if(e.key==='ArrowUp'&&cmdHist.length){
  hi=Math.max(0,hi-1);$('cmd').value=cmdHist[hi];e.preventDefault();}
 else if(e.key==='ArrowDown'&&cmdHist.length){
  hi=Math.min(cmdHist.length,hi+1);$('cmd').value=cmdHist[hi]||'';e.preventDefault();}});

function matrixView(j){let h='<thead><tr><th>from＼to</th>'+
 HOSTS.map(x=>'<th>'+x+'</th>').join('')+'</tr></thead><tbody class="minc">';
 j.rows.forEach(r=>{h+='<tr><td>'+r.src+'</td>'+r.cells.map(c=>{
  if(c.txt=='--')return '<td class="diag">--</td>';
  return '<td class="'+(c.txt=='OK'?'ok':'fail')+'">'+c.txt+
   (c.ms!=null?'<small>'+c.ms+' ms</small>':'')+'</td>';}).join('')+
  '</tr>';});
 h+='</tbody>';
 $('mtable').innerHTML=h;
 $('mres').innerHTML='<span class="badge '+(j.ok?'ok':'bad')+'">'+
  (j.ok?'all 30 pairs OK':'loss detected ↑')+'</span>';}

async function hist(){try{const r=await fetch('/api/history');
  const j=await r.json();
 $('hist').innerHTML=j.rows.map(x=>'<tr><td>'+x.time+'</td><td>'+
  esc(x.op)+'</td><td><span class="badge '+(x.ok?'ok':'bad')+'">'+
  (x.ok?'ok':'FAIL')+'</span></td><td>'+esc((x.detail||'').slice(0,220))+
  '</td></tr>').join('');}catch(e){}}

function topo(s){
 const L=s.links||{};
 for(const k of ['s1-s2','s2-s3']){const e=$('e_'+k);
  if(e)e.classList.toggle('cut',L[k]===false);}
 const on=s.state==='running';
 for(const sw of ['s1','s2','s3']){
  const dark=on&&(s.connected||[]).indexOf(sw)<0;
  $('q_'+sw).classList.toggle('dark',dark);
  $('c_'+sw).classList.toggle('off',dark);}}

function mmss(u){u=u||0;
 return Math.floor(u/60)+':'+String(u%60).padStart(2,'0');}

async function poll(){try{const r=await fetch('/api/status');
  const s=await r.json();
 document.body.dataset.state=s.state;
 $('d_ovs').className='dot'+(s.ovs?' on':'');
 $('d_ryu').className='dot'+(s.ryu?' on':'');
 $('d_mn').className='dot'+(s.mininet?' on':'');
 const cs=$('chip_state');cs.dataset.s=s.state;
 cs.textContent=s.state==='running'
  ?('running · '+mmss(s.uptime)+' · '+(s.connected||[]).length+'/3 connected')
  :s.state;
 topo(s);
 for(const b of document.querySelectorAll('main button'))
  b.disabled=(busy>0)||s.state==='starting';
 if(s.state==='starting')note('starting the lab (up to ~45 s) …');
 else if(!busy)note('');
}catch(e){}}
setInterval(poll,2000);poll();hist();setInterval(hist,15000);
</script>
</body>
</html>"""


if __name__ == '__main__':
    HOST, PORT = '127.0.0.1', 5000
    print('* SDN lab control panel  ->  http://localhost:%d  (Ctrl-C stops it)'
          % PORT, flush=True)
    app.run(host=HOST, port=PORT, threaded=True)
