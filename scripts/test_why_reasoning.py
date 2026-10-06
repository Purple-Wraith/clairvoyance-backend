#!/usr/bin/env python3
"""'Why this pick' reasoning (docs/app.html _attachReasoning / _mcLineMargin / _altReasoning / _whyThisPickHTML and scripts/auto_lock_settle.py _alt_finish):
adjusted-line picks describe the SHIFTED line (never the posted line's tier/EV), JS and Python write identical text, the projected margin is signed for the FAVORITE
(it printed the home-signed value next to an away favorite), assumed prices are labelled, and the adjusted-lines tracker shows the WHY row.

    python3 scripts/test_why_reasoning.py
"""
import functools, http.server, socketserver, sys, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import auto_lock_settle as A  # noqa: E402

POSTED_REASONING = ("PICK: BAL -11.5 — OPTIMAL (63.5% win prob, EV +21.2%)\n"
                    "MODEL: Projected margin +14.1 (BAL), total 44.2 — BAL covers the 11.5-pt line in 63.5% of simulated outcomes, from a 25k-sim Monte Carlo.\n"
                    "WHY: BAL is the stronger side.")


def nfl_leg():
    return {"kind": "GAME", "sport": "NFL", "hA": "BAL", "awA": "CLE", "side": "sprdFav", "label": "BAL -11.5", "prob": .635, "ml": "-110", "dec": 1.909, "tierN": 2,
            "evVal": .212, "reasoning": POSTED_REASONING}


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class PythonSide(unittest.TestCase):
    def test_adjusted_pick_describes_the_shifted_line_not_the_posted_one(self):
        r = A._alt_shift_leg(nfl_leg())
        txt = r["reasoning"]
        self.assertTrue(txt.startswith("PICK: BAL -5.5 — ADJUSTED LINE (67.3% estimated"))
        for gone in ("OPTIMAL", "EV +21.2", "11.5-pt line in", "63.5% win prob"):
            self.assertNotIn(gone, txt)
        self.assertIn("estimate", txt)
        self.assertIn("MODEL: Projected margin +14.1 (BAL), total 44.2, from a 25k-sim Monte Carlo.", txt)    # the projection survives, the posted cover claim does not
        self.assertIn("rated this side 63.5%", txt)

    def test_hockey_uses_goals_and_a_missing_model_line_is_fine(self):
        leg = {"kind": "GAME", "sport": "NHL", "hA": "A", "awA": "B", "side": "over", "label": "OVER 6.5", "prob": .62, "ml": "-110", "dec": 1.909, "tierN": 2, "evVal": .03}
        r = A._alt_shift_leg(leg)
        if r:                                                      # (a pick that already clears the floor keeps its posted line)
            self.assertIn("goal", r["reasoning"])
            self.assertNotIn("MODEL:", r["reasoning"])


