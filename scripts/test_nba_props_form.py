#!/usr/bin/env python3
"""NBA player-prop projection inputs (owner rules 2026-10-22): form = last 5 games, this season's first and topped up from last season's (feed side: test_nba_espn);
whole-number averages round DOWN to the .5 below; opponent defence capped at 7% for NBA (NHL keeps 12%); game pace + team offense; adjusted lines (PTS 5-6, REB/AST 2-3).

    python3 scripts/test_nba_props_form.py
"""
import functools, http.server, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class NbaPropsForm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.pg = cls.browser.new_page()
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _generateNBAProps==='function'&&typeof _propOppDefFactor==='function'")
        cls.pg.wait_for_timeout(1500)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def form_used(self, last_game):
        return self.pg.evaluate("""(lg)=>{
          const pl={name:'Test Guy',team:'AAA',pos:'G',gp:70,mpg:32,ppg:20,rpg:5,apg:5,last5:{n:5,ppg:10,rpg:2,apg:2},stdev:{pts:6,reb:2,ast:2},lastGame:lg,status:'active'};
          const out=_generateNBAProps([{h:'AAA',a:'BBB'}],{'test guy':pl}).filter(p=>p.player==='Test Guy'&&p.stat==='ppg');
          return out.length?out[0].form:'none'}""", last_game)

    def test_form_blends_in_whatever_the_feed_gives(self):
        self.assertEqual(self.form_used("2026-04-13"), 10)

    def props(self, **over):
        return self.pg.evaluate("""(o)=>{
          const pl=Object.assign({name:'Test Guy',team:'AAA',pos:'G',gp:70,mpg:32,ppg:20,rpg:6,apg:6,last5:null,stdev:{pts:6,reb:2,ast:2},status:'active'},o);
          return _generateNBAProps([{h:'AAA',a:'BBB'}],{'test guy':pl}).map(p=>({stat:p.stat,line:p.line,over:p.over,prob:p.prob,alt:p.alt,mu:p.mu}))}""", over)

    def test_whole_number_average_rounds_down(self):
        by = {p["stat"]: p for p in self.props(ppg=20.0)}
        self.assertEqual(by["ppg"]["line"], 19.5)

    def test_adjusted_line_moves_toward_safety_inside_the_band(self):
        for p in self.props(ppg=24.3, rpg=8.2, apg=7.4):
            a = p["alt"]
            if not a:
                continue
            band = {"ppg": (5, 6), "rpg": (2, 3), "apg": (2, 3)}[p["stat"]]
            self.assertIn(a["shift"], band)
            self.assertEqual(a["line"], p["line"] - a["shift"] if p["over"] else p["line"] + a["shift"])
            self.assertGreater(a["prob"], p["prob"])
            self.assertGreaterEqual(a["prob"], 0.62)
            self.assertIsNotNone(a["ml"])

    def test_no_adjusted_line_when_already_strong(self):
        self.assertIsNone(self.pg.evaluate("_propAltLine('ppg',19.5,true,30,5,.70)"))

    def test_pace_and_offense_environment(self):
        self.pg.evaluate("""window.__CV_DATA=window.__CV_DATA||{};window.__CV_DATA.nba=window.__CV_DATA.nba||{};
          const adv={};for(let i=0;i<10;i++)adv['T'+i]={ortg:114,drtg:114,pace:98};adv.FAST={ortg:114,drtg:114,pace:104};adv.SLOW={ortg:120,drtg:114,pace:96};
          window.__CV_DATA.nba.teamAdv=adv""")
        fast = self.pg.evaluate("_propNbaEnv('SLOW','FAST')")      # slow team meets a fast one: game runs faster than his usual
        self.assertGreater(fast["paceP"], 0)
        self.assertLessEqual(fast["paceP"], 0.04)
        self.assertLess(fast["offScale"], 1)                         # strong offense (120 vs ~114.6 league) -> defence matters a little less
        slow = self.pg.evaluate("_propNbaEnv('FAST','SLOW')")
        self.assertLess(slow["paceP"], 0)
        self.assertFalse(self.pg.evaluate("_propNbaEnv('NOPE','FAST')")["have"])

    def test_locking_at_the_adjusted_line_records_the_posted_line_and_dedupes(self):
        r = self.pg.evaluate("""()=>{
          localStorage.removeItem('locked_props');LOCKED_PROPS.length=0;saveP(getP().filter(p=>p.betType!=='PROP'));
          lockProp('AAA','Test Guy',19.5,true,0.68,'-246','NBA','BBB','PTS',null,{posted:24.5,shift:5,postedProb:.6,postedMl:'-150'});
          lockProp('AAA','Test Guy',24.5,true,0.6,'-150','NBA','BBB','PTS');          // same prop at the posted line: the same bet, not a second one
          const ps=getP().filter(p=>p.betType==='PROP'&&p.player==='Test Guy');
          return {n:ps.length,alt:ps[0]&&ps[0].altLine,betOn:ps[0]&&ps[0].betOn,note:ps[0]?_altNoteHTML(ps[0]):'',src:ps[0]&&ps[0].priceSource}}""")
        self.assertEqual(r["n"], 1)
        self.assertEqual(r["betOn"], "Test Guy OVER PTS 19.5")
        self.assertEqual((r["alt"]["posted"], r["alt"]["line"], r["alt"]["shift"]), (24.5, 19.5, 5))
        self.assertEqual(r["src"], "estimated")
        self.assertIn("POSTED", r["note"])
        self.pg.evaluate("()=>{LOCKED_PROPS.length=0;localStorage.removeItem('locked_props');saveP(getP().filter(p=>p.betType!=='PROP'))}")

    def test_opponent_cap_is_7_for_nba_12_for_nhl(self):
        src = (ROOT / "docs" / "app.html").read_text()
        self.assertIn("const cap=sport==='NBA'?.07:.12;", src)
        f = self.pg.evaluate("(()=>{const r=_propOppDefFactor('NBA','OKC');return r.factor})()")
        self.assertLessEqual(abs(f), 0.07)


if __name__ == "__main__":
    unittest.main(verbosity=2)
