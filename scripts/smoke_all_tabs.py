#!/usr/bin/env python3
"""Smoke test: load docs/app.html with a real-size ledger, click EVERY visible navigation / sub-navigation button once and report JavaScript errors (page errors + console
errors that are not just blocked network calls).  Used to prove a big removal (e.g. the parlay feature) did not break any tab.   python3 scripts/smoke_all_tabs.py [--json out.json]"""
import functools, http.server, json, socketserver, sys, threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def run() -> dict:
    from playwright.sync_api import sync_playwright
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    bk = (ROOT / "docs" / "picks_backup.json").read_text()
    res = {"errors": {}, "clicked": 0}
    with sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1400, "height": 900})
        cur = ["boot"]
        def rec(msg): res["errors"].setdefault(cur[0], []).append(msg[:160])
        pg.on("pageerror", lambda e: rec("PAGEERROR " + str(e)))
        pg.on("console", lambda m: rec("CONSOLE " + m.text) if m.type == "error" and not any(x in m.text for x in ("Failed to load resource", "net::ERR", "CORS", "ERR_FAILED", "Access to fetch")) else None)
        pg.route("**/*espn.com/**", lambda r: r.abort()); pg.route("**/*workers.dev/**", lambda r: r.abort()); pg.route("**/*nhle.com/**", lambda r: r.abort())
        pg.add_init_script("localStorage.setItem('preds',%s)" % json.dumps(bk))
        pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/app.html?nosb=1&slim=1"); pg.wait_for_timeout(6000)
        seen = set()
        SKIP = ("lockPick", "recR", "removePick", "doUpdate", "wipe", "clear", "delete", "remove", "reset", "import", "export", "location", "logout", "saveGH", "dispatch",
                "sync", "SYNC", "window.open", "confirm", "prompt", "download", "lockH", "lockCFB", "lockNFL", "_lock", "Lock(", "toggleLock")
        SEL = "button[onclick]"
        def visible():
            return pg.evaluate("""()=>[...document.querySelectorAll('button[onclick]')].filter(b=>b.offsetParent!==null).map(b=>({oc:(b.getAttribute('onclick')||'').slice(0,170),t:b.textContent.trim().slice(0,28)}))""")
        def click(bt):
            key = bt["oc"]
            if not key or key in seen or any(x in key for x in SKIP):
                return False
            seen.add(key); cur[0] = f"{bt['t']} :: {key[:70]}"
            try:
                pg.evaluate("(oc)=>{const b=[...document.querySelectorAll('button[onclick]')].find(x=>(x.getAttribute('onclick')||'').slice(0,170)===oc&&x.offsetParent!==null);if(b)b.click()}", key)
                pg.wait_for_timeout(250); res["clicked"] += 1
            except Exception as e:
                rec("CLICKFAIL " + str(e)[:100])
            return True
        tops = [b for b in visible() if "navTap(this" in b["oc"]]
        for top in tops:
            click(top)
            for depth in range(3):                              # sub-tabs, then the sub-sub-tabs that appear
                subs = [b for b in visible() if any(k in b["oc"] for k in ("setSub", "T('", "Sub(", "SubTab", "Tab(", "show", "set", "render", "switch", "open"))]
                if not any([click(b) for b in subs]):      # a list, not a generator: click EVERY sub-tab, not just the first
                    break
        b.close()
    srv.shutdown()
    return res


if __name__ == "__main__":
    r = run()
    print(f"clicked {r['clicked']} buttons; locations with JS errors: {len(r['errors'])}")
    for k, v in r["errors"].items():
        print(" -", k, "\n    ", "\n     ".join(sorted(set(v))[:3]))
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(r, indent=1))
