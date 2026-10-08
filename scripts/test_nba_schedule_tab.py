#!/usr/bin/env python3
"""NBA MATCHES tab (owner request 2026-10-08): the same shape as the other leagues -- a date dropdown of REGULAR-SEASON game days (no preseason / play-in / playoffs; days that have passed
drop off) and one game card per game for the chosen day (ESPN's scoreboard, season type 2 only), with the shared slate controls and the labelled grade tags.

    python3 scripts/test_nba_schedule_tab.py
"""
import functools, http.server, json, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = json.loads((ROOT / "scripts" / "fixtures" / "nba_scoreboard_20261021.json").read_text())
SCHED = json.loads((ROOT / "docs" / "nba_schedule.json").read_text())


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Tab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.b = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def page(self, today="2026-10-10"):
        pg = self.b.new_page(viewport={"width": 1300, "height": 1000})
        self.espn_days = []

        def espn(route):
            url = route.request.url
            if "/basketball/nba/scoreboard" in url and "dates=20261021" in url:
                self.espn_days.append("20261021")
                return route.fulfill(status=200, headers={"access-control-allow-origin": "*", "content-type": "application/json"}, body=json.dumps(FIX))
            if "/basketball/nba/" in url:
                return route.fulfill(status=200, headers={"access-control-allow-origin": "*", "content-type": "application/json"}, body=json.dumps({"events": [], "injuries": []}))
            return route.continue_()
        pg.route("https://site.api.espn.com/**", espn)
        pg.goto(f"http://127.0.0.1:{self.srv.server_address[1]}/app.html?nosb=1")
        pg.wait_for_function("typeof renderNBAUpcoming==='function'&&window._NBA_SCHED!==undefined")
        pg.wait_for_timeout(1500)
        pg.evaluate(f"window._nbaEtToday=()=>'{today}'")
        pg.evaluate("navTap(document.querySelector(\"[onclick*=\\\"navTap(this,'nba')\\\"]\"),'nba')")
        pg.wait_for_timeout(500)
        return pg

    def options(self, pg):
        pg.evaluate("renderNBAUpcoming(true)")
        pg.wait_for_function("document.getElementById('nba-filter-day').options.length>0")
        return pg.evaluate("[...document.getElementById('nba-filter-day').options].map(o=>o.value)")

    def test_dropdown_lists_only_regular_season_game_days(self):
        pg = self.page("2026-10-10")
        opts = self.options(pg)
        reg = set(SCHED["regularSeasonDays"])
        self.assertTrue(opts and all(o in reg for o in opts))
        self.assertEqual(opts[0], "2026-10-20")                                      # opening night: nothing from the preseason before it
        self.assertEqual(pg.evaluate("document.getElementById('nba-filter-day').value"), "2026-10-20")
        preseason_days = {g["day"] for g in SCHED["games"] if g.get("preseason")}
        self.assertFalse(preseason_days & set(opts))
        pg.close()

    def test_days_that_have_passed_drop_off_and_today_is_the_default(self):
        pg = self.page("2026-10-25")
        opts = self.options(pg)
        self.assertEqual(opts[0], "2026-10-25")
        self.assertNotIn("2026-10-21", opts)
        self.assertEqual(pg.evaluate("document.getElementById('nba-filter-day').value"), "2026-10-25")
        self.assertIn("GAMES", pg.evaluate("document.getElementById('nba-filter-day').options[0].text"))   # 'GAMES · <day>' marks today, like the NHL list
        pg.close()

    def test_a_day_renders_one_card_per_regular_season_game_and_drops_preseason(self):
        pg = self.page("2026-10-10")
        self.options(pg)
        pg.select_option("#nba-filter-day", "2026-10-21")
        pg.wait_for_function("document.querySelectorAll('#nba-upcoming .gc').length>0")
        pg.wait_for_function("!!document.querySelector('#nba-upcoming .slate2')", timeout=5000)      # the shared toolbar attaches shortly after the cards
        r = pg.evaluate("""()=>({cards:document.querySelectorAll('#nba-upcoming .gc').length,head:document.querySelector('#nba-upcoming').innerText.split('\\n')[0],
          bar:!!document.querySelector('#nba-upcoming .slate2'),tags:[...document.querySelectorAll('#nba-upcoming .cgrade2')].map(e=>e.innerText.replace(/\\n/g,' | ')),
          pre:document.querySelector('#nba-upcoming').innerText.includes('PRE @ SEA')})""")
        self.assertEqual(r["cards"], 2)                                              # the injected preseason event (season type 1) is dropped
        self.assertIn("2 GAMES", r["head"].upper())
        self.assertFalse(r["pre"])
        self.assertTrue(r["bar"])                                                    # shared sort / filter / open-collapse controls
        self.assertTrue(any("▲ OVER" in t and "▼ UNDER" in t for t in r["tags"]), r["tags"])   # labelled per-side grade tags with the line
        pg.close()

    def test_old_placeholder_is_gone(self):
        pg = self.page()
        self.assertNotIn("RETURNING · OCTOBER 2026", pg.content())
        pg.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
