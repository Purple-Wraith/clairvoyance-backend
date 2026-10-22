#!/usr/bin/env python3
"""Service worker (docs/cv-sw.js + _cvSwInit in docs/app.html), 2026-10-22: the home-screen app stalled on cold GitHub Pages fetches.

Runs the real page in Chromium against a local server that can delay files and change the app's ETag, and checks: the shell loads from cache with the network OFF; a slow data file falls back to
its last good copy; a new shell raises the tap-to-update bar WITHOUT reloading the page; version/automation/manual-lock files are never cached; cache-busting params do not multiply cache entries;
?nosw=1 removes it; automation never registers it unless asked; the worker file itself can never reload or navigate a page.

    python3 scripts/test_service_worker.py
"""
import functools, http.server, json, re, socketserver, threading, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


class Handler(http.server.SimpleHTTPRequestHandler):
    delay = {}            # path -> seconds to stall before answering
    etag = '"shell-v1"'   # ETag served for app.html / index.html
    hits = {}

    def log_message(self, *a, **k):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        Handler.hits[path] = Handler.hits.get(path, 0) + 1
        d = Handler.delay.get(path)
        if d:
            time.sleep(d)
        for h in ("If-Modified-Since", "If-None-Match"):          # always a full 200: a "deploy" here only changes the ETag, so a 304 would hide it
            if h in self.headers:
                del self.headers[h]
        super().do_GET()

    def end_headers(self):
        if self.path.split("?")[0].endswith(("/app.html", "/index.html", "/")):
            self.send_header("ETag", Handler.etag)
        super().end_headers()


