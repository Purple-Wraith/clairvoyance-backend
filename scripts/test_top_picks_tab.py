#!/usr/bin/env python3
"""TOP PICKS tab changes of 2026-10-24: the win-rate square grid sits at the TOP of the tab; each league lists its best 5 qualifying picks (fewer when fewer qualify); and a day's record -- in the
grid and beside every league -- is the WHOLE day (every qualifying pick), not just the 10 overall / 5 per league the lists show.

    python3 scripts/test_top_picks_tab.py
"""
import functools, http.server, os, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(os.environ.get("CV_ROOT") or Path(__file__).resolve().parent.parent)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def pick(i, sport, outcome, day, wp=None):
    return {"id": f"p{sport}{i}", "sport": sport, "league": sport, "betType": "ML", "betOn": f"{sport}H{i}", "hA": f"{sport}H{i}", "awA": f"{sport}A{i}", "date": day, "ml": "-125", "decOdds": 1.8,
            "winProb": wp if wp is not None else .78 - i * .002, "outcome": outcome, "lockedAt": 1}


class TopPicks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page(viewport={"width": 1000, "height": 900})
        cls.pg.route("**/*espn.com/**", lambda r: r.abort()); cls.pg.route("**/*workers.dev/**", lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _topPicksDayWinRate==='function'&&typeof saveP==='function'"); cls.pg.wait_for_timeout(1200)
        cls.day = cls.pg.evaluate("_scShift(today(),-1)")

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def ledger(self, picks):
        self.pg.evaluate("(p)=>{saveP(p);_tpRecCache={at:0,v:null}}", picks)

    def test_the_grid_sits_at_the_top_of_the_top_picks_tab(self):
        order = self.pg.evaluate("[...document.getElementById('tp-view-picks').children].map(c=>c.id)")
        self.assertEqual(order[:3], ["toppicks-heatmap", "toppicks-date-row", "toppicks-content"])

    def test_a_days_record_counts_every_qualifying_pick_not_just_the_top_ten(self):
        self.ledger([pick(i, "NBA", "win" if i % 4 else "loss", self.day) for i in range(14)])
        r = self.pg.evaluate("(d)=>{const legs=_topPicksLegsForDate(d);const q=_topPicksRankLegs(legs);return{...(_topPicksDayWinRate(d)),qual:q.qualifying.length,top:q.top10.length}}", self.day)
        self.assertEqual(r["top"], 10); self.assertGreater(r["qual"], 10)
        self.assertEqual((r["n"], r["graded"]), (r["qual"], r["qual"]))                       # all of them, not 10
        self.assertEqual(r["w"] + r["l"], r["qual"])

    def test_each_league_shows_its_best_five_and_the_record_covers_all(self):
        picks = [pick(i, "NBA", "win" if i < 6 else "loss", self.day) for i in range(8)] + [pick(i, "NHL", "win", self.day) for i in range(3)]
        self.ledger(picks)
        html = self.pg.evaluate("(d)=>_topPicksBodyHTML(_topPicksLegsForDate(d),'','none')", self.day)
        txt = self.pg.evaluate("(h)=>{const d=document.createElement('div');d.innerHTML=h;return d.innerText.replace(/\\n+/g,' | ')}", html)
        self.assertIn("NBA — TOP 5 OF 8", txt); self.assertIn("6W-2L", txt); self.assertIn("record covers all 8", txt)
        self.assertIn("NHL — TOP 3", txt); self.assertNotIn("NHL — TOP 3 OF", txt)                  # fewer than 5 qualify: show them all, no "of"
        n = self.pg.evaluate("""(h)=>{const d=document.createElement('div');d.innerHTML=h;const out={};[...d.querySelectorAll('div')].forEach(x=>{const m=/^(\\w+) — TOP (\\d+)/.exec(x.firstElementChild?x.firstElementChild.innerText:'');});
          const secs=[...d.querySelectorAll('div')].filter(x=>/^\\w+ — TOP \\d+/.test((x.firstElementChild&&x.firstElementChild.innerText)||''));
          return secs.map(s=>[s.firstElementChild.innerText.slice(0,12),s.children.length-1])}""", html)
        nba = next(c for t, c in n if t.startswith("NBA")); self.assertEqual(nba, 5)

    def test_the_grid_cell_for_a_day_is_the_whole_day(self):
        self.ledger([pick(i, "NBA", "win" if i % 4 else "loss", self.day) for i in range(14)])
        self.pg.evaluate("renderTopPicksHeatmap()")
        tips = self.pg.evaluate("[...document.querySelectorAll('#toppicks-heatmap .tp-heat-cell')].map(c=>c.dataset.tip)")
        full = self.pg.evaluate("(d)=>_topPicksDayWinRate(d)", self.day)
        want = f"{full['w']}W-{full['l']}L"
        self.assertTrue(any("Top Picks: " + want in t for t in tips), (want, [t for t in tips if "Top Picks" in t][:3]))
        self.assertIn("WHOLE Top Picks record", self.pg.evaluate("document.getElementById('toppicks-heatmap').innerText"))

    def test_the_two_grids_share_one_square_layout(self):
        self.ledger([pick(i, "NBA", "win", self.day) for i in range(3)] + [{**pick(9, "NBA", "win", self.day), "id": "adj1", "betType": "OU", "betOn": "OVER 200.5", "altLine": {"posted": 205.5, "line": 200.5, "shift": 5, "postedLabel": "OVER 205.5"}}])
        self.pg.evaluate("renderTopPicksHeatmap()")
        a = self.pg.evaluate("document.querySelectorAll('#toppicks-heatmap .tp-heat-cell').length")
        b = self.pg.evaluate("(()=>{const d=document.createElement('div');d.innerHTML=_topAdjDailyHTML();return d.querySelectorAll('.tp-heat-cell').length})()")
        self.assertEqual(a, b); self.assertGreater(a, 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
