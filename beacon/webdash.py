"""Phone dashboard: a small web page Beacon serves on your Wi-Fi.

Open http://<this PC's address>:<port> on a phone on the same network to see
the live status and switch relay buttons. Optional PIN. No internet involved."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def local_addresses():
    """This PC's LAN IPv4 address: the one the default route uses, so virtual
    adapters (e.g. VirtualBox's 192.168.56.1) don't show. Else any others."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))                # no packet is sent
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith(("127.", "0.")):
            return [ip]
    except OSError:
        pass
    out = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out and not ip.startswith(("127.", "169.254.")):
                out.append(ip)
    except OSError:
        pass
    return out or ["127.0.0.1"]


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0E1014"><title>__NAME__</title>
<style>
:root{--bg:#0E1014;--card:#171A21;--line:#232733;--text:#F1F5F9;--muted:#8B93A3;--accent:#3B82F6}
@media (prefers-color-scheme:light){:root{--bg:#F4F5F7;--card:#fff;--line:#E5E7EB;--text:#0F172A;--muted:#6B7280;--accent:#2563EB}}
*{box-sizing:border-box}body{margin:0;font:16px -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text);padding:18px 16px 40px}
h1{font-size:17px;margin:0 0 14px;font-weight:700;display:flex;justify-content:space-between;align-items:center}
h1 small{color:var(--muted);font-weight:500;font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:22px;text-align:center;margin-bottom:14px}
.orb{width:120px;height:120px;border-radius:50%;margin:6px auto 14px;transition:background .3s,box-shadow .3s}
.title{font-size:30px;font-weight:800}.sub{color:var(--muted);margin-top:4px}
.bar{height:8px;border-radius:4px;background:var(--line);margin:16px 0 4px;overflow:hidden;display:none}
.bar i{display:block;height:100%;background:var(--accent);width:0;transition:width .5s}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.btn{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:14px;padding:14px 8px;font:600 15px inherit;width:100%}
.btn.on{background:#22C55E;border-color:#22C55E;color:#fff}
.btn small{display:block;font-weight:500;font-size:12px;opacity:.85;margin-top:2px}
.stats{display:flex;gap:10px;margin-bottom:14px}.stats div{flex:1;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px}
.stats b{display:block;font-size:18px}.stats span{color:var(--muted);font-size:12px}
.err{color:#EF4444;text-align:center;margin:10px 0;display:none}
form{max-width:260px;margin:30vh auto;text-align:center}input{font-size:22px;padding:10px;width:100%;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--text);text-align:center}
</style></head><body>
__BODY__
</body></html>"""

MAIN = r"""<h1>__NAME__ <small id="upd"></small></h1>
<div class="card"><div class="orb" id="orb"></div><div class="title" id="title">…</div>
<div class="sub" id="sub"></div><div class="bar" id="bar"><i id="fill"></i></div></div>
<div class="stats"><div><b id="today">–</b><span>Laser on today</span></div><div><b id="jobs">–</b><span>Jobs today</span></div></div>
<div class="err" id="err">Can't reach Beacon. Is the PC on?</div>
<div class="grid" id="relays"></div>
<script>
const pin=new URLSearchParams(location.search).get('pin')||'';
const q=p=>p+(p.includes('?')?'&':'?')+'pin='+encodeURIComponent(pin);
async function load(){try{const r=await fetch(q('/api/status'));if(r.status==401){location.reload();return}
const s=await r.json();document.getElementById('err').style.display='none';
const o=document.getElementById('orb');o.style.background=s.color;o.style.boxShadow='0 0 60px '+s.color+'99';
title.textContent=s.title;sub.textContent=s.subtitle;today.textContent=s.today;jobs.textContent=s.jobs_today;
const b=document.getElementById('bar');if(s.progress!=null){b.style.display='block';fill.style.width=s.progress+'%'}else b.style.display='none';
upd.textContent=new Date().toLocaleTimeString();
const g=document.getElementById('relays');g.innerHTML='';s.relays.forEach(x=>{const e=document.createElement('button');
e.className='btn'+(x.on?' on':'');e.innerHTML=x.name+'<small>'+(x.on?'On':'Off')+(x.auto?' · auto':'')+'</small>';
e.onclick=async()=>{await fetch(q('/api/relay'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:x.index})});load()};g.appendChild(e)})}
catch(e){document.getElementById('err').style.display='block'}}
load();setInterval(load,2000);
</script>"""

LOGIN = r"""<form method="get"><h1 style="justify-content:center">__NAME__</h1>
<input name="pin" type="password" inputmode="numeric" placeholder="PIN" autofocus>
<p style="color:var(--muted)">Enter the PIN set in Beacon's settings</p></form>"""


class Dashboard:
    """get_state() -> dict for /api/status (called from the server thread, so
    it must only read a snapshot). command(name, data) is queued for the UI."""

    def __init__(self, name, get_state, command):
        self.name, self.get_state, self.command = name, get_state, command
        self.server = None
        self.port = None
        self.pin = ""
        self.error = ""

    def running(self):
        return self.server is not None

    def start(self, port, pin=""):
        self.stop()
        self.pin = str(pin or "")
        dash = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _ok_pin(self):
                if not dash.pin:
                    return True
                return parse_qs(urlparse(self.path).query).get("pin", [""])[0] == dash.pin

            def _send(self, code, body, ctype="application/json"):
                data = body.encode("utf-8") if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    body = MAIN if self._ok_pin() else LOGIN
                    self._send(200, PAGE.replace("__BODY__", body).replace("__NAME__", dash.name),
                               "text/html")
                elif path == "/api/status":
                    if not self._ok_pin():
                        return self._send(401, '{"error":"pin"}')
                    self._send(200, json.dumps(dash.get_state()))
                else:
                    self._send(404, '{"error":"not found"}')

            def do_POST(self):
                if not self._ok_pin():
                    return self._send(401, '{"error":"pin"}')
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    data = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    data = {}
                path = urlparse(self.path).path
                if path == "/api/relay" and isinstance(data.get("index"), int):
                    dash.command("relay", data)
                    return self._send(200, '{"ok":true}')
                self._send(404, '{"error":"not found"}')

        try:
            self.server = ThreadingHTTPServer(("0.0.0.0", int(port)), Handler)
        except OSError as e:
            self.server, self.error = None, f"Port {port} is in use or blocked ({e.strerror})."
            return False
        self.server.daemon_threads = True
        self.port, self.error = int(port), ""
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return True

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def urls(self):
        if not self.server:
            return []
        suffix = f"/?pin={self.pin}" if self.pin else ""
        return [f"http://{ip}:{self.port}{suffix}" for ip in local_addresses()]
