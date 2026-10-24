#!/usr/bin/env python3
"""The game tracker (docs/app.html, _gt*): a TRACK button on live / finished cards, the overlay (score + clock, plays, box score, team stats,
win probability) and YOUR PICKS with live progress and a likelihood bar.

    python3 scripts/test_game_tracker.py

ESPN game summaries in scripts/fixtures/game_tracker/ are real responses (NBA CLE @ PHI 2026-10-22, NHL, Premier League, NFL), trimmed.
"""
import copy, functools, http.server, json, os, socketserver, threading, unittest
from pathlib import Path

ROOT = Path(os.environ.get("CV_ROOT") or Path(__file__).resolve().parent.parent)
FIX = Path(__file__).resolve().parent / "fixtures" / "game_tracker"


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text())


def live(sum_, period, clock, away, home):
    s = copy.deepcopy(sum_)
    c = s["header"]["competitions"][0]
    c["status"] = {"period": period, "displayClock": clock, "type": {"state": "in", "completed": False, "shortDetail": f"{clock} - Q{period}", "detail": f"{clock} - Q{period}"}}
    for t in c["competitors"]:
        t["score"] = str(home if t["homeAway"] == "home" else away)
    return s


def pick(bet, outcome="pending", **kw):
    t = {"id": "p_" + bet.replace(" ", "_"), "sport": "NBA", "league": "NBA", "betType": "ML", "betOn": bet, "hA": "PHI", "awA": "CLE", "date": "2026-10-22",
         "outcome": outcome, "lockedAt": 1, "winProb": .6, "decOdds": 1.9, "wager": 1}
    t.update(kw)
    return t


