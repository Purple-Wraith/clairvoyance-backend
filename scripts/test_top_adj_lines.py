#!/usr/bin/env python3
"""TOP PICKS > TOP ADJUSTED LINES sub-tab (docs/app.html tpSub / _topAdjEntries / _topAdjHTML), owner request 2026-10-23.

The cards register every adjusted line in window._altReg; the sub-tab ranks today's by estimated win probability at the adjusted line (best side per game + market only, 3 per league in the top 10,
a kind with a weak recent record demoted).  Synthetic registry entries (no network, no model run).

    python3 scripts/test_top_adj_lines.py
"""
import functools, http.server, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


SEED = """([entries,ledger,live])=>{
  window._altReg={};
  entries.forEach((e,i)=>{window._altReg['k'+i+'|'+e.mkt+(e.opp?'|opp':'')]={sport:e.sport,hA:e.hA,awA:e.awA,date:today(),
    alt:{label:e.label,prob:e.prob,dec:1.4,ml:e.ml||'-250',posted:e.posted||220.5,line:e.line||214.5,shift:e.shift||6,postedLabel:e.postedLabel||'OVER 220.5',mkt:e.mkt}}});
  saveP(ledger||[]);_tpRecCache={at:0,v:null};
  _LV.games=live||[];_LV.relay={ts:0,games:[],ok:false};
  window._topPicksCache={ts:Date.now(),dateKey:today(),html:''};window._topPicksInFlight=false;
  tpSub('adj');return null}"""


def e(sport, home, away, label, prob, mkt="OU", **kw):
    d = {"sport": sport, "hA": home, "awA": away, "label": label, "prob": prob, "mkt": mkt}
    d.update(kw)
    return d


