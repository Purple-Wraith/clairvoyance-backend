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


# ───────────────────────── locks / digest emails (auto_lock_settle.py) ─────────────────────────
import auto_lock_settle as a  # noqa: E402


def _game(sport="NFL", label="SF +3.5", alt=None, tier=3, ev=0.08, **kw):
    q = {"kind": "GAME", "sport": sport, "hA": "SF", "awA": "SEA", "side": "sprdDog", "label": label, "prob": 0.69, "ml": "-272", "dec": 1.37,
         "tierN": tier, "evVal": ev, "lane": False, "priceSource": "estimated" if alt else "market"}
    if alt:
        q["altLine"] = alt
    q.update(kw)
    return q


ALT = {"posted": -3.0, "line": 3.5, "shift": 6.5, "postedLabel": "SF -3.0", "postedProb": 0.85}


class LocksEmail(unittest.TestCase):
    def test_adjusted_line_pick_is_labelled_and_has_no_ev_or_posted_tier(self):
        html = a._leg_html(_game(alt=ALT))
        self.assertIn("ADJUSTED LINE", html); self.assertIn("SF -3.0", html)             # the original posted line is shown
        self.assertIn("estimate", html); self.assertNotIn("EV ", html); self.assertNotIn("PREMIUM", html)
        plain = a._leg_html(_game())                                                   # a normal pick is unchanged: tier badge + EV
        self.assertIn("PREMIUM", plain); self.assertIn("EV +8.0%", plain); self.assertNotIn("ADJUSTED", plain)

    def test_legend_explains_adjusted_lines_and_hockey_claims_are_true(self):
        html = a.build_locks_email_html([_game(alt=ALT)], live=True, locked_count=1)
        self.assertIn("ADJUSTED LINE", html)
        self.assertIn("exception (see ADJUSTED LINE)", html)                           # hockey 'real price' claim no longer absolute
        self.assertIn("price at the posted line is the consensus", html)
        self.assertIn("clairvoyanceengine.info", html); self.assertIn("reply", html.lower())  # subscriber contact footer
        for retired in ("MLS", "Bundesliga", "parlay", "MLB", "WNBA"):
            self.assertNotIn(retired, html)

    def test_posted_line_context_is_labelled_when_a_leg_is_shifted(self):
        leg = _game(alt=ALT, mcSummary="MC: SF wins 85%", best={"label": "SF -3.0", "tierN": 3})
        html = a.build_locks_email_html([leg], live=True, locked_count=1)
        self.assertIn("Context at the POSTED line", html)

    def test_send_failure_is_recorded_and_returned(self):
        orig = (a._send_gmail, a.LOCKS_EMAIL_TO, a.OWNER_EMAIL)
        a._send_gmail = lambda *x, **k: (False, "smtp down"); a.LOCKS_EMAIL_TO = "to@example.com"
        a.EMAIL_FAILURES.clear()
        try:
            self.assertFalse(a.send_locks_email([_game()], live=True, locked_count=1, label="NFL", to=["to@example.com"]))
            self.assertEqual(len(a.EMAIL_FAILURES), 1)
            a._send_gmail = lambda *x, **k: (True, "")
            self.assertTrue(a.send_locks_email([_game()], live=True, locked_count=1, label="NFL", to=["to@example.com"]))
        finally:
            a._send_gmail, a.LOCKS_EMAIL_TO, a.OWNER_EMAIL = orig; a.EMAIL_FAILURES.clear()

    def test_subject_date_is_mountain_time(self):
        seen = {}
        orig = a._send_gmail
        a._send_gmail = lambda subject, *x, **k: (seen.setdefault("s", subject) and (True, ""))
        try:
            a.send_locks_email([_game()], live=True, locked_count=1, label="NFL", to=["to@example.com"])
        finally:
            a._send_gmail = orig
        today_mt = datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%d")
        self.assertIn(today_mt, seen["s"])


