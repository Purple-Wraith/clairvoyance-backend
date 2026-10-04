#!/usr/bin/env python3
"""Parlays are OUT of the engine (owner decision 2026-10-04): they must never influence a figure, and no parlay UI / code may remain.
Checks the app (scope, archive, landing generator, source), and the backend (public stats history, scope helpers, Top Picks digest)."""
import functools, http.server, json, re, socketserver, sys, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import lock_timing as lt  # noqa: E402
import generate_social_cards as g  # noqa: E402

REMOVED_NAMES = ["cvParlayInit", "cvParlayToggle", "cvParlayAddLeg", "cvParlayCalc", "cvParlayLock", "renderEngineParlays", "_loadEngineParlayToBuilder", "cvXParlayInit",
                 "renderParlayTracker", "saveParlayToTracker", "renderNBAParlayG", "lockNBAParlay", "lockParlay", "calcNHLP", "clrNHLP", "_ouParlayDir", "_spreadParlayDir",
                 "cvParlayTag", "cvParlayLegTag", "_parlayLegs", "_ibsAddLeg", "nhl-tab-parlay", "nba-tab-parlay", "cv_parlays", "_epGatherLegs"]
# the only lines in the app source that may still say "parlay": the guards that keep them OUT, the decision notes, the archive label, and note text on historical straight-bet seed rows
ALLOWED = re.compile(r"_isParlay|PL_PARLAY|owner decision 2026-10-04|parlays are out of the app|No PARLAY row|settled parlays|PARLAYS ·|'PARLAY':_normSport|note:'[^']*[Pp]arlay")


def pick(i, bt, sport="NHL", outcome="win", date="2026-10-02"):
    return {"id": f"x{i}", "sport": sport, "betType": bt, "betOn": "X", "hA": "AAA" if bt != "PARLAY" else "PARLAY", "awA": "BBB", "date": date, "lockedAt": 1791000000000 + i,
            "outcome": outcome, "decOdds": 2.0, "ml": "+100", "winProb": 0.6, "wager": 1}


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class NoParlays(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]
        cls.pw = sync_playwright().start(); cls.browser = cls.pw.chromium.launch()
        cls.src = (ROOT / "docs" / "app.html").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def page(self, picks, qs=""):
        pg = self.browser.new_page(viewport={"width": 1300, "height": 900})
        pg.goto(f"http://127.0.0.1:{self.port}/app.html?nosb=1{qs}")
        pg.wait_for_function("typeof saveP==='function'&&typeof _isParlay==='function'"); pg.wait_for_timeout(900)
        pg.evaluate("(p)=>saveP(p)", picks)
        return pg

    # ── source ──
    def test_parlay_code_is_gone(self):
        for n in REMOVED_NAMES:
            self.assertNotIn(n, self.src, f"{n} is still in docs/app.html")
        bad = [ln.strip()[:120] for ln in self.src.split("\n") if re.search("parlay", ln, re.I) and not ALLOWED.search(ln)]
        self.assertEqual(bad, [], bad[:5])

    def test_no_parlay_seed_picks(self):
        self.assertEqual(len(re.findall(r"betType:\s*['\"]PARLAY['\"]", self.src)), 0)

    # ── app scope ──
    def test_parlay_never_counts_and_is_archived(self):
        picks = [pick(1, "ML"), pick(2, "ML", outcome="loss"), pick(3, "PARLAY", outcome="loss"), pick(4, "PARLAY", sport="NBA", outcome="win")]
        pg = self.page(picks, "&slim=1")
        ids = pg.evaluate("getP().map(p=>p.id)")
        self.assertEqual(sorted(ids), ["x1", "x2"])                                  # settled parlays archived out of the working ledger
        self.assertFalse(pg.evaluate("_cvScoped(%s)" % json.dumps(pick(9, "PARLAY"))))
        self.assertTrue(pg.evaluate("_archivable(%s)" % json.dumps(pick(9, "PARLAY", date="2026-10-03"))))   # any age
        self.assertFalse(pg.evaluate("_archivable(%s)" % json.dumps(pick(9, "PARLAY", outcome="pending"))))  # but never while pending
        pg.close()

    def test_unslimmed_automation_still_ignores_parlays_in_figures(self):
        picks = [pick(1, "ML"), pick(2, "ML", outcome="loss"), pick(3, "PARLAY", outcome="loss"), pick(4, "PARLAY", sport="NBA", outcome="win")]
        pg = self.page(picks, "")                                                    # webdriver => not slimmed: parlays ARE in getP (CI sees the full ledger) ...
        self.assertEqual(pg.evaluate("getP().length"), 4)
        pf = g.public_filter(pg)
        sub = {p["key"]: p for p in g.get_engine_performance_subscriber(pg, dict(pf, ids=[]))}
        full = {p["key"]: p for p in g.get_engine_performance(pg, dict(pf, ids=[]))}
        for res in (sub, full):                                                      # ... but no published figure counts them
            self.assertEqual((res["ALL_TIME"]["w"], res["ALL_TIME"]["l"]), (1, 1))
        spf = g.get_sport_performance(pg, dict(pf, ids=[]))
        w = sum(x["w"] for x in spf["allTime"]["leagues"]); l = sum(x["l"] for x in spf["allTime"]["leagues"])
        self.assertEqual((w, l), (1, 1))
        pg.close()

    def test_bet_type_lists_have_no_parlay(self):
        pg = self.page([pick(1, "ML"), pick(2, "SPREAD"), pick(3, "PARLAY")], "&slim=0")     # slim=0: even if a parlay were in the ledger, no list shows it
        pg.evaluate("renderOverall()"); pg.wait_for_timeout(400)
        self.assertNotIn("PARLAY", pg.evaluate("document.getElementById('ovr-dashboard').innerText").upper())
        pg.close()


class NoParlaysBackend(unittest.TestCase):
    def test_backend_scope_helpers(self):
        self.assertTrue(lt.is_parlay({"betType": "PARLAY"})); self.assertTrue(lt.is_parlay({"betType": "pl_parlay"})); self.assertTrue(lt.is_parlay({"hA": "NBA-PARLAY"}))
        self.assertFalse(lt.is_parlay({"betType": "ML", "hA": "BOS"}))
        pf = {"rows": [pick(1, "ML"), pick(3, "PARLAY")], "idx": {}}
        scoped = g.basis_for_engine(pf)["basis_detail"]
        self.assertEqual(scoped["settled_unknown_timing_included"] + scoped["settled_pre_start"], 1)     # only the straight bet is counted

    def test_public_stats_history_drops_parlays(self):
        try:
            import bs4  # noqa: F401  (clairvoyance_update imports its scrapers' dependencies)
        except ImportError:
            self.skipTest("bs4/lxml not installed in this interpreter")
        import clairvoyance_update as cu
        rows = [{"id": "s1", "outcome": "win", "sport": "NHL", "date": "2026-10-02", "dec_odds": 2.0, "raw": pick(1, "ML")},
                {"id": "p1", "outcome": "loss", "sport": "NHL", "date": "2026-10-02", "dec_odds": 5.0, "raw": pick(3, "PARLAY")}]
        out = cu.supabase_bets_to_history(rows)
        self.assertEqual([b["id"] for b in out], ["s1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
