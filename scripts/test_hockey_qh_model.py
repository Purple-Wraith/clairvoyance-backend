#!/usr/bin/env python3
"""European hockey PP% / PK% / goalie SV% from QuantHockey as SEPARATE metrics (owner, 2026-10-17): ingest, last/this-season blend on the NHL curve, real-data coverage, the card panel -- and proof they never touch goals or probabilities.

    python3 scripts/test_hockey_qh_model.py
"""
import functools, http.server, json, sys, tempfile, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import quanthockey_ingest as Q  # noqa: E402

COMPACT = """## shl|2026-27
aa-team~10~30~300~10.0%~240~0.900~30~9~30.0%~30~6~80.0%
bb-new~10~20~280~7.0%~250~0.910~28~4~14.3%~28~7~75.0%
## shl|2025-26
aa-team~52~150~1400~10.7%~1200~0.900~150~36~24.0%~140~28~80.0%
old-gone~52~100~1300~7.0%~1300~0.900~140~20~14.0%~150~30~80.0%
"""


class Ingest(unittest.TestCase):
    def test_parse_and_merge_keeps_names_and_adds_last_season(self):
        d = Q.parse_compact(COMPACT)
        self.assertEqual(set(d), {("shl", "2026-27"), ("shl", "2025-26")})
        cur, prev = d[("shl", "2026-27")], d[("shl", "2025-26")]
        self.assertEqual(cur["aa-team"]["ppPct"], 30.0)
        out = Q.merge("shl", "2026-27", cur, prev, {"teams": {"aa-team": {"name": "AA Team", "slug": "aa-team", "pim": 99}}})
        a, b = out["teams"]["aa-team"], out["teams"]["bb-new"]
        self.assertEqual(a["name"], "AA Team"); self.assertNotIn("pim", a)                 # stale columns are dropped
        self.assertEqual(a["gPerGp"], 3.0); self.assertEqual(a["shotsPerGp"], 30.0)
        self.assertEqual(a["prev"]["gp"], 52); self.assertEqual(a["prev"]["svPct"], 0.9)
        self.assertNotIn("prev", b)                                                         # promoted / returning team: no last season
        self.assertNotIn("old-gone", out["teams"])                                         # a team that left the league is not carried

    def test_bad_row_is_refused(self):
        with self.assertRaises(ValueError):
            Q.parse_compact("## shl|2026-27\nonly~three~cells\n")


class Model(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        H = type("Q", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None})
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page()
        cls.pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1") else r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1", wait_until="domcontentloaded")
        cls.pg.wait_for_function("typeof _hkQHBlend==='function'&&typeof loadShlScheduleData==='function'", timeout=90000)
        cls.pg.evaluate("Promise.all([loadLiigaScheduleData(),loadShlScheduleData(),loadNlaScheduleData(),loadExtraligaScheduleData(),loadLiigaQuantHockeyData(),loadShlQuantHockeyData(),loadNlaQuantHockeyData(),loadExtraligaQuantHockeyData()])")

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def test_blend_uses_the_nhl_curve_and_degrades_without_last_season(self):
        t = lambda gp, prev=True: {"gp": gp, "ppPct": 30.0, "pkPct": 80.0, "svPct": 0.920, **({"prev": {"gp": 52, "ppPct": 24.0, "pkPct": 76.0, "svPct": 0.900}} if prev else {})}
        pp = lambda gp, prev=True: self.pg.evaluate("t=>_hkQHBlend(t).pp", t(gp, prev))
        self.assertAlmostEqual(pp(10), 0.6 * 30 + 0.4 * 24, places=6)                    # 10 games: 60% this season
        self.assertAlmostEqual(pp(2), 0.1 * 30 + 0.9 * 24, places=6)                     # 2 games: 10%
        self.assertAlmostEqual(pp(20), 30.0, places=6)                                   # 20 games: this season only
        self.assertAlmostEqual(pp(10, prev=False), 30.0, places=6)                       # promoted / returning team: this season alone
        b = self.pg.evaluate("t=>_hkQHBlend(t)", t(5))
        self.assertAlmostEqual(b["pk"], 0.3 * 80 + 0.7 * 76, places=6); self.assertAlmostEqual(b["sv"], 0.3 * 0.920 + 0.7 * 0.900, places=6)
        self.assertAlmostEqual(b["w"], 0.3, places=6)

    def test_every_real_team_in_all_four_leagues_has_data(self):
        r = self.pg.evaluate("""() => { const o = {};
          const cfg = {LIIGA: [_LIIGA_DATA, _LIIGA_QH_DATA], SHL: [_SHL_DATA, _SHL_QH_DATA], NLA: [_NLA_DATA, _NLA_QH_DATA], EXTRALIGA: [_EXTRALIGA_DATA, _EXTRALIGA_QH_DATA]};
          for (const [k, [D, qh]] of Object.entries(cfg)) { const miss = []; for (const t of Object.values(D.teams)) { const row = _hkQHStatsFor(t.name, qh), b = _hkQHBlend(row); if (!b || b.pp == null || b.pk == null || b.sv == null) miss.push(t.name); } o[k] = miss; }
          return o; }""")
        self.assertEqual(r, {"LIIGA": [], "SHL": [], "NLA": [], "EXTRALIGA": []}, r)

    def test_they_never_change_goals_or_probabilities(self):
        """PP% / PK% / SV% are separate metrics (owner, 2026-10-17): every projection must be identical with the QuantHockey snapshot present, absent, or wildly different."""
        r = self.pg.evaluate("""() => { const out = {}; const cfg = {LIIGA: [_LIIGA_DATA, liigaMC, '_LIIGA_QH_DATA'], SHL: [_SHL_DATA, shlMC, '_SHL_QH_DATA'], NLA: [_NLA_DATA, nlaMC, '_NLA_QH_DATA'], EXTRALIGA: [_EXTRALIGA_DATA, extraligaMC, '_EXTRALIGA_QH_DATA']};
          for (const [k, [D, mc, v]] of Object.entries(cfg)) { const ids = Object.keys(D.teams); const keep = eval(v);
            const snap = (a, b) => { const x = mc(a, b, 6.5); return [x.avgH, x.avgA, x.hwP, x.overP, x.rl15].join(','); };
            let same = true; const pairs = [[ids[0], ids[1]], [ids[2], ids[3]], [ids[4], ids[0]]];
            const base = pairs.map(p => snap(...p));
            eval(v + ' = null'); const none = pairs.map(p => snap(...p));
            const crazy = JSON.parse(JSON.stringify(keep)); Object.values(crazy.teams).forEach(t => { t.ppPct = 99; t.pkPct = 10; t.svPct = 0.99; });
            eval(v + ' = crazy'); const wild = pairs.map(p => snap(...p));
            eval(v + ' = keep');
            out[k] = {none: none.join('|') === base.join('|'), wild: wild.join('|') === base.join('|')}; }
          return out; }""")
        for k, v in r.items():
            self.assertEqual(v, {"none": True, "wild": True}, (k, v))

    def test_cards_show_the_separate_metrics_with_their_weights(self):
        html = self.pg.evaluate("""() => { const ids = Object.keys(_SHL_DATA.teams); const h = _SHL_DATA.teams[ids[0]], a = _SHL_DATA.teams[ids[1]];
          return _hkCompareRadarHTML('t_shl', h.name, a.name, 'SHL', h, a, _shlBlendedRates(h), _shlBlendedRates(a)); }""")
        for needle in ("SPECIAL TEAMS", "POWER PLAY %", "PENALTY KILL %", "TEAM SAVE %", "WEIGHTS", "this /", "do not feed goals for/against"):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
