#!/usr/bin/env python3
"""MC PROJ bar on every match card (audit 2026-10-29): the bar lists the AWAY team first with each score tagged by its team (the card header reads "AWAY at HOME", the reasoning text and emails list the
away team first), and the margin reads as the named team's lead.  It used to print "home - away" with no names (a reader took 2.24 - 3.10 as Farjestad 2.24 - Brynas 3.10 when the model projects
Farjestad 3.10 - Brynas 2.24) and printed "-0.9 (Farjestad)" for a lead.

    python3 scripts/test_mc_card_display.py
"""
import functools, html as _html, http.server, re, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Bar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&nosw=1")
        cls.pg.wait_for_function("typeof _mcProjBarHTML==='function'&&typeof shlMC==='function'"); cls.pg.wait_for_timeout(2500)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def text(self, html):
        return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", html))).strip()

    def bar(self, h, a, hn, an, **o):
        return self.text(self.pg.evaluate("([mc,hn,an,o])=>_mcProjBarHTML(mc,'5.5',hn,an,o)", [{"avgH": h, "avgA": a, "avgT": h + a, "overP": .4}, hn, an, o]))

    def test_away_first_with_team_names(self):
        t = self.bar(2.24, 3.10, "Brynas", "Farjestad", dec=2)
        self.assertIn("Farjestad 3.10 – Brynas 2.24", t)

    def test_margin_is_a_plus_for_the_leading_team_whichever_side(self):
        self.assertIn("Margin +0.9 (Farjestad)", self.bar(2.24, 3.10, "Brynas", "Farjestad", dec=2))       # away leads: used to read -0.9
        self.assertIn("Margin +0.9 (Brynas)", self.bar(3.10, 2.24, "Brynas", "Farjestad", dec=2))          # home leads
        self.assertNotIn("Margin -", self.bar(2.24, 3.10, "Brynas", "Farjestad", dec=2))

    def test_total_overP_and_units_unchanged(self):
        t = self.bar(2.24, 3.10, "Brynas", "Farjestad", dec=2)
        self.assertIn("Total 5.3 vs 5.5 · 40% O", t)
        self.assertIn("pts", self.bar(110.2, 118.4, "DET", "BOS", dec=1, unit="pts"))

    def test_names_are_escaped(self):
        self.assertNotIn("<img", self.pg.evaluate("()=>_mcProjBarHTML({avgH:1,avgA:2,avgT:3},'5.5','<img src=x>','A&B',{})"))

    def test_a_real_card_lists_the_away_team_first_like_its_header_and_text(self):
        r = self.pg.evaluate("""()=>{const g=(_SHL_DATA.games||[]).find(x=>x.state==='pre'&&x.odds);if(!g)return null;
          const d=document.createElement('div');d.innerHTML=_shlMatchCardRaw(g);const mc=shlMC(g.home,g.away,g.ou);
          return {away:g.awayName,home:g.homeName,html:d.innerHTML,avgH:mc.avgH,avgA:mc.avgA}}""")
        if r is None:
            self.skipTest("no upcoming SHL game with odds in the schedule file")
        t = self.text(r["html"])
        i = t.find("PROJ (exact")
        self.assertGreater(i, 0)
        seg = t[i:i + 160]
        self.assertLess(seg.find(r["away"]), seg.find(r["home"]), seg)                     # away team named before the home team
        self.assertRegex(seg, re.escape(r["away"]) + r" \d\.\d\d – " + re.escape(r["home"]) + r" \d\.\d\d")


if __name__ == "__main__":
    unittest.main(verbosity=2)
