#!/usr/bin/env python3
"""The live-score relay call is skipped on the pipeline's own headless page (127.0.0.1 + ?nologo=1), 2026-11-02.  It used to log a CORS 'browser error' on every watchdog / lock / settle run (the relay refuses a
local origin) for data nothing there uses.  A page without ?nologo=1 (a real browser, the deployed-site fallback) still calls it.

    python3 scripts/test_relay_skip.py
"""
import functools, http.server, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class RelaySkip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def relay_calls(self, query):
        pg = self.browser.new_page()
        hits = []
        pg.on("request", lambda r: hits.append(r.url) if "workers.dev" in r.url else None)
        pg.route("**/*workers.dev*", lambda r: r.abort())
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?{query}")
        pg.wait_for_function("typeof refreshRelay==='function'&&typeof _lvRelaySkipHere==='function'")
        pg.wait_for_timeout(800)
        hits.clear()
        res = pg.evaluate("refreshRelay(true)")
        pg.wait_for_timeout(300)
        skip = pg.evaluate("_lvRelaySkipHere()")
        pg.close()
        return res, skip, [h for h in hits if "/hockey" in h]

    def test_the_pipeline_page_never_calls_the_relay(self):
        res, skip, hits = self.relay_calls("nosb=1&nosw=1&nologo=1")
        self.assertTrue(skip)
        self.assertFalse(res)
        self.assertEqual(hits, [])

    def test_an_ordinary_page_still_calls_the_relay(self):
        res, skip, hits = self.relay_calls("nosb=1&nosw=1")
        self.assertFalse(skip)
        self.assertGreaterEqual(len(hits), 1)                                 # blocked in the test, but attempted (the app's own poller may add one more)


if __name__ == "__main__":
    unittest.main()
