#!/usr/bin/env python3
"""SIMULATOR tab (owner request 2026-10-12): the sim must use the engine's matchup-specific scoring, take the over/under into account (posted line + editable O/U LINE),
generate realistic scores (Poisson goals / total+margin normals), have no random jitter (seeded), and stay readable on a 390px phone.

Playwright, ?nosb=1 (Supabase never touched), every non-local host aborted (Google Fonts, ESPN), Date pinned to 2026-10-12 22:00 UTC so the committed schedule files' prices count as fresh.

    python3 scripts/test_simulator.py
"""
import datetime as dt, functools, http.server, json, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 10, 12, 22, 0, tzinfo=dt.timezone.utc)
FLAGS = "{injury:true,home:true,neutral:false,playoff:false,form:true,weather:false}"

# two different matchups per league; `line` is a common typed line so OVER% is comparable across the pair
PAIRS = {
    "nba": (("BOS", "LAL"), ("OKC", "WSH"), 228.5),
    "nhl": (("COL", "EDM"), ("BOS", "CHI"), 5.5),
    "nfl": (("Kansas City Chiefs", "Buffalo Bills"), ("Indianapolis Colts", "Tennessee Titans"), 47.5),
    "cfb": (("Ohio State Buckeyes", "Michigan Wolverines"), ("Alabama Crimson Tide", "Georgia Bulldogs"), 45.5),
    "pl": (("Arsenal", "Ipswich Town"), ("Brentford", "Everton"), 2.5),    # a top side at home to a promoted one vs two mid-table sides: a real strength gap whatever this week's ratings are (Arsenal v Liverpool vs Brentford v Everton landed 0.0002 apart after a data refresh)
    "liga": (("Real Madrid", "Barcelona"), ("Girona", "Getafe"), 2.5),
    "seriea": (("Inter Milan", "Napoli"), ("Genoa", "Cagliari"), 2.5),
}
ALL = list(PAIRS) + ["liiga", "shl", "nla", "extraliga"]
SPORT_LEAGUE = {"nba": ("basketball", "nba"), "nhl": ("hockey", "nhl"), "nfl": ("football", "nfl"), "cfb": ("football", "cfb"), "pl": ("soccer", "pl"), "liga": ("soccer", "liga"),
                "seriea": ("soccer", "seriea"), "liiga": ("hockey", "liiga"), "shl": ("hockey", "shl"), "nla": ("hockey", "nla"), "extraliga": ("hockey", "extraliga")}


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Sim(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()
        cls.pg = cls.new_page(1300)
        cls.pg.wait_for_timeout(5000)                       # let the app's async boot loads (calibration, odds) settle
        cls.pg.evaluate("Promise.all([loadLiigaScheduleData(),loadShlScheduleData(),loadNlaScheduleData(),loadExtraligaScheduleData()])")

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    @classmethod
    def new_page(cls, width, height=1000, pin_nba=False):
        ctx = cls.b.new_context(viewport={"width": width, "height": height}, locale="en-US")
        pg = ctx.new_page()
        pg.set_default_timeout(60000)
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith(f"http://127.0.0.1:{cls.srv.server_address[1]}/") else r.abort())
        if pin_nba:                                       # serve a schedule with a posted ATL-OKC 221.5 game, whatever the live file holds today (and across any refetch)
            def _pinned(route):
                import json
                j = json.loads((ROOT / "docs" / "nba_schedule.json").read_text())
                j["games"] = [x for x in j.get("games", []) if not (x.get("home") == "ATL" and x.get("away") == "OKC")] + [{"home": "ATL", "away": "OKC", "state": "pre", "ou": 221.5, "date": "2026-10-13T00:00Z", "day": "2026-10-12", "provider": "test"}]
                route.fulfill(status=200, content_type="application/json", body=json.dumps(j))
            pg.route("**/nba_schedule.json*", _pinned)
        pg.clock.set_fixed_time(NOW)
        pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1", wait_until="domcontentloaded")
        pg.wait_for_function("typeof runSimulator==='function'&&!!_NFL_DATA&&!!_CFB_DATA&&!!window._NBA_SCHED&&typeof _NHL_SCHEDULE_DATA!=='undefined'&&!!_NHL_SCHEDULE_DATA")
        return pg

    # helpers ---------------------------------------------------------------------------------------------------------------
    def sim(self, sport, home, away, line=None, posted="null", flags=FLAGS, N=20000, pg=None):
        """_simRun with an explicit posted-line object (null = none) so the numbers do not depend on today's slate."""
        pg = pg or self.pg
        ln = "null" if line is None else repr(line)
        return pg.evaluate("""() => { const r=_simRun(%r,%r,%r,%d,%s,{line:%s,posted:%s});
          const o={}; for(const k of ['hP','aP','dP','otP','hMean','aMean','hStd','aStd','totMean','totStd','modelT','center','line','lineSrc','overP','underP','pushP','hasModel','wMkt','margMean','blowoutP','seedKey'])o[k]=r[k];
          o.alt=r.alt; o.hasPosted=!!r.posted; return o; }""" % (sport, home, away, N, flags, ln, posted))

    def lg_teams(self, sport):
        return self.pg.evaluate("Object.values(({liiga:_LIIGA_DATA,shl:_SHL_DATA,nla:_NLA_DATA,extraliga:_EXTRALIGA_DATA})[%r].teams).sort((a,b)=>(b.gm||0)-(a.gm||0)).map(t=>t.name)" % sport)

    def pair(self, sport):
        if sport in PAIRS:
            return PAIRS[sport]
        t = self.lg_teams(sport)
        return ((t[0], t[1]), (t[-1], t[-2]), 5.5)       # highest-scoring (G/M) teams vs lowest-scoring teams: a real scoring-environment gap

    # (i) different matchups -> different numbers ------------------------------------------------------------------------------
    def test_i_two_matchups_in_each_league_differ(self):
        for sp in ALL:
            (h1, a1), (h2, a2), line = self.pair(sp)
            r1, r2 = self.sim(sp, h1, a1, line), self.sim(sp, h2, a2, line)
            self.assertTrue(r1["hasModel"] and r2["hasModel"], f"{sp}: team model missing {r1['hasModel']} {r2['hasModel']}")
            tol = 1.0 if sp in ("nba", "nfl", "cfb") else (0.1 if sp in ("liiga", "shl", "nla", "extraliga") else 0.15)
            self.assertGreater(abs(r1["totMean"] - r2["totMean"]), tol, f"{sp}: mean total identical {r1['totMean']} vs {r2['totMean']}")
            self.assertGreater(max(abs(r1["hP"] - r2["hP"]), abs(r1["overP"] - r2["overP"])), 0.01, f"{sp}: win% and OVER% both identical")
            if sp not in ("liiga", "shl", "nla", "extraliga"):
                self.assertGreater(abs(r1["hP"] - r2["hP"]), 0.003, f"{sp}: win% identical")   # seeded sims: any real matchup gap clears this; ratings drift with live data
                self.assertGreater(abs(r1["overP"] - r2["overP"]), 0.01, f"{sp}: OVER% at the same line identical {r1['overP']} vs {r2['overP']}")
            self.assertGreater(abs(r1["hMean"] - r2["hMean"]) + abs(r1["aMean"] - r2["aMean"]), tol, f"{sp}: team scores identical")

    def test_viii_old_behaviour_would_fail_the_matchup_test(self):
        """The old simulator drew every score from a per-sport constant table; the same predicate that passes above must fail on that behaviour."""
        out = self.pg.evaluate("""() => {
          // the old generator, verbatim in shape: constant means/sds per sport, matchup only nudges by (adjHP-.5)*mu*.3
          const SP={nfl:{muH:23,muA:20,sdH:9.5,sdA:9.5,ou:44}};
          function oldSim(adjHP,N){const sp=SP.nfl;let tot=0,over=0;for(let i=0;i<N;i++){const u1=Math.random()||1e-9,u2=Math.random()||1e-9,z1=Math.sqrt(-2*Math.log(u1))*Math.cos(2*Math.PI*u2),z2=Math.sqrt(-2*Math.log(u1))*Math.sin(2*Math.PI*u2);
            const hAdj=adjHP>.5?(adjHP-.5)*sp.muH*.3:0,aAdj=adjHP<.5?(.5-adjHP)*sp.muA*.3:0;const hs=Math.round(Math.max(0,sp.muH+hAdj+z1*sp.sdH)),as=Math.round(Math.max(0,sp.muA+aAdj+z2*sp.sdA));tot+=hs+as;if(hs+as>sp.ou)over++;}
            return {tot:tot/N,over:over/N};}
          // old NFL had NO team model: every matchup used adjHP = .5
          const a=oldSim(.5,20000),b=oldSim(.5,20000);
          return {dTot:Math.abs(a.tot-b.tot),dOver:Math.abs(a.over-b.over)};}""")
        self.assertLess(out["dTot"], 1.0)         # => the "totals differ by > 1" predicate used in test_i is False for the old behaviour
        self.assertLess(out["dOver"], 0.02)
        (h1, a1), (h2, a2), line = PAIRS["nfl"]
        r1, r2 = self.sim("nfl", h1, a1, line), self.sim("nfl", h2, a2, line)
        self.assertGreater(abs(r1["totMean"] - r2["totMean"]), 1.0)                       # new behaviour satisfies it

    def test_engine_win_probability_is_kept(self):
        # one evaluate so the engine state (async-loaded calibration etc.) is identical for both sides
        eng, sim = self.pg.evaluate("""() => { const f=%s,g=(s,h,a)=>_simRun(s,h,a,20000,f,{}).hP;
          return [[nbaEns('BOS','LAL').p, nhlEns('COL','EDM').p, nflEns('KC','BUF',null,{home:'KC',away:'BUF'}).p],
                  [g('nba','BOS','LAL'), g('nhl','COL','EDM'), g('nfl','Kansas City Chiefs','Buffalo Bills')]]; }""" % FLAGS)
        for e, s in zip(eng, sim):
            self.assertLess(abs(e - s), 0.03, (eng, sim))

    # (ii) the O/U line ----------------------------------------------------------------------------------------------------------
    def test_ii_raising_the_line_lowers_over_monotonically(self):
        for sp, h, a, base in [("nba", "BOS", "LAL", 224.5), ("nhl", "COL", "EDM", 4.0), ("nfl", "Kansas City Chiefs", "Buffalo Bills", 50.0), ("pl", "Arsenal", "Liverpool", 2.0)]:
            step = 0.5
            vals = [self.sim(sp, h, a, base + i * step)["overP"] for i in range(9)]
            for x, y in zip(vals, vals[1:]):
                self.assertGreaterEqual(x + 1e-12, y, f"{sp}: OVER% not monotone {vals}")
            self.assertGreater(vals[0] - vals[-1], 0.05, f"{sp}: line barely moves OVER% {vals}")
            r = self.sim(sp, h, a, base)
            self.assertAlmostEqual(r["overP"] + r["underP"] + r["pushP"], 1.0, places=6)

    def test_ii_typed_line_overrides_posted_and_blank_uses_posted_then_model(self):
        posted = "{line:221.5,src:'test',overDec:1.909,underDec:1.909,priceSrc:'assumed',hMLdec:null,aMLdec:null,dMLdec:null,hMLam:null,aMLam:null,game:{}}"
        typed = self.sim("nba", "ATL", "OKC", 230.5, posted)
        self.assertEqual((typed["line"], typed["lineSrc"]), (230.5, "typed"))
        blank = self.sim("nba", "ATL", "OKC", None, posted)
        self.assertEqual((blank["line"], blank["lineSrc"]), (221.5, "posted"))
        none = self.sim("nba", "ATL", "OKC", None, "null")
        self.assertEqual(none["lineSrc"], "model")
        self.assertEqual(none["line"], round(none["modelT"] * 2) / 2)                       # nearest half
        # the distribution is centered on the blend (50/50 for NBA), not on the typed line: typing a line only moves the threshold
        self.assertAlmostEqual(typed["totMean"], blank["totMean"], places=6)
        self.assertAlmostEqual(blank["center"], (blank["modelT"] + 221.5) / 2, places=4)
        self.assertLess(typed["overP"], blank["overP"])
        self.assertAlmostEqual(blank["wMkt"], 0.5)

    def test_ii_posted_line_pulls_the_total_toward_the_market_with_the_engines_weights(self):
        hp = "{line:%s,src:'test',overDec:1.909,underDec:1.909,priceSrc:'assumed',hMLdec:null,aMLdec:null,dMLdec:null,game:{}}"
        lo = self.sim("nhl", "BOS", "CHI", None, hp % "8.5")
        none = self.sim("nhl", "BOS", "CHI", None, "null")
        self.assertGreater(lo["center"], none["center"] + 1.0)                              # model total ~6.1, market 8.5 -> hockey alpha pulls it most of the way
        self.assertGreater(lo["wMkt"], 0.6)                                                  # HOCKEY_MKT_BLEND_ALPHA.OU .65 (+ early-season ramp)
        nfl = self.sim("nfl", "Kansas City Chiefs", "Buffalo Bills", None, hp % "44.5")
        self.assertAlmostEqual(nfl["totMean"], 44.5, delta=2.0)                              # NFL_MKT_ANCHOR.total: model weight <= 10% -> ~the line

    def test_ii_integer_line_shows_push(self):
        r = self.sim("nhl", "COL", "EDM", 5.0)
        self.assertGreater(r["pushP"], 0.04)                                              # a whole-number total really can tie (about 7-11% depending on the live team data the sim is seeded from; a hard 8% floor flaked on 2026-10-22)
        self.assertLess(r["pushP"], 0.25)
        h = self.sim("nhl", "COL", "EDM", 5.5)
        self.assertEqual(h["pushP"], 0)
        self.assertTrue(any(a["push"] > 0 for a in r["alt"]) and any(a["cur"] for a in r["alt"]))
        self.assertEqual(len(r["alt"]), 9)                                                   # line -2 .. +2 in half points

    # (iii) seeding -----------------------------------------------------------------------------------------------------------------
    def test_iii_identical_inputs_reproduce_exactly_and_different_inputs_differ(self):
        a, b = self.sim("nhl", "COL", "EDM", 5.5), self.sim("nhl", "COL", "EDM", 5.5)
        self.assertEqual(a, b)
        c = self.sim("nhl", "COL", "EDM", 5.5, N=10000)
        self.assertNotEqual(a["hMean"], c["hMean"])
        d = self.sim("nhl", "COL", "BOS", 5.5)
        self.assertNotEqual(a["totMean"], d["totMean"])
        # no Math.random() input: the form/rest jitter is gone (same flags -> same win%, run after run)
        w = [self.sim("nba", "BOS", "LAL")["hP"] for _ in range(3)]
        self.assertEqual(len(set(w)), 1)
        # Math.random is restored after a run
        self.assertTrue(self.pg.evaluate("(()=>{const r=Math.random;_simRun('nba','BOS','LAL',2000,%s,{});return Math.random===r;})()" % FLAGS))

    # (iv) realistic scores ----------------------------------------------------------------------------------------------------------
    def test_iv_goal_sports_integer_and_win_draw_loss_consistent_and_nfl_spread_realistic(self):
        for sp in ("nhl", "liiga", "pl", "liga"):
            (h, a), _, line = self.pair(sp)
            r = self.sim(sp, h, a, line)
            self.assertAlmostEqual(r["hMean"] * 20000, round(r["hMean"] * 20000), places=4)            # integer goals every sim
            self.assertAlmostEqual(r["aMean"] * 20000, round(r["aMean"] * 20000), places=4)
            self.assertAlmostEqual(r["hP"] + r["aP"] + r["dP"], 1.0, places=6)
            if sp in ("pl", "liga"):
                self.assertGreater(r["dP"], 0.15); self.assertLess(r["dP"], 0.35)
            else:
                self.assertEqual(r["dP"], 0)
                self.assertGreater(r["otP"], 0.06); self.assertLess(r["otP"], 0.30)
            self.assertLess(abs(r["totStd"] ** 2 - r["totMean"]), 0.6)                               # Poisson total: variance ~ mean
        nfl = self.sim("nfl", "Kansas City Chiefs", "Buffalo Bills", 47.5)
        self.assertTrue(11.0 <= nfl["totStd"] <= 15.5, nfl["totStd"])                                 # the engine's own 13.0 total sigma
        self.assertTrue(8.5 <= nfl["hStd"] <= 11.0, nfl["hStd"])
        nba = self.sim("nba", "BOS", "LAL", 228.5)
        self.assertTrue(16.5 <= nba["totStd"] <= 20.5, nba["totStd"])
        self.assertAlmostEqual(nba["hMean"] * 20000, round(nba["hMean"] * 20000), places=4)
        # margin sd = engine sigma: win prob and margin distribution agree (P(margin>0) is the win prob by construction)
        self.assertLess(abs(nfl["hP"] - self.pg.evaluate("nflEns('KC','BUF',null,{home:'KC',away:'BUF'}).p")), 0.03)

    def test_venue_modes_move_the_numbers_sensibly(self):
        base = self.sim("nba", "BOS", "LAL")
        neu = self.sim("nba", "BOS", "LAL", flags="{injury:true,home:false,neutral:true,playoff:false,form:true,weather:false}")
        po = self.sim("nba", "BOS", "LAL", flags="{injury:true,home:false,neutral:false,playoff:true,form:true,weather:false}")
        self.assertLess(neu["hP"], base["hP"])                    # home edge removed from the favourite
        self.assertGreater(po["hP"], base["hP"])                  # playoff mode amplifies the edge
        off = self.sim("nba", "BOS", "LAL", flags="{injury:true,home:false,neutral:false,playoff:false,form:true,weather:false}")
        self.assertAlmostEqual(off["hP"], neu["hP"], delta=0.02)    # Home Adv. unchecked == neutral (up to MC noise: the flag set seeds the run)
        h = self.sim("nhl", "COL", "EDM"); hn = self.sim("nhl", "COL", "EDM", flags="{injury:true,home:false,neutral:true,playoff:false,form:true,weather:false}")
        self.assertLess(hn["hP"], h["hP"])

    # (v) no NaN anywhere ------------------------------------------------------------------------------------------------------------
    def test_v_no_nan_or_undefined_in_any_sport_by_venue_mode(self):
        bad = []
        for sp in ALL:
            (h, a), _, _ = self.pair(sp)
            for venue in ("home", "neutral", "playoff"):
                flags = "{injury:true,home:%s,neutral:%s,playoff:%s,form:true,weather:false}" % tuple(str(venue == v).lower() for v in ("home", "neutral", "playoff"))
                txt = self.pg.evaluate("""() => { const el=document.createElement('div'); document.body.appendChild(el);
                  const r=_simRun(%r,%r,%r,3000,%s,{}); _simRender(el,%r,%r,%r,3000,r); const t=el.innerText+' '+el.innerHTML; el.remove(); return t; }""" % (sp, h, a, flags, sp, h, a))
                for w in ("NaN", "undefined", "Infinity", "null"):
                    if w in txt:
                        bad.append((sp, venue, w))
        self.assertEqual(bad, [])
        self.assertEqual(self.pg.errors, [])

    def test_no_team_model_is_labelled(self):
        txt = self.pg.evaluate("""() => { const el=document.createElement('div'); document.body.appendChild(el);
          const r=_simRun('cbb','Duke Blue Devils','Kansas Jayhawks',3000,%s,{}); _simRender(el,'cbb','Duke Blue Devils','Kansas Jayhawks',3000,r); const t=el.innerText; el.remove(); return [t,r.hasModel]; }""" % FLAGS)
        self.assertFalse(txt[1]); self.assertIn("NO TEAM MODEL", txt[0])
        txt2 = self.pg.evaluate("""() => { const el=document.createElement('div'); document.body.appendChild(el);
          const r=_simRun('nba','BOS','LAL',3000,%s,{}); _simRender(el,'nba','BOS','LAL',3000,r); const t=el.innerText; el.remove(); return t; }""" % FLAGS)
        self.assertIn("ENGINE-DERIVED", txt2); self.assertNotIn("NO TEAM MODEL", txt2)

    # performance ------------------------------------------------------------------------------------------------------------------------
    def test_20k_runs_are_fast(self):
        ms = self.pg.evaluate("""() => { const t=performance.now(); for(const [s,h,a] of [['nba','BOS','LAL'],['nhl','COL','EDM'],['nfl','Kansas City Chiefs','Buffalo Bills'],['pl','Arsenal','Liverpool']])_simRun(s,h,a,20000,%s,{}); return performance.now()-t; }""" % FLAGS)
        self.assertLess(ms, 3000, ms)

    # UI: the controls, history, and the phone layout -------------------------------------------------------------------------------------
    def drive(self, pg, sport, league, home, away, line=""):
        pg.select_option("#sim-sport", sport); pg.wait_for_timeout(250)
        pg.select_option("#sim-league", league); pg.wait_for_timeout(400)
        pg.select_option("#sim-home", home); pg.select_option("#sim-away", away)
        pg.fill("#sim-ou", line)
        pg.evaluate("document.getElementById('sim-results').innerHTML=''")
        pg.click("button:has-text('RUN SIMULATION')")
        pg.wait_for_selector("#sim-total-card", timeout=30000)

    def open_sim(self, pg):
        pg.click("#sbar .sp:has-text('SIM')")
        pg.wait_for_selector("#sim-ou")
        pg.wait_for_timeout(400)

    def test_ui_ou_input_posted_hint_and_history(self):
        pg = self.new_page(1300, pin_nba=True)
        self.open_sim(pg)
        self.assertEqual(pg.get_attribute("#sim-ou", "placeholder"), "auto")
        self.assertEqual(pg.get_attribute("#sim-ou", "step"), "0.5")
        pg.evaluate("localStorage.removeItem('sim_history')")
        self.drive(pg, "basketball", "nba", "ATL", "OKC")
        self.assertIn("POSTED O/U 221.5", pg.inner_text("#sim-ou-hint"))
        self.assertEqual(pg.inner_text("#sim-postedline"), "221.5")
        self.assertEqual(pg.inner_text("#sim-lineused"), "221.5")
        o1 = float(pg.inner_text("#sim-overp").strip("%"))
        self.drive(pg, "basketball", "nba", "ATL", "OKC", "230.5")
        self.assertEqual(pg.inner_text("#sim-lineused"), "230.5")
        self.assertEqual(pg.inner_text("#sim-postedline"), "221.5")           # the posted line is still shown next to the typed one
        o2 = float(pg.inner_text("#sim-overp").strip("%"))
        self.assertLess(o2, o1 - 10)
        self.drive(pg, "hockey", "nhl", "COL", "EDM", "5")                      # integer line -> PUSH box
        self.assertTrue(pg.locator("#sim-pushp").count() == 1)
        self.assertIn("No posted O/U", pg.inner_text("#sim-ou-hint"))
        h = pg.evaluate("JSON.parse(localStorage.getItem('sim_history'))")
        self.assertEqual(len(h), 3)
        self.assertEqual((h[0]["line"], h[0]["lineSrc"]), (5, "typed")); self.assertTrue(0 < h[0]["overP"] < 1); self.assertIn("tot", h[0])
        self.assertEqual(h[2]["line"], 221.5); self.assertEqual(h[2]["lineSrc"], "posted")
        txt = pg.inner_text("#sim-history-list")
        self.assertIn("O/U 221.5", txt); self.assertIn("O/U 5", txt); self.assertIn("% O", txt)
        self.assertEqual(pg.errors, [])
        pg.context.close()

    def test_vi_phone_390_no_overflow_and_readable_text(self):
        pg = self.new_page(390, 900)
        self.open_sim(pg)
        for sport, lg, h, a, ln in [("basketball", "nba", "ATL", "OKC", ""), ("hockey", "nhl", "NJD", "OTT", ""), ("football", "nfl", "Los Angeles Rams", "Buffalo Bills", "50.5"), ("soccer", "pl", "Arsenal", "Liverpool", "3")]:
            self.drive(pg, sport, lg, h, a, ln)
            pg.wait_for_timeout(300)
            m = pg.evaluate("""() => { const root=document.getElementById('sp-sim'),w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT),sizes=[];let n;
              while(n=w.nextNode()){if(!n.textContent.trim())continue;const e=n.parentElement;if(!e.getClientRects().length)continue;sizes.push(parseFloat(getComputedStyle(e).fontSize));}
              const over=[...root.querySelectorAll('*')].filter(e=>e.getClientRects().length&&e.getBoundingClientRect().right>document.documentElement.clientWidth+1&&!e.closest('#sbar')).length;
              return {sw:document.documentElement.scrollWidth,cw:document.documentElement.clientWidth,min:Math.min(...sizes),over:over,cols:getComputedStyle(document.getElementById('sim-controls-grid')).gridTemplateColumns.split(' ').length}; }""")
            self.assertLessEqual(m["sw"], m["cw"], (lg, m))
            self.assertEqual(m["over"], 0, (lg, m))
            self.assertGreaterEqual(m["min"], 11, (lg, m))
            self.assertIn(m["cols"], (2, 3), m)
        pg.context.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
