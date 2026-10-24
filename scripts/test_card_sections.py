#!/usr/bin/env python3
"""Game-card sections after the 2026-10-24 changes (docs/app.html), checked on the REAL rendered cards where the data is local (College football, NFL, NHL, the four European hockey leagues, soccer) and on
the NBA card's source (its card needs a live ESPN event):

  * NBA: no MODEL RECOMMENDED LINE section; the best-market strip reads BEST UNADJUSTED MARKET.
  * NFL + college football: no MONEYLINE / SPREAD / O/U REASONING section; BEST UNADJUSTED MARKET.
  * soccer: no model-recommended line; adjusted lines for the total and the handicap; BEST UNADJUSTED MARKET.
  * every card that shows adjusted lines marks the top one with the neon magenta border AND restates it as a BEST ADJUSTED LINE strip under the boxes.
  * NHL + the four European leagues show both totals.

    python3 scripts/test_card_sections.py
"""
import functools, http.server, os, re, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(os.environ.get("CV_ROOT") or Path(__file__).resolve().parent.parent)
SRC = (ROOT / "docs" / "app.html").read_text()


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def fn(name):
    i = SRC.index(f"function {name}(")
    j = SRC.find("\nfunction ", i + 10)
    return SRC[i:j]


class Source(unittest.TestCase):
    def test_nba_card_has_no_model_recommended_line_and_keeps_the_reworded_best_market(self):
        f = fn("_nbaGameCard")
        self.assertNotIn("_recommendedLineHTML", f)
        self.assertIn("_bestMarketBoxHTML(", f); self.assertIn("_altRowFor('NBA'", f)

    def test_nfl_and_cfb_cards_have_no_reasoning_section(self):
        for name in ("_nflGameCard2", "_cfbGameCard"):
            f = fn(name)
            self.assertNotIn("REASONING</div>", f, name)
            self.assertIn("_bestMarketBoxHTML(", f, name)

    def test_the_best_market_strip_says_unadjusted_everywhere(self):
        i = SRC.index("function _bestMarketBoxHTML(")
        self.assertIn("kicker||'BEST UNADJUSTED MARKET'", SRC[i:i + 900])
        self.assertNotIn("'BEST MARKET'", SRC[i:i + 900])

    def test_the_soccer_card_dropped_its_model_recommendation_and_gained_adjusted_lines(self):
        f = fn("_renderSocMatchCardRaw")
        self.assertNotIn("_recommendedLineHTML", f)
        self.assertIn("_altRowFor('SOC_'", f); self.assertIn("lockSport:_lockType", f)


class NoRecommendation(unittest.TestCase):
    def test_no_card_builds_a_model_recommended_line(self):
        self.assertEqual(SRC.count("_recommendedLineHTML("), 1)           # only the (now unused) definition is left
        self.assertEqual(SRC.count("MODEL RECOMMENDED LINE"), 1)


class Rendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page(viewport={"width": 900, "height": 900})
        cls.pg.route("**/*espn.com/**", lambda r: r.abort()); cls.pg.route("**/*workers.dev/**", lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _renderSocMatchCardRaw==='function'&&typeof SS==='function'"); cls.pg.wait_for_timeout(1500)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def settle(self, sel, secs=12):
        try:
            self.pg.wait_for_function("(s)=>document.querySelectorAll(s+' .gc').length>0", arg=sel, timeout=secs * 1000)
        except Exception:
            pass
        self.pg.wait_for_timeout(500)

    def text(self, sel):
        return self.pg.evaluate("(s)=>document.querySelector(s)?document.querySelector(s).innerText:''", sel)

    def adj_summary(self, sel):
        return self.pg.evaluate("""(s)=>{const rows=[...document.querySelectorAll(s+' .altrow')];return {rows:rows.length,
          perRow:rows.map(r=>({chips:r.querySelectorAll('.altc').length,top:r.querySelectorAll('.altc.alt-top').length,strip:r.querySelectorAll('.bmk-adj').length,stripText:(r.querySelector('.bmk-adj')||{innerText:''}).innerText.replace(/\\s+/g,' ')}))}}""", sel)

    def check_adjusted_rows(self, sel):
        s = self.adj_summary(sel)
        for r in s["perRow"]:
            self.assertEqual(r["top"], 1, s); self.assertEqual(r["strip"], 1, s)
            self.assertIn("BEST ADJUSTED LINE", r["stripText"])
        return s

    def test_college_football_cards(self):
        self.pg.evaluate("SS('fb');setSub('fb','cfb')"); self.settle("#cfb-games-list", 20)
        t = self.text("#cfb-games-list")
        self.assertGreater(self.pg.evaluate("document.querySelectorAll('#cfb-games-list .gc').length"), 0)
        self.assertNotIn("REASONING", t); self.assertNotIn("BEST MARKET", t); self.assertNotIn("MODEL RECOMMENDED", t)
        s = self.check_adjusted_rows("#cfb-games-list")
        self.assertTrue(all(r["chips"] >= 1 for r in s["perRow"]))
        if "BEST UNADJUSTED MARKET" not in t:
            self.assertEqual(self.pg.evaluate("document.querySelectorAll('#cfb-games-list .bmk2:not(.bmk-adj)').length"), 0)

    def test_nfl_cards(self):
        self.pg.evaluate("SS('fb');setSub('fb','nfl')"); self.settle("#nfl2-games-list", 20)
        t = self.text("#nfl2-games-list")
        self.assertNotIn("REASONING", t); self.assertNotIn("BEST MARKET", t)
        self.check_adjusted_rows("#nfl2-games-list")

    def test_nhl_and_european_hockey_cards(self):
        for sub, sel in (("nhl", "#nhl-upcoming"), ("liiga", "#liiga-matches-list"), ("shl", "#shl-matches-list"), ("nla", "#nla-matches-list"), ("extraliga", "#extraliga-matches-list")):
            self.pg.evaluate(f"SS('hk');setSub('hk','{sub}')"); self.settle(sel, 15)
            t = self.text(sel)
            self.assertNotIn("BEST MARKET", t, sub); self.assertNotIn("MODEL RECOMMENDED", t, sub)
            self.check_adjusted_rows(sel)

    def test_a_soccer_card(self):
        html = self.pg.evaluate("""()=>{try{return _renderSocMatchCardRaw({home:'Arsenal',away:'Chelsea',date:'2026-12-30T17:30:00Z',ou:2.5,hML:-165,dML:+290,aML:+420,hAbbr:'ARS',aAbbr:'CHE',state:'pre'},'pl')}catch(e){return 'ERR '+e}}""")
        self.assertFalse(html.startswith("ERR"), html[:200])
        d = self.pg.evaluate("(h)=>{const d=document.createElement('div');d.innerHTML=h;return {t:d.innerText.replace(/\\s+/g,' '),rows:d.querySelectorAll('.altrow').length,chips:d.querySelectorAll('.altc').length,top:d.querySelectorAll('.altc.alt-top').length,strip:d.querySelectorAll('.bmk-adj').length}}", html)
        self.assertNotIn("MODEL RECOMMENDED LINE", d["t"]); self.assertNotIn("BEST MARKET", d["t"])
        self.assertEqual(d["rows"], 1); self.assertGreaterEqual(d["chips"], 2)
        self.assertEqual((d["top"], d["strip"]), (1, 1)); self.assertIn("BEST ADJUSTED LINE", d["t"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
