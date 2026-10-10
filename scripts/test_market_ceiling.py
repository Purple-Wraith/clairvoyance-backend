#!/usr/bin/env python3
"""The non-moneyline confidence ceiling (MARKET_P_CEILING in docs/app.html, mirrored in scripts/market_ceiling.py): the clamp math, JS/Python parity, how it interacts with
tiers / lanes / alt lines / the CFB selection band / the lock net / the "why this pick" text, and that moneylines are untouched.

    python3 scripts/test_market_ceiling.py
"""
import functools
import http.server
import json
import socketserver
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import auto_lock_settle as A  # noqa: E402
import market_ceiling as M  # noqa: E402

LEAGUES = ["CFB", "NFL", "NBA", "NHL", "SHL", "LIIGA", "NLA", "EXTRALIGA", "PL", "LIGA", "CL", "SERIEA", "SOC_PL", "SOC_ITA", "SOC_CL", "", None, "MLB"]
MKTS = ["ML", "SPREAD", "OU", "PROP", "PL", "RL", "", "FOO"]
GRID = [0.0, 0.01, 0.05, 0.2, 0.38, 0.4, 0.5, 0.55, 0.6, 0.61, 0.62, 0.6201, 0.65, 0.67, 0.68, 0.6801, 0.7, 0.75, 0.8, 0.9, 0.95, 0.99, 1.0]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Math(unittest.TestCase):
    """The pure functions (Python mirror)."""

    def test_table_values_and_the_optimal_floor_rule(self):
        self.assertEqual(M.ceiling_for("CFB", "OU"), .62)
        self.assertEqual(M.ceiling_for("CFB", "SPREAD"), .62)
        self.assertEqual(M.ceiling_for("NFL", "PROP"), .62)
        self.assertEqual(M.ceiling_for("NHL", "OU"), .68)
        self.assertEqual(M.ceiling_for("SOC_PL", "SPREAD"), .68)
        self.assertEqual(M.ceiling_for("SHL", "PL"), .68)             # PL / RL are spreads
        self.assertIsNone(M.ceiling_for("CFB", "ML"))
        self.assertIsNone(M.ceiling_for("CFB", "FOO"))
        # no ceiling may sit below the OPTIMAL probability floor: a lower one would stop EVERY spread / O-U of that league from qualifying (and kill the alt-line locks)
        for c in [M.MARKET_P_CEILING["default"], *M.MARKET_P_CEILING["byLeague"].values()]:
            for v in c.values():
                self.assertGreaterEqual(v, M.OPTIMAL_P_FLOOR)
        # the generic ceiling is not below PREMIUM's floor either (.67): everything that could be PREMIUM before still can be
        self.assertGreaterEqual(M.MARKET_P_CEILING["default"]["OU"], .67)

    def test_clamp_is_symmetric_idempotent_monotone_and_leaves_moneyline_alone(self):
        for lg in LEAGUES:
            for mk in MKTS:
                prev = -1
                for p in GRID:
                    q = M.cap_prob(p, mk, lg)
                    self.assertEqual(M.cap_prob(q, mk, lg), q, (lg, mk, p))          # idempotent
                    self.assertGreaterEqual(q, prev - 1e-12)                          # monotone non-decreasing in p
                    prev = q
                    c = M.ceiling_for(lg, mk)
                    if c is None or not (0 < p < 1):
                        self.assertEqual(q, p, (lg, mk, p))
                    else:
                        self.assertLessEqual(q, c)
                        self.assertGreaterEqual(q, 1 - c)
                        self.assertAlmostEqual(M.cap_prob(1 - p, mk, lg), 1 - q, places=12)     # the other side of the market stays the complement
                        if 1 - c <= p <= c:
                            self.assertEqual(q, p)                                    # inside the band nothing moves

    def test_ev_rescale_keeps_the_odds(self):
        dec = 1.909
        e = .91 * dec - 1
        self.assertAlmostEqual(M.rescale_ev(e, .91, .62), .62 * dec - 1, places=9)
        self.assertAlmostEqual(M.rescale_ev(.123, .7, .7), .123, places=12)

    def test_the_wilson_rule(self):
        # a stream that really wins 55% at every stated level is supported only up to ~.55-.6
        import random
        rng = random.Random(1)
        rows = [{"p": rng.uniform(.5, .9), "y": 1 if rng.random() < .55 else 0} for _ in range(600)]
        c, tab = M.supported_ceiling(rows)
        self.assertIsNotNone(c)
        self.assertLess(c, .70)
        # a calibrated stream is supported all the way up
        ok = [{"p": .8, "y": 1 if i % 5 else 0} for i in range(200)]
        c2, _ = M.supported_ceiling(ok, lo=.55, hi=.85)
        self.assertGreaterEqual(c2, .80)
        lo, hi = M.wilson(55, 100)
        self.assertTrue(lo < .55 < hi)


