#!/usr/bin/env python3
"""What the ticker shows and in what order (docs/app.html renderTicker), owner request 2026-10-23.

  1. games in progress first (your locked ones ahead of the rest)
  2. then your locked games that have not started, soonest first, each with its start time
  3. then your locked games that ended in the last 30 minutes (latest first); after that they drop off
An unlocked game shows only while it is in progress.  Synthetic games only (no network).

    python3 scripts/test_ticker_rules.py
"""
import functools, http.server, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def g(i, home, away, state, **kw):
    d = {"k": "nhl", "tag": "NHL", "id": i, "home": home, "away": away, "homeScore": 1, "awayScore": 2, "state": state, "note": "P2 12:34", "period": 2, "displayClock": "12:34"}
    d.update(kw)
    return d


def lock(i, home, away, d="today()"):
    return f"{{id:'{i}',hA:'{home}',awA:'{away}',sport:'NHL',betType:'ML',betOn:'{home}',date:{d},outcome:'pending',lockedAt:Date.now(),decOdds:1.9}}"


class Ticker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_context(timezone_id="UTC", viewport={"width": 1280, "height": 800}).new_page()   # CI runs in UTC; so do these tests, wherever they are run
        cls.pg.route("**/*espn.com/**", lambda r: r.abort())
        cls.pg.route("**/*workers.dev/**", lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof renderTicker==='function'&&typeof _TKM!=='undefined'&&typeof saveP==='function'"); cls.pg.wait_for_timeout(1500)
        cls.pg.evaluate("window.__realEuro=_tkEuroGames")

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()


    def denver_midnight(self):
        """ms of today's 00:00 in America/Denver (-06:00 in MDT, -07:00 in MST)"""
        return self.pg.evaluate("()=>{let t=Date.parse(today()+'T00:00:00-06:00');if(_scDayOf(t)!==today())t=Date.parse(today()+'T00:00:00-07:00');return t}")

    def run_ticker(self, games, locks, fin=None, euro=None):
        """returns {order:[keys], html:{key:text}, chip:text, shown:bool}"""
        import json
        return self.pg.evaluate("""([games,locksJs,fin,euro])=>{
          _LV.games=games;_LV.relay={ts:0,games:[],ok:false};
          window._tkEuroGames=function(){return euro||[]};
          saveP(eval('['+locksJs+']'));_TKM.fin=fin||{};_TKM.sig='';renderTicker();
          const el=document.getElementById('cv-ticker');
          const out={order:_TKM.order.slice(),html:{},shown:getComputedStyle(el).display!=='none',chip:(el.querySelector('[data-k="__chip"]')||{}).innerText||''};
          el.querySelectorAll('.tkgrp:first-child .tk[data-k]').forEach(n=>{out.html[n.dataset.k]=n.innerText.replace(/\\n/g,' ')});return out}""",
                                [games, ",".join(locks), fin or {}, euro or []])

    def test_order_is_live_locked_then_live_then_upcoming_then_just_finished(self):
        now = self.pg.evaluate("Date.now()")
        games = [
            g("f1", "Pelicans", "Marmots", "post", startMs=now - 3 * 3600000, note="Final"),
            g("p2", "Ocelots", "Tapirs", "pre", startMs=now + 3 * 3600000, homeScore=0, awayScore=0, note="7:00 PM"),
            g("l1", "Zephyrs", "Quokkas", "in"),
            g("p1", "Walruses", "Narwhals", "pre", startMs=now + 1 * 3600000, homeScore=0, awayScore=0, note="5:00 PM"),
            g("l2", "Gulls", "Herons", "in"),
        ]
        locks = [lock("a", "Pelicans", "Marmots"), lock("b", "Ocelots", "Tapirs"), lock("c", "Walruses", "Narwhals"), lock("d", "Gulls", "Herons")]
        r = self.run_ticker(games, locks, fin={"nhl|f1": now - 5 * 60000})
        # live+locked (Gulls), live unlocked (Zephyrs), upcoming soonest first (Walruses, Ocelots), then the final
        self.assertEqual(r["order"], ["nhl|l2", "nhl|l1", "nhl|p1", "nhl|p2", "nhl|f1"])
        self.assertIn("2 LIVE", r["chip"])

    def test_an_upcoming_locked_game_shows_its_start_time_and_no_score(self):
        now = self.pg.evaluate("Date.now()")
        r = self.run_ticker([g("p1", "Walruses", "Narwhals", "pre", startMs=now + 3600000, homeScore=0, awayScore=0)], [lock("c", "Walruses", "Narwhals")])
        t = r["html"]["nhl|p1"]
        self.assertIn("Narwhals @ Walruses", t)
        self.assertRegex(t, r"\d{1,2}:\d{2}\s?(AM|PM)")                         # the start time
        self.assertNotIn("0 @", t)                                               # no 0-0 score before the puck drops
        self.assertIn("LOCKED", t)
        self.assertNotIn("●", t)                                                 # not styled as live

    def test_an_upcoming_game_without_a_lock_is_not_shown(self):
        now = self.pg.evaluate("Date.now()")
        r = self.run_ticker([g("p1", "Walruses", "Narwhals", "pre", startMs=now + 3600000), g("l1", "Zephyrs", "Quokkas", "in")], [])
        self.assertEqual(r["order"], ["nhl|l1"])

    def test_a_locked_final_stays_for_30_minutes_then_drops(self):
        now = self.pg.evaluate("Date.now()")
        fin = lambda: [g("f1", "Pelicans", "Marmots", "post", startMs=now - 3 * 3600000, note="Final")]
        lk = [lock("a", "Pelicans", "Marmots")]
        self.assertEqual(self.run_ticker(fin(), lk, fin={"nhl|f1": now - 29 * 60000})["order"], ["nhl|f1"])
        self.assertFalse(self.run_ticker(fin(), lk, fin={"nhl|f1": now - 31 * 60000})["shown"])
        r = self.run_ticker(fin(), lk, fin={"nhl|f1": now - 2 * 60000})
        self.assertIn("FINAL", r["html"]["nhl|f1"])

    def test_an_unlocked_final_never_shows(self):
        now = self.pg.evaluate("Date.now()")
        r = self.run_ticker([g("f1", "Pelicans", "Marmots", "post", startMs=now - 3 * 3600000), g("l1", "Zephyrs", "Quokkas", "in")], [], fin={"nhl|f1": now - 60000})
        self.assertEqual(r["order"], ["nhl|l1"])

    def test_a_game_first_seen_already_final_is_dated_from_its_start(self):
        now = self.pg.evaluate("Date.now()")
        lk = [lock("a", "Pelicans", "Marmots")]
        old = self.run_ticker([g("f1", "Pelicans", "Marmots", "post", startMs=now - 9 * 3600000)], lk)               # ended long ago: opening the app now must not resurrect it
        self.assertFalse(old["shown"])
        fresh = self.run_ticker([g("f2", "Pelicans", "Marmots", "post", startMs=now - 3 * 3600000)], lk)            # NHL start + 2.8 h = ended ~12 min ago: still inside the window
        self.assertEqual(fresh["order"], ["nhl|f2"])

    def test_the_end_time_is_the_moment_it_was_first_seen_final(self):
        now = self.pg.evaluate("Date.now()")
        lk = [lock("a", "Pelicans", "Marmots")]
        self.run_ticker([g("f3", "Pelicans", "Marmots", "in", startMs=now - 2 * 3600000)], lk)
        self.pg.evaluate("_LV.games=[Object.assign(_LV.games[0],{state:'post',note:'Final'})];_TKM.sig='';renderTicker()")
        stamp = self.pg.evaluate("_TKM.fin['nhl|f3']")
        self.assertAlmostEqual(stamp, self.pg.evaluate("Date.now()"), delta=5000)
        self.assertIn("nhl|f3", self.pg.evaluate("_TKM.order"))
        self.assertIsNotNone(self.pg.evaluate("localStorage.getItem('cv_tk_fin')"))                                  # survives a reload

    def test_a_late_game_that_ends_after_midnight_keeps_yesterdays_lock_for_30_minutes(self):
        now = self.pg.evaluate("Date.now()")
        midnight = self.denver_midnight()
        games = [g("f1", "Pelicans", "Marmots", "post", startMs=midnight - 3 * 3600000, note="Final")]
        r = self.run_ticker(games, [lock("y", "Pelicans", "Marmots", "yesterday()")], fin={"nhl|f1": now - 60000})
        self.assertEqual(r["order"], ["nhl|f1"])
        # a game that started TODAY must not borrow yesterday's lock for the same two teams
        r = self.run_ticker([g("f2", "Pelicans", "Marmots", "post", startMs=midnight + 3600000)], [lock("y", "Pelicans", "Marmots", "yesterday()")], fin={"nhl|f2": now - 60000})
        self.assertFalse(r["shown"])

    def test_european_hockey_schedule_games_show_before_the_start_for_the_ticker_only(self):
        r = self.pg.evaluate("""()=>{
          window._tkEuroGames=window.__realEuro;
          const t=new Date(Date.now()+2*3600000).toISOString();
          _SHL_DATA.games=(_SHL_DATA.games||[]).concat([{id:'e-test',date:t,homeName:'Rogle',awayName:'Frolunda'}]);
          _LV.relay={ts:0,games:[],ok:false};
          const withPre=_tkEuroGames(true).filter(x=>x.id==='e-test'),without=_tkEuroGames().filter(x=>x.id==='e-test');
          _SHL_DATA.games=_SHL_DATA.games.filter(x=>x.id!=='e-test');
          return {withPre:withPre.map(x=>x.state),without:without.length,startMs:withPre[0]&&withPre[0].startMs>Date.now()}}""")
        self.assertEqual(r["withPre"], ["pre"])
        self.assertEqual(r["without"], 0)                                        # the scores tab / card painter keep seeing only started games
        self.assertTrue(r["startMs"])

    def test_a_final_that_ended_long_ago_is_dropped_and_its_end_time_is_kept(self):
        now = self.pg.evaluate("Date.now()")
        r = self.run_ticker([g("f9", "Pelicans", "Marmots", "post", startMs=now - 24 * 3600000, note="Final", k="nfl", tag="NFL")], [lock("y", "Pelicans", "Marmots", "yesterday()").replace("sport:'NHL'", "sport:'NFL'")])
        self.assertFalse(r["shown"])                                             # a 24 h old final (e.g. still on a week-wide scoreboard) is not resurrected by yesterday's lock
        self.assertIsNotNone(self.pg.evaluate("_TKM.fin['nfl|f9']"))             # and the end time was actually stored (the prune used to delete it)

    def test_a_long_game_seen_live_keeps_its_full_30_minutes_after_it_ends(self):
        now = self.pg.evaluate("Date.now()")
        lk = [lock("a", "Pelicans", "Marmots")]
        self.pg.evaluate("localStorage.removeItem('cv_tk_live');_TKM.seen=null")
        self.run_ticker([g("f8", "Pelicans", "Marmots", "in", startMs=now - 4 * 3600000)], lk)               # three overtimes: 4 h in progress
        r = self.run_ticker([g("f8", "Pelicans", "Marmots", "post", startMs=now - 4 * 3600000, note="Final")], lk)
        self.assertEqual(r["order"], ["nhl|f8"])                                 # not dated from start + 2.8 h (which would already be past the window)

    def test_a_postponed_or_abandoned_game_never_shows_as_final(self):
        now = self.pg.evaluate("Date.now()")
        r = self.run_ticker([g("v1", "Pelicans", "Marmots", "post", startMs=now - 3600000, note="Postponed", homeScore=0, awayScore=0, completed=False)], [lock("a", "Pelicans", "Marmots")])
        self.assertFalse(r["shown"])

    def test_started_yesterday_is_judged_by_the_denver_date_not_the_browsers(self):
        now = self.pg.evaluate("Date.now()")
        denver_midnight = self.denver_midnight()
        # a game that started 2 h before the Denver midnight (yesterday in Denver) and just finished, with a lock dated yesterday
        r = self.run_ticker([g("d1", "Pelicans", "Marmots", "post", startMs=denver_midnight - 2 * 3600000, note="Final")], [lock("y", "Pelicans", "Marmots", "yesterday()")], fin={"nhl|d1": now - 60000})
        self.assertEqual(r["order"], ["nhl|d1"])

    def test_nothing_live_and_nothing_locked_hides_the_ticker(self):
        now = self.pg.evaluate("Date.now()")
        r = self.run_ticker([g("p1", "Walruses", "Narwhals", "pre", startMs=now + 3600000)], [])
        self.assertFalse(r["shown"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