class NbaPreseasonGuard(unittest.TestCase):
    def test_preseason_legs_are_never_qualified(self):
        pre = datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%dT18:00Z")
        orig = a._nba_preseason_pairs
        a._nba_preseason_pairs = lambda: {frozenset({"UTAH", "DEN"})}
        try:
            game = {"sport": "NBA", "hA": "DEN", "awA": "UTAH", "startMs": None, "markets": [{"side": "over", "label": "OVER 220.5", "prob": .8, "tierN": 3, "evVal": .1, "ml": "-110", "dec": 1.9}]}
            reg = dict(game, hA="BOS", awA="NYK")
            props = [{"grade": "PREMIUM", "sportTag": "NBA", "hA": "DEN", "awA": "UTAH", "player": "X"}, {"grade": "PREMIUM", "sportTag": "NBA", "hA": "BOS", "awA": "NYK", "player": "Y"}]
            q = a.build_qualifying({"gameLegs": [game, reg], "propLegs": props})
        finally:
            a._nba_preseason_pairs = orig
        teams = {(x.get("hA") or x["leg"].get("hA")) for x in q}
        self.assertNotIn("DEN", teams); self.assertIn("BOS", teams)          # preseason pair dropped (game AND prop), regular-season pair kept

    def test_pairs_come_from_the_schedule_files(self):
        pairs = a._nba_preseason_pairs()                                      # runs against docs/nba_schedule.json: must be a set and never raise
        self.assertIsInstance(pairs, set)


class DigestEmail(unittest.TestCase):
    def test_adjusted_line_rows_make_no_market_edge_claim_and_send_failure_raises(self):
        row = a._digest_pick_row_html({"awA": "SEA", "hA": "SF", "betOn": "SF +3.5", "ml": "-272", "winProb": .69, "decOdds": 1.37, "altLine": ALT, "priceSource": "estimated"})
        self.assertIn("ADJUSTED LINE", row); self.assertIn("estimate", row); self.assertNotIn("pp edge", row); self.assertNotIn("implied by the price", row)
        normal = a._digest_pick_row_html({"awA": "SEA", "hA": "SF", "betOn": "SF ML", "ml": "-110", "winProb": .6, "decOdds": 1.9})
        self.assertIn("pp edge", normal)
        orig = a._send_gmail; a._send_gmail = lambda *x, **k: (False, "nope")
        try:
            with self.assertRaises(RuntimeError):
                a.send_top_picks_digest_email([{"awA": "B", "hA": "A", "betOn": "OVER 5.5", "ml": "-110", "winProb": .7, "decOdds": 1.9, "league": "NHL"}], "2026-10-04")
        finally:
            a._send_gmail = orig
        html = a.build_top_picks_digest_html({"top7": [], "top4ByLeague": {}}, "2026-10-04")
        self.assertNotIn("Questions, or want to stop", html)                  # owner-only email: no subscriber footer


class PickOfDay(unittest.TestCase):
    def setUp(self):
        import generate_pick_of_day_social as pod
        self.pod = pod

    def test_only_premium_optimal_in_scope_sports(self):
        qs = [_game(tier=3), _game(tier=1, label="SF -1.5"), _game(tier=0, label="SF -2.5"), _game(sport="NCAAB", tier=3, label="DUKE -4"), _game(sport="MLS", tier=3, label="X -1")]
        picks = self.pod.select_top_picks(qs, n=5)
        self.assertEqual([c["rank_tier"] for c in picks], [3])

    def test_alt_line_caption_has_no_edge_claim_or_lock_emoji(self):
        c = self.pod.select_top_picks([_game(alt=ALT, ev=0.12)])[0]
        cap = self.pod.build_pick_caption(c, "October 4, 2026")
        self.assertNotIn("Model edge", cap)
        self.assertNotIn("\U0001f512", cap)
        self.assertIn("price is an estimate", cap)
        self.assertIn("SF -3.0", cap)

    def test_market_priced_caption_keeps_edge(self):
        c = self.pod.select_top_picks([_game(ev=0.08)])[0]
        self.assertIn("Model edge: EV +8.0%", self.pod.build_pick_caption(c, "October 4, 2026"))

    def test_workflow_marker_requires_a_real_send(self):
        wf = (ROOT / ".github" / "workflows" / "pick-of-day-social-daily.yml").read_text()
        self.assertIn("id: post", wf)
        self.assertIn("steps.post.outputs.sent == '1'", wf)

    def test_sent_flag_written_only_when_called(self):
        import os, tempfile
        with tempfile.NamedTemporaryFile("r+", delete=False) as fh:
            os.environ["GITHUB_OUTPUT"] = fh.name
            self.pod._flag_sent()
            fh.seek(0)
            self.assertEqual(fh.read(), "sent=1\n")
        os.environ.pop("GITHUB_OUTPUT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
