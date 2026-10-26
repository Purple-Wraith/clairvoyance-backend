#!/usr/bin/env python3
"""2026-10-26 owner fixes: NBA Props tab (top 10 of the day + props grouped by game), soccer goal-spread team labels follow the stepper, and the app's college football list /
capture use the SAME conference rule as the lock engine (>=1 team in AAC/ACC/Big 12/Big Ten/MWC/Pac-12/SEC, or Notre Dame).

    python3 scripts/test_ui_20261026.py
"""
import functools, http.server, json, socketserver, sys, threading, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import auto_lock_settle as A  # noqa: E402


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass


class Ui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "docs")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.pg = cls.browser.new_page()
        for pat in ("**/*espn*/**", "**/*workers.dev*", "**/*open-meteo*/**", "**/*nominatim*/**"):
            cls.pg.route(pat, lambda r: r.abort())
        cls.pg.goto(f"http://127.0.0.1:{cls.srv.server_address[1]}/app.html?nosb=1&nosw=1")
        cls.pg.wait_for_function("typeof renderNBADailyProps==='function'&&typeof loadCFBData==='function'&&typeof _adjV==='function'")
        cls.pg.wait_for_timeout(1500)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    # ---------- NBA props tab ----------
    def render_props(self, props, slate):
        return self.pg.evaluate("""async([props,slate])=>{
          document.getElementById('nba-dp-list')||document.body.insertAdjacentHTML('beforeend','<div id="nba-dp-list"></div>');
          window._fetchNBAPlayerStats=async()=>({x:1});
          window._generateNBAProps=()=>props;
          NBA_TONIGHT.length=0;slate.forEach(g=>NBA_TONIGHT.push(g));
          await renderNBADailyProps();
          const el=document.getElementById('nba-dp-list');
          return {html:el.innerHTML,text:el.innerText,top:[...el.querySelectorAll('.card')].length}}""", [props, slate])

    def prop(self, player, hA, awA, prob, grade="OPTIMAL", over=True, team=None):
        return {"player": player, "team": team or hA, "opp": awA if (team or hA) == hA else hA, "hA": hA, "awA": awA, "stat": "ppg", "statAbbr": "PTS", "line": 20.5, "over": over,
                "ml": "-150", "prob": prob, "grade": grade, "hitRate": f"{round(prob*100)}%", "adjustor": 0.5, "basis": "b", "alt": None}

    def test_top_ten_then_games(self):
        props = [self.prop(f"Player {i}", "BOS", "NYK", .70 - i * .01) for i in range(8)] + [self.prop(f"Guy {i}", "DEN", "LAL", .68 - i * .01, team="LAL") for i in range(6)]
        props += [self.prop("Skippy", "BOS", "NYK", .99, grade="SKIP"), self.prop("Late", "MIA", "ORL", .90)]
        slate = [{"h": "BOS", "a": "NYK", "state": "pre", "id": "1"}, {"h": "DEN", "a": "LAL", "state": "pre", "id": "2"}, {"h": "MIA", "a": "ORL", "state": "post", "id": "3", "hScore": 100, "aScore": 90}]
        r = self.render_props(props, slate)
        t = r["text"]
        self.assertIn("TOP 10 PROPS TODAY", t)
        top = t.split("TOP 10 PROPS TODAY")[1].split("PROPS ·")[0]
        self.assertNotIn("Skippy", top)                         # SKIP is never listed
        self.assertNotIn("Late", top)                           # its game is already final, others are still ahead
        self.assertEqual(top.count("PTS"), 10)
        self.assertLess(t.index("NYK @ BOS"), t.index("LAL @ DEN"))   # slate order
        self.assertIn("NYK @ BOS", t); self.assertIn("ORL @ MIA", t)
        self.assertIn("FINAL", t)
        self.assertIn("Skippy", t.split("NYK @ BOS", 1)[1])      # every prop still appears under its game

    def test_apostrophe_names_do_not_break_the_lock_button(self):
        r = self.render_props([self.prop("De'Aaron Fox", "SA", "OKC", .66)], [{"h": "SA", "a": "OKC", "state": "pre", "id": "9"}])
        self.assertIn("De\\'Aaron Fox", r["html"])
        ok = self.pg.evaluate("""()=>{let bad=0;document.querySelectorAll('#nba-dp-list button[onclick]').forEach(b=>{try{new Function(b.getAttribute('onclick'))}catch(e){bad++}});return bad}""")
        self.assertEqual(ok, 0)

    def test_all_started_falls_back_to_whole_slate(self):
        r = self.render_props([self.prop("Done Guy", "MIA", "ORL", .8)], [{"h": "MIA", "a": "ORL", "state": "post", "id": "3", "hScore": 1, "aScore": 2}])
        self.assertIn("TOP 1 PROPS TODAY", r["text"])

    # ---------- soccer goal spread ----------
    def test_goal_spread_labels_follow_the_stepper(self):
        out = self.pg.evaluate("""()=>{
          document.body.insertAdjacentHTML('beforeend','<div id="t_ah"><input id="ahinp_lg_x_A_B" value="1.5"><button><span id="fav_ahinp_lg_x_A_B">-1.5</span></button><button><span id="dog_ahinp_lg_x_A_B">+1.5</span></button></div>');
          const f=()=>[document.getElementById('fav_ahinp_lg_x_A_B').textContent,document.getElementById('dog_ahinp_lg_x_A_B').textContent];
          _adjV('ahinp_lg_x_A_B',0.5);const a=f();_adjV('ahinp_lg_x_A_B',-1);const b=f();
          const i=document.getElementById('ahinp_lg_x_A_B');i.value='2.5';_syncSocAHLabels(i.id);const c=f();
          return [a,b,c]}""")
        self.assertEqual(out[0], ["-2.0", "+2.0"])
        self.assertEqual(out[1], ["-1.0", "+1.0"])
        self.assertEqual(out[2], ["-2.5", "+2.5"])

    def test_goal_spread_card_wires_labels_and_typing(self):
        src = (ROOT / "docs" / "app.html").read_text()
        self.assertIn("""<span id="fav_'+ahId+'">-'+ahAbsLine.toFixed(1)+'</span>""", src)
        self.assertIn("""<span id="dog_'+ahId+'">+'+ahAbsLine.toFixed(1)+'</span>""", src)
        self.assertIn("""id="'+ahId+'" oninput="_syncSocAHLabels(this.id)\"""", src)

    # ---------- college football ----------
    def test_in_app_rule_matches_the_lock_engine_for_every_team(self):
        res = self.pg.evaluate("""async()=>{const D=await loadCFBData();return Object.keys(D.teamConf).map(a=>[a,_cfbTeamLockable(D,a)])}""")
        bad = [a for a, ok in res if ok != (a.upper() in A.cfb_lock_teams())]
        self.assertEqual(bad, [])
        self.assertTrue(self.pg.evaluate("()=>_cfbLockEligible(_CFB_DATA,'ALA','NOPE')"))       # one team is enough
        self.assertFalse(self.pg.evaluate("()=>_cfbLockEligible(_CFB_DATA,'KENN','DEL')"))     # Conference USA vs Conference USA
        self.assertFalse(self.pg.evaluate("()=>_cfbLockEligible(_CFB_DATA,'CONN','BALL')"))    # independent UConn vs MAC
        self.assertTrue(self.pg.evaluate("()=>_cfbLockEligible(_CFB_DATA,'ND','CONN')"))       # Notre Dame

    def test_games_tab_lists_only_lock_conference_games(self):
        sched = json.loads((ROOT / "docs" / "cfb_schedule.json").read_text())["weeks"]
        offered = self.pg.evaluate("async()=>{await loadCFBData();_cfbPopulateWeekFilter();return [...document.getElementById('cfb-filter-week').options].map(o=>o.value)}")
        mixed = [w for w in offered if any(not A.cfb_lock_eligible(g["home"], g["away"]) for g in sched[w]) and any(A.cfb_lock_eligible(g["home"], g["away"]) for g in sched[w])]
        self.assertTrue(mixed, "no offered week mixes lock-conference and other games")
        week = mixed[0]
        want = sum(1 for g in sched[week] if A.cfb_lock_eligible(g["home"], g["away"]))
        got = self.pg.evaluate("""(week)=>{
          const w=document.getElementById('cfb-filter-week');w.value=week;
          document.getElementById('cfb-filter-conf').value='all';window._cfbTop25Only=false;
          renderCFBGames();
          return document.querySelectorAll('#cfb-games-list > .gc').length}""", week)
        self.assertEqual(got, want)
        opts = self.pg.evaluate("[...document.querySelectorAll('#cfb-filter-conf option')].map(o=>o.textContent)")
        self.assertNotIn("MAC", opts)
        self.assertIn("Notre Dame", opts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
