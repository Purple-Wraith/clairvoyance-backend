#!/usr/bin/env python3
"""Offline tests for the injury-data wiring (2026-10-03).  No network, no Supabase, no browser.

    python3 scripts/test_injury_wiring.py

PRODUCER side (pure parsers in scripts/_espn_injuries.py + scripts/_nhl_skaters.py, fixtures shaped like the REAL ESPN / NHL-API
responses captured 2026-10-03):
  * the NHL roster bug (`'str' object has no attribute 'get'` on ESPN's position-grouped athletes) is reproduced against the OLD loop
    and shown fixed; flat (NBA) and NFL-group shapes still parse;
  * injury rows get their team from athlete.team.abbreviation (ESPN no longer sends it on the team entry -> every row had team "" and
    docs/nfl_injuries.json was {"teams":{}}), the ESPN athlete id from the player-card link, NHL abbreviations mapped to the app's keys;
  * the NFL file keeps the same row schema, drops "Active" rows, and carries playerId;
  * skater-value shrinkage math, rookie default, name-collision lists.
APP side (node): the injury helpers + nflMC are extracted verbatim from docs/app.html and run in a vm sandbox:
  fail-open (no data / stale / unmatched / ambiguous / wrong team => exactly zero), identity matching, severity, caps, the goalie rule,
  the NFL "never adjust a market-anchored margin" rule, and that app-side name normalisation == the Python one.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _espn_injuries as E  # noqa: E402
import _nhl_skaters as K  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "docs" / "app.html").read_text()


def player(pid, name, pos_abbr, team_abbr=None, injuries=None):
    p = {"id": pid, "fullName": name, "displayName": name, "position": {"abbreviation": pos_abbr}, "injuries": injuries or []}
    if team_abbr:
        p["team"] = {"abbreviation": team_abbr}
    return p


# ---- fixtures shaped like the live responses ---------------------------------------------------------------------------------------
NHL_ROSTER_GROUPED = {  # ESPN hockey/nhl/teams/{id}/roster: athletes grouped by position, group.position is a STRING
    "team": {"abbreviation": "ANA"},
    "athletes": [
        {"position": "Centers", "items": [player("5149153", "Leo Carlsson", "C"), player("5831", "Mikael Granlund", "C")]},
        {"position": "Defense", "items": [player("4565270", "Drew Helleson", "D")]},
        {"position": "Goalies", "items": [player("5", "Lukas Dostal", "G")]},
    ],
}
NBA_ROSTER_FLAT = {"athletes": [player("1", "Nickeil Alexander-Walker", "G"), player("2", "Cameron Corhen", "F")]}
NFL_ROSTER_GROUPS = {"athletes": [
    {"position": "offense", "items": [player("11", "Kyler Murray", "QB")]},
    {"position": "defense", "items": []},
    {"position": "suspended", "items": []},
]}


def inj_row(pid_link, name, pos, team_abbr, status, **details):
    ath = {"displayName": name, "position": {"abbreviation": pos},
           "links": [{"rel": ["playercard"], "href": f"https://www.espn.com/nhl/player/_/id/{pid_link}/some-name"}]}
    if team_abbr:
        ath["team"] = {"abbreviation": team_abbr}
    return {"id": "999999", "status": status, "date": "2026-10-01T10:00Z", "athlete": ath,
            "details": details, "shortComment": "short", "longComment": "x" * 400}


INJ_PAYLOAD = {"injuries": [   # team ENTRY has id + displayName only (no abbreviation) -- the real 2026 shape
    {"id": "25", "displayName": "Anaheim Ducks", "injuries": [
        inj_row("4565270", "Drew Helleson", "D", "ANA", "Injured Reserve", type="Lower Body", returnDate="2026-10-07"),
    ]},
    {"id": "5", "displayName": "Los Angeles Kings", "injuries": [
        inj_row("111", "Some King", "C", "LA", "Out", type="Upper Body"),
        inj_row("222", "No Link Guy", "LW", "LA", "Day-To-Day"),
    ]},
]}


class OldBugReproduction:
    @staticmethod
    def old_roster_loop(roster):  # verbatim shape of the pre-fix loop
        out = {}
        for p in (roster or {}).get("athletes", []):
            pos = (p.get("position") or {}).get("abbreviation", "")
            name = p.get("fullName", "")
            if name:
                out[name.lower()] = {"team": "X", "pos": pos}
        return out


class RosterParsing(unittest.TestCase):
    def test_old_loop_crashed_on_grouped_nhl_shape(self):
        with self.assertRaises(AttributeError) as cm:
            OldBugReproduction.old_roster_loop(NHL_ROSTER_GROUPED)
        self.assertIn("'str' object has no attribute 'get'", str(cm.exception))

    def test_grouped_nhl_roster_parses(self):
        r = E.parse_roster(NHL_ROSTER_GROUPED, "ANA")
        self.assertEqual(len(r), 4)
        self.assertEqual(r["leo carlsson"], {"team": "ANA", "pos": "C", "id": "5149153"})
        self.assertEqual(r["lukas dostal"]["pos"], "G")

    def test_nhl_abbreviation_mapped_to_app_keys(self):
        self.assertEqual(E.parse_roster(NHL_ROSTER_GROUPED, "LA", E.NHL_ABBR_FIX)["leo carlsson"]["team"], "LAK")

    def test_flat_and_nfl_group_shapes_still_parse(self):
        self.assertEqual(E.parse_roster(NBA_ROSTER_FLAT, "ATL")["cameron corhen"], {"team": "ATL", "pos": "F", "id": "2"})
        self.assertEqual(list(E.parse_roster(NFL_ROSTER_GROUPS, "ARI")), ["kyler murray"])

    def test_garbage_is_skipped_not_raised(self):
        self.assertEqual(E.parse_roster({"athletes": ["Centers", None, {"position": "Centers"}, {"items": "nope"}]}, "X"), {})
        self.assertEqual(E.parse_roster(None, "X"), {})
        self.assertEqual(E.parse_roster({}, "X"), {})


class InjuryParsing(unittest.TestCase):
    def test_team_comes_from_athlete_not_the_entry(self):
        rows = E.parse_injuries(INJ_PAYLOAD, "nhl")
        self.assertEqual([r["team"] for r in rows], ["ANA", "LA", "LA"])
        self.assertTrue(all(r["team"] for r in rows))

    def test_nhl_abbr_fix(self):
        rows = E.parse_injuries(INJ_PAYLOAD, "nhl", abbr_fix=E.NHL_ABBR_FIX)
        self.assertEqual([r["team"] for r in rows], ["ANA", "LAK", "LAK"])

    def test_athlete_id_from_link_not_injury_record_id(self):
        rows = E.parse_injuries(INJ_PAYLOAD, "nhl")
        self.assertEqual(rows[0]["id"], "4565270")  # NOT the injury record's "999999"

    def test_schema_is_backward_compatible(self):
        r = E.parse_injuries(INJ_PAYLOAD, "nhl")[0]
        for k in ("team", "name", "pos", "status", "detail", "return", "sport"):
            self.assertIn(k, r)
        self.assertEqual((r["detail"], r["return"], r["status"], r["sport"]), ("Lower Body", "2026-10-07", "Injured Reserve", "nhl"))

    def test_team_fallbacks(self):
        d = {"injuries": [{"team": {"abbreviation": "OLD"}, "injuries": [{"status": "Out", "athlete": {"displayName": "A"}}]},
                          {"team": {"id": "7"}, "injuries": [{"status": "Out", "athlete": {"displayName": "B"}}]}]}
        rows = E.parse_injuries(d, "x", id_to_abbr={"7": "SEV"})
        self.assertEqual([r["team"] for r in rows], ["OLD", "SEV"])
        self.assertEqual(E.parse_injuries(None, "x"), [])

    def test_nfl_file_shape(self):
        d = {"injuries": [{"id": "22", "displayName": "Arizona Cardinals", "injuries": [
            inj_row("4428633", "Dadrion Taylor-Demerson", "S", "ARI", "Out", type="Back", returnDate="2026-10-11"),
            inj_row("1", "Healthy Guy", "WR", "ARI", "Active"),
            inj_row("2", "Quest Guy", "QB", "ARI", "Questionable"),
        ]}]}
        out = E.parse_nfl_injuries(d)
        self.assertEqual(list(out), ["teams"])
        rows = out["teams"]["ARI"]
        self.assertEqual([r["player"] for r in rows], ["Dadrion Taylor-Demerson", "Quest Guy"])  # Active dropped
        self.assertEqual(rows[0]["playerId"], "4428633")
        self.assertEqual(set(rows[0]), {"player", "playerId", "position", "status", "injury", "estimatedReturn", "comment", "date"})
        self.assertLessEqual(len(rows[0]["comment"]), 240)
        self.assertEqual(E.parse_nfl_injuries({"injuries": []}), {"teams": {}})


class NormName(unittest.TestCase):
    def test_cases(self):
        self.assertEqual(E.norm_name("Leevi Meriläinen"), "leevi merilainen")
        self.assertEqual(E.norm_name("  J.T.  Miller "), "j t miller")
        self.assertEqual(E.norm_name("Odell Beckham Jr."), "odell beckham jr")
        self.assertEqual(E.norm_name(None), "")


class SkaterValue(unittest.TestCase):
    def row(self, name, pos, gp, pts):
        return {"skaterFullName": name, "positionCode": pos, "gamesPlayed": gp, "points": pts}

    def test_shrinkage_math(self):
        # prior 100 pts / 80 gp = 1.25 ppg ; now 2 pts / 3 gp -> (2 + 20*1.25)/(3+20)
        out = K.build_skater_value([self.row("Star Man", "C", 3, 2)], [self.row("Star Man", "C", 80, 100)])
        self.assertAlmostEqual(out["star man"][0][1], (2 + 20 * 1.25) / 23, places=3)
        self.assertEqual(out["star man"][0][0], "F")

    def test_injured_star_with_no_current_games_is_his_prior_rate(self):
        out = K.build_skater_value([], [self.row("Hurt Star", "R", 70, 98)])
        self.assertAlmostEqual(out["hurt star"][0][1], 1.4, places=3)

    def test_rookie_is_shrunk_to_replacement_level(self):
        self.assertEqual(K.POS_DEFAULT_PPG, {"F": 0.30, "D": 0.20})
        self.assertNotIn("quiet rookie", K.build_skater_value([self.row("Quiet Rookie", "L", 4, 1)], []))      # (1+6)/24 = .29 -> dropped
        hot = K.build_skater_value([self.row("Hot Rookie", "L", 4, 6)], [])                                      # (6+6)/24 = .50 -> kept, far below a star
        self.assertAlmostEqual(hot["hot rookie"][0][1], 0.5, places=3)

    def test_short_prior_season_not_trusted(self):
        out = K.build_skater_value([], [self.row("Cup Of Coffee", "C", 5, 9)])   # 9 pts in 5 gp must NOT become 1.8 ppg
        self.assertNotIn("cup of coffee", out)

    def test_collision_keeps_both_groups_and_goalies_are_not_skaters(self):
        out = K.build_skater_value([], [self.row("Sebastian Aho", "C", 80, 77), self.row("Sebastian Aho", "D", 80, 40),
                                        self.row("Goalie Guy", "G", 60, 2)])
        self.assertEqual(sorted(g for g, _ in out["sebastian aho"]), ["D", "F"])
        self.assertNotIn("goalie guy", out)

    def test_fringe_dropped(self):
        self.assertEqual(K.build_skater_value([], [self.row("Fringe", "C", 60, 12)]), {})


# ---- app-side logic, extracted verbatim from docs/app.html and run in node ---------------------------------------------------------
def _between(start: str, end: str) -> str:
    i = APP.index(start)
    j = APP.index(end, i)
    return APP[i:j]


NHL_BLOCK = _between("// ═══ INJURY ADJUSTMENTS — shared helpers + NHL", "function nhlMC(hA,awA,n=25000,marketOU){")
NFL_BLOCK = _between("// ── NFL injury adjustment (added 2026-10-03", "function nflMC(hA,awA,n,ouLine,g){")
NFL_MC = _between("function nflMC(hA,awA,n,ouLine,g){", "// Beta-binomial posterior on this season's actual W-L record")

JS_HARNESS = r"""
const vm=require('vm');
const ctx={console,Date,Math,JSON,String,Array,Object,isFinite,parseFloat,Number,RegExp};
ctx.window={};
vm.createContext(ctx);
function run(code){return vm.runInContext(code,ctx);}
run('var NHL={}; var _NFL_DATA=null; const _NHL_LG_GF60=2.9;');
run(%(NHL_BLOCK)s);
run(%(NFL_BLOCK)s);
run('const _CFB_WX_CACHE={}; const cfbWeatherImpact=()=>({totalAdj:0,label:""}); const _forceHalfLine=x=>Math.round(x*2)/2; const _boxMullerZ=()=>0; const _NFL_LG_TOTAL=44, _NFL_SIGMA_MARGIN=13.5,_NFL_SIGMA_TOTAL=10.0; function _nflHFA(){return 1.8;}');
run(%(NFL_MC)s);
const now=new Date().toISOString();
const old=new Date(Date.now()-100*3600*1000).toISOString();
function setNhl(o){ctx.window.__CV_DATA=o;}
const SV={players:{
  'superstar one':[['F',1.85]], 'good forward':[['F',.80]], 'ok defender':[['D',.60]], 'depth guy':[['F',.30]],
  'sebastian aho':[['D',.41],['F',.97]], 'twin name':[['F',.9],['F',.5]],
  'f1':[['F',.9]],'f2':[['F',.9]],'f3':[['F',.9]],'f4':[['F',.9]],'f5':[['F',.9]]}};
