#!/usr/bin/env python3
"""Soccer moneyline lock-time market reference (`mktRef`, 2026-10-31): every soccer ML pick locked while the card had real 3-way prices stores {p: de-vigged market probability, dec: market decimal price}
of its outcome, so the pick can later be judged against the real lock-time price (the 2026-10-31 PL moneyline investigation had to re-fetch ESPN odds). AUDIT ONLY: it changes no probability, grade,
EV or pick, and it is NOT `marketProb` (that field switches on the market-anchored rules).

    python3 scripts/test_soccer_mkt_ref.py
"""
from __future__ import annotations

import functools
import http.server
import os
import socketserver
import sys
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("CV_ROOT") or HERE.parent)
sys.path.insert(0, str(HERE))
import auto_lock_settle as A  # noqa: E402

PRICES = {"hML": -150, "dML": 260, "aML": 400}


def devig(*ml):
    dec = [1 + (100 / -m if m < 0 else m / 100) for m in ml]
    inv = [1 / d for d in dec]
    return [x / sum(inv) for x in inv], dec


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Python(unittest.TestCase):
    def leg(self, **kw):
        d = {"side": "home", "label": "Alpha FC Win", "prob": .70, "ml": "-110", "dec": 1.91, "tierN": 2, "evVal": .05, "priceSource": None, "modelProb": None, "marketProb": None,
             "blendAlpha": None, "hkLane": False, "reasoning": "PICK", "mktRef": {"p": .5917, "dec": 1.667}}
        d.update(kw)
        return d

    def qual(self, markets, sport="SOC_PL"):
        return A.build_qualifying({"gameLegs": [{"sport": sport, "hA": "Alpha FC", "awA": "Beta FC", "markets": markets, "mcSummary": None, "best": None, "socFactors": None, "startMs": None}], "propLegs": []}, now=0)

    def test_build_qualifying_carries_mktref_and_leaves_everything_else_alone(self):
        with_ref = self.qual([self.leg()])
        without = self.qual([self.leg(mktRef=None)])
        self.assertEqual(with_ref[0]["mktRef"], {"p": .5917, "dec": 1.667})
        self.assertIsNone(without[0]["mktRef"])
        a, b = dict(with_ref[0]), dict(without[0])
        a.pop("mktRef"), b.pop("mktRef")
        self.assertEqual(a, b)                                                       # nothing graded or priced differs
        self.assertIsNone(with_ref[0]["marketProb"])                                  # NOT the market-anchor field

    def test_lock_game_leg_passes_mktref_to_the_page(self):
        class Page:
            args = None

            def evaluate(self, js, arg=None):
                Page.args = arg
                Page.js = js
                return "locked"
        leg = dict(self.leg(), kind="GAME", sport="SOC_PL", hA="Alpha FC", awA="Beta FC", startMs=None)
        old = A.start_guard
        A.start_guard = lambda *a, **k: (True, "", 999)
        try:
            A.lock_game_leg(Page(), leg)
            self.assertEqual(Page.args["mktRef"], {"p": .5917, "dec": 1.667})
            self.assertIn("mktRef", Page.js)
            self.assertIn("...(mktRef ? { mktRef } : {})", Page.js)                  # lands in lockPick's extraMeta only when present
            A.lock_game_leg(Page(), dict(leg, mktRef=None))
            self.assertIsNone(Page.args["mktRef"])
        finally:
            A.start_guard = old


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
        for pat in ("**/*espn*/**", "**/*workers.dev*", "**/*open-meteo*/**", "**/*nominatim*/**"):
            cls.pg.route(pat, lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&nosw=1")
        cls.pg.wait_for_function("typeof _renderSocMatchCardRaw==='function'&&typeof lockPick==='function'&&typeof _socMarketBlend==='function'")
        cls.pg.wait_for_timeout(1200)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()

    def render(self, home, away, prices):
        """Render a PL card for a fake fixture; -> {markets:[{side,label,prob,mktRef,marketProb}], cache:{...}}"""
        return self.pg.evaluate("""([home,away,pr])=>{
          window._autoLockLegs=[];window._socMktRef={};
          const g=Object.assign({id:'t_'+home,home,away,hAbbr:'ALP',aAbbr:'BET',hScore:0,aScore:0,status:'pre',clock:'',date:new Date(Date.now()+6*3600e3).toISOString(),ou:2.5},pr);
          _renderSocMatchCardRaw(g,'pl');
          const e=window._autoLockLegs.find(l=>(l.markets||[]).some(m=>m.side==='home'));
          return {markets:e?e.markets.map(m=>({side:m.side,label:m.label,prob:m.prob,mktRef:m.mktRef,marketProb:m.marketProb,dec:m.dec})):null,cache:Object.assign({},window._socMktRef)}}""",
                                [home, away, prices])

    def test_card_marks_each_ml_leg_with_its_devigged_market_probability_and_price(self):
        out = self.render("Alpha FC", "Beta FC", PRICES)
        (ph, pd, pa), (dh, dd, da) = devig(PRICES["hML"], PRICES["dML"], PRICES["aML"])
        by = {m["side"]: m for m in out["markets"]}
        for side, p, d in (("home", ph, dh), ("draw", pd, dd), ("away", pa, da)):
            ref = by[side]["mktRef"]
            self.assertAlmostEqual(ref["p"], p, places=3, msg=side)
            self.assertAlmostEqual(ref["dec"], d, places=3, msg=side)
        self.assertAlmostEqual(sum(by[s]["mktRef"]["p"] for s in ("home", "draw", "away")), 1.0, places=3)
        for side in ("over", "under", "ahFav", "ahDog"):
            if side in by:
                self.assertIsNone(by[side]["mktRef"], side)                          # only the moneyline legs carry it
        for m in out["markets"]:
            self.assertIsNone(m["marketProb"], m["side"])                            # never the market-anchor field

    def test_card_probabilities_are_exactly_the_existing_blend_mktref_changes_nothing(self):
        """The stated probability of each ML leg is still _socMarketBlend's number (model .85 / market .15 then _socCal): the new field moves no probability."""
        out = self.render("Alpha FC", "Beta FC", PRICES)
        by = {m["side"]: m for m in out["markets"]}
        got = self.pg.evaluate("""(pr)=>{const mc=_soccerMC(_socExpectedGoals(_socXG('Alpha FC','pl'),_socXG('Beta FC','pl'),'pl','Alpha FC','Beta FC').hxg,_socExpectedGoals(_socXG('Alpha FC','pl'),_socXG('Beta FC','pl'),'pl','Alpha FC','Beta FC').axg,_SOC_MC_N);
          const b=_socMarketBlend(mc.hWin,mc.draw,mc.aWin,pr.hML,pr.dML,pr.aML);return [b.hP,b.dP,b.aP,b.blended]}""", PRICES)
        self.assertTrue(got[3])
        for side, v in zip(("home", "draw", "away"), got[:3]):
            self.assertAlmostEqual(by[side]["prob"], v, places=2, msg=side)         # the MC itself is exact now; places=2 only absorbs the injury-adjustment hook

    def test_no_market_prices_means_no_mktref_anywhere(self):
        out = self.render("Gamma FC", "Delta FC", {})
        self.assertTrue(out["markets"])
        for m in out["markets"]:
            self.assertIsNone(m["mktRef"], m["side"])
        self.assertEqual(out["cache"], {})
        out = self.render("Gamma FC", "Delta FC", {"hML": -150, "aML": 400})          # a missing draw price: no blend, no reference
        for m in out["markets"]:
            self.assertIsNone(m["mktRef"], m["side"])

    def test_lockpick_stores_mktref_from_the_auto_pipeline_and_from_the_manual_chips(self):
        out = self.pg.evaluate("""async(pr)=>{
          const st=Date.now()+7200000,day=new Date().toLocaleDateString('sv-SE',{timeZone:'America/Denver'});
          saveP([]);window._autoLockLegs=[];window._socMktRef={};
          // auto pipeline: extraMeta.mktRef
          await lockPick('Alpha FC','Beta FC','PL_SOC','Alpha FC Win',.71,'-150',1.667,day,undefined,'ML',{lockOrigin:'auto',startMs:st,mktRef:{p:.5917,dec:1.667}});
          // manual chips: the card caches the references, lockPick reads them by home|away|betOn
          const g=Object.assign({id:'m1',home:'Gamma FC',away:'Delta FC',hAbbr:'GAM',aAbbr:'DEL',hScore:0,aScore:0,status:'pre',clock:'',date:new Date(st).toISOString(),ou:2.5},pr);
          _renderSocMatchCardRaw(g,'pl');
          await lockPick('Gamma FC','Delta FC','PL_SOC','DRAW',.26,'+260',3.6,day);
          // a lock with nothing cached and nothing passed
          await lockPick('Eta FC','Theta FC','PL_SOC','Eta FC',.6,'-110',1.91,day);
          // an O/U lock never gets one
          await lockPick('Alpha FC','Beta FC','PL_SOC','OVER 2.5',.6,'-110',1.909,day,undefined,'OU',{lockOrigin:'auto',startMs:st});
          const o=getP().map(p=>({bet:p.betOn,bt:p.betType,mktRef:p.mktRef===undefined?'ABSENT':p.mktRef,winProb:p.winProb,marketProb:p.marketProb}));saveP([]);return o}""", PRICES)
        by = {x["bet"]: x for x in out}
        self.assertEqual(by["Alpha FC Win"]["mktRef"], {"p": .5917, "dec": 1.667})
        (_, pd, _), (_, dd, _) = devig(PRICES["hML"], PRICES["dML"], PRICES["aML"])
        self.assertAlmostEqual(by["DRAW"]["mktRef"]["p"], pd, places=3)
        self.assertAlmostEqual(by["DRAW"]["mktRef"]["dec"], dd, places=3)
        self.assertEqual(by["Eta FC"]["mktRef"], "ABSENT")                                    # no reference available: the key is simply not written
        self.assertEqual(by["OVER 2.5"]["mktRef"], "ABSENT")
        self.assertEqual(by["Alpha FC Win"]["winProb"], .71)                                  # the stored probability is untouched
        for x in out:
            self.assertIsNone(x["marketProb"])

    def test_no_page_errors(self):
        self.render("Alpha FC", "Beta FC", PRICES)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
