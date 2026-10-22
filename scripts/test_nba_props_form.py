#!/usr/bin/env python3
"""NBA player-prop projection inputs. Before the season a player's 'last 5' is last April's tail end (below his season average), which tilted the whole slate UNDER; form now
only counts when his last game is recent. Opponent-defense adjustment is capped at 7% for NBA (NHL keeps 12%).

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

    def test_last_aprils_form_is_ignored(self):
        self.assertIn(self.form_used("2026-04-13"), (None, "none"))

    def test_recent_form_counts(self):
        v = self.pg.evaluate("new Date(Date.now()-3*864e5).toISOString().slice(0,10)")
        self.assertEqual(self.form_used(v), 10)

    def test_opponent_cap_is_7_for_nba_12_for_nhl(self):
        src = (ROOT / "docs" / "app.html").read_text()
        self.assertIn("const cap=sport==='NBA'?.07:.12;", src)
        f = self.pg.evaluate("(()=>{const r=_propOppDefFactor('NBA','OKC');return r.factor})()")
        self.assertLessEqual(abs(f), 0.07)


if __name__ == "__main__":
    unittest.main(verbosity=2)