class Browser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.pg = cls.browser.new_page()
        cls.errors = []
        cls.pg.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _capMktP==='function'&&typeof lockPick==='function'&&typeof _cfbGameCard==='function'&&typeof loadCFBData==='function'")
        cls.pg.wait_for_timeout(1500)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()

    def ev(self, js, arg=None):
        return self.pg.evaluate(js, arg) if arg is not None else self.pg.evaluate(js)

    # ── parity ────────────────────────────────────────────────────────────────────────────────────────────────────────
    def test_js_and_python_tables_and_functions_agree(self):
        js_table = self.ev("()=>JSON.parse(JSON.stringify(MARKET_P_CEILING))")
        self.assertEqual(js_table, M.MARKET_P_CEILING)
        cases = [[lg, mk, p] for lg in LEAGUES for mk in MKTS for p in GRID]
        got = self.ev("(cs)=>cs.map(([lg,mk,p])=>[_mktCeiling(lg,mk),_capMktP(p,mk,lg)])", cases)
        for (lg, mk, p), (c, q) in zip(cases, got):
            self.assertEqual(c, M.ceiling_for(lg, mk), (lg, mk))
            self.assertAlmostEqual(q, M.cap_prob(p, mk, lg), places=12, msg=(lg, mk, p))

    # ── _evalMkts: probability, EV, tier, lanes ───────────────────────────────────────────────────────────────────────
    def rows(self, league, specs):
        """specs: [(side, prob, dec, extra)] -> the _evalMkts rows."""
        return self.ev("""([lg,specs])=>{const nm=_evalMkts(specs.map(([side,prob,dec,x])=>Object.assign({label:side+' X',prob,dec,ml:'-110',side,evVal:ev(prob,dec)},x||{})),lg);
          return Object.fromEntries(nm.mkts.map(m=>[m.side,{p:m.prob,raw:m.rawProb,c:m.capCeil,ev:m.evVal,t:m.tierN,lane:!!m.hkLane,grade:_gradeName(m)}]))}""", [league, specs])

    def test_moneyline_is_never_touched(self):
        for lg in ("CFB", "NFL", "NHL", None):
            r = self.rows(lg, [("mlFav", .91, 1.12, None), ("mlDog", .09, 8.0, None), ("home", .93, 1.1, None), ("draw", .05, 20, None)])
            for side, p in (("mlFav", .91), ("mlDog", .09), ("home", .93), ("draw", .05)):
                self.assertEqual(r[side]["p"], p, (lg, side))
                self.assertIsNone(r[side]["raw"])
                self.assertIsNone(r[side]["c"])

    def test_cfb_over_under_and_spread_are_clamped_and_never_premium(self):
        r = self.rows("CFB", [("over", .91, 1.909, None), ("under", .09, 1.909, None), ("sprdFav", .85, 1.909, None), ("sprdDog", .15, 1.909, None)])
        self.assertEqual((r["over"]["p"], r["over"]["raw"], r["over"]["c"]), (.62, .91, .62))
        self.assertEqual(r["under"]["p"], .38)                                        # the other side is the complement
        self.assertEqual(r["sprdFav"]["p"], .62)
        for s in ("over", "sprdFav"):
            self.assertEqual(r[s]["t"], 2, s)                                          # OPTIMAL at most
            self.assertEqual(r[s]["grade"], "OPTIMAL")
            self.assertAlmostEqual(r[s]["ev"], .62 * 1.909 - 1, places=3)             # EV re-priced at the SAME odds
        self.assertEqual(r["under"]["t"], 0)

    def test_the_tier_is_monotone_in_the_raw_probability_and_the_clamp_cannot_be_walked_around(self):
        """Raw .50 .. .99 through the real _evalMkts: CFB tier never above OPTIMAL, never lower at a higher raw p; generic markets cap at .68 (still PREMIUM)."""
        ps = [i / 100 for i in range(50, 100)]
        cfb = [self.rows("CFB", [("over", p, 1.909, None)])["over"] for p in ps]
        gen = [self.rows(None, [("over", p, 1.909, None)])["over"] for p in ps]
        for rows, cap in ((cfb, .62), (gen, .68)):
            self.assertTrue(all(a["p"] <= cap + 1e-9 for a in rows))
            self.assertTrue(all(b["t"] >= a["t"] for a, b in zip(rows, rows[1:])), [x["t"] for x in rows])
        self.assertLessEqual(max(x["t"] for x in cfb), 2)
        self.assertEqual(max(x["t"] for x in gen), 3)

    def test_qualification_is_unchanged_the_ceiling_only_moves_football_premium_to_optimal(self):
        """For every raw probability, a CFB spread / O-U that qualified before (tier >= 2) still qualifies, and one that did not still does not."""
        for mk_side in ("over", "sprdFav"):
            for p in [i / 100 for i in range(40, 100)]:
                before = self.ev("([p,t])=>tier(p,ev(p,1.909),t)", [p, "OU" if mk_side == "over" else "SPREAD"])      # the pre-ceiling grading rule (tier() with its band re-pricing, no clamp)
                after = self.rows("CFB", [(mk_side, p, 1.909, None)])[mk_side]["t"]
                self.assertEqual(before >= 2, after >= 2, (mk_side, p, before, after))
                self.assertLessEqual(after, before, (mk_side, p))
                if before < 3:
                    self.assertEqual(after, before, (mk_side, p))                       # below PREMIUM nothing moves at all

    def test_market_anchored_hockey_rows_and_the_lane_are_untouched(self):
        """A hockey row whose probability is already blended with the real price (marketProb set) is exempt; the HIGH PROB lane keeps working on it."""
        anch = {"hk": True, "priceReal": True, "mktDec": 1.5, "mktMl": "-200", "mktEv": -.02, "priceSource": "market", "modelProb": .74, "marketProb": .66, "blendAlpha": .7}
        r = self.rows("NHL", [("plDog", .70, 1.5, anch), ("under", .72, 1.5, anch), ("mlFav", .70, 1.5, anch)])
        for s, p in (("plDog", .70), ("under", .72), ("mlFav", .70)):
            self.assertEqual(r[s]["p"], p)
            self.assertIsNone(r[s]["raw"])
        self.assertTrue(r["plDog"]["lane"])                                            # +1.5 dog at >= .65 on a real price: still HIGH PROB
        # ... and the same probability on a row with no market anchor (assumed price) IS clamped
        noanch = {"hk": True, "priceReal": False, "priceSource": "assumed"}
        r2 = self.rows("NHL", [("under", .85, 1.9, noanch)])
        self.assertEqual((r2["under"]["p"], r2["under"]["raw"]), (.68, .85))

    def test_alt_line_rows_are_exempt(self):
        r = self.rows("CFB", [("over", .69, 1.4, {"altLine": {"posted": 50.5, "line": 40.5}})])
        self.assertEqual(r["over"]["p"], .69)
        self.assertIsNone(r["over"]["raw"])

    def test_default_ceiling_applies_to_soccer_and_nba_style_rows(self):
        r = self.rows("SOC_PL", [("over", .95, 1.909, None), ("ahFav", .80, 1.909, None), ("home", .95, 1.2, None)])
        self.assertEqual((r["over"]["p"], r["ahFav"]["p"], r["home"]["p"]), (.68, .68, .95))

    # ── text, grades ──────────────────────────────────────────────────────────────────────────────────────────────────
    def test_why_this_pick_says_it_was_capped_and_never_claims_the_raw_number_as_the_pick(self):
        txt = self.ev("""()=>{const nm=_evalMkts([{label:'OVER 50.5',prob:.91,dec:1.909,ml:'-110',side:'over',evVal:ev(.91,1.909)},{label:'ZZ ML',prob:.8,dec:1.25,ml:'-400',side:'mlFav',evVal:ev(.8,1.25)}],'CFB');
          _attachReasoning(nm,null,null);return Object.fromEntries(nm.mkts.map(m=>[m.side,m.reasoning]))}""")
        self.assertIn("PICK: OVER 50.5", txt["over"])
        self.assertIn("62.0% win prob", txt["over"])
        self.assertIn("CAPPED at 62% (raw model 91.0%)", txt["over"])
        self.assertNotIn("91.0% win prob", txt["over"])
        self.assertNotIn("CAPPED", txt["mlFav"])
        self.assertIn("80.0% win prob", txt["mlFav"])

    def test_grade_label_and_colour_come_from_the_clamped_tier(self):
        r = self.rows("CFB", [("over", .95, 1.909, None)])["over"]
        self.assertEqual(r["grade"], ["SKIP", "LEAN", "OPTIMAL", "PREMIUM"][r["t"]])
        self.assertEqual(self.ev("()=>_gradeColVar('OPTIMAL')"), "var(--pc)")

    def test_alt_chip_posts_the_models_own_number_not_the_clamped_one(self):
        txt = self.ev("""()=>{const nm=_evalMkts([{label:'OVER 50.5',prob:.91,dec:1.909,ml:'-110',side:'over',evVal:ev(.91,1.909)},{label:'UNDER 50.5',prob:.09,dec:1.909,ml:'-110',side:'under',evVal:ev(.09,1.909)}],'CFB');
          const h=_altLineRowHTML('CFB',{home:'AAA',away:'BBB',date:'2026-10-11T18:00Z'},nm,false);const k=Object.keys(window._altReg).filter(x=>x.indexOf('AAA|BBB')>0)[0];
          return {html:h.length>0,postedProb:window._altReg[k].alt.postedProb,prob:window._altReg[k].alt.prob}}""")
        self.assertTrue(txt["html"])
        self.assertAlmostEqual(txt["postedProb"], .91, places=6)
        self.assertGreater(txt["prob"], .62)                                       # the adjusted-line probability is the empirical curve's, untouched by the ceiling

    # ── the lock net ──────────────────────────────────────────────────────────────────────────────────────────────────
    def test_lockpick_never_stores_more_than_the_ceiling_but_moneyline_and_alt_lines_pass_through(self):
        r = self.ev("""async()=>{saveP([]);const st=Date.now()+7200000;
          await lockPick('ZZA','ZZB','CFB','OVER 50.5',.91,'-110',1.909,'2026-10-11',undefined,'OU',{lockOrigin:'auto',startMs:st});
          await lockPick('ZZC','ZZD','CFB','ZZC ML',.91,'-300',1.33,'2026-10-11',undefined,'ML',{lockOrigin:'auto',startMs:st});
          await lockPick('ZZE','ZZF','CFB','OVER 40.5',.69,'-250',1.4,'2026-10-11',undefined,'OU',{lockOrigin:'auto',startMs:st,altLine:{posted:50.5,line:40.5,shift:10}});
          await lockPick('ZZG','ZZH','NHL','ZZG -1.5',.9,'-110',1.909,'2026-10-11','manual','SPREAD',{lockOrigin:'auto',startMs:st,modelProb:.9,marketProb:.7});
          await lockPick('ZZI','ZZJ','PL_SOC','OVER 2.5',.93,'-110',1.909,'2026-10-11',undefined,'OU',{lockOrigin:'auto',startMs:st});
          const out=getP().map(p=>({bt:p.betType,lg:p.league,p:p.winProb,pCap:p.pCap}));saveP([]);return out}""")
        by = {(x["lg"], x["bt"]): x for x in r}
        cfb_ou = sorted([x for x in r if x["lg"] == "CFB" and x["bt"] == "OU"], key=lambda x: x["p"])
        self.assertEqual(cfb_ou[0]["p"], .62)
        self.assertEqual(cfb_ou[0]["pCap"], {"raw": .91, "ceiling": .62})
        self.assertEqual(cfb_ou[1]["p"], .69)                                        # the alt-line pick keeps its curve probability
        self.assertIsNone(cfb_ou[1]["pCap"])
        self.assertEqual(by[("CFB", "ML")]["p"], .91)
        self.assertIsNone(by[("CFB", "ML")]["pCap"])
        self.assertEqual(by[("NHL", "SPREAD")]["p"], .9)                             # market-anchored: exempt
        self.assertEqual(by[("PL", "OU")]["p"], .68)                                 # soccer O/U: generic ceiling
        self.assertEqual(by[("PL", "OU")]["pCap"], {"raw": .93, "ceiling": .68})

    # ── end to end: the real CFB card ─────────────────────────────────────────────────────────────────────────────────
    def test_cfb_card_end_to_end_stated_probabilities_never_exceed_the_ceiling(self):
        r = self.ev("""async()=>{await loadCFBData();const D=_CFB_DATA;const games=[];Object.values(D.weeks).forEach(w=>(w||[]).forEach(g=>{if(g.state!=='post'&&games.length<16)games.push(g);}));
          window._autoLockLegs=[];const html=games.map(g=>_cfbGameCard(g));
          return {n:games.length,html:html.join('').length,legs:window._autoLockLegs.map(l=>l.markets.map(m=>({s:m.side,p:m.prob,raw:m.rawProb,c:m.capCeil,t:m.tierN,r:m.reasoning})))}}""")
        self.assertGreaterEqual(r["n"], 4)
        seen_capped = seen_ml = 0
        for mk in r["legs"]:
            for m in mk:
                if m["s"] in ("over", "under", "sprdFav", "sprdDog"):
                    self.assertLessEqual(m["p"], .62 + 1e-9, m)
                    self.assertGreaterEqual(m["p"], .38 - 1e-9, m)
                    self.assertLessEqual(m["t"], 2, m)
                    if m["raw"] is not None:
                        seen_capped += 1
                        self.assertNotEqual(m["raw"], m["p"])
                        self.assertEqual(m["c"], .62)
                        if m["p"] >= .62:
                            self.assertGreater(m["raw"], m["p"])
                            self.assertIn("CAPPED at 62%", m["r"])
                else:
                    seen_ml += 1
                    self.assertIsNone(m["raw"], m)
        self.assertGreater(seen_capped, 0)
        self.assertGreater(seen_ml, 0)
        self.assertEqual(self.errors, [])