class SW(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        cls.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(DOCS)))
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def setUp(self):
        Handler.delay = {}; Handler.etag = '"shell-v1"'; Handler.hits = {}
        self.ctx = self.browser.new_context(viewport={"width": 1300, "height": 900})

    def tearDown(self):
        self.ctx.close()

    def open(self, qs="?nosb=1&sw=1", wait_sw=True):
        pg = self.ctx.new_page()
        pg.goto(f"{self.base}/app.html{qs}")
        pg.wait_for_function("typeof getP==='function'")
        if wait_sw:
            pg.evaluate("navigator.serviceWorker.ready.then(()=>true)")
        return pg

    def cache_keys(self, pg, kind):
        return pg.evaluate("""async(k)=>{const n=(await caches.keys()).filter(x=>x.endsWith(':'+k));let out=[];for(const c of n){out=out.concat((await (await caches.open(c)).keys()).map(r=>r.url))}return out}""", kind)

    def wait_for(self, fn, secs=8):
        t0 = time.time()
        while time.time() - t0 < secs:
            v = fn()
            if v:
                return v
            time.sleep(0.2)
        return fn()

    # ── registration rules ────────────────────────────────────────────────────────────────────────────────────────────
    def test_automation_never_registers_it_unless_asked(self):
        pg = self.open("?nosb=1", wait_sw=False)
        pg.wait_for_timeout(1500)
        self.assertEqual(pg.evaluate("navigator.serviceWorker.getRegistrations().then(r=>r.length)"), 0)
        pg.close()

    def test_registers_and_caches_the_shell_on_install(self):
        pg = self.open()
        keys = self.wait_for(lambda: self.cache_keys(pg, "shell"))
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].endswith("/app.html"))
        self.assertEqual(pg.evaluate("navigator.serviceWorker.getRegistrations().then(r=>r.length)"), 1)
        pg.close()

    def test_kill_switch_removes_it_and_clears_its_caches(self):
        pg = self.open()
        self.wait_for(lambda: self.cache_keys(pg, "shell"))
        pg.goto(f"{self.base}/app.html?nosb=1&nosw=1")
        pg.wait_for_function("typeof getP==='function'")
        self.assertTrue(self.wait_for(lambda: pg.evaluate("navigator.serviceWorker.getRegistrations().then(r=>r.length)") == 0))
        self.assertTrue(self.wait_for(lambda: pg.evaluate("caches.keys().then(k=>k.filter(x=>x.startsWith('cv-v')).length)") == 0))
        pg.close()

    # ── the actual fix: loading when the network is slow or gone ───────────────────────────────────────────────────────
    def test_the_shell_loads_with_the_network_off(self):
        pg = self.open()
        self.wait_for(lambda: self.cache_keys(pg, "shell"))
        pg.reload(); pg.wait_for_function("typeof getP==='function'")        # now controlled by the worker
        self.ctx.set_offline(True)
        pg.reload()
        pg.wait_for_function("typeof getP==='function'&&!!document.getElementById('hdr')")
        self.assertTrue(pg.evaluate("document.title.length>0"))
        self.ctx.set_offline(False)
        pg.close()

    def test_the_shell_is_instant_even_when_the_server_stalls(self):
        pg = self.open()
        self.wait_for(lambda: self.cache_keys(pg, "shell"))
        pg.reload(); pg.wait_for_function("typeof getP==='function'")
        Handler.delay = {"/app.html": 8}                                      # a cold CDN edge
        t0 = time.time()
        pg.goto(f"{self.base}/app.html?nosb=1&sw=1")
        pg.wait_for_function("typeof getP==='function'")
        self.assertLess(time.time() - t0, 5, "the cached shell should not wait for a stalled server")
        pg.close()

    def test_a_slow_data_file_falls_back_to_its_last_good_copy(self):
        pg = self.open()
        pg.reload(); pg.wait_for_function("typeof getP==='function'")
        self.assertTrue(self.wait_for(lambda: any(k.split("?")[0].endswith("/data.json") for k in self.cache_keys(pg, "data")), 12))
        Handler.delay = {"/data.json": 9}
        t0 = time.time()
        ok = pg.evaluate("async()=>{const r=await fetch('data.json?_cb=987654');const j=await r.json();return {s:r.status,n:Object.keys(j).length}}")
        dt = time.time() - t0
        self.assertEqual(ok["s"], 200)
        self.assertGreater(ok["n"], 3)
        self.assertLess(dt, 6.5)                                              # served after DATA_TIMEOUT (3.5s), not after the stall
        self.assertGreater(dt, 3.0)
        pg.close()

    def test_cache_busting_params_do_not_multiply_cache_entries(self):
        pg = self.open()
        pg.reload(); pg.wait_for_function("typeof getP==='function'")
        pg.evaluate("async()=>{for(const q of ['_cb=1','_cb=2','_v=9','cb=3'])await fetch('data.json?'+q)}")
        self.assertEqual(len([k for k in self.cache_keys(pg, "data") if k.split("?")[0].endswith("/data.json")]), 1)
        pg.close()

    def test_live_files_are_never_cached(self):
        pg = self.open()
        pg.reload(); pg.wait_for_function("typeof getP==='function'")
        pg.evaluate("async()=>{for(const f of ['version.json','automation_status.json','manual_locks.json','relay_ping.json'])try{await fetch(f+'?x='+Date.now())}catch(e){}}")
        pg.wait_for_timeout(500)
        urls = " ".join(self.cache_keys(pg, "data") + self.cache_keys(pg, "shell") + self.cache_keys(pg, "asset"))
        for f in ("version.json", "automation_status.json", "manual_locks.json", "relay_ping.json"):
            self.assertNotIn(f, urls)
        pg.close()

    # ── updates ───────────────────────────────────────────────────────────────────────────────────────────────────────────
    def test_a_newer_shell_shows_the_update_bar_and_never_reloads_by_itself(self):
        pg = self.open()
        self.wait_for(lambda: self.cache_keys(pg, "shell"))
        pg.reload(); pg.wait_for_function("typeof getP==='function'")
        Handler.etag = '"shell-v2"'                                           # a new deploy
        pg.reload(); pg.wait_for_function("typeof getP==='function'")
        pg.evaluate("window.__marker=12345")
        self.assertTrue(self.wait_for(lambda: pg.evaluate("!!document.getElementById('cv-sw-bar')"), 10))
        pg.wait_for_timeout(1500)
        self.assertEqual(pg.evaluate("window.__marker"), 12345)                # the page was NOT reloaded under the user
        self.assertIn("TAP TO UPDATE", pg.inner_text("#cv-sw-bar"))
        pg.close()

    def test_no_update_bar_when_the_shell_is_unchanged(self):
        pg = self.open()
        self.wait_for(lambda: self.cache_keys(pg, "shell"))
        pg.reload(); pg.wait_for_function("typeof getP==='function'")
        pg.wait_for_timeout(2500)
        self.assertFalse(pg.evaluate("!!document.getElementById('cv-sw-bar')"))
        pg.close()

    # ── the file itself ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def test_worker_source_rules(self):
        src = (DOCS / "cv-sw.js").read_text()
        self.assertNotRegex(src, r"location\.reload|\.navigate\(|clients\.openWindow")       # never reloads or navigates a page itself
        self.assertIn("u.origin !== self.location.origin", src)                               # other origins are never intercepted
        self.assertIn("r.method !== 'GET'", src)
        legacy = (DOCS / "sw.js").read_text()
        self.assertIn("unregister", legacy)                                                   # the old self-destruct worker stays to clean up stragglers

    def test_page_source_rules(self):
        html = (DOCS / "app.html").read_text()
        self.assertEqual(html.count("serviceWorker.register"), 1)
        self.assertEqual((DOCS / "index.html").read_text(), html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
