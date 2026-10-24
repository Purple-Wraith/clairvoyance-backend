#!/usr/bin/env python3
"""The College (CFB) and NFL game lists load on the FIRST visit to the Football tab (docs/app.html).

Before: SS('fb') had no football branch, so nothing rendered the visible CFB pane; the only render came from the live-score repaint, which ran before the week
dropdown was filled and printed "NO GAMES MATCH THESE FILTERS" until the user went to NFL and back (setSub -> cfbSubTab, the one place that filled the dropdown).

    python3 scripts/test_football_first_load.py
"""
import functools, http.server, os, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(os.environ.get("CV_ROOT") or Path(__file__).resolve().parent.parent)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class FirstLoad(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def page(self):
        pg = self.b.new_page()
        pg.route("**/*espn.com/**", lambda r: r.abort()); pg.route("**/*workers.dev/**", lambda r: r.abort())
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1")
        pg.wait_for_function("typeof SS==='function'&&typeof renderCFBGames==='function'"); pg.wait_for_timeout(1200)
        return pg

    def state(self, pg, sel):
        return pg.evaluate("(s)=>{const e=document.querySelector(s);return {cards:e.querySelectorAll('.gc').length,text:e.innerText.slice(0,60)}}", sel)

    def wait_cards(self, pg, sel):
        try:
            pg.wait_for_function("(s)=>document.querySelectorAll(s+' .gc').length>0", arg=sel, timeout=15000)
        except Exception:
            pass
        return self.state(pg, sel)

    def test_cfb_loads_the_first_time_the_football_tab_is_opened(self):
        pg = self.page()
        pg.evaluate("SS('fb')")                                   # what clicking FOOTBALL does -- no setSub afterwards
        r = self.wait_cards(pg, "#cfb-games-list")
        self.assertGreater(r["cards"], 0, r)
        self.assertNotIn("NO GAMES", r["text"].upper())
        pg.close()

    def test_a_live_score_repaint_before_the_dropdown_is_filled_does_not_blank_the_list(self):
        pg = self.page()
        pg.evaluate("()=>{window._CFB_DATA=null;document.getElementById('cfb-filter-week').innerHTML='';renderCFBGames()}")      # the live layer's repaint, on a cold page
        r = self.wait_cards(pg, "#cfb-games-list")
        self.assertGreater(r["cards"], 0, r)
        self.assertGreater(pg.evaluate("document.getElementById('cfb-filter-week').options.length"), 0)
        pg.close()

    def test_nfl_has_the_same_protection(self):
        pg = self.page()
        pg.evaluate("()=>{window._NFL_DATA=null;document.getElementById('nfl2-filter-week').innerHTML='';renderNFLGames2()}")
        r = self.wait_cards(pg, "#nfl2-games-list")
        self.assertGreater(r["cards"], 0, r)
        pg.close()

    def test_reopening_football_keeps_the_nfl_pane_if_that_is_where_you_were(self):
        pg = self.page()
        pg.evaluate("SS('fb');setSub('fb','nfl')"); self.wait_cards(pg, "#nfl2-games-list")
        pg.evaluate("SS('home');SS('fb')"); pg.wait_for_timeout(500)
        self.assertTrue(pg.evaluate("document.getElementById('fb-nfl').classList.contains('act')"))
        self.assertGreater(self.state(pg, "#nfl2-games-list")["cards"], 0)
        pg.close()

    def test_the_live_score_repaint_only_renders_football_while_the_football_pane_is_open(self):
        pg = self.page()
        self.assertFalse(pg.evaluate("_fbPaneOpen('cfb')"))                                      # on the home page: the repaint must not build the College slate (a Monte Carlo run per card)
        pg.evaluate("SS('fb')"); pg.wait_for_timeout(300)
        self.assertTrue(pg.evaluate("_fbPaneOpen('cfb')")); self.assertFalse(pg.evaluate("_fbPaneOpen('nfl')"))
        pg.evaluate("setSub('fb','nfl')"); self.assertTrue(pg.evaluate("_fbPaneOpen('nfl')"))
        src = (ROOT / "docs" / "app.html").read_text()
        self.assertIn("window._cfbActiveTab==='games'&&_fbPaneOpen('cfb')", src); self.assertIn("window._nflActiveTab==='games'&&_fbPaneOpen('nfl')", src)
        pg.close()

    def test_the_week_you_picked_survives_a_repaint(self):
        pg = self.page()
        pg.evaluate("SS('fb')"); self.wait_cards(pg, "#cfb-games-list")
        weeks = pg.evaluate("[...document.getElementById('cfb-filter-week').options].map(o=>o.value)")
        if len(weeks) > 1:
            pick = weeks[-1]
            pg.evaluate("(w)=>{const s=document.getElementById('cfb-filter-week');s.value=w;renderCFBGames()}", pick)
            pg.evaluate("renderCFBGames()")
            self.assertEqual(pg.evaluate("document.getElementById('cfb-filter-week').value"), pick)
        pg.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