class Python(unittest.TestCase):
    def leg(self, side, label, prob, tier=2, raw=None, ceil=None, **kw):
        d = {"side": side, "label": label, "prob": prob, "ml": "-110", "dec": 1.909, "tierN": tier, "evVal": prob * 1.909 - 1, "rawProb": raw, "capCeil": ceil, "priceSource": None,
             "modelProb": None, "marketProb": None, "blendAlpha": None, "hkLane": False, "reasoning": "PICK"}
        d.update(kw)
        return d

    def qual(self, markets, sport="CFB"):
        res = {"gameLegs": [{"sport": sport, "hA": "HOME", "awA": "AWAY", "markets": markets, "mcSummary": None, "best": None, "socFactors": None, "startMs": None}], "propLegs": []}
        return A.build_qualifying(res, now=0)

    def test_build_qualifying_carries_the_raw_probability_and_ceiling(self):
        q = self.qual([self.leg("over", "OVER 2.5", .68, raw=.93, ceil=.68)], sport="SOC_PL")
        self.assertEqual((q[0]["prob"], q[0]["rawProb"], q[0]["capCeil"]), (.68, .93, .68))
        # CFB: a raw .88 O/U is still dropped by the CFB selection band, exactly as before the ceiling existed ...
        self.assertEqual(self.qual([self.leg("over", "OVER 50.5", .62, raw=.88, ceil=.62)]), [])
        # ... one inside the band is kept and (CFB is an alt-line sport) re-priced from the empirical margin curve, with the model's own number in the audit trail
        q = self.qual([self.leg("over", "OVER 50.5", .62, raw=.70, ceil=.62)])
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["altLine"]["postedProb"], .70)
        self.assertGreater(q[0]["prob"], .62)

    def test_cfb_selection_band_still_judges_the_models_own_number(self):
        """Before the ceiling, a spread / O-U at >= .75 was excluded by CFB_NONML_BAND (.60-.75). With the stated p clamped to .62 it would have walked into the band: rawProb keeps it out."""
        ml = self.leg("mlFav", "HOME ML", .72, tier=3)
        out_of_band = self.leg("over", "OVER 50.5", .62, raw=.88, ceil=.62)
        in_band = self.leg("under", "UNDER 50.5", .62, raw=.70, ceil=.62)
        self.assertEqual([x["label"] for x in A._cfb_select([dict(ml, kind="GAME"), dict(out_of_band, kind="GAME")])], ["HOME ML"])
        # no moneyline: the O/U at raw .88 is excluded, the one at raw .70 is kept
        self.assertEqual(A._cfb_select([dict(out_of_band)]), [])
        self.assertEqual([x["label"] for x in A._cfb_select([dict(in_band)])], ["UNDER 50.5"])
        # rule A (ML >= .75 pairs with an in-band O/U) is unchanged, and a leg that was never clamped behaves exactly as before
        self.assertEqual([x["label"] for x in A._cfb_select([dict(ml, prob=.80), dict(in_band)])], ["HOME ML", "UNDER 50.5"])
        plain = self.leg("over", "OVER 44.5", .66)
        self.assertEqual([x["label"] for x in A._cfb_select([dict(plain)])], ["OVER 44.5"])
        self.assertEqual(A._cfb_select([dict(self.leg("over", "OVER 44.5", .80))]), [])

    def test_a_clamped_leg_that_qualified_still_qualifies(self):
        q = self.qual([self.leg("over", "OVER 2.5", .68, tier=3, raw=.93, ceil=.68), self.leg("home", "Home Win", .80, tier=3)], sport="SOC_PL")
        self.assertEqual(sorted(x["label"] for x in q), ["Home Win", "OVER 2.5"])
        q = self.qual([self.leg("over", "OVER 50.5", .62, tier=2, raw=.70, ceil=.62), self.leg("mlFav", "HOME ML", .80, tier=3)])
        self.assertEqual(len(q), 2)

    def test_alt_line_keeps_its_curve_probability_and_reports_the_models_number(self):
        leg = dict(self.leg("over", "OVER 50.5", .62, raw=.88, ceil=.62), kind="GAME", sport="CFB", hA="H", awA="A")
        alt = A._alt_shift_leg(leg)
        self.assertIsNotNone(alt)
        self.assertGreater(alt["prob"], .62)                                          # the ceiling does not touch the empirical alt-line probability
        self.assertEqual(alt["altLine"]["postedProb"], .88)                           # ... and the audit trail records the model's own number
        self.assertIn("rated this side 88.0%", alt["reasoning"])

    def test_lock_game_leg_passes_pcap_for_a_clamped_leg_and_nothing_for_others(self):
        class Page:
            def __init__(self):
                self.args = None

            def evaluate(self, js, arg=None):
                self.args = arg
                return "locked"
        leg = dict(self.leg("over", "OVER 50.5", .62, raw=.88, ceil=.62), kind="GAME", sport="CFB", hA="H", awA="A", startMs=None)
        pg = Page()
        old = A.start_guard
        A.start_guard = lambda *a, **k: (True, "", 999)
        try:
            A.lock_game_leg(pg, leg)
            self.assertEqual(pg.args["pCap"], {"raw": .88, "ceiling": .62})
            A.lock_game_leg(pg, dict(leg, rawProb=None, capCeil=None))
            self.assertIsNone(pg.args["pCap"])
            A.lock_game_leg(pg, dict(leg, altLine={"posted": 50.5}))
            self.assertIsNone(pg.args["pCap"])
            A.lock_game_leg(pg, dict(self.leg("mlFav", "H ML", .9, tier=3), kind="GAME", sport="CFB", hA="H", awA="A", startMs=None))
            self.assertIsNone(pg.args["pCap"])
        finally:
            A.start_guard = old


class Evidence(unittest.TestCase):
    def test_the_evidence_tool_runs_on_the_committed_ledger_and_the_table_is_not_looser_than_the_data_allow(self):
        rows = M.clean_nonml(M.load_ledger())
        self.assertGreater(len(rows), 500)
        g = M.groups(rows)
        # football: the data cannot support more than the OPTIMAL floor (this is why the football ceiling is at the floor and not higher)
        for name in ("CFB O/U", "CFB spread"):
            c, _ = M.supported_ceiling(g[name])
            self.assertIsNotNone(c)
            self.assertLessEqual(c, M.MARKET_P_CEILING["byLeague"]["CFB"]["OU"] + .02, name)
        # nothing in the book above the ceiling is documented as better than the ceiling claims
        c_else, _ = M.supported_ceiling(g["everything else (hockey, soccer, NBA props)"])
        self.assertLessEqual(M.MARKET_P_CEILING["default"]["OU"], (c_else or 0) + .03)
        # the headline numbers quoted in the app.html comment
        self.assertEqual(len(rows), len(g["ALL non-moneyline"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
