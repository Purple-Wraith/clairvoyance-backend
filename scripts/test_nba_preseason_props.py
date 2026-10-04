#!/usr/bin/env python3
"""NBA PRESEASON never gets a player prop (owner decision; found 2026-10-04 when the lock pass locked a Utah-Denver preseason prop): _generateNBAProps must skip games flagged
seasonType 1 / preseason:true, in the automated lock path and the Props tab alike, while regular-season games still get props."""
import functools, http.server, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATS = {n: {"name": n, "team": t, "gp": 30, "ppg": 24.0 + i, "rpg": 7.0, "apg": 6.5, "status": ""} for i, (n, t) in enumerate(
    [("A One", "UTAH"), ("A Two", "UTAH"), ("D One", "DEN"), ("D Two", "DEN"), ("B One", "BOS"), ("B Two", "BOS"), ("N One", "NYK"), ("N Two", "NYK")])}


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class PreseasonProps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.browser = cls.pw.chromium.launch()
        cls.pg = cls.browser.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _generateNBAProps==='function'"); cls.pg.wait_for_timeout(800)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def teams(self, games):
        props = self.pg.evaluate("([g,s])=>_generateNBAProps(g,s).map(p=>[p.hA,p.awA,p.player])", [games, STATS])
        return {(p[0], p[1]) for p in props}

    def test_preseason_games_get_no_props(self):
        self.assertEqual(self.teams([{"h": "DEN", "a": "UTAH", "seasonType": 1}]), set())
        self.assertEqual(self.teams([{"h": "DEN", "a": "UTAH", "preseason": True}]), set())

    def test_regular_season_still_gets_props_and_preseason_is_ignored_alongside(self):
        got = self.teams([{"h": "DEN", "a": "UTAH", "seasonType": 1}, {"h": "BOS", "a": "NYK", "seasonType": 2}, {"h": "BOS", "a": "NYK"}])
        self.assertEqual(got, {("BOS", "NYK")})


if __name__ == "__main__":
    unittest.main(verbosity=2)
