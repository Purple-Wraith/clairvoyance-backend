#!/usr/bin/env python3
"""NFL market anchor (NFL_MKT_ANCHOR in docs/app.html, mirrored in scripts/nfl_engine.py): the math, JS/Python parity, and the real card end to end -- the spread / O-U probability is anchored,
the moneyline is NOT, the audit-trail fields are written, and switching the anchor off restores the old path exactly.

    python3 scripts/test_nfl_engine_anchor.py        (Playwright: the page is loaded with ?nosb=1 and Google Fonts are aborted)
"""
import functools
import http.server
import json
import math
import socketserver
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import nfl_engine as NE  # noqa: E402


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


class Math(unittest.TestCase):
    def test_weights_ramp_with_games_and_stay_small(self):
        w = [NE.anchor_weight("margin", n) for n in range(0, 19)]
        self.assertEqual(w[0], 0.0)
        self.assertTrue(all(b > a for a, b in zip(w, w[1:])))                    # strictly more trust as the ratings mature
        self.assertAlmostEqual(w[3], 0.3 * 3 / 11, places=9)
        self.assertLess(w[4], 0.11)                                              # inside the audit's bootstrap upper bound (0.102) for the weeks the data cover
        self.assertLess(w[17], 0.21)
        wt = [NE.anchor_weight("total", n) for n in range(0, 19)]
        self.assertTrue(all(t < m for t, m in zip(wt[1:], w[1:])))               # the total gets even less of the model's voice
        self.assertLess(max(wt), 0.07)

    def test_off_means_the_raw_model(self):
        off = {"on": False, "margin": {"max": .3, "half": 8}, "total": {"max": .1, "half": 8}}
        self.assertEqual(NE.anchor_weight("margin", 4, off), 1.0)
        self.assertEqual(NE.anchor_weight("total", 0, off), 1.0)

    def test_anchored_value_is_a_convex_combination(self):
        self.assertEqual(NE.anchored(10.0, 3.0, 0.0), 3.0)
        self.assertEqual(NE.anchored(10.0, 3.0, 1.0), 10.0)
        self.assertAlmostEqual(NE.anchored(10.0, 3.0, 0.25), 4.75)
        self.assertEqual(NE.anchored(10.0, None, 0.25), 10.0)                    # no posted line: nothing to anchor to

    def test_a_huge_raw_disagreement_no_longer_prints_a_huge_probability(self):
        # BAL -11.5 with a raw model margin of +16.6 at home after 3 games (a real 2026-10-04 card): was a 'PREMIUM-looking' 63-86%, is now ~54%
        old = NE.spread_cover(16.6, 11.5, 3, anchor=False)
        new = NE.spread_cover(16.6, 11.5, 3)
        self.assertGreater(old, 0.6)
        self.assertLess(new, 0.56)
        self.assertGreater(new, 0.5)
        # mirror image (the road team with the same disagreement): the cover probability of the mirrored game is the complement
        self.assertAlmostEqual(NE.spread_cover(-16.6, -11.5, 3), 1 - new, places=9)

    def test_total_sigma_and_a_wild_total_projection(self):
        # CAR-DET: raw model total 60.0 vs a 51.5 line after 3 games: was OVER ~73-80% (sigma 10), is now ~51%
        old = NE.over_prob(60.0, 51.5, 3, anchor=False, sigma=10.0)
        new = NE.over_prob(60.0, 51.5, 3)
        self.assertGreater(old, 0.78)
        self.assertLess(new, 0.52)
        self.assertEqual(NE.NFL_SIGMA_TOTAL, 13.0)

    def test_no_game_has_a_rating_means_the_line_itself(self):
        self.assertAlmostEqual(NE.spread_cover(20.0, 3.5, 0), 0.5, places=9)
        self.assertAlmostEqual(NE.over_prob(60.0, 44.5, 0), 0.5, places=9)

    def test_lane_selector_is_the_legacy_tier_and_only_for_nfl_spread_ou(self):
        self.assertTrue(NE.lane_qualifies("NFL", "SPREAD", 2))
        self.assertTrue(NE.lane_qualifies("NFL", "OU", 3))
        self.assertFalse(NE.lane_qualifies("NFL", "OU", 1))
        self.assertFalse(NE.lane_qualifies("NFL", "ML", 3))
        self.assertFalse(NE.lane_qualifies("CFB", "SPREAD", 3))
        self.assertFalse(NE.lane_qualifies("NFL", "SPREAD", None))                # a card that did not anchor carries no legacyTierN


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
        cls.pg.set_default_timeout(120000)
        cls.pg.route("**/*", lambda r: r.abort() if ("fonts.googleapis" in r.request.url or "fonts.gstatic" in r.request.url) else r.continue_())
        cls.errors = []
        cls.pg.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _nflGameCard2==='function'&&typeof loadNFLData==='function'&&typeof lockPick==='function'")
        cls.pg.wait_for_timeout(1500)
        cls.pg.evaluate("async()=>{await loadNFLData();}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()

    def ev(self, js, arg=None):
        return self.pg.evaluate(js, arg) if arg is not None else self.pg.evaluate(js)

    FIXTURE = """()=>{const D=_NFL_DATA;
      D.standings.ZZH={wins:'3',losses:'0',ties:'0',differential:'+60',abbr:'ZZH'};
      D.standings.ZZA={wins:'0',losses:'3',ties:'0',differential:'-30',abbr:'ZZA'};
      D.standings.ZZN={wins:'0',losses:'0',ties:'0',differential:'0',abbr:'ZZN'};
      return true}"""

    def test_constants_and_weight_function_match_python(self):
        self.assertEqual(self.ev("()=>JSON.parse(JSON.stringify(NFL_MKT_ANCHOR))"), NE.NFL_MKT_ANCHOR)
        self.assertEqual(self.ev("()=>[_NFL_SIGMA_MARGIN,_NFL_SIGMA_TOTAL]"), [NE.NFL_SIGMA_MARGIN, NE.NFL_SIGMA_TOTAL])
        ns = [0, 0.5, 1, 2, 3, 4, 5, 8, 12, 16, 17, 25, None, -3]
        got = self.ev("(ns)=>ns.map(n=>[_nflAnchorW('margin',n),_nflAnchorW('total',n)])", ns)
        for n, (m, t) in zip(ns, got):
            self.assertAlmostEqual(m, NE.anchor_weight("margin", n), places=12, msg=n)
            self.assertAlmostEqual(t, NE.anchor_weight("total", n), places=12, msg=n)

    def test_nflmc_anchors_the_spread_and_total_but_not_the_moneyline(self):
        self.ev(self.FIXTURE)
        # ZZH 3-0 (+60 = +20/g) hosts ZZA 0-3 (-30 = -10/g): raw margin = 20 - (-10) + 1.8 HFA = 31.8; posted ZZH -3.5 (home margin 3.5), O/U 44.5; no team stats -> league total 44
        r = self.ev("""()=>{const g={id:'zz',home:'ZZH',away:'ZZA',spread:-3.5,overUnder:44.5,date:'2026-10-18T17:00Z'};
          const on=nflMC('ZZH','ZZA',200000,44.5,g);NFL_MKT_ANCHOR.on=false;const off=nflMC('ZZH','ZZA',200000,44.5,g);NFL_MKT_ANCHOR.on=true;
          return {on:{hw:on.hwP,m:on.avgMargin,t:on.avgTotal,over:on.overP,a:on.anchor,m0:on.margin0},off:{hw:off.hwP,m:off.avgMargin,t:off.avgTotal,over:off.overP,a:off.anchor}}}""")
        w_m, w_t = NE.anchor_weight("margin", 3), NE.anchor_weight("total", 3)
        self.assertAlmostEqual(r["on"]["m0"], 31.8, places=2)
        self.assertAlmostEqual(r["on"]["a"]["marginA"], NE.anchored(31.8, 3.5, w_m), places=2)
        self.assertAlmostEqual(r["on"]["a"]["totalA"], NE.anchored(44.0, 44.5, w_t), places=2)
        self.assertEqual((r["on"]["a"]["wM"], r["on"]["a"]["nMin"]), (round(w_m, 4), 3))
        self.assertAlmostEqual(r["on"]["m"], r["on"]["a"]["marginA"], delta=0.12)            # the simulated mean is the anchored one
        self.assertAlmostEqual(r["off"]["m"], 31.8, delta=0.12)                               # anchor off: the raw model, as before
        # the MONEYLINE probability is the raw one in both modes
        self.assertAlmostEqual(r["on"]["hw"], phi(31.8 / 13.5), delta=0.004)
        self.assertAlmostEqual(r["on"]["hw"], r["off"]["hw"], delta=0.004)
        self.assertFalse(r["off"]["a"]["margin"] or r["off"]["a"]["total"])

    def test_no_posted_line_or_no_games_means_nothing_to_anchor(self):
        self.ev(self.FIXTURE)
        r = self.ev("""()=>{const a=nflMC('ZZH','ZZA',20000,null,{id:'zz2',home:'ZZH',away:'ZZA',spread:null,overUnder:null,date:'2026-10-18T17:00Z'});
          const b=nflMC('ZZH','ZZN',20000,44.5,{id:'zz3',home:'ZZH',away:'ZZN',spread:-3.5,overUnder:44.5,date:'2026-10-18T17:00Z'});
          return {a:a.anchor,aM:a.avgMargin,b:b.anchor,bm0:b.margin0}}""")
        self.assertFalse(r["a"]["margin"] or r["a"]["total"])                                   # no line, no anchor: the raw model stands (and the card falls back to the old path)
        self.assertAlmostEqual(r["aM"], 31.8, delta=0.4)
        self.assertEqual(r["b"]["nMin"], 0)                                                     # ZZN has played nothing: weight 0 = the posted line itself
        self.assertAlmostEqual(r["b"]["marginA"], 3.5, places=2)

    def test_card_end_to_end_honest_probabilities_and_audit_fields(self):
        self.ev(self.FIXTURE)
        r = self.ev("""()=>{const D=_NFL_DATA;const g={id:'zz4',home:'ZZH',away:'ZZA',homeName:'ZZH',awayName:'ZZA',spread:-3.5,overUnder:44.5,date:'2026-10-18T17:00Z',state:'pre',venue:'x',city:'Nowhere',week:5,seasonType:2};
          window._autoLockLegs=[];_nflGameCard2(g);const on=window._autoLockLegs.find(l=>l.hA==='ZZH');
          NFL_MKT_ANCHOR.on=false;window._autoLockLegs=[];_nflGameCard2(g);const off=window._autoLockLegs.find(l=>l.hA==='ZZH');NFL_MKT_ANCHOR.on=true;
          const pick=l=>Object.fromEntries(l.markets.map(m=>[m.side,{p:m.prob,mp:m.modelProb,mk:m.marketProb,ba:m.blendAlpha,raw:m.rawProb,cap:m.capCeil,t:m.tierN,lt:m.legacyTierN,r:m.reasoning}]));
          return {on:pick(on),off:pick(off)}}""")
        on, off = r["on"], r["off"]
        w_m, w_t = NE.anchor_weight("margin", 3), NE.anchor_weight("total", 3)
        # anchored: the favourite's cover probability is the anchored-mean one (~.57), nowhere near the raw ~.95 the old path clamped to .62
        want = NE.spread_cover(31.8, 3.5, 3)
        self.assertAlmostEqual(on["sprdFav"]["p"], want, delta=0.01)
        self.assertAlmostEqual(on["sprdDog"]["p"], 1 - on["sprdFav"]["p"], places=3)
        self.assertLess(on["sprdFav"]["p"], 0.62)
        self.assertEqual(on["sprdFav"]["mk"], 0.5)
        self.assertAlmostEqual(on["sprdFav"]["ba"], round(1 - w_m, 3), places=3)
        self.assertAlmostEqual(on["sprdFav"]["mp"], 0.95, places=2)                             # the audit trail keeps the raw model number (clamped to .95)
        self.assertIsNone(on["sprdFav"]["raw"])                                                  # market-anchored rows are exempt from the 62% ceiling
        self.assertAlmostEqual(on["over"]["ba"], round(1 - w_t, 3), places=3)
        self.assertAlmostEqual(abs(on["over"]["p"] - 0.5), 0, delta=0.02)
        self.assertIn("Market-anchored", on["sprdFav"]["r"])
        # the OLD path (anchor off) is untouched: ceiling-clamped, no audit fields, no legacy selector
        self.assertEqual(off["sprdFav"]["p"], 0.62)
        self.assertEqual(off["sprdFav"]["cap"], 0.62)
        self.assertIsNone(off["sprdFav"]["mk"])
        self.assertIsNone(off["sprdFav"]["lt"])
        # legacy selector: the old funnel graded the favourite's cover OPTIMAL+; the anchored card shows it as the honest, low grade
        self.assertGreaterEqual(on["sprdFav"]["lt"], 2)
        self.assertLess(on["sprdFav"]["t"], 2)
        # the moneyline is identical in both modes (same probability, no audit fields)
        self.assertAlmostEqual(on["mlFav"]["p"], off["mlFav"]["p"], delta=1e-9)
        self.assertIsNone(on["mlFav"]["mk"])
        self.assertIsNone(on["mlFav"]["lt"])

    def test_the_real_slate_never_prints_a_spread_or_total_probability_far_from_even(self):
        r = self.ev("""async()=>{const D=_NFL_DATA;const games=[];Object.values(D.weeks).forEach(w=>(w||[]).forEach(g=>{if(g.state!=='post'&&g.seasonType!==1&&g.spread!=null&&games.length<30)games.push(g);}));
          window._autoLockLegs=[];games.forEach(g=>{try{_nflGameCard2(g)}catch(e){}});
          return {n:games.length,rows:window._autoLockLegs.map(l=>l.markets.filter(m=>/^(sprd|over|under)/.test(m.side)).map(m=>({s:m.side,p:m.prob,mk:m.marketProb,ba:m.blendAlpha})))}}""")
        self.assertGreaterEqual(r["n"], 4)
        seen = 0
        for rows in r["rows"]:
            for m in rows:
                if m["mk"] is not None:
                    seen += 1
                    self.assertLessEqual(abs(m["p"] - 0.5), 0.2, m)       # w <= .3 and |raw - line| capped by the 5-95% clamp: a stated cover probability is never an 85% claim again
                    self.assertGreater(m["ba"], 0.7)
        self.assertGreater(seen, 4)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
