#!/usr/bin/env python3
"""League filter on the PENDING lists (owner 2026-10-25): the home page's PENDING PICKS and the History tab's PENDING BETS each get a chip bar built from the leagues that have a pending pick
(with counts); tapping a league narrows that list, ALL restores it, the header reads "N OF M", the two lists keep separate selections, and a league whose last pending pick settles falls back to ALL.

    python3 scripts/test_pending_league_filter.py
"""
import functools, http.server, json, os, socketserver, threading, time, unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(os.environ.get("CV_ROOT") or Path(__file__).resolve().parent.parent)
SRC = (ROOT / "docs" / "app.html").read_text()


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def pick(i, sport, hA, awA, outcome="pending"):
    d = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    return {"id": f"pf{i}", "sport": sport, "league": sport, "hA": hA, "awA": awA, "betOn": hA, "betType": "ML", "outcome": outcome, "date": d,
            "lockedAt": int(time.time() * 1000) - i * 1000, "winProb": .6, "decOdds": 1.9, "mlOdds": "-110", "odds": "-110"}


PICKS = [pick(1, "NBA", "LAL", "BOS"), pick(2, "NBA", "MIA", "NYK"), pick(3, "NHL", "BOS", "TOR"), pick(4, "CFB", "OSU", "MICH")]


class Source(unittest.TestCase):
    def test_both_pending_lists_use_the_shared_filter(self):
        self.assertEqual(SRC.count("_pendLgBar(pendingFull,"), 2)
        self.assertIn("_pendLgFilter(pendingFull,'home')", SRC); self.assertIn("_pendLgFilter(pendingFull,'hist')", SRC)
        self.assertIn(".btn.pend-lg{min-height:40px!important}", SRC)       # thumb-sized on phones

    def test_index_copy_is_identical(self):
        self.assertEqual(SRC, (ROOT / "docs" / "index.html").read_text())


class Rendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page(viewport={"width": 900, "height": 900})
        cls.errs = []; cls.pg.on("pageerror", lambda e: cls.errs.append(str(e)))
        cls.pg.route("**/*espn.com/**", lambda r: r.abort()); cls.pg.route("**/*workers.dev/**", lambda r: r.abort())
        cls.pg.add_init_script("localStorage.setItem('preds'," + json.dumps(json.dumps(PICKS)) + ")")
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&nosw=1")
        cls.pg.wait_for_function("typeof renderHomePage==='function'&&typeof _pendLgBar==='function'"); cls.pg.wait_for_timeout(1200)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def setUp(self):
        self.pg.evaluate("localStorage.setItem('preds'," + json.dumps(json.dumps(PICKS)) + ");window._pendLg={};renderHomePage()")
        self.pg.wait_for_timeout(200)

    def home(self):
        return self.pg.evaluate("""()=>{const el=document.getElementById('home-pending-bets');
          return {head:(el.innerText.match(/PENDING PICKS[^\\n]*/)||[''])[0], chips:[...el.querySelectorAll('.pend-lg')].map(b=>b.innerText.trim()),
                  rows:el.querySelectorAll('.pk-row,[data-pick-id],.pickrow').length, text:el.innerText}}""")

    def test_chips_list_each_league_with_its_count(self):
        h = self.home()
        self.assertEqual(h["chips"], ["ALL 4", "CFB 1", "NBA 2", "NHL 1"], h)
        self.assertIn("4 PENDING", h["head"])

    def test_tapping_a_league_narrows_the_list_and_all_restores_it(self):
        self.pg.click("#home-pending-bets .pend-lg:has-text('NBA')"); self.pg.wait_for_timeout(200)
        h = self.home()
        self.assertIn("2 OF 4 PENDING", h["head"], h)
        self.assertIn("LAL", h["text"]); self.assertIn("MIA", h["text"]); self.assertNotIn("OSU", h["text"]); self.assertNotIn("TOR", h["text"])
        self.assertEqual(self.pg.get_attribute("#home-pending-bets .pend-lg:has-text('NBA')", "aria-pressed"), "true")
        self.pg.click("#home-pending-bets .pend-lg:has-text('ALL')"); self.pg.wait_for_timeout(200)
        h = self.home()
        self.assertIn("4 PENDING", h["head"]); self.assertNotIn(" OF ", h["head"]); self.assertIn("OSU", h["text"])

    def test_a_league_with_no_pending_left_falls_back_to_all(self):
        self.pg.evaluate("window._pendLg.home='NHL'"); self.pg.evaluate("renderHomePage()"); self.pg.wait_for_timeout(150)
        self.assertIn("1 OF 4", self.home()["head"])
        left = [dict(p, outcome="win") if p["sport"] == "NHL" else p for p in PICKS]
        self.pg.evaluate("localStorage.setItem('preds'," + json.dumps(json.dumps(left)) + ");renderHomePage()"); self.pg.wait_for_timeout(200)
        h = self.home()
        self.assertEqual(h["chips"], ["ALL 3", "CFB 1", "NBA 2"], h); self.assertIn("3 PENDING", h["head"]); self.assertNotIn(" OF ", h["head"])

    def test_a_single_league_shows_no_chip_bar(self):
        only = [p for p in PICKS if p["sport"] == "NBA"]
        self.pg.evaluate("localStorage.setItem('preds'," + json.dumps(json.dumps(only)) + ");renderHomePage()"); self.pg.wait_for_timeout(200)
        self.assertEqual(self.home()["chips"], [])

    def test_history_tab_has_its_own_independent_filter(self):
        self.pg.evaluate("renderOverallHistory('ALL')"); self.pg.wait_for_timeout(250)
        chips = self.pg.evaluate("[...document.querySelectorAll('#ovr-pending-section .pend-lg')].map(b=>b.innerText.trim())")
        self.assertEqual(chips, ["ALL 4", "CFB 1", "NBA 2", "NHL 1"])
        self.pg.evaluate("[...document.querySelectorAll('#ovr-pending-section .pend-lg')].find(b=>b.innerText.startsWith('CFB')).click()"); self.pg.wait_for_timeout(250)     # (the History tab is not the open page)
        head = self.pg.evaluate("document.querySelector('#ovr-pending-section').innerText.match(/PENDING BETS[^\\n]*/)[0]")
        self.assertIn("1 OF 4", head)
        self.assertNotIn(" OF ", self.home()["head"])                      # the home list kept ALL
        self.assertEqual(self.pg.evaluate("window._pendLg.hist"), "CFB"); self.assertIn(self.pg.evaluate("window._pendLg.home||'ALL'"), ("ALL",))

    def test_no_page_errors(self):
        self.assertEqual([e for e in self.errs if "pend" in e.lower()], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
