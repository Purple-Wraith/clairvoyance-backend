#!/usr/bin/env python3
"""Regression tests for the 2026-10-04 email audit fixes (owner-facing social/digest emails, subscriber emails, alerts).  Pure-Python builders (no network, nothing is sent)
plus one browser check for the yearly-total scope. Sections are added per fix group."""
import functools, http.server, json, socketserver, sys, threading, unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import generate_social_cards as g  # noqa: E402


class SocialCards(unittest.TestCase):
    def test_stale_hockey_stripper_is_gone(self):
        self.assertFalse(hasattr(g, "_strip_stale_hockey"))            # it deleted REAL hockey from the monthly recap

    def test_basis_wording_is_honest(self):
        self.assertEqual(g.TALLY_NOTE, "Excludes picks known to have been locked after game start.")
        stats = {"w": 10, "l": 5, "pct": 0.66, "units": 4.0, "lockedCount": 15}
        now = datetime(2026, 11, 1, 9, 0, tzinfo=ZoneInfo("America/Denver"))
        caps = [g.build_monthly_caption(stats, now), g.build_yearly_caption(stats, 2026), g.build_alltime_caption(stats), g.build_covers_caption()]
        for c in caps:
            blob = (c["instagram"] + c["x"]).lower()
            for bad in ("cherry", "deleted losses", "every result"):
                self.assertNotIn(bad, blob)

    def test_no_retired_leagues_or_typos(self):
        self.assertNotIn("KHL", g.SPORT_LEAGUES["HOCKEY"])
        cap = g.build_daily_caption({"w": 3, "l": 1, "pct": .75, "units": 1.0, "lockedCount": 4}, datetime(2026, 10, 3, tzinfo=ZoneInfo("America/Denver")))
        self.assertNotIn("Yesterdays", cap["instagram"])

    def test_failed_send_is_recorded_not_raised(self):
        calls = []
        orig = (g._send_gmail, g.SOCIAL_CARD_EMAIL_TO)
        g._send_gmail = lambda *a, **k: (calls.append(a[0]) or (False, "boom"))
        g.SOCIAL_CARD_EMAIL_TO = "owner@example.com"
        g.SEND_FAILURES.clear()
        try:
            g.send_email("Subj A", [], {"instagram": "i", "x": "x"})        # must not raise: later emails still go out
            g.send_email("Subj B", [], {"instagram": "i", "x": "x"})
            self.assertEqual(calls, ["Subj A", "Subj B"]); self.assertEqual(g.SEND_FAILURES, ["Subj A", "Subj B"])
        finally:
            g._send_gmail, g.SOCIAL_CARD_EMAIL_TO = orig; g.SEND_FAILURES.clear()


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class YearScope(unittest.TestCase):
    def test_year_total_matches_its_rows_and_excludes_parlays_and_retired(self):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        def pk(i, sport, bt, outcome, hA="AAA"):
            return {"id": f"y{i}", "sport": sport, "betType": bt, "betOn": "X", "hA": hA, "awA": "BBB", "date": "2026-10-02", "lockedAt": 1791000000000 + i, "outcome": outcome, "decOdds": 2.0, "ml": "+100", "winProb": .6}
        picks = [pk(1, "NHL", "ML", "win"), pk(2, "NFL", "ML", "loss"), pk(3, "NHL", "PARLAY", "win", hA="PARLAY"), pk(4, "MLS", "ML", "win"), pk(5, "MLB", "ML", "win")]
        with sync_playwright() as p:
            b = p.chromium.launch(); pg = b.new_page()
            pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/app.html?nosb=1")
            pg.wait_for_function("typeof saveP==='function'"); pg.wait_for_timeout(800); pg.evaluate("(x)=>saveP(x)", picks)
            ys = g.get_year_stats(pg, 2026, {"ids": [], "rows": picks, "idx": {}})
            b.close()
        srv.shutdown()
        self.assertEqual((ys["w"], ys["l"]), (1, 1))                                  # only the NHL win and the NFL loss count
        self.assertEqual(sum(s["w"] for s in ys["bySport"]), ys["w"]); self.assertEqual(sum(s["l"] for s in ys["bySport"]), ys["l"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