function inj(team,name,pos,status,extra){return Object.assign({team,name,pos,status,sport:'nhl'},extra||{});}
function nhl(rows,opts){opts=opts||{};setNhl({generated:opts.generated||now,injuries:{nhl:rows},nhl:{skaterValue:opts.noSV?undefined:SV,edge:{goalies:{BOS:{name:'Linus Ullmark'}}}}});}
const R={};
// --- helpers
R.norm=['Leevi Meriläinen','  J.T.  Miller ','Odell Beckham Jr.','Nickeil Alexander-Walker',"D'Angelo Russell",'',null].map(n=>run('_injNorm')(n));
R.sev=['Out','Injured Reserve','15-Day-IL','Suspension','Doubtful','Questionable','Day-To-Day','Active','','Probable',null,'Out For Season'].map(s=>run('_injSev')(s));
R.stale={fresh:run('_injStale')(now,72),old:run('_injStale')(old,72),none:run('_injStale')(undefined,72),utc:run('_injStale')('2999-01-01 00:00 UTC',72),bad:run('_injStale')('garbage',72)};
// --- NHL
ctx.window.__CV_DATA=null; R.nhl_nodata=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Superstar One','C','Injured Reserve')]); R.nhl_star=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Good Forward','LW','Out')]); R.nhl_good=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Good Forward','LW','Questionable')]); R.nhl_quest=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Good Forward','LW','Injured Reserve',{return:'2020-01-01'})]); R.nhl_pastreturn=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Ok Defender','D','Out')]); R.nhl_d=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Depth Guy','C','Out')]); R.nhl_depth=run('_nhlInjAdj')('BOS');
nhl([inj('TOR','Good Forward','LW','Out')]); R.nhl_wrongteam=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Nobody Known','LW','Out')]); R.nhl_unknown=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Twin Name','C','Out')]); R.nhl_ambiguous=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Sebastian Aho','C','Out')]); R.nhl_aho_f=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Sebastian Aho','D','Out')]); R.nhl_aho_d=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Sebastian Aho','G','Out')]); R.nhl_aho_g=run('_nhlInjAdj')('BOS');
nhl(['F1','F2','F3','F4','F5'].map(n=>inj('BOS',n,'C','Out'))); R.nhl_teamcap=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Good Forward','LW','Out')],{generated:old}); R.nhl_stale=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Good Forward','LW','Out')],{noSV:true}); R.nhl_nosv=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Good Forward','LW','Active')]); R.nhl_active=run('_nhlInjAdj')('BOS');
// goalie: primary (MoneyPuck) goalie out -> mp factor; edge goalie out -> edge factor; backup out -> nothing
run('NHL.BOS={_liveGoalieStarter:"Jeremy Swayman"};');
nhl([inj('BOS','Jeremy Swayman','G','Injured Reserve')]); R.nhl_g_primary=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Joonas Korpisalo','G','Injured Reserve')]); R.nhl_g_backup=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Jeremy Swayman','G','Doubtful')]); R.nhl_g_doubtful=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Linus Ullmark','G','Out')]); R.nhl_g_edge=run('_nhlInjAdj')('BOS');
nhl([inj('BOS','Jeremy Swayman','G','Injured Reserve'),inj('BOS','Good Forward','LW','Out')]);
R.nhl_note=run('_nhlInjNote')({hA:'BOS',aA:'TOR',injAdj:{h:run('_nhlInjAdj')('BOS'),a:run('_nhlInjAdj')('TOR')}});
R.nhl_note_none=run('_nhlInjNote')({hA:'BOS',aA:'TOR',injAdj:{h:run('_nhlInjAdj')('TOR'),a:run('_nhlInjAdj')('TOR')}});
// --- NFL
const KEY=[{name:'Pat Mahomes',position:'QB',id:'100'},{name:'Wide One',position:'WR',id:'101'},{name:'Wide Two',position:'WR',id:'102'},
  {name:'Run Back',position:'RB',id:'103'},{name:'Tight End',position:'TE',id:'104'}];