class JsSide(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.pg = cls.browser.new_page(viewport={"width": 1100, "height": 900})
        cls.errors = []
        cls.pg.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _altReasoning==='function'&&typeof _mcLineMargin==='function'&&typeof saveP==='function'")
        cls.pg.wait_for_timeout(800)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def test_alt_reasoning_is_identical_in_js_and_python(self):
        model = "MODEL: Projected margin +14.1 (BAL), total 44.2, from a 25k-sim Monte Carlo."
        for label, p, posted_label, posted_p, k, unit, ml in (("BAL -5.5", .673, "BAL -11.5", .635, 6, "pt", model), ("UNDER 233.5", .67, "UNDER 225.5", .6, 8, "pt", None),
                                                              ("MIA +13.5", .682, "MIA +7.5", None, 6.5, "pt", None), ("OVER 5.5", .61, "OVER 6.5", .58, 1, "goal", None),
                                                              ("OVER 4.5", .64, "OVER 6.5", .5, 2, "goal", None)):
            py = A._alt_reasoning(label, p, posted_label, posted_p, k, unit, ml)
            js = self.pg.evaluate("([a,b,c,d,e,f,g])=>_altReasoning(a,b,c,d,e,f,g)", [label, p, posted_label, posted_p, k, unit, ml])
            self.assertEqual(js, py, label)

    def test_projected_margin_is_signed_for_the_favorite(self):
        mc = {"avgMargin": 3.0, "avgTotal": 41.0}                                             # HOME-signed: the home team is +3
        away_fav = self.pg.evaluate("(mc)=>_mcLineMargin(mc,'sprdFav','DET','GB',2.5,.55,.5,41.5,-3.0)", mc)   # the favorite is the AWAY team: favorite-signed margin is -3
        self.assertIn("Projected margin -3.0 (DET)", away_fav)
        home_fav = self.pg.evaluate("(mc)=>_mcLineMargin(mc,'sprdFav','GB','DET',2.5,.55,.5,41.5,3.0)", mc)
        self.assertIn("Projected margin +3.0 (GB)", home_fav)
        legacy = self.pg.evaluate("(mc)=>_mcLineMargin(mc,'sprdFav','GB','DET',2.5,.55,.5,41.5)", mc)            # no favorite-signed value passed: old behaviour
        self.assertIn("Projected margin +3.0 (GB)", legacy)

    def test_assumed_price_is_labelled_but_moneylines_and_market_prices_are_not(self):
        nm = self.pg.evaluate("""()=>{
          const nm={mkts:[{label:'BAL -5.5',side:'sprdFav',tierN:2,prob:.62,evVal:.07},{label:'BAL ML',side:'mlFav',tierN:2,prob:.7,evVal:.04},
                          {label:'OVER 5.5',side:'over',tierN:2,prob:.6,evVal:.05,priceSource:'market',mktEv:.02,mktMl:-120}]};
          _attachReasoning(nm,null,null);return nm.mkts.map(m=>m.reasoning)}""")
        self.assertIn("EV assumes a standard -110 price, not a market quote", nm[0])
        self.assertNotIn("assumes a standard", nm[1])
        self.assertNotIn("assumes a standard", nm[2])

    def test_high_prob_lane_pick_is_labelled_as_such(self):
        out = self.pg.evaluate("""()=>{const nm={mkts:[{label:'Karpat ML',side:'mlFav',tierN:0,hkLane:true,prob:.7,evVal:-.02},{label:'Ilves ML',side:'mlFav',tierN:0,prob:.7,evVal:-.02}]};
          _attachReasoning(nm,null,null);return nm.mkts.map(m=>m.reasoning.split('\\n')[0])}""")
        self.assertIn("— HIGH PROB (", out[0])
        self.assertIn("— SKIP (", out[1])
        html = (ROOT / "docs" / "app.html").read_text()
        self.assertNotIn("unpriced by a flat moneyline", html)

    def _manual_lock(self, call):
        return self.pg.evaluate("""(call)=>{
          window._reasoningCache={'BOS|NYK|OU':[{label:'OVER 225.5',reasoning:'PICK: OVER 225.5 — OPTIMAL (62%)\\nMODEL: Projected margin +4.0 (BOS), total 231.0 — OVER hits 62.0% of simulated outcomes vs the 225.5 line, from a 25k-sim Monte Carlo.'}],
                                  'BOS|NYK|SPREAD':[{label:'BOS -4.5',reasoning:'PICK: BOS -4.5 — OPTIMAL (60%)\\nMODEL: Projected margin +6.0 (BOS), total 231.0, from a 25k-sim Monte Carlo.'}]};
          let got=null;const orig=window.lockPick;window.lockPick=function(){got=[...arguments];};
          let inp=document.getElementById('tstInp');if(!inp){inp=document.createElement('input');inp.id='tstInp';document.body.appendChild(inp);}
          try{eval(call.js);}finally{window.lockPick=orig;}
          return got?{label:got[3],p:got[4],meta:got[10]||null}:null}""", call)

    def test_manual_lock_with_an_edited_line_stores_reasoning(self):
        r = self._manual_lock({"js": "document.getElementById('tstInp').value='222.5';_ouLockDir('BOS','NYK','2026-10-05',.62,225.5,'tstInp','OVER','NBA')"})
        self.assertEqual(r["label"], "OVER 222.5")
        txt = r["meta"]["reasoning"]
        self.assertIn("MANUAL LINE", txt)
        self.assertIn("changed by hand from OVER 225.5 to OVER 222.5", txt)
        self.assertIn("not a market quote", txt)
        self.assertIn("MODEL: Projected margin +4.0 (BOS), total 231.0, from a 25k-sim Monte Carlo.", txt)      # the card's projection survives, its posted-line cover % does not
        self.assertNotIn("vs the 225.5 line", txt)
        r = self._manual_lock({"js": "document.getElementById('tstInp').value='-7';_spreadLockDir('BOS','NYK','BOS','NYK','2026-10-05',.6,4.5,'tstInp','FAV','NBA')"})
        self.assertEqual(r["label"], "BOS -7.5")
        self.assertIn("changed by hand from BOS -4.5 to BOS -7.5", r["meta"]["reasoning"])

    def test_manual_lock_with_the_cards_own_line_is_unchanged(self):
        r = self._manual_lock({"js": "document.getElementById('tstInp').value='225.5';_ouLockDir('BOS','NYK','2026-10-05',.62,225.5,'tstInp','OVER','NBA')"})
        self.assertEqual(r["label"], "OVER 225.5")
        self.assertIsNone(r["meta"])                                    # no override: lockPick's own cache lookup supplies the card's reasoning as before

    def test_generated_props_carry_reasoning_and_lock_with_it(self):
        out = self.pg.evaluate("""()=>{
          const stats={'Jalen Brunson':{name:'Jalen Brunson',team:'NY',ppg:27.6,rpg:3.4,apg:6.8,gp:70,last5:{ppg:31,rpg:4,apg:7,n:5},stdev:{pts:7.1,reb:1.9,ast:2.4}},
                       'Derrick White':{name:'Derrick White',team:'BOS',ppg:16.2,rpg:4.5,apg:4.9,gp:65}};
          const props=_generateNBAProps([{h:'BOS',a:'NY'}],stats);
          const p=props.find(x=>x.player==='Jalen Brunson'&&x.statAbbr==='PTS');
          LOCKED_PROPS.length=0;
          lockProp(p.team,p.player,String(p.line),p.over,p.prob,p.ml,'NBA',p.opp,p.statAbbr);                       // the LOCK button's call: no reasoning passed
          const exact=LOCKED_PROPS.slice(-1)[0];
          lockProp(p.team,p.player,String(p.line+2),p.over,p.prob,p.ml,'NBA',p.opp,p.statAbbr);                     // line edited by hand
          const edited=LOCKED_PROPS.slice(-1)[0];
          return {n:props.length,reasoning:p.reasoning,exact:exact.reasoning,edited:edited.reasoning,line:p.line,over:p.over}}""")
        r = out["reasoning"]
        self.assertGreater(out["n"], 0)
        self.assertTrue(r.startswith("PICK: Jalen Brunson "), r)
        for part in ("MODEL: projection", "season average 27.6", "last-5 form 31", "5k-sim Monte Carlo", "WHY: Jalen Brunson averages", "PRICE:", "no edge or EV is claimed"):
            self.assertIn(part, r)
        self.assertEqual(out["exact"], r)                                      # the LOCK button path finds it through the registry
        self.assertIn("LINE EDITED: the line was changed by hand from", out["edited"])
        self.assertNotIn("MODEL: projection", out["edited"])                   # the old line's projection is not carried onto a different line

    def test_nhl_props_register_and_lock_with_reasoning(self):
        out = self.pg.evaluate("""()=>{
          const stats={'Connor McDavid':{name:'Connor McDavid',team:'EDM',ppg:1.35,gpg:0.55,apg:0.8,gp:60}};
          const props=_generateNHLPropsLive([{h:'EDM',a:'CGY'}],stats);
          const p=props.find(x=>x.player==='Connor McDavid'&&x.stat==='POINTS');
          LOCKED_PROPS.length=0;
          lockNHLProp(p.player,p.stat,p.over?'OVER':'UNDER',p.prob,p.ml,p.line);
          return {has:!!p,reasoning:p&&p.reasoning,locked:LOCKED_PROPS.slice(-1)[0]&&LOCKED_PROPS.slice(-1)[0].reasoning}}""")
        self.assertTrue(out["has"])
        self.assertIn("MODEL: projection", out["reasoning"])
        self.assertEqual(out["locked"], out["reasoning"])

    def test_python_prop_lock_passes_the_reasoning_through(self):
        calls = []

        class FakePage:
            def evaluate(self, js, arg=None):
                calls.append((js, arg))
                return "locked"
        A.lock_prop_leg(FakePage(), "NBA", {"team": "NY", "player": "X", "line": 12.5, "over": True, "prob": .6, "ml": "-110", "opp": "BOS", "statAbbr": "REB", "reasoning": "PICK: x"})
        A.lock_prop_leg(FakePage(), "NHL", {"player": "Y", "stat": "POINTS", "over": False, "prob": .6, "ml": "-110", "line": 0.5, "reasoning": "PICK: y"})
        self.assertEqual(calls[0][1]["reasoning"], "PICK: x")
        self.assertEqual(calls[1][1]["reasoning"], "PICK: y")
        self.assertIn("reasoning", calls[0][0])

    def test_tracker_list_shows_the_why_row_and_no_stale_nba_spread_claim(self):
        picks = [{"id": "w1", "sport": "NFL", "betType": "SPREAD", "betOn": "BAL -5.5", "hA": "BAL", "awA": "CLE", "date": "2026-10-04", "lockedAt": 1790000000000,
                  "outcome": "pending", "winProb": .67, "decOdds": 1.39, "ml": "-257", "priceSource": "estimated", "lockOrigin": "auto",
                  "altLine": {"posted": -11.5, "line": -5.5, "shift": 6, "postedLabel": "BAL -11.5"}, "reasoning": "PICK: BAL -5.5 — ADJUSTED LINE <b>x</b>"},
                 {"id": "w2", "sport": "NFL", "betType": "OU", "betOn": "UNDER 50.5", "hA": "A", "awA": "B", "date": "2026-10-03", "lockedAt": 1789990000000,
                  "outcome": "pending", "winProb": .67, "decOdds": 1.39, "ml": "-257", "priceSource": "estimated", "lockOrigin": "auto",
                  "altLine": {"posted": 44.5, "line": 50.5, "shift": 6, "postedLabel": "UNDER 44.5"}}]
        html = self.pg.evaluate("(p)=>{saveP(p);window._altPeriod='all';return _altTrackerHTML('full')}", picks)
        self.assertGreaterEqual(html.count("whypick_"), 2)                                   # both rows get the toggle (the second says no reasoning captured gracefully)
        self.assertIn("No detailed reasoning captured", html)
        self.assertNotIn("<b>x</b>", html)                                                   # stored text is escaped, never rendered as HTML
        self.assertNotIn("NBA SPREADS", html)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
