#!/usr/bin/env python3
"""COMPARE panel for NHL / SHL / Liiga / NLA / Extraliga (owner, 2026-10-17): STATS (head to head numbers + who leads) and FORM (last results, bubbles) tabs next to the radars.

    python3 scripts/test_hockey_compare.py
"""
import functools, http.server, threading, unittest
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


class Compare(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        H = type("Q", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None})
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch()
        cls.pg = cls.b.new_page(viewport={"width": 390, "height": 800})
        cls.errors = []
        cls.pg.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1") else r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1", wait_until="domcontentloaded")
        cls.pg.wait_for_function("typeof _cmpStatsHTML==='function'&&typeof loadShlScheduleData==='function'&&typeof _nhlCompareTabs==='function'&&typeof _NHL_SCHEDULE_DATA!=='undefined'&&!!_NHL_SCHEDULE_DATA&&Object.keys(NHL).length>20", timeout=90000)
        cls.pg.evaluate("Promise.all([loadLiigaScheduleData(),loadShlScheduleData(),loadNlaScheduleData(),loadExtraligaScheduleData(),loadLiigaQuantHockeyData(),loadShlQuantHockeyData(),loadNlaQuantHockeyData(),loadExtraligaQuantHockeyData()])")

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def test_stats_rows_lead_counts_and_edge_cases(self):
        rows = [
            {"lbl": "RECORD", "a": "5-2-1", "h": "4-4-0", "txt": 1},
            {"lbl": "GOALS FOR", "a": 3.5, "h": 2.5, "fmt": "x", "dir": 1, "scale": 1},
            {"lbl": "GOALS AGAINST", "a": 2.0, "h": 3.0, "fmt": "x", "dir": -1, "scale": 1},
            {"lbl": "PACE", "a": 6.0, "h": 5.0, "fmt": "x", "dir": 0, "scale": 1},
            {"lbl": "SAVE", "a": 0.9, "h": 0.9, "fmt": "x", "dir": 1, "scale": .02},
            {"lbl": "MISSING", "a": None, "h": 1.0, "fmt": "x", "dir": 1, "scale": 1},
        ]
        html = self.pg.evaluate("""r => { r.forEach(x => { if (x.fmt) x.fmt = v => v.toFixed(1); }); return _cmpStatsHTML('AWY', 'HOM', r, 'a note'); }""", rows)
        self.assertIn("AWY leads 2", html); self.assertIn("HOM leads 0", html); self.assertIn("1 even", html)      # GF + GA lead away; SAVE is even; PACE neutral and MISSING are not counted
        self.assertIn("5-2-1", html); self.assertNotIn("MISSING", html); self.assertIn("a note", html)
        empty = self.pg.evaluate("_cmpStatsHTML('A', 'H', [{lbl: 'X', a: null, h: null, fmt: String, dir: 1}], '')")
        self.assertIn("No comparable numbers", empty)
        esc = self.pg.evaluate("_cmpStatsHTML('<b>A</b>', 'H', [], '')")
        self.assertNotIn("<b>A</b>", esc)

    def test_results_are_newest_first_and_form_renders_bubbles(self):
        r = self.pg.evaluate("""() => { const G = [
          {state: 'post', home: 'X', away: 'Y', homeScore: 3, awayScore: 1, date: '2026-10-01T00:00Z', homeName: 'Xs', awayName: 'Ys'},
          {state: 'post', home: 'Y', away: 'X', homeScore: 2, awayScore: 4, date: '2026-10-03T00:00Z', homeName: 'Ys', awayName: 'Xs'},
          {state: 'pre', home: 'X', away: 'Z', homeScore: 0, awayScore: 0, date: '2026-10-05T00:00Z'},
          {state: 'post', home: 'Z', away: 'X', homeScore: 5, awayScore: 1, date: '2026-10-02T00:00Z', homeName: 'Zs', awayName: 'Xs'}];
          const last = _cmpResults(G, 'X', 5);
          return {dates: last.map(x => x.date), wins: last.map(x => x.win), venues: last.map(x => x.home), html: _cmpFormHTML('X', 'Y', last, _cmpResults(G, 'Y', 5), _cmpResults(G, 'X', 99), _cmpResults(G, 'Y', 99))}; }""")
        self.assertEqual(r["dates"], ["2026-10-03", "2026-10-02", "2026-10-01"])                      # newest first, the unplayed game is skipped
        self.assertEqual(r["wins"], [True, False, True]); self.assertEqual(r["venues"], [False, False, True])
        self.assertIn("mom-pip w", r["html"]); self.assertIn("mom-pip l", r["html"]); self.assertIn("WIN 4-2 at Ys", r["html"]); self.assertIn("data-tip=", r["html"]); self.assertIn("showDotTip(this)", r["html"]); self.assertIn("home 1-0 · road 1-1", r["html"])

    def test_hovering_a_bubble_shows_the_game(self):
        """Hover on the FORM bubbles has to open the same tooltip the Home bubbles use (reported not working 2026-10-18)."""
        pg = self.pg
        pg.evaluate("""() => { const ids = Object.keys(_SHL_DATA.teams); const h = _SHL_DATA.teams[ids[0]], a = _SHL_DATA.teams[ids[1]];
          document.getElementById('cmpbox')?.remove();
          const w = document.createElement('div'); w.id = 'cmpbox'; w.style.cssText = 'position:fixed;top:0;left:0;width:370px;background:#06010e;padding:8px;z-index:90000';
          w.innerHTML = _hkCompareRadarHTML('t_hv', h.name, a.name, 'SHL', h, a, _shlBlendedRates(h), _shlBlendedRates(a), ids[0], ids[1]); document.body.appendChild(w); _switchRadarTab('t_hv', 4); }""")
        pip = pg.locator("#cmpbox .mom-pip").first
        self.assertGreater(pip.count(), 0)
        pip.hover()
        pg.wait_for_selector("#dot-tip", timeout=3000)
        txt = pg.inner_text("#dot-tip")
        self.assertRegex(txt, r"\d{4}-\d{2}-\d{2} · (HOME|ROAD)"); self.assertRegex(txt, r"\d+-\d+"); self.assertRegex(txt, r"(WIN|LOSS)")
        pg.mouse.move(5, 790)
        pg.wait_for_selector("#dot-tip", state="detached", timeout=3000)
        pg.evaluate("document.getElementById('cmpbox')?.remove()")

    def test_every_league_card_panel_has_the_new_tabs(self):
        out = self.pg.evaluate("""() => { const o = {};
          const cfg = {SHL: [_SHL_DATA, _shlBlendedRates], LIIGA: [_LIIGA_DATA, _liigaBlendedRates], NLA: [_NLA_DATA, _nlaBlendedRates], EXTRALIGA: [_EXTRALIGA_DATA, _extraligaBlendedRates]};
          for (const [k, [D, br]] of Object.entries(cfg)) { const ids = Object.keys(D.teams), h = D.teams[ids[0]], a = D.teams[ids[1]];
            const html = _hkCompareRadarHTML('t_' + k, h.name, a.name, k, h, a, br(h), br(a), ids[0], ids[1]);
            o[k] = {stats: html.includes('>STATS<'), form: html.includes('>FORM<'), pp: html.includes('POWER PLAY %'), gf: html.includes('GOALS FOR /GAME'), lead: /leads \\d+/.test(html), pips: (html.match(/mom-pip/g) || []).length > 0}; }
          const t = _nhlCompareTabs('COL', 'EDM'); o.NHL = {labels: t.map(x => x.label), stats: t[0].svg.includes('POWER PLAY %') && t[0].svg.includes('OFFENSIVE-ZONE STARTS %') && /leads \\d+/.test(t[0].svg), pips: (t[1].svg.match(/mom-pip/g) || []).length > 0};
          return o; }""")
        for k in ("SHL", "LIIGA", "NLA", "EXTRALIGA"):
            self.assertEqual(out[k], {"stats": True, "form": True, "pp": True, "gf": True, "lead": True, "pips": True}, (k, out[k]))
        self.assertEqual(out["NHL"]["labels"], ["STATS", "FORM"]); self.assertTrue(out["NHL"]["stats"], out["NHL"]); self.assertTrue(out["NHL"]["pips"], out["NHL"])

    def test_teams_without_data_degrade_instead_of_throwing(self):
        out = self.pg.evaluate("""() => { const t = _nhlCompareTabs('XXX', 'YYY'); const e = _hkCompareExtraTabs(null, 'A', 'B', 'x', 'y', null, null, null, null, null, null, null, null);
          return {nhl: t.map(x => x.label), euro: e.map(x => x.label)}; }""")
        self.assertEqual(out["nhl"], ["STATS", "FORM"]); self.assertEqual(out["euro"], ["STATS", "FORM"])

    def test_phone_width_has_no_overflow_in_the_stats_tab(self):
        r = self.pg.evaluate("""() => { const h = _SHL_DATA.teams[Object.keys(_SHL_DATA.teams)[0]], a = _SHL_DATA.teams[Object.keys(_SHL_DATA.teams)[1]], ids = Object.keys(_SHL_DATA.teams);
          const w = document.createElement('div'); w.id = 'cmpbox'; w.style.cssText = 'position:fixed;top:0;left:0;width:370px;background:#06010e;padding:8px;z-index:99999';
          w.innerHTML = _hkCompareRadarHTML('t_ov', h.name, a.name, 'SHL', h, a, _shlBlendedRates(h), _shlBlendedRates(a), ids[0], ids[1]); document.body.appendChild(w);
          _switchRadarTab('t_ov', 3);
          return {sw: w.scrollWidth, cw: w.clientWidth}; }""")
        self.assertLessEqual(r["sw"], r["cw"] + 1, r)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
