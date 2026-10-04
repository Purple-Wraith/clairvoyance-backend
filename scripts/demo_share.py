#!/usr/bin/env python3
"""Time-limited, VIEW-ONLY demo of the Clairvoyance app, to show someone without giving them the real thing.

  python3 scripts/demo_share.py --minutes 15            # serves on 127.0.0.1:8899, prints the private link, exits when time is up

What the viewer gets: a SNAPSHOT of docs/ (copied to a temp dir at start) opened with ?nosb=1, so the page never talks to Supabase or GitHub and nothing they do can
reach the real ledger. On top of that a read-only layer blocks locking, settling, removing, syncing and token fields (a banner says "VIEW-ONLY DEMO"), and a private
random key is required (cookie set by the first link), so a guessed address shows nothing. After --minutes the server answers 410 "expired" and then exits.

This script only serves on localhost. Making it reachable from the internet is a separate, explicit step (a tunnel pointed at --port), see the notes printed at start.
"""
from __future__ import annotations
import argparse, http.server, json, secrets, shutil, socketserver, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCKED = ["lockPick", "lockCFBGame", "lockNFLGame", "recR", "recRFromLog", "removePick", "removePicksByDate", "removePicksByMonth", "doUpdate",
           "saveGHToken", "dispatchWorkflow", "relayCheck", "actionsCheck", "_doSupabaseSync", "syncBetsToSupabase", "_syncBetsToSupabaseNow"]

READONLY_JS = """<script>
/* VIEW-ONLY DEMO layer (scripts/demo_share.py) */
(function(){
  window.__CV_DEMO=true;
  function toast(m){try{var t=document.getElementById('cv-demo-toast');if(!t){t=document.createElement('div');t.id='cv-demo-toast';t.style.cssText='position:fixed;left:50%;bottom:26px;transform:translateX(-50%);z-index:100000;background:#12001f;border:1px solid #f000ff;color:#ffd6ff;font:14px/1.3 monospace;letter-spacing:1px;padding:10px 16px;border-radius:4px;box-shadow:0 0 18px rgba(240,0,255,.5);transition:opacity .3s';document.body.appendChild(t);}t.textContent=m;t.style.opacity=1;clearTimeout(t._h);t._h=setTimeout(function(){t.style.opacity=0;},2200);}catch(e){}}
  var names=__BLOCKED__;
  function arm(){names.forEach(function(n){window[n]=function(){toast('VIEW-ONLY DEMO — this action is switched off');return Promise.resolve(null);};});window.saveP=function(){};}
  window.addEventListener('load',function(){arm();setTimeout(arm,1500);setInterval(arm,5000);});
  // a read-only snapshot of the ledger, so the pages are populated (nothing is ever written back anywhere)
  try{if(!localStorage.getItem('preds')){var x=new XMLHttpRequest();x.open('GET','picks_backup.json',false);x.send();if(x.status===200)localStorage.setItem('preds',x.responseText);}}catch(e){}
  document.addEventListener('DOMContentLoaded',function(){
    var b=document.createElement('div');b.id='cv-demo-banner';
    b.style.cssText='position:fixed;top:0;left:0;right:0;z-index:99999;text-align:center;background:linear-gradient(90deg,#f000ff,#4d79ff);color:#0b0612;font:700 clamp(9px,2.6vw,12px)/1.1 monospace;letter-spacing:1px;padding:4px 6px;white-space:nowrap;overflow:hidden;pointer-events:none';
    b.textContent='LIVE DEMO · TAP TABS TO EXPLORE · ENDS __UNTIL__';document.body.appendChild(b);document.body.style.paddingTop='20px';
    setTimeout(function(){toast('This is the live app — tap any tab at the top to explore. Editing is switched off.');},3500);
  });
})();
</script>"""

EXPIRED = b"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Expired</title><body style='background:#0b0612;color:#ffd6ff;font:16px monospace;display:grid;place-items:center;height:100vh;margin:0'><div style='text-align:center;letter-spacing:2px'>THIS DEMO LINK HAS EXPIRED</div>"


def make_handler(site: Path, key: str, deadline: float, until_label: str):
    inject = READONLY_JS.replace("__BLOCKED__", json.dumps(BLOCKED)).replace("__UNTIL__", until_label).encode()

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(site), **k)

        def log_message(self, *a, **k):
            pass

        def _deny(self, code, body=b"", ctype="text/html; charset=utf-8"):
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)

        def _authorised(self) -> bool:
            for part in (self.headers.get("Cookie") or "").split(";"):
                if part.strip() == f"cvdemo={key}":
                    return True
            return False

        def do_POST(self):  # nothing to accept
            self._deny(405, b"read-only", "text/plain")

        do_PUT = do_DELETE = do_PATCH = do_POST

        def do_GET(self):
            if time.time() >= deadline:
                return self._deny(410, EXPIRED)
            path, _, query = self.path.partition("?")
            # Key in the PATH (/d/<key>/...) works in every browser, including in-app browsers that refuse cookies; every asset the page loads is relative, so it keeps the prefix.
            pref = f"/d/{key}"
            via_path = path == pref or path.startswith(pref + "/")
            if via_path:
                path = path[len(pref):] or "/"
                self.path = path + ("?" + query if query else "")
                if path == "/":
                    self.send_response(302); self.send_header("Location", f"{pref}/app.html?nosb=1"); self.send_header("Cache-Control", "no-store"); self.end_headers(); return
            elif f"k={key}" in query.split("&"):   # legacy link: set the cookie, then land on the app
                self.send_response(302); self.send_header("Set-Cookie", f"cvdemo={key}; Path=/; HttpOnly; SameSite=Lax; Max-Age={int(deadline - time.time())}")
                self.send_header("Location", f"{pref}/app.html?nosb=1"); self.send_header("Cache-Control", "no-store"); self.end_headers(); return
            if not (via_path or self._authorised()):
                return self._deny(403, b"<!doctype html><meta charset=utf-8><body style='background:#0b0612;color:#ffd6ff;font:16px monospace;display:grid;place-items:center;height:100vh;margin:0'>PRIVATE DEMO - USE THE LINK YOU WERE SENT")
            base = pref if via_path else ""
            if path in ("/", "/index.html", "/app.html") and "nosb=1" not in query:
                self.send_response(302); self.send_header("Location", f"{base}/app.html?nosb=1"); self.send_header("Cache-Control", "no-store"); self.end_headers(); return
            if path in ("/app.html", "/index.html"):
                html = (site / "app.html").read_bytes()
                i = html.find(b"</head>")
                html = html[:i] + inject + html[i:] if i >= 0 else inject + html
                return self._deny(200, html)
            return super().do_GET()

    return H


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=15)
    ap.add_argument("--port", type=int, default=8899)
    a = ap.parse_args()
    site = Path(tempfile.mkdtemp(prefix="cv-demo-"))
    shutil.copytree(ROOT / "docs", site, dirs_exist_ok=True, ignore=shutil.ignore_patterns("manual_locks.json", "relay_ping.json", ".git*"))
    key = secrets.token_urlsafe(18)
    deadline = time.time() + a.minutes * 60
    until = time.strftime("%-I:%M %p", time.localtime(deadline)) + " MT"
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", a.port), make_handler(site, key, deadline, until))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"READY http://127.0.0.1:{a.port}/d/{key}/\nEXPIRES {until} ({a.minutes:g} min). Local only until a tunnel is pointed at port {a.port}.", flush=True)
    while time.time() < deadline + 5:
        time.sleep(1)
    srv.shutdown(); shutil.rmtree(site, ignore_errors=True)
    print("EXPIRED - server stopped, snapshot deleted", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