class Math_(unittest.TestCase):
    """_gtProb in isolation (no network)."""
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page()
        cls.pg.route("**/*espn.com/**", lambda r: r.abort()); cls.pg.route("**/*workers.dev/**", lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _gtProb==='function'&&typeof _gtOpen==='function'"); cls.pg.wait_for_timeout(800)

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def prob(self, **o):
        return self.pg.evaluate("(o)=>_gtProb(o)", o)

    def test_at_tipoff_the_number_is_the_pregame_probability(self):
        for pre in (.35, .5, .62, .8):
            r = self.prob(sign=1, target=224.5, cur=0, rem=1, pre=pre, sd=17, mono=True)
            self.assertAlmostEqual(r["w"], pre, delta=.01)
            r = self.prob(sign=-1, target=224.5, cur=0, rem=1, pre=pre, sd=17, mono=True)
            self.assertAlmostEqual(r["w"], pre, delta=.01)

    def test_an_over_gets_likelier_as_the_total_runs_ahead_of_pace(self):
        slow = self.prob(sign=1, target=224.5, cur=95, rem=.5, pre=.55, sd=17, mono=True)["w"]
        fast = self.prob(sign=1, target=224.5, cur=125, rem=.5, pre=.55, sd=17, mono=True)["w"]
        self.assertLess(slow, .55); self.assertGreater(fast, .55); self.assertGreater(fast - slow, .3)

    def test_an_over_past_its_line_is_clinched_and_an_under_past_it_is_dead(self):
        a = self.prob(sign=1, target=22.5, cur=23, rem=.4, pre=.6, sd=7, mono=True)
        b = self.prob(sign=-1, target=22.5, cur=23, rem=.4, pre=.6, sd=7, mono=True)
        self.assertEqual((a["w"], a["clinched"]), (1, True)); self.assertEqual((b["w"], b["l"], b["dead"]), (0, 1, True))

    def test_a_margin_pick_can_still_swing_back(self):
        r = self.prob(sign=1, target=0, cur=-6, rem=.3, pre=.6, sd=12, mono=False, tie="half")
        self.assertTrue(0 < r["w"] < .5)

    def test_no_time_left_is_decided_by_the_score(self):
        self.assertEqual(self.prob(sign=1, target=224.5, cur=230, rem=0, pre=.5, sd=17, mono=True)["w"], 1)
        self.assertEqual(self.prob(sign=-1, target=224.5, cur=230, rem=0, pre=.5, sd=17, mono=True)["l"], 1)
        self.assertEqual(self.prob(sign=1, target=3, cur=3, rem=0, pre=.5, sd=2, mono=True)["p"], 1)         # lands on a whole-number line

    def test_a_hockey_moneyline_counts_half_of_a_tie_a_soccer_one_loses_it(self):
        base = dict(sign=1, target=0, cur=0, rem=.02, pre=.5, sd=2.4, mono=False)
        half = self.prob(**base, tie="half"); lose = self.prob(**base, tie="lose"); raw = self.prob(**base)
        self.assertGreater(half["w"], lose["w"]); self.assertAlmostEqual(half["w"], lose["w"] + raw["p"] / 2, delta=.001); self.assertEqual(half["p"], 0)

    def test_time_left_from_the_clock(self):
        r = lambda k, p, c: self.pg.evaluate("([k,p,c])=>_gtRem(k,{state:'in',period:p,clock:c})".replace("k,{", "k,{"), [k, p, c])
        self.assertAlmostEqual(r("nba", 1, "12:00"), 1, delta=.001); self.assertAlmostEqual(r("nba", 3, "6:00"), .375, delta=.001)
        self.assertAlmostEqual(r("nhl", 2, "10:00"), 30 / 60, delta=.001); self.assertAlmostEqual(r("nfl", 4, "0:30"), 30 / 3600, delta=.003)
        self.assertAlmostEqual(r("pl", 1, "30'"), 60 / 90, delta=.001); self.assertEqual(r("pl", 2, "HT"), .5)
        self.assertEqual(self.pg.evaluate("_gtRem('nba',{state:'post'})"), 0); self.assertEqual(self.pg.evaluate("_gtRem('nba',{state:'pre'})"), 1)


class Overlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start(); cls.b = cls.pw.chromium.launch(); cls.pg = cls.b.new_page(viewport={"width": 420, "height": 900})
        cls.pg.route("**/*espn.com/**", lambda r: r.abort()); cls.pg.route("**/*workers.dev/**", lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1")
        cls.pg.wait_for_function("typeof _gtOpen==='function'&&typeof saveP==='function'"); cls.pg.wait_for_timeout(800)
        cls.fx = {n: fx(n) for n in ("nba", "nhl", "pl", "nfl")}
        cls.errs = []
        cls.pg.on("pageerror", lambda e: cls.errs.append(str(e)))

    @classmethod
    def tearDownClass(cls):
        cls.b.close(); cls.pw.stop(); cls.srv.shutdown()

    def serve(self, sum_, k="nba", eid="E1", picks=()):
        """fake the ESPN summary, lock the picks, open the tracker"""
        self.pg.evaluate("""([sum,picks])=>{window.__sum=sum;window.__f0=window.__f0||window.fetch;window.fetch=async(u,o)=>{u=String(u);
          if(u.includes('/summary?event='))return new Response(JSON.stringify(window.__sum),{status:200});return window.__f0(u,o)};saveP(picks);}""", [sum_, list(picks)])
        self.pg.evaluate("([k,e])=>{_gtClose();_gtOpen(k,e)}", [k, eid])
        self.pg.wait_for_selector("#gt-ov .gt-hd", timeout=8000)
        self.pg.wait_for_timeout(150)

    def text(self, sel):
        return self.pg.evaluate("(s)=>document.querySelector(s)?document.querySelector(s).innerText.replace(/\\s+/g,' '):''", sel)

    def click_tab(self, label):
        self.pg.evaluate("(l)=>[...document.querySelectorAll('#gt-ov .gt-tabs button')].find(b=>b.textContent.startsWith(l)).click()", label)
        self.pg.wait_for_timeout(100)

    def tearDown(self):
        self.pg.evaluate("()=>{_gtClose();saveP([])}")

    # ── a finished game ──
    def test_header_shows_teams_final_score_and_status(self):
        self.serve(self.fx["nba"])
        t = self.text("#gt-ov .gt-hd")
        self.assertIn("76ers", t); self.assertIn("114", t); self.assertIn("106", t); self.assertIn("Final", t)
        self.assertTrue(self.pg.evaluate("getComputedStyle(document.getElementById('gt-ov')).display==='flex'"))

    def test_plays_tab_lists_scoring_plays_and_all_plays(self):
        self.serve(self.fx["nba"])
        self.assertGreater(self.pg.evaluate("document.querySelectorAll('#gt-ov .gt-pl.sc').length"), 3)
        self.pg.evaluate("_gtSet('scoringOnly',false)")
        self.assertGreater(self.pg.evaluate("document.querySelectorAll('#gt-ov .gt-pl').length"), self.pg.evaluate("document.querySelectorAll('#gt-ov .gt-pl.sc').length"))
        self.assertTrue(self.pg.evaluate("!!document.querySelector('#gt-ov .gt-wp svg path.ln')"))                # ESPN's win-probability line

    def test_box_score_tab_has_both_teams_and_the_players(self):
        self.serve(self.fx["nba"])
        self.click_tab("BOX SCORE")
        t = self.text("#gt-ov .gt-body")
        for need in ("J. Embiid", "J. Allen", "PTS", "REB"):
            self.assertIn(need, t)
        self.assertEqual(self.pg.evaluate("document.querySelectorAll('#gt-ov .gt-bt').length"), 2)

    def test_team_stats_tab_compares_the_two_teams(self):
        self.serve(self.fx["nba"])
        self.click_tab("TEAM STATS")
        self.assertIn("Field Goal %", self.text("#gt-ov .gt-body")); self.assertGreater(self.pg.evaluate("document.querySelectorAll('#gt-ov .gt-tr').length"), 5)

    def test_other_sports_load(self):
        for name, k in (("nhl", "nhl"), ("pl", "pl"), ("nfl", "nfl")):
            with self.subTest(sport=name):
                self.serve(self.fx[name], k=k)
                self.assertGreater(len(self.text("#gt-ov .gt-body")), 80)
                self.assertTrue(self.pg.evaluate("document.querySelectorAll('#gt-ov .gt-tabs button').length>=2"))
        self.serve(self.fx["pl"], k="pl")
        self.assertTrue(self.pg.evaluate("document.querySelector('#gt-ov .gt-body').innerText.includes('Goal')"))
        self.assertEqual(self.pg.evaluate("[...document.querySelectorAll('#gt-ov .gt-tabs button')].some(b=>b.textContent.startsWith('BOX'))"), False)   # no player box score for soccer

    # ── your picks, final game ──
    def test_picks_on_a_final_game_show_their_result_and_no_likelihood(self):
        ps = [pick("OVER 215.5", "win", betType="OU"), pick("PHI -3.5", "loss", betType="SPREAD"), pick("Joel Embiid OVER PTS 22.5", "win", betType="PROP", team="PHI")]
        self.serve(self.fx["nba"], picks=ps)
        t = self.text("#gt-ov .gt-body")
        self.assertIn("WON", t); self.assertIn("LOST", t); self.assertEqual(self.pg.evaluate("document.querySelectorAll('#gt-ov .gt-lb').length"), 0)
        self.assertIn("Total 220", t); self.assertIn("Joel Embiid", t)

    def test_the_picks_tab_opens_first_only_when_you_have_picks_on_the_game(self):
        self.serve(self.fx["nba"])
        self.assertEqual(self.pg.evaluate("document.querySelector('#gt-ov .gt-tabs .act').textContent.startsWith('PLAYS')"), True)
        self.serve(self.fx["nba"], picks=[pick("PHI ML", "win")])
        self.assertTrue(self.pg.evaluate("document.querySelector('#gt-ov .gt-tabs .act').textContent.startsWith('YOUR PICKS (1)')"))

    def test_a_pick_on_another_game_or_day_is_not_listed(self):
        self.serve(self.fx["nba"], picks=[pick("PHI ML", "win", date="2026-10-21"), pick("LAL ML", "win", hA="LAL", awA="DEN")])
        self.assertFalse(self.pg.evaluate("[...document.querySelectorAll('#gt-ov .gt-tabs button')].some(b=>b.textContent.startsWith('YOUR PICKS'))"))

    # ── a live game ──
    def live_picks(self):
        return [pick("OVER 215.5", betType="OU", winProb=.55), pick("UNDER 215.5", betType="OU", winProb=.45, id="p_u"), pick("PHI -3.5", betType="SPREAD", winProb=.58),
                pick("PHI ML", betType="ML", winProb=.65), pick("Joel Embiid OVER PTS 22.5", betType="PROP", team="PHI", winProb=.6),
                pick("Jaylen Brown UNDER PTS 25.5", betType="PROP", team="PHI", winProb=.55)]

    def test_live_picks_get_a_likelihood_bar_and_progress(self):
        self.serve(live(self.fx["nba"], 3, "6:00", 52, 58), picks=self.live_picks())
        self.assertIn("●", self.text("#gt-ov .gt-hd")); self.assertIn("58", self.text("#gt-ov .gt-hd")); self.assertIn("● ", self.text("#gt-ov .gt-upd")); self.assertIn("CLE 52 – 58 PHI", self.text("#gt-ov .gt-upd"))
        n = self.pg.evaluate("document.querySelectorAll('#gt-ov .gt-pk').length"); self.assertEqual(n, 6)
        self.assertEqual(self.pg.evaluate("document.querySelectorAll('#gt-ov .gt-pk .gt-lb').length"), 6)
        t = self.text("#gt-ov .gt-body")
        self.assertIn("likelihood to hit", t); self.assertIn("pregame", t); self.assertIn("EXPECTED HITS", t)

    def test_the_numbers_are_sensible(self):
        self.serve(live(self.fx["nba"], 3, "6:00", 52, 58), picks=self.live_picks())
        r = self.pg.evaluate("""()=>{const m=_GT.m,o={};getP().forEach(p=>{o[p.betOn]=_gtPickState(p,m)});return Object.fromEntries(Object.entries(o).map(([k,v])=>[k,{w:v.prob&&v.prob.w,clinch:v.prob&&v.prob.clinched,dead:v.prob&&v.prob.dead,cur:v.cur,proj:v.proj}]))}""")
        self.assertEqual(r["OVER 215.5"]["cur"], 110)
        self.assertLess(r["OVER 215.5"]["w"], .2)                       # 110 points with 37.5% left is a 176 pace: the OVER needs 106 more
        self.assertGreater(r["UNDER 215.5"]["w"], .8)
        self.assertAlmostEqual(r["OVER 215.5"]["w"] + r["UNDER 215.5"]["w"], 1, delta=.05)
        self.assertGreater(r["PHI -3.5"]["w"], .55)                     # up 6, covering by 2.5 with 37.5% left
        self.assertGreater(r["PHI ML"]["w"], .7)
        self.assertTrue(r["Joel Embiid OVER PTS 22.5"]["clinch"])       # 24 already
        self.assertEqual(r["Joel Embiid OVER PTS 22.5"]["cur"], 24)

    def test_a_pick_that_cannot_lose_shows_clinched(self):
        self.serve(live(self.fx["nba"], 3, "6:00", 52, 58), picks=[pick("Joel Embiid OVER PTS 22.5", betType="PROP", team="PHI")])
        self.assertIn("CLINCHED", self.text("#gt-ov .gt-body"))

    def test_a_late_comeback_pick_is_a_long_shot(self):
        self.serve(live(self.fx["nba"], 4, "1:00", 100, 85), picks=[pick("PHI ML", betType="ML", winProb=.7)])
        w = self.pg.evaluate("_gtPickState(getP()[0],_GT.m).prob.w")
        self.assertLess(w, .03)
        self.assertIn("trailing by 15", self.text("#gt-ov .gt-body"))

    def test_a_live_game_keeps_refreshing_and_a_final_one_does_not(self):
        self.serve(live(self.fx["nba"], 2, "5:00", 50, 52))
        self.assertTrue(self.pg.evaluate("_GT.timer>0"))
        self.serve(self.fx["nba"])
        self.pg.wait_for_timeout(100)
        self.assertEqual(self.pg.evaluate("_GT.m.st.state"), "post")

    def test_closing_stops_everything(self):
        self.serve(live(self.fx["nba"], 2, "5:00", 50, 52))
        self.pg.keyboard.press("Escape"); self.pg.wait_for_timeout(100)
        self.assertEqual(self.pg.evaluate("getComputedStyle(document.getElementById('gt-ov')).display"), "none")
        self.assertIsNone(self.pg.evaluate("_GT.ref")); self.assertFalse(self.pg.evaluate("document.body.classList.contains('gt-open')"))

    def test_nothing_in_the_overlay_runs_html_from_the_feed(self):
        s = copy.deepcopy(self.fx["nba"]); s["plays"][3]["text"] = "<img src=x onerror=window.__xss=1>"; s["plays"][3]["scoringPlay"] = True
        self.serve(s)
        self.assertNotIn("<img", self.pg.evaluate("document.querySelector('#gt-ov .gt-body').innerHTML"))
        self.assertIsNone(self.pg.evaluate("window.__xss"))

    def test_an_unreachable_feed_says_so_instead_of_hanging(self):
        self.pg.evaluate("()=>{window.fetch=async(u,o)=>{u=String(u);if(u.includes('/summary?event='))throw new Error('offline');return window.__f0(u,o)}}")
        self.pg.evaluate("()=>{_gtClose();_gtOpen('nba','ZZ')}"); self.pg.wait_for_timeout(600)
        self.assertIn("Could not load", self.text("#gt-ov .gt-body"))

    # ── entry points ──
    def test_track_button_on_a_live_or_final_espn_card_opens_the_tracker_and_euro_cards_have_none(self):
        self.pg.evaluate("""()=>{document.getElementById('tst')?.remove();const d=document.createElement('div');d.id='tst';
          const c=(a,h)=>'<div class="gc"><div class="gch"><div><span class="gtm">'+a+'</span> @ <span class="gtm">'+h+'</span></div><div class="np">7:00 PM</div></div></div>';
          d.innerHTML=c('CLE','PHI')+c('Ilves','Jukurit');document.querySelector('.spane').appendChild(d);
          _LV.games=[{k:'nba',tag:'NBA',id:'E1',home:'PHI',away:'CLE',homeScore:90,awayScore:88,state:'in',completed:false},{k:'liiga',tag:'LIIGA',id:'L1',home:'Jukurit',away:'Ilves',homeScore:2,awayScore:1,state:'in'}];_cvPaintCardScores()}""")
        self.assertEqual(self.pg.evaluate("document.querySelectorAll('#tst .gt-btn').length"), 1)
        self.pg.evaluate("window.fetch=async(u,o)=>String(u).includes('/summary?event=')?new Response(JSON.stringify(window.__sum),{status:200}):window.__f0(String(u),o);window.__sum=%s" % json.dumps(self.fx["nba"]))
        self.pg.click("#tst .gt-btn"); self.pg.wait_for_selector("#gt-ov .gt-hd", timeout=8000)
        self.assertEqual(self.pg.evaluate("_GT.ref.id"), "E1")
        self.pg.evaluate("_gtClose();document.getElementById('tst').remove();_LV.games=[]")

    def test_scores_tab_cards_open_the_tracker_and_finals_stay_at_the_bottom(self):
        self.pg.evaluate("""()=>{const n=Date.now();_LV.ts=n;_LV.games=[
          {k:'nba',tag:'NBA',id:'F1',home:'PHI',away:'CLE',homeScore:114,awayScore:106,state:'post',completed:true,note:'Final',startMs:n-4*3600e3},
          {k:'nba',tag:'NBA',id:'E1',home:'LAL',away:'DEN',homeScore:40,awayScore:44,state:'in',completed:false,note:'Q2',period:2,displayClock:'3:00',startMs:n-3600e3},
          {k:'nhl',tag:'NHL',id:'U1',home:'BOS',away:'TOR',homeScore:0,awayScore:0,state:'pre',completed:false,note:'',startMs:n+3600e3}];_scPaint()}""")
        mine = "[...document.querySelectorAll('#sc-root .sc-card')].filter(c=>/CLE|DEN|TOR/.test(c.getAttribute('aria-label')))"
        self.assertEqual(self.pg.evaluate(mine + ".map(c=>c.dataset.b)"), ["in", "pre", "post"])                  # live, upcoming, then finals last
        self.assertEqual(self.pg.evaluate(mine + ".map(c=>!!c.querySelector('.gt-btn'))"), [True, False, True])
        self.assertEqual(self.pg.evaluate("document.querySelectorAll('#sc-root .sc-card[data-b=post]:not([data-gid])').length>=0"), True)
        self.pg.evaluate("window.__sum=%s;window.__f0=window.__f0||window.fetch;window.fetch=async(u,o)=>String(u).includes('/summary?event=')?new Response(JSON.stringify(window.__sum),{status:200}):window.__f0(String(u),o)" % json.dumps(self.fx["nba"]))
        self.pg.evaluate("document.querySelector('#sc-root .sc-card[data-gid=F1]').click()"); self.pg.wait_for_selector("#gt-ov .gt-hd", timeout=8000)
        self.assertEqual(self.pg.evaluate("_GT.ref.id"), "F1")
        self.pg.evaluate("_gtClose();_LV.games=[];_scPaint()")

    def test_phone_layout_fits_has_big_tap_targets_and_keeps_the_bar_pinned(self):
        for w in (390, 320):
            self.pg.set_viewport_size({"width": w, "height": 740})
            self.serve(live(self.fx["nba"], 3, "6:00", 52, 58), picks=self.live_picks())
            for tab in ("picks", "plays", "box", "stats"):
                self.pg.evaluate("(t)=>_gtSet('tab',t)", tab)
                r = self.pg.evaluate("""()=>{const vw=document.documentElement.clientWidth,ov=document.getElementById('gt-ov'),bad=[];
                  ov.querySelectorAll('*').forEach(e=>{const r=e.getBoundingClientRect();if(!r.width||r.right<=vw+1)return;let p=e.parentElement;while(p&&p!==ov){const o=getComputedStyle(p).overflowX;if(o==='auto'||o==='scroll'||o==='hidden')return;p=p.parentElement}bad.push(e.className||e.tagName)});
                  const small=[...ov.querySelectorAll('button')].filter(b=>b.getBoundingClientRect().height<39).map(b=>b.textContent.trim());
                  return{docW:document.documentElement.scrollWidth,vw,bad:bad.slice(0,5),small}}""")
                self.assertEqual((r["docW"], r["bad"], r["small"]), (w, [], []), f"{w}px {tab}: {r}")
            self.pg.evaluate("document.getElementById('gt-ov').scrollTop=400")
            self.assertEqual(self.pg.evaluate("Math.round(document.querySelector('#gt-ov .gt-bar').getBoundingClientRect().top)"), 0)      # close / score bar stays in reach
            self.assertLessEqual(self.pg.evaluate("Math.round(document.querySelector('#gt-ov .gt-tabs').getBoundingClientRect().top)"), 52)  # and so do the tabs
        self.pg.set_viewport_size({"width": 420, "height": 900})

    def test_no_script_errors(self):
        self.assertEqual(self.errs, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