class AdjTab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page(viewport={"width": 1280, "height": 900})
        cls.pg.route("**/*espn.com/**", lambda r: r.abort())
        cls.pg.route("**/*workers.dev/**", lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof tpSub==='function'&&typeof saveP==='function'&&typeof _LV!=='undefined'"); cls.pg.wait_for_timeout(1500)
        cls.pg.evaluate("window.__realLock=_lockAltKey")

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def seed(self, entries, ledger=None, live=None):
        self.pg.evaluate(SEED, [entries, ledger or [], live or []])
        self.pg.wait_for_timeout(150)

    def rows(self):
        return self.pg.evaluate("""[...document.querySelectorAll('#toppicks-adj .tp-adj-row')].map(r=>r.innerText.replace(/\\n/g,' | '))""")

    def top_rows(self):
        """rows inside the TOP 10 block (before BY LEAGUE)"""
        return self.pg.evaluate("""()=>{const el=document.getElementById('toppicks-adj');const html=el.innerHTML;const cut=html.indexOf('BY LEAGUE');
          const d=document.createElement('div');d.innerHTML=cut>0?html.slice(0,cut):html;return [...d.querySelectorAll('.tp-adj-row')].map(r=>r.innerText.replace(/\\n/g,' | '))}""")

    def test_the_two_sub_tabs_switch_views(self):
        self.pg.evaluate("tpSub('picks')")
        self.assertEqual(self.pg.evaluate("getComputedStyle(document.getElementById('tp-view-adj')).display"), "none")
        self.assertNotEqual(self.pg.evaluate("getComputedStyle(document.getElementById('tp-view-picks')).display"), "none")
        self.pg.evaluate("tpSub('adj')")
        self.assertEqual(self.pg.evaluate("getComputedStyle(document.getElementById('tp-view-picks')).display"), "none")
        self.assertNotEqual(self.pg.evaluate("getComputedStyle(document.getElementById('tp-view-adj')).display"), "none")
        self.assertEqual(self.pg.evaluate("[...document.querySelectorAll('#tp-subtabs .tpsb')].map(b=>b.dataset.v+':'+b.classList.contains('act')).join()"), "picks:false,adj:true")
        self.assertEqual(self.pg.evaluate("[...document.querySelectorAll('#tp-subtabs .tpsb')].map(b=>b.innerText).join('|')"), "TOP PICKS|TOP ADJUSTED LINES")
        self.pg.evaluate("tpSub('picks')")

    def test_clicking_the_buttons_works(self):
        self.seed([e("NBA", "Zephyrs", "Quokkas", "OVER 214.5", .70)])
        self.pg.evaluate("tpSub('picks')")
        self.pg.evaluate("document.getElementById('sp-toppicks').classList.add('act')")
        self.pg.click("#tp-subtabs .tpsb[data-v='adj']")
        self.pg.wait_for_function("document.querySelectorAll('#toppicks-adj .tp-adj-row').length>0", timeout=4000)
        self.pg.click("#tp-subtabs .tpsb[data-v='picks']")
        self.assertEqual(self.pg.evaluate("getComputedStyle(document.getElementById('tp-view-adj')).display"), "none")
        self.pg.evaluate("document.getElementById('sp-toppicks').classList.remove('act')")

    def test_ranked_by_probability_at_the_adjusted_line(self):
        self.seed([e("NBA", "Zephyrs", "Quokkas", "OVER 214.5", .64), e("CFB", "Walruses", "Narwhals", "Walruses +10.5", .78, "SPREAD", postedLabel="Walruses +3.5"),
                   e("NHL", "Pelicans", "Marmots", "UNDER 6.5", .66, line=6.5, shift=1, postedLabel="UNDER 5.5")])
        r = self.top_rows()
        self.assertEqual(len(r), 3)
        self.assertIn("Walruses +10.5", r[0]); self.assertIn("78.0%", r[0]); self.assertIn("PREMIUM", r[0])
        self.assertIn("UNDER 6.5", r[1]); self.assertIn("66.0%", r[1]); self.assertIn("OPTIMAL", r[1])
        self.assertIn("OVER 214.5", r[2]); self.assertIn("64.0%", r[2]); self.assertIn("OPTIMAL", r[2])

    def test_each_row_shows_how_far_the_line_moved_and_from_what(self):
        self.seed([e("NBA", "Zephyrs", "Quokkas", "OVER 214.5", .70, shift=6, postedLabel="OVER 220.5"), e("NHL", "Pelicans", "Marmots", "UNDER 6.5", .66, shift=1, postedLabel="UNDER 5.5")])
        r = " ".join(self.rows())
        self.assertIn("moved 6 pts from posted OVER 220.5", r)
        self.assertIn("moved 1 goal from posted UNDER 5.5", r)
        self.assertIn("est. -250", r)

    def test_only_the_better_side_of_a_game_market_is_listed(self):
        self.seed([e("NBA", "Zephyrs", "Quokkas", "UNDER 224.5", .66), e("NBA", "Zephyrs", "Quokkas", "OVER 214.5", .71, opp=True),
                   e("NBA", "Zephyrs", "Quokkas", "Zephyrs +9.5", .69, "SPREAD")])
        r = self.top_rows()
        self.assertEqual(len(r), 2)                                              # one total + one spread
        self.assertTrue(any("OVER 214.5" in x for x in r)); self.assertFalse(any("UNDER 224.5" in x for x in r))

    def test_three_per_league_in_the_top_ten(self):
        self.seed([e("NBA", f"Home{i}", f"Away{i}", f"OVER {200+i}.5", .80 - i * .01) for i in range(5)] + [e("NHL", "Pelicans", "Marmots", "UNDER 6.5", .62)])
        r = self.top_rows()
        self.assertEqual(len(r), 6)                                              # fewer than 10 picks: nothing is left out
        self.assertEqual(sum("NBA ·" in x for x in r), 5)
        self.seed([e("NBA", f"Home{i}", f"Away{i}", f"OVER {200+i}.5", .90 - i * .01) for i in range(9)] + [e("NHL", f"P{i}", f"M{i}", "UNDER 6.5", .62) for i in range(3)])
        r = self.top_rows()
        self.assertEqual(len(r), 10)
        self.assertEqual(sum("NHL ·" in x for x in r), 3)            # the cap holds NBA to 3 until the other leagues are in, then the best of what is left fills the list
        self.assertEqual(sum("NBA ·" in x for x in r), 7)

    def test_a_locked_line_shows_its_result_and_no_button(self):
        td = self.pg.evaluate("today()")
        led = [{"id": "L1", "hA": "Zephyrs", "awA": "Quokkas", "sport": "NBA", "betType": "OU", "betOn": "OVER 214.5", "date": td, "outcome": "win", "altLine": {"posted": 220.5, "line": 214.5, "shift": 6, "postedLabel": "OVER 220.5"}, "decOdds": 1.4, "lockedAt": 1}]
        self.seed([e("NBA", "Zephyrs", "Quokkas", "OVER 214.5", .70), e("NBA", "Walruses", "Narwhals", "OVER 221.5", .66)], led)
        r = self.pg.evaluate("""[...document.querySelectorAll('#toppicks-adj .tp-adj-row')].map(x=>({t:x.innerText,b:x.querySelectorAll('.tp-adj-lock').length}))""")
        locked = [x for x in r if "Quokkas" in x["t"]]; open_ = [x for x in r if "Narwhals" in x["t"]]
        self.assertTrue(locked and open_)
        for x in locked:
            self.assertIn("WIN", x["t"]); self.assertEqual(x["b"], 0)
        for x in open_:
            self.assertEqual(x["b"], 1)
        # a pending lock reads LOCKED
        led[0]["outcome"] = "pending"
        self.seed([e("NBA", "Zephyrs", "Quokkas", "OVER 214.5", .70)], led)
        self.assertIn("LOCKED", " ".join(self.rows()))

    def test_lock_buttons_exist_only_for_lines_not_locked_and_not_started(self):
        self.seed([e("NBA", "Walruses", "Narwhals", "OVER 221.5", .66)])
        self.assertGreaterEqual(self.pg.evaluate("document.querySelectorAll('#toppicks-adj .tp-adj-lock').length"), 1)
        live = [{"k": "nba", "tag": "NBA", "id": "x", "home": "Walruses", "away": "Narwhals", "homeScore": 50, "awayScore": 48, "state": "in", "note": "Q2 3:00"}]
        self.seed([e("NBA", "Walruses", "Narwhals", "OVER 221.5", .66)], live=live)
        self.assertEqual(self.pg.evaluate("document.querySelectorAll('#toppicks-adj .tp-adj-lock').length"), 0)
        self.assertIn("Narwhals 48", " ".join(self.rows()))                       # the live score shows instead
        live[0]["state"] = "post"
        self.seed([e("NBA", "Walruses", "Narwhals", "OVER 221.5", .66)], live=live)
        self.assertIn("FINAL", " ".join(self.rows()))

    def test_finished_games_do_not_take_top_ten_places_from_lines_you_can_still_lock(self):
        live = [{"k": "nhl", "tag": "NHL", "id": "x", "home": "Pelicans", "away": "Marmots", "homeScore": 4, "awayScore": 1, "state": "post", "note": "Final"}]
        self.seed([e("NHL", "Pelicans", "Marmots", "UNDER 6.5", .80), e("NBA", "Zephyrs", "Quokkas", "OVER 214.5", .64)], live=live)
        top = self.top_rows()
        self.assertEqual(len(top), 1)
        self.assertIn("OVER 214.5", top[0])                                      # the open line leads the top block
        allr = self.rows()
        self.assertTrue(any("UNDER 6.5" in x and "FINAL" in x for x in allr))     # the finished one is still listed under its league
        self.seed([e("NHL", "Pelicans", "Marmots", "UNDER 6.5", .80)], live=live)
        self.assertEqual(len(self.top_rows()), 1)                                # nothing open at all: the finished lines are ranked rather than leaving the block empty

    def final(self, home, away, hs, as_, note="Final", tag="NBA", k="nba", **kw):
        g = {"k": k, "tag": tag, "id": "x" + home, "home": home, "away": away, "homeScore": hs, "awayScore": as_, "state": "post", "note": note}
        g.update(kw)
        return g

    def test_a_finished_line_shows_the_score_and_won_or_lost(self):
        live = [self.final("Walruses", "Narwhals", 120, 108), self.final("Zephyrs", "Quokkas", 105, 99)]
        self.seed([e("NBA", "Walruses", "Narwhals", "OVER 221.5", .66, line=221.5, posted=227.5),        # total 228 > 221.5: WON (and the posted 227.5 also won)
                   e("NBA", "Zephyrs", "Quokkas", "OVER 214.5", .70, line=214.5, posted=220.5)], live=live)   # total 204 < 214.5: LOST
        r = self.pg.evaluate("""[...document.querySelectorAll('#toppicks-adj .tp-adj-row')].map(x=>x.innerText.replace(/\\n+/g,' | '))""")
        w = next(x for x in r if "Narwhals" in x); l = next(x for x in r if "Quokkas" in x)
        self.assertIn("✓ WON", w); self.assertIn("Narwhals 108 – Walruses 120", w); self.assertIn("FINAL", w)
        self.assertIn("✗ LOST", l); self.assertIn("Quokkas 99 – Zephyrs 105", l)
        self.assertIn("NOT LOCKED", w)
        self.assertIn("posted line would have", w)

    def test_a_spread_is_graded_on_the_adjusted_line(self):
        live = [self.final("Walruses", "Narwhals", 100, 104)]                                             # Narwhals won by 4
        self.seed([e("NBA", "Walruses", "Narwhals", "Walruses +6.5", .70, "SPREAD", line=6.5, posted=-1.5, postedLabel="Walruses -1.5")], live=live)
        t = " ".join(self.rows())
        self.assertIn("✓ WON", t)                                                                         # Walruses lost by 4 but cover +6.5
        self.assertIn("posted line would have LOST", t)                                                   # -1.5 would not have

    def test_the_header_shows_the_results_record(self):
        live = [self.final("Walruses", "Narwhals", 120, 108), self.final("Zephyrs", "Quokkas", 105, 99), self.final("Gulls", "Herons", 110, 100)]
        self.seed([e("NBA", "Walruses", "Narwhals", "OVER 221.5", .66, line=221.5), e("NBA", "Zephyrs", "Quokkas", "OVER 214.5", .70), e("NBA", "Gulls", "Herons", "OVER 205.5", .68, line=205.5)], live=live)
        self.assertIn("2W-1L", self.pg.evaluate("document.getElementById('toppicks-adj').innerText"))

    def test_a_locked_line_uses_the_ledger_result(self):
        td = self.pg.evaluate("today()")
        led = [{"id": "L1", "hA": "Zephyrs", "awA": "Quokkas", "sport": "NBA", "betType": "OU", "betOn": "OVER 214.5", "date": td, "outcome": "win", "altLine": {"posted": 220.5, "line": 214.5, "shift": 6, "postedLabel": "OVER 220.5"}, "decOdds": 1.4, "lockedAt": 1}]
        self.seed([e("NBA", "Zephyrs", "Quokkas", "OVER 214.5", .70)], led, live=[self.final("Zephyrs", "Quokkas", 130, 99)])
        t = " ".join(self.rows())
        self.assertIn("✓ WON", t); self.assertIn("WIN", t)

    def test_a_postponed_game_has_no_result(self):
        live = [self.final("Walruses", "Narwhals", 0, 0, completed=False)]
        self.seed([e("NBA", "Walruses", "Narwhals", "OVER 221.5", .66)], live=live)
        t = " ".join(self.rows())
        self.assertNotIn("WON", t); self.assertNotIn("LOST", t)
        self.assertIn("POSTPONED", t); self.assertIn("NO RESULT", t)

    def test_a_shootout_game_is_not_graded_from_the_feed_score(self):
        live = [self.final("Pelicans", "Marmots", 4, 3, note="Final/SO", tag="NHL", k="nhl")]
        self.seed([e("NHL", "Pelicans", "Marmots", "UNDER 6.5", .66, line=6.5, posted=5.5)], live=live)
        t = " ".join(self.rows())
        self.assertNotIn("WON", t); self.assertNotIn("LOST", t)

    def test_top_picks_rows_show_the_result_line(self):
        r = self.pg.evaluate("""()=>{
          _LV.games=[{k:'nba',tag:'NBA',id:'z',home:'Walruses',away:'Narwhals',homeScore:120,awayScore:108,state:'post',note:'Final'}];
          const mk=(label,side,prob)=>({sport:'NBA',hA:'Walruses',awA:'Narwhals',best:{label,side,tierN:2},markets:[{side,label,prob}]});
          const ranked=_topPicksRankLegs([mk('Walruses ML','mlFav',.7)]);
          return ranked.top10.map(x=>_topPickRowHTML(x)).join('')}""")
        import re
        txt = re.sub(r"<[^>]+>", " ", r)
        self.assertIn("✓ WON", txt)
        self.assertIn("Narwhals 108 – Walruses 120", txt)
        self.assertIn("FINAL", txt)

    def test_the_lock_button_locks_that_exact_line(self):
        self.seed([e("NBA", "Walruses", "Narwhals", "OVER 221.5", .66)])
        self.pg.evaluate("window.__locked=[];window._lockAltKey=function(k){window.__locked.push(k)};document.getElementById('sp-toppicks').classList.add('act')")
        self.pg.click("#toppicks-adj .tp-adj-lock >> nth=0")
        self.assertEqual(self.pg.evaluate("window.__locked.length"), 1)
        self.assertIn("OU", self.pg.evaluate("window.__locked[0]"))
        self.assertEqual(self.pg.evaluate("window.__locked[0] in window._altReg"), True)
        self.pg.evaluate("window._lockAltKey=window.__realLock;document.getElementById('sp-toppicks').classList.remove('act')")

    def test_a_weak_recent_kind_is_demoted_below_a_better_one(self):
        td = self.pg.evaluate("today()")
        now = self.pg.evaluate("Date.now()")
        led = []
        for i in range(40):                                                       # NBA adjusted totals have won only 45% recently; NHL adjusted totals 85%
            led.append({"id": f"a{i}", "hA": "A", "awA": "B", "sport": "NBA", "league": "NBA", "betType": "OU", "betOn": "OVER 200.5", "date": "2026-10-15", "outcome": "win" if i % 20 < 9 else "loss", "altLine": {"posted": 1, "line": 2, "shift": 1, "postedLabel": "x"}, "decOdds": 1.7, "winProb": .65, "lockedAt": now - 5 * 86400000, "settledAt": now - 4 * 86400000})
            led.append({"id": f"b{i}", "hA": "A", "awA": "B", "sport": "NHL", "league": "NHL", "betType": "OU", "betOn": "UNDER 6.5", "date": "2026-10-15", "outcome": "win" if i % 20 < 17 else "loss", "altLine": {"posted": 1, "line": 2, "shift": 1, "postedLabel": "x"}, "decOdds": 1.7, "winProb": .65, "lockedAt": now - 5 * 86400000, "settledAt": now - 4 * 86400000})
        for i in range(30):
            led.append({"id": f"c{i}", "hA": "A", "awA": "B", "sport": "NFL", "league": "NFL", "betType": "ML", "betOn": "A", "date": "2026-10-15", "outcome": "win" if i % 10 < 7 else "loss", "decOdds": 1.7, "winProb": .65, "lockedAt": now - 5 * 86400000, "settledAt": now - 4 * 86400000})
        self.seed([e("NBA", "Zephyrs", "Quokkas", "OVER 214.5", .72), e("NHL", "Pelicans", "Marmots", "UNDER 6.5", .64, line=6.5, shift=1)], led)
        r = self.top_rows()
        self.assertIn("UNDER 6.5", r[0])                                          # the strong kind leads even though its probability is lower
        self.assertIn("DEMOTED", r[1])
        self.assertIn("85% LAST 40 SAME KIND", r[0])

    def test_lines_from_other_days_are_ignored(self):
        self.seed([e("NBA", "Zephyrs", "Quokkas", "OVER 214.5", .70)])
        self.pg.evaluate("Object.values(window._altReg).forEach(r=>{r.date='2020-01-01'});document.getElementById('toppicks-adj').innerHTML=_topAdjHTML()")
        self.assertEqual(len(self.rows()), 0)
        self.assertIn("NO ADJUSTED LINES TODAY YET", self.pg.evaluate("document.getElementById('toppicks-adj').innerText"))

    def test_the_live_layer_refresh_repaints_the_open_sub_tab_without_a_model_run(self):
        self.seed([e("NBA", "Walruses", "Narwhals", "OVER 221.5", .66)])
        self.pg.evaluate("document.getElementById('sp-toppicks').classList.add('act')")
        self.pg.evaluate("_LV.games=[{k:'nba',tag:'NBA',id:'x',home:'Walruses',away:'Narwhals',homeScore:60,awayScore:55,state:'in',note:'Q3 4:00'}];_tpRefreshResults()")
        self.assertIn("Narwhals 55", " ".join(self.rows()))
        self.pg.evaluate("document.getElementById('sp-toppicks').classList.remove('act')")


if __name__ == "__main__":
    unittest.main(verbosity=2)