function setNfl(rows,gen,standings,team){ctx.__nfl={injuriesByTeam:{[team||'KC']:rows},playerStats:{KC:KEY,LV:[{name:'Her QB',position:'QB',id:'200'}]},generatedAt:{injuries:gen||now},
  standings:standings||{KC:{wins:3,losses:0,ties:0,differential:30},LV:{wins:1,losses:2,ties:0,differential:-10}},stats:{KC:{},LV:{}}};run('_NFL_DATA=globalThis.__nfl');}
function ir(player,position,status,playerId,extra){return Object.assign({player,position,status,playerId,estimatedReturn:null},extra||{});}
run('_NFL_DATA=null'); R.nfl_nodata=run('_nflInjAdj')('KC');
setNfl([ir('Pat Mahomes','QB','Out','100')]); R.nfl_qb_id=run('_nflInjAdj')('KC');
setNfl([ir('Pat Mahomes','QB','Out',undefined)]); R.nfl_qb_name=run('_nflInjAdj')('KC');
setNfl([ir('Pat Mahomes','QB','Out','999')]); R.nfl_qb_idmismatch=run('_nflInjAdj')('KC');
setNfl([ir('Pat Mahomes','QB','Questionable','100')]); R.nfl_qb_quest=run('_nflInjAdj')('KC');
setNfl([ir('Pat Mahomes','QB','Active','100')]); R.nfl_qb_active=run('_nflInjAdj')('KC');
setNfl([ir('Some Backup','QB','Out','555')]); R.nfl_backup_qb=run('_nflInjAdj')('KC');
setNfl([ir('Wide One','WR','Out','101'),ir('Wide Two','WR','Out','102'),ir('Run Back','RB','Out','103'),ir('Tight End','TE','Out','104')]); R.nfl_skill_cap=run('_nflInjAdj')('KC');
setNfl([ir('Pat Mahomes','QB','Out','100'),ir('Wide One','WR','Out','101'),ir('Wide Two','WR','Out','102'),ir('Run Back','RB','Out','103'),ir('Tight End','TE','Out','104')]); R.nfl_team_cap=run('_nflInjAdj')('KC');
setNfl([ir('Left Tackle','OT','Out','777')]); R.nfl_ol=run('_nflInjAdj')('KC');
setNfl([ir('Pat Mahomes','QB','Out','100')],old); R.nfl_stale=run('_nflInjAdj')('KC');
setNfl([ir('Pat Mahomes','QB','Out','100')]); R.nfl_far_game=run('_nflInjAdj')('KC',{date:new Date(Date.now()+20*864e5).toISOString()});
setNfl([ir('Pat Mahomes','QB','Out','100')]); R.nfl_near_game=run('_nflInjAdj')('KC',{date:new Date(Date.now()+2*864e5).toISOString()});
// nflMC integration: margin0 / baseTotal move by exactly the cap-limited amounts, only on the model-derived margin
setNfl([]);
const g={home:'KC',away:'LV',date:new Date(Date.now()+2*864e5).toISOString()};
const base=run('nflMC')('KC','LV',10,null,g);
setNfl([ir('Pat Mahomes','QB','Out','100')]);
const hurtHome=run('nflMC')('KC','LV',10,null,g);
setNfl([ir('Her QB','QB','Out','200')],undefined,undefined,'LV');
const hurtAway=run('nflMC')('KC','LV',10,null,g);
R.mc={base:base.margin0,hurtHome:hurtHome.margin0,hurtAway:hurtAway.margin0,baseTotal:base.baseTotal,totHome:hurtHome.baseTotal,inj:hurtHome.inj.marginApplied,
      note:run('_nflInjNote')(hurtHome,g)};
