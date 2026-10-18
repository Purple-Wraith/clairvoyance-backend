#!/usr/bin/env python3
"""European hockey PP% / PK% / goalie SV% from QuantHockey in the model (owner, 2026-10-17): ingest, last/this-season blend, regression, capped nudges, switch, and real-data coverage.

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
        cls.pg.wait_for_function("typeof _hkQHAdjFor==='function'&&typeof loadShlScheduleData==='function'", timeout=90000)
        cls.pg.evaluate("Promise.all([loadLiigaScheduleData(),loadShlScheduleData(),loadNlaScheduleData(),loadExtraligaScheduleData()])")

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def test_blend_and_regression_match_the_formula(self):
        qh = {"teams": {}}
        def team(slug, gp, ppg_cur, ppg_prev):
            return {"name": slug, "slug": slug, "gp": gp, "ppo": 100, "ppg": ppg_cur, "pko": 100, "ppgAllowed": 20, "sv": 900, "svPct": .9,
                    "prev": {"gp": 52, "ppo": 100, "ppg": ppg_prev, "pko": 100, "ppgAllowed": 20, "sv": 900, "svPct": .9}}
        qh["teams"]["a"] = team("a", 10, 40, 20); qh["teams"]["b"] = team("b", 10, 20, 20); qh["teams"]["c"] = team("c", 10, 20, 20)
        got = self.pg.evaluate("q=>_hkQHLeague(q).bySlug.a.pp", qh)
        lg_cur = (40 + 20 + 20) / 300
        cur = (40 + 40 * lg_cur) / (100 + 40); prev = 0.20
        self.assertAlmostEqual(got, 0.6 * cur + 0.4 * prev, places=6)                   # 10 games: 60% this season on the NHL curve
        early = self.pg.evaluate("q=>{q.teams.a.gp=2;return _hkQHLeague(q).bySlug.a.pp}", json.loads(json.dumps(qh)))
        self.assertAlmostEqual(early, 0.1 * cur + 0.9 * prev, places=6)                  # 2 games: 10%
        full = self.pg.evaluate("q=>{q.teams.a.gp=20;return _hkQHLeague(q).bySlug.a.pp}", json.loads(json.dumps(qh)))
        self.assertAlmostEqual(full, cur, places=6)                                      # 20 games: this season only
        noprev = json.loads(json.dumps(qh)); del noprev["teams"]["a"]["prev"]
        self.assertAlmostEqual(self.pg.evaluate("q=>_hkQHLeague(q).bySlug.a.pp", noprev), (40 + 40 * (80 / 300)) / 140, places=3)   # promoted team: this season alone (league mean from the others)

    def test_every_real_team_in_all_four_leagues_has_data(self):
        r = self.pg.evaluate("""() => { const o = {};
          const cfg = {LIIGA: [_LIIGA_DATA, _LIIGA_QH_DATA], SHL: [_SHL_DATA, _SHL_QH_DATA], NLA: [_NLA_DATA, _NLA_QH_DATA], EXTRALIGA: [_EXTRALIGA_DATA, _EXTRALIGA_QH_DATA]};
          for (const [k, [D, qh]] of Object.entries(cfg)) { const miss = []; for (const t of Object.values(D.teams)) { const x = _hkQHTeamRates(qh, t.name); if (!x || x.pp == null || x.pk == null || x.sv == null) miss.push(t.name); } o[k] = miss; }
          return o; }""")
        self.assertEqual(r, {"LIIGA": [], "SHL": [], "NLA": [], "EXTRALIGA": []}, r)

    def test_nudges_are_capped_and_league_neutral(self):
        r = self.pg.evaluate("""() => { const out = {}; const cfg = {LIIGA: [_LIIGA_DATA, liigaMC], SHL: [_SHL_DATA, shlMC], NLA: [_NLA_DATA, nlaMC], EXTRALIGA: [_EXTRALIGA_DATA, extraligaMC]};
          for (const [k, [D, mc]] of Object.entries(cfg)) { const ids = Object.keys(D.teams); let on = 0, off = 0, n = 0, mx = 0;
            for (const a of ids) for (const b of ids) { if (a === b) continue; HK_QH_MODEL_ON = true; const x = mc(a, b, 6.5); HK_QH_MODEL_ON = false; const y = mc(a, b, 6.5); HK_QH_MODEL_ON = true;
              on += x.avgT; off += y.avgT; n++; mx = Math.max(mx, Math.abs(x.avgH / y.avgH - 1), Math.abs(x.avgA / y.avgA - 1)); }
            out[k] = {shift: (on - off) / n, maxRel: mx}; }
          return out; }""")
        for k, v in r.items():
            self.assertLess(abs(v["shift"]), 0.06, (k, v))                              # a league-average matchup moves ~nothing: terms are relative to the league mean
            self.assertLess(v["maxRel"], 0.05 * 1.04 + 0.04 * 1.05 + 0.02, (k, v))      # ST cap 5% x goalie cap 4%

    def test_switch_off_and_missing_snapshot_are_exactly_the_old_model(self):
        r = self.pg.evaluate("""() => { const ids = Object.keys(_SHL_DATA.teams); const [a, b] = ids;
          HK_QH_MODEL_ON = false; const off = shlMC(a, b, 6.5).avgT; HK_QH_MODEL_ON = true;
          const keep = _SHL_QH_DATA; _SHL_QH_DATA = null; const none = shlMC(a, b, 6.5).avgT; _SHL_QH_DATA = keep;
          const adj = _hkQHAdjFor('SHL', {name: 'No Such Club'}, _SHL_DATA.teams[b]);
          return {off, none, adj}; }""")
        self.assertEqual(r["off"], r["none"])
        self.assertEqual(r["adj"], {"h": 1, "a": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
