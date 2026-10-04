#!/usr/bin/env python3
"""Ticker scroll (docs/app.html _tkSetAnim/_tkMeasure): the scroll is a compositor-driven Web Animation (a main-thread stall must not freeze it), with a JS rAF fallback.
Uses synthetic games (no network). Checks: speed, hover pause/resume, seamless wrap, resuming at the same offset after a rebuild, offset kept when an item grows, fallback path."""
import functools, http.server, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GAMES_JS = """()=>{ _LV.games=Array.from({length:40},(_, i)=>({k:'nhl',tag:'NHL',id:'g'+i,home:'HOME'+i,away:'AWAY'+i,homeScore:i%5,awayScore:(i+2)%4,state:i%3?'in':'post',note:'P2 12:34',period:2,displayClock:'12:34',seasonType:2}));
  _TKM.sig='';renderTicker(); }"""
TX = "(()=>{const t=document.querySelector('#cv-ticker .tk-track');return new DOMMatrix(getComputedStyle(t).transform).m41})()"


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class TickerScroll(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def page(self, init=""):
        pg = self.browser.new_page(viewport={"width": 1280, "height": 800})
        if init:
            pg.add_init_script(init)
        pg.goto(f"http://127.0.0.1:{self.port}/app.html?nosb=1")
        pg.wait_for_function("typeof renderTicker==='function'&&typeof _TKM!=='undefined'")
        pg.wait_for_timeout(1500)
        pg.evaluate(GAMES_JS)
        pg.wait_for_timeout(300)
        return pg

    def test_animation_speed_hover_wrap(self):
        pg = self.page()
        self.assertTrue(pg.evaluate("!!_TKM.anim&&_TKM.anim.playState==='running'&&!_TKM.raf"))
        a = pg.evaluate(TX); pg.wait_for_timeout(1500); b = pg.evaluate(TX)
        self.assertAlmostEqual(a - b, 46 * 1.5, delta=12)               # ~46 px/s
        pg.hover("#cv-ticker"); pg.wait_for_timeout(300)
        self.assertEqual(pg.evaluate("_TKM.anim.playState"), "paused")
        a = pg.evaluate(TX); pg.wait_for_timeout(600); self.assertAlmostEqual(pg.evaluate(TX), a, delta=0.5)
        pg.mouse.move(2, 700); pg.wait_for_timeout(300)
        self.assertEqual(pg.evaluate("_TKM.anim.playState"), "running")
        pg.evaluate("_TKM.anim.currentTime=_TKM.animDur-250"); pg.wait_for_timeout(700)
        self.assertGreater(pg.evaluate(TX), -300)                        # wrapped back to the start, no gap
        pg.close()

    def test_rebuild_and_width_change_keep_position(self):
        pg = self.page()
        pg.evaluate("_TKM.anim.currentTime=_TKM.animDur*0.4"); pg.wait_for_timeout(100); before = pg.evaluate(TX)
        pg.evaluate("_TKM.sig='';renderTicker()"); pg.wait_for_timeout(150)
        self.assertAlmostEqual(pg.evaluate(TX), before, delta=25)       # rebuild resumes where it was
        before = pg.evaluate(TX)
        pg.evaluate("document.querySelector('#cv-ticker .tkgrp .tkn').textContent+=' ####################';_tkMeasure(document.getElementById('cv-ticker'))")
        pg.wait_for_timeout(150)
        self.assertAlmostEqual(pg.evaluate(TX), before, delta=25)       # a longer item changes the loop length, not the position
        pg.close()

    def test_only_locked_and_live_games(self):
        pg = self.page()
        # synthetic set: i%3==0 -> final, otherwise live. Finals WITHOUT a lock must not appear; a final WITH a lock must.
        pg.evaluate("""()=>{ saveP([{id:'t1',hA:'HOME0',awA:'AWAY0',sport:'NHL',betType:'ML',betOn:'HOME0',date:today(),outcome:'pending',lockedAt:Date.now(),decOdds:1.9}]);
            _TKM.sig='';renderTicker(); }""")
        pg.wait_for_timeout(300)
        keys = pg.evaluate("_TKM.order")
        live = [k for k in keys if int(k.split('g')[-1]) % 3 != 0]
        finals = [k for k in keys if int(k.split('g')[-1]) % 3 == 0]
        self.assertEqual(len(live), 26)                      # all 26 live games (i not divisible by 3, of 40)
        self.assertEqual(finals, ["nhl|g0"])                 # the one final that carries a lock; the other 13 finals are gone (names chosen so the app's fuzzy team match cannot collide)
        # nothing live and nothing locked -> the ticker hides
        pg.evaluate("_LV.games=_LV.games.filter(g=>g.state==='post'&&g.id!=='g0');saveP([]);renderTicker()")
        pg.wait_for_timeout(200)
        self.assertEqual(pg.evaluate("getComputedStyle(document.getElementById('cv-ticker')).display"), "none")
        pg.close()

    def test_yesterdays_locks_do_not_keep_finished_games(self):
        pg = self.page()
        pg.evaluate("""()=>{ saveP([{id:'y1',hA:'HOME0',awA:'AWAY0',sport:'NHL',betType:'ML',betOn:'HOME0',date:yesterday(),outcome:'win',lockedAt:Date.now()-86400000,decOdds:1.9},
            {id:'y2',hA:'HOME1',awA:'AWAY1',sport:'NHL',betType:'ML',betOn:'HOME1',date:yesterday(),outcome:'pending',lockedAt:Date.now()-86400000,decOdds:1.9}]);
            _TKM.sig='';renderTicker(); }""")
        pg.wait_for_timeout(300)
        keys = pg.evaluate("_TKM.order")
        self.assertNotIn("nhl|g0", keys)                       # g0 is a FINAL with only a yesterday lock -> gone
        self.assertIn("nhl|g1", keys)                          # g1 is LIVE: stays, and keeps its yesterday-dated lock chip
        self.assertEqual(pg.evaluate("document.querySelectorAll('#cv-ticker .tkgrp:first-child [data-k=\"nhl|g1\"] .tkl').length"), 1)
        pg.close()

    def test_js_fallback_without_element_animate(self):
        pg = self.page("delete Element.prototype.animate;")
        self.assertTrue(pg.evaluate("!_TKM.anim&&!!_TKM.raf"))
        a = pg.evaluate(TX); pg.wait_for_timeout(1500); b = pg.evaluate(TX)
        self.assertAlmostEqual(a - b, 46 * 1.5, delta=14)
        pg.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