// market-anchored margin (LV has no standings differential): margin must NOT move, total still does
setNfl([ir('Pat Mahomes','QB','Out','100')],now,{KC:{wins:3,losses:0,ties:0,differential:30}});
const gm=Object.assign({spread:-3},g);
const mkt=run('nflMC')('KC','LV',10,null,gm);
setNfl([],now,{KC:{wins:3,losses:0,ties:0,differential:30}});
const mkt0=run('nflMC')('KC','LV',10,null,gm);
R.mc_market={m0:mkt0.margin0,m1:mkt.margin0,t0:mkt0.baseTotal,t1:mkt.baseTotal,applied:mkt.inj.marginApplied,note:run('_nflInjNote')(mkt,gm)};
console.log(JSON.stringify(R));
"""


def run_node() -> dict:
    code = JS_HARNESS % {"NHL_BLOCK": json.dumps(NHL_BLOCK), "NFL_BLOCK": json.dumps(NFL_BLOCK), "NFL_MC": json.dumps(NFL_MC)}
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(code)
        path = f.name
    r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AssertionError(f"node harness failed:\n{r.stderr[-3000:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


class AppSide(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.R = run_node()

    def test_constants_match_what_the_python_side_assumes(self):
        self.assertIn("_NHL_LG_GF60=2.9", APP)
        self.assertIn("NHL_INJ_REPL_PPG={F:.30,D:.20}", APP)       # scripts/_nhl_skaters.py POS_DEFAULT_PPG == replacement level
        self.assertEqual(K.POS_DEFAULT_PPG, {"F": 0.30, "D": 0.20})

    def test_name_normaliser_matches_python(self):
        names = ["Leevi Meriläinen", "  J.T.  Miller ", "Odell Beckham Jr.", "Nickeil Alexander-Walker", "D'Angelo Russell", "", None]
        self.assertEqual(self.R["norm"], [E.norm_name(n) for n in names])

    def test_severity(self):
        self.assertEqual(self.R["sev"], [1, 1, 1, 1, 0.75, 0.3, 0.3, 0, 0, 0, 0, 1])

    def test_staleness(self):
        s = self.R["stale"]
        self.assertEqual((s["fresh"], s["old"], s["none"], s["utc"], s["bad"]), (False, True, True, False, True))

    def test_nhl_fail_open_cases_are_exactly_zero(self):
        for k in ("nhl_nodata", "nhl_wrongteam", "nhl_unknown", "nhl_ambiguous", "nhl_stale", "nhl_nosv", "nhl_active", "nhl_depth",
                  "nhl_aho_g", "nhl_g_backup"):
            a = self.R[k]
            self.assertEqual((a["frac"], a["any"], a["players"], a["goalie"]), (0, False, [], {"mp": 0, "edge": 0}), k)

    def test_nhl_star_is_capped_at_5pct(self):
        a = self.R["nhl_star"]
        self.assertAlmostEqual(a["frac"], 0.05)             # (1.85-.30)*.20/2.9 = 10.7% -> capped
        self.assertEqual(a["players"][0]["pct"], 5.0)

    def test_nhl_regular_forward_magnitude_and_severity(self):
        self.assertAlmostEqual(self.R["nhl_good"]["frac"], (0.80 - 0.30) * 0.20 / 2.9, places=6)           # ~3.4%
        self.assertAlmostEqual(self.R["nhl_quest"]["frac"], 0.3 * (0.80 - 0.30) * 0.20 / 2.9, places=6)
        self.assertAlmostEqual(self.R["nhl_pastreturn"]["frac"], 0.5 * (0.80 - 0.30) * 0.20 / 2.9, places=6)
        self.assertAlmostEqual(self.R["nhl_d"]["frac"], (0.60 - 0.20) * 0.20 / 2.9, places=6)

    def test_nhl_team_cap_8pct(self):
        self.assertAlmostEqual(self.R["nhl_teamcap"]["frac"], 0.08)
        self.assertEqual(len(self.R["nhl_teamcap"]["players"]), 5)

    def test_nhl_same_name_resolved_by_position_group(self):
        self.assertAlmostEqual(self.R["nhl_aho_f"]["frac"], (0.97 - 0.30) * 0.20 / 2.9, places=6)
        self.assertAlmostEqual(self.R["nhl_aho_d"]["frac"], (0.41 - 0.20) * 0.20 / 2.9, places=6)

    def test_nhl_goalie_rule(self):
        # fixture: BOS's MoneyPuck (gsax/hdsv) goalie is Swayman, its NHL-stats (sv) goalie is Ullmark -- each feed is neutralised independently
        self.assertEqual(self.R["nhl_g_primary"]["goalie"], {"mp": 1, "edge": 0})
        self.assertEqual(self.R["nhl_g_edge"]["goalie"], {"mp": 0, "edge": 1})
        self.assertEqual(self.R["nhl_g_doubtful"]["goalie"], {"mp": 0.75, "edge": 0})
        self.assertEqual(self.R["nhl_g_primary"]["frac"], 0)   # a goalie never creates a skater scoring penalty

    def test_nhl_note_names_the_players(self):
        n = self.R["nhl_note"]
        self.assertIn("Jeremy Swayman G OUT", n)
        self.assertIn("Good Forward LW OUT", n)
        self.assertIn("goalie rating set to league average", n)
        self.assertEqual(self.R["nhl_note_none"], "")

    def test_nfl_fail_open_cases_are_exactly_zero(self):
        for k in ("nfl_nodata", "nfl_qb_idmismatch", "nfl_qb_active", "nfl_backup_qb", "nfl_ol", "nfl_stale", "nfl_far_game"):
            a = self.R[k]
            self.assertEqual((a["pts"], a["any"], a["players"]), (0, False, []), k)

    def test_nfl_qb_magnitude_and_matching(self):
        self.assertEqual(self.R["nfl_qb_id"]["pts"], 2.5)
        self.assertEqual(self.R["nfl_qb_name"]["pts"], 2.5)          # no id on the injury row -> normalized name + position
        self.assertAlmostEqual(self.R["nfl_qb_quest"]["pts"], 0.75)
        self.assertEqual(self.R["nfl_near_game"]["pts"], 2.5)

    def test_nfl_caps(self):
        self.assertEqual(self.R["nfl_skill_cap"]["pts"], 1.0)        # 4 x .4 = 1.6 -> skill cap 1.0
        self.assertEqual(self.R["nfl_team_cap"]["pts"], 3.5)         # 2.5 + 1.0 = 3.5 (== team cap)

    def test_nflmc_moves_margin_by_the_adjustment_in_the_right_direction(self):
        m = self.R["mc"]
        self.assertAlmostEqual(m["base"] - m["hurtHome"], 2.5, places=2)      # home QB out -> home margin falls
        self.assertAlmostEqual(m["hurtAway"] - m["base"], 2.5, places=2)      # away QB out -> home margin rises
        self.assertAlmostEqual(m["baseTotal"] - m["totHome"], 1.25, places=2)  # half of the margin effect lands on the total
        self.assertTrue(m["inj"])
        self.assertIn("Pat Mahomes QB OUT", m["note"])

    def test_nflmc_never_adjusts_a_market_anchored_margin(self):
        m = self.R["mc_market"]
        self.assertEqual(m["m0"], m["m1"])
        self.assertAlmostEqual(m["t0"] - m["t1"], 1.25, places=2)
        self.assertFalse(m["applied"])
        self.assertIn("margin NOT adjusted", m["note"])


class StaticWiring(unittest.TestCase):
    def test_every_nhl_card_reasoning_string_carries_the_injury_note(self):
        self.assertEqual(APP.count("+_nhlInjNote(mc);"), 4)

    def test_nhl_lambdas_and_goalie_term_read_the_adjustment(self):
        self.assertIn("*hForm.mult*(1-injH.frac))", APP)
        self.assertIn("*aForm.mult*(1-injA.frac))", APP)
        self.assertIn("goalieComposite(aw,injA.goalie)", APP)

    def test_producer_no_longer_scrapes_dead_sources(self):
        src = (ROOT / "scripts" / "clairvoyance_update.py").read_text()
        self.assertNotIn("def fetch_hockeyviz", src)
        self.assertNotIn("hockeyviz.com", src)
        self.assertNotIn("def fetch_hockey_reference", src)
        self.assertIn("SOCCER_ROSTER_FETCH_ENABLED = False", src)
        for key in ('"hockeyviz":', '"hockeyRef":', '"hockeyRefTeams":'):
            self.assertIn(key, src)   # bundle keys stay present (empty) for defensive readers


if __name__ == "__main__":
    unittest.main(verbosity=1)
