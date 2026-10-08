#!/usr/bin/env python3
"""Lock rows on game cards (owner, 2026-10-08): a game IN PROGRESS keeps its lock buttons (a manual lock then is recorded as late -- lockPick never blocked manual locks); only a FINAL game
closes them ("LOCKS CLOSED (INFO ONLY)").  Covers the generic wrapper every league's card goes through plus the sport-specific card markup.

    python3 scripts/test_locks_live_open.py
"""
import functools, http.server, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "docs" / "app.html").read_text()


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Source(unittest.TestCase):
    def test_nhl_and_nba_cards_only_close_a_final_game(self):
        self.assertIn("""<div class="brow${(pre||live)?'':' lockoff'}"${(pre||live)?'':' inert aria-disabled="true"'}>""", SRC)
        self.assertNotIn("""<div class="brow${pre?'':' lockoff'}\"""", SRC)
        self.assertIn("_altRowFor('NHL',hn,an,espnEv.date,_nm3,!(pre||live))", SRC)
        self.assertIn("_altRowFor('NBA',hn,an,espnEv.date,_nmNBA,!(pre||live))", SRC)

    def test_football_cards_only_close_a_final_game(self):
        self.assertEqual(SRC.count("""<div class="brow${(liveStatus&&!liveStatus.isLive)?' lockoff':''}\""""), 2)
        self.assertIn("_altLineRowHTML('NFL',g,nm,!!(liveStatus&&!liveStatus.isLive))", SRC)
        self.assertIn("_altLineRowHTML('CFB',g,nm,!!(liveStatus&&!liveStatus.isLive))", SRC)


class Browser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _closeLocksHTML==='function'")
        cls.pg.wait_for_timeout(500)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    HTML = '<div class="gc"><div class="brow"><button>LOCK</button></div><div class="altrow"><div>ALT</div></div></div>'

    def close(self, off):
        return self.pg.evaluate("([h,o])=>_closeLocksHTML(h,o)", [self.HTML, off])

    def test_live_game_keeps_open_lock_rows_with_a_note(self):
        h = self.close("live")
        self.assertNotIn("lockoff", h)
        self.assertNotIn("inert", h)
        self.assertIn('<div class="brow"><button>LOCK</button>', h)
        self.assertIn("LOCKS STAY OPEN", h)                       # the banner now says so (and that a lock is recorded as late)

    def test_final_game_is_still_closed(self):
        h = self.close("final")
        self.assertEqual(h.count("lockoff"), 2)                   # lock row + adjusted-line row
        self.assertIn("inert", h)
        self.assertIn("FINAL — LOCKS CLOSED", h)

    def test_game_not_started_is_untouched(self):
        self.assertEqual(self.close(None), self.HTML)


if __name__ == "__main__":
    unittest.main(verbosity=2)
