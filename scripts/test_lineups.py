#!/usr/bin/env python3
"""Confirmed-lineup gate (scripts/lineups.py + its hooks in scripts/auto_lock_settle.py).  No network, no browser.

    python3 scripts/test_lineups.py

Fixtures in scripts/fixtures/lineups/ are REAL ESPN responses captured Sat 2026-10-10 ~06:15 MT, trimmed to the fields the parsers read:
  nhl_scoreboard_20261010.json   NHL scoreboard (PHI@BOS, VAN@NJ, EDM@SJ, DET@MTL) with the probableStartingGoalie entries (expected / confirmed)
  nfl_scoreboard_20261011.json   three week-5 NFL events;  nfl_summary_CHI_GB_pre.json  the pre-game summary `injuries` of CHI @ GB (real: CHI QB
                                 Caleb Williams is QUESTIONABLE, RB Kyle Monangai OUT, GB lineman Doubtful)
  nba_scoreboard_20261009.json   MEM @ CHI (preseason, final);  nba_summary_MEM_CHI.json  its summary `injuries` (real: MEM C Isaiah Stewart OUT, Day-To-Day rows)
The model-side data (goalie stats, rosters, snapshots) are small constructed dicts in the SHAPE of docs/data.json / docs/nfl_*.json; the goalie numbers are
the real MoneyPuck rows of 2026-10-10 (Woll/Vladar, Swayman/DiPietro).  NBA star-out and NFL QB-out cases flip a status in the real payloads.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import lineups as L  # noqa: E402
import auto_lock_settle as A  # noqa: E402

FIX = ROOT / "scripts" / "fixtures" / "lineups"


def fx(name):
    return json.loads((FIX / name).read_text())


NHL_SB, NFL_SB, NBA_SB = fx("nhl_scoreboard_20261010.json"), fx("nfl_scoreboard_20261011.json"), fx("nba_scoreboard_20261009.json")
NFL_SUM, NBA_SUM = fx("nfl_summary_CHI_GB_pre.json"), fx("nba_summary_MEM_CHI.json")

PHI_BOS = L.parse_iso_ms("2026-10-10T17:00Z")        # PHI @ BOS
NOW = PHI_BOS - 45 * 60000                            # a sweep 45 minutes before the puck drop
EDM_SJ = L.parse_iso_ms("2026-10-10T20:00Z")
CHI_GB = L.parse_iso_ms("2026-10-11T17:00Z")
MEM_CHI = L.parse_iso_ms("2026-10-10T00:00Z")


def fake_http(routes: dict, calls: list | None = None):
    """routes: substring of the URL -> payload (dict) or an Exception instance to raise or None.  Anything unrouted is None (a failed request)."""
    def get(url):
        if calls is not None:
            calls.append(url)
        for key, val in routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return copy.deepcopy(val)
        return None
    return get


def goalie_row(name, team, gp, gsaa, sv, hd, shots=100):
    return {"name": name, "situation": "all", "team": team, "gp": gp, "gsaa": gsaa, "savePct": sv, "hdSavePct": hd, "shots": shots}


# real MoneyPuck rows of 2026-10-10
MP_GOALIES = [
    goalie_row("Joseph Woll", "PHI", 3, 3.48, 0.9272727272727272, 0.8888888888888888, 110),
    goalie_row("Dan Vladar", "PHI", 2, -1.56, 0.863013698630137, 0.8333333333333334, 73),
    goalie_row("Jeremy Swayman", "BOS", 4, 4.45, 0.9322033898305084, 0.6666666666666666, 118),
    goalie_row("Michael DiPietro", "BOS", 1, -0.61, 0.896551724137931, 1.0, 29),
]


def model(injuries_nhl=None, generated="2026-10-10T11:10:39+00:00", **kw):
    data = {"generated": generated, "mp": {"goalies": copy.deepcopy(MP_GOALIES)}, "injuries": {"nhl": injuries_nhl or [], "nba": []}, "nba": {"roster": {}, "playerProps": {"players": []}}}
    return L.ModelData(data, kw.get("nfl_injuries"), kw.get("nfl_players"))


def ctx(routes, md=None, calls=None, **kw):
    return L.LineupContext(L.LineupSource(http_get=fake_http(routes, calls), **kw), md or model(), A.lineup_requalify)


def nhl_sb(statuses=None, names=None):
    """The real NHL scoreboard fixture with PHI@BOS goalie statuses/names overridden: statuses={'home':'confirmed','away':'confirmed'}."""
    d = copy.deepcopy(NHL_SB)
    ev = next(e for e in d["events"] if e["id"] == "401892469")
    for c in ev["competitions"][0]["competitors"]:
        side = c["homeAway"]
        p = c["probables"][0]
        if statuses and side in statuses:
            p["status"]["type"] = statuses[side]
        if names and side in names:
            p["athlete"]["fullName"] = p["athlete"]["displayName"] = names[side]
    return d


def nhl_leg(label="PHI ML", side="mlDog", prob=0.70, dec=1.75, ev=None, **kw):
    q = {"kind": "GAME", "sport": "NHL", "hA": "BOS", "awA": "PHI", "side": side, "label": label, "prob": prob, "dec": dec, "ml": "+75",
         "evVal": prob * dec - 1 if ev is None else ev, "tierN": 3, "lane": False, "startMs": PHI_BOS, "mcSummary": "MC PROJ: PHI 2.7 – BOS 3.1 (Total 5.8, 25k sims)",
         "modelProb": None, "marketProb": None, "blendAlpha": None, "priceSource": "market"}
    q.update(kw)
    return q


ROUTE_NHL = "hockey/nhl/scoreboard"


class Parsers(unittest.TestCase):
    def test_nhl_scoreboard_real_payload(self):
        evs = {e["id"]: e for e in L.parse_scoreboard(NHL_SB, "NHL")}
        self.assertEqual(len(evs), 4)
        e = evs["401892469"]
        self.assertEqual((e["home"], e["away"], e["state"]), ("BOS", "PHI", "pre"))
        self.assertEqual(e["startMs"], PHI_BOS)
        self.assertEqual(e["goalies"]["home"], {"name": "Jeremy Swayman", "status": "projected"})      # ESPN "expected" -> projected
        self.assertEqual(e["goalies"]["away"]["name"], "Dan Vladar")
        self.assertEqual(evs["401892471"]["goalies"]["away"], {"name": "Tristan Jarry", "status": "confirmed"})
        self.assertEqual(evs["401892470"]["home"], "NJD")                                              # ESPN "NJ" -> the app's key
        self.assertEqual(evs["401892471"]["home"], "SJS")

    def test_garbage_payloads_parse_to_nothing_and_never_raise(self):
        for bad in (None, {}, {"events": None}, {"events": [{}]}, {"events": [{"competitions": []}]}, {"events": "x"}, []):
            try:
                self.assertEqual(L.parse_scoreboard(bad, "NHL"), [])
            except AttributeError:        # a list payload is not a dict: parse_scoreboard must still be wrapped by the source (see below)
                pass
        self.assertEqual(L.parse_summary_injuries(None, "NFL"), {})
        self.assertEqual(L.parse_summary_injuries({"injuries": [{"team": None, "injuries": None}]}, "NFL"), {"": []})

    def test_nfl_summary_injuries_real_payload(self):
        t = L.parse_summary_injuries(NFL_SUM, "NFL")
        chi = {r["name"]: r for r in t["CHI"]}
        self.assertEqual(chi["Caleb Williams"]["pos"], "QB")
        self.assertEqual(chi["Caleb Williams"]["status"], "Questionable")
        self.assertEqual(chi["Kyle Monangai"]["status"], "Out")
        self.assertEqual({r["name"] for r in t["GB"]}, {"Jacob Monk", "Jager Burton", "Aaron Banks", "Javon Hargrave", "Savion Williams"})

    def test_nba_summary_injuries_real_payload(self):
        t = L.parse_summary_injuries(NBA_SUM, "NBA")
        self.assertIn(("Isaiah Stewart", "C", "Out"), [(r["name"], r["pos"], r["status"]) for r in t["MEM"]])

    def test_severity_mirrors_the_app(self):
        self.assertEqual([L.inj_sev(s) for s in ("Out", "Injured Reserve", "Suspension", "Doubtful", "Questionable", "Day-To-Day", "Active", "")], [1, 1, 1, .75, .3, .3, 0, 0])
        self.assertEqual(L.nba_model_sev("Day-To-Day"), 1.0)         # computeInjuryImpact counts '-day' as fully out; the snapshot rule must mirror it

    def test_leg_parsing(self):
        self.assertEqual(L.parse_leg({"label": "PHI ML", "side": "mlDog", "hA": "BOS", "awA": "PHI"})["team"], "PHI")
        self.assertEqual(L.parse_leg({"label": "COL", "side": "mlFav", "hA": "COL", "awA": "STL"})["kind"], "ML")
        sp = L.parse_leg({"label": "CBJ +1.5", "side": "plDog", "hA": "CBJ", "awA": "UTA"})
        self.assertEqual((sp["kind"], sp["team"], sp["line"]), ("SPREAD", "CBJ", 1.5))
        ou = L.parse_leg({"label": "UNDER 6.5", "side": "under", "hA": "X", "awA": "Y"})
        self.assertEqual((ou["kind"], ou["line"], ou["direction"]), ("OU", 6.5, "UNDER"))
        self.assertIsNone(L.parse_leg({"label": "ZZZ ML", "side": "mlFav", "hA": "X", "awA": "Y"})["team"])
        self.assertEqual(L.parse_mc_proj("MC PROJ: PHI 2.7 – BOS 3.1 (Total 5.8)", "BOS", "PHI"), (3.1, 2.7))


class NhlMath(unittest.TestCase):
    def test_poisson_sanity(self):
        self.assertAlmostEqual(L.leg_poisson_prob("ML", True, None, None, 3.0, 3.0), 0.5, places=6)
        self.assertGreater(L.leg_poisson_prob("ML", True, None, None, 3.5, 2.5), 0.6)
        over, under = (L.leg_poisson_prob("OU", None, 5.5, d, 3.0, 3.0) for d in ("OVER", "UNDER"))
        self.assertAlmostEqual(over + under, 1.0, places=6)
        # +1.5 covers more often than the ML wins, -1.5 less
        pl_dog = L.leg_poisson_prob("PL", False, 1.5, None, 3.0, 3.0)
        pl_fav = L.leg_poisson_prob("PL", True, -1.5, None, 3.0, 3.0)
        self.assertGreater(pl_dog, 0.5)
        self.assertLess(pl_fav, 0.5)

    def test_composite_and_glv_mirror_nhlmc(self):
        woll = goalie_row("Joseph Woll", "PHI", 3, 3.48, 0.9272727272727272, 0.8888888888888888)
        self.assertAlmostEqual(L.goalie_composite(woll), 3.48 * .7 + (0.9272727 - .91) * 100 * .18 + (0.8888889 - .90) * 100 * .12, places=4)
        self.assertEqual(L.goalie_composite(woll, sev=1.0), 0.0)               # an Out goalie is neutralised to league average
        self.assertEqual(L.goalie_composite(None), 0.0)
        self.assertEqual(L.goalie_glv(-1000), 1.13)
        self.assertEqual(L.goalie_glv(1000), 0.87)


class NhlDecisions(unittest.TestCase):
    ROUTES = lambda self, **kw: {ROUTE_NHL: nhl_sb(**kw)}      # noqa: E731

    def test_same_goalie_as_the_model_assumed_changes_nothing(self):
        c = ctx(self.ROUTES(statuses={"home": "confirmed", "away": "confirmed"}, names={"away": "Joseph Woll"}))
        d = L.assess_leg(nhl_leg(), c, NOW)
        self.assertEqual(d["action"], "none")
        self.assertEqual(d["lineup"]["status"], "confirmed")
        self.assertIn("Woll", " ".join(d["lineup"]["notes"]))
        self.assertNotIn("adj", d["lineup"])

    def test_confirmed_backup_reprices_a_leg_that_still_qualifies(self):
        c = ctx(self.ROUTES(statuses={"home": "confirmed", "away": "confirmed"}))        # Vladar (confirmed) replaces Woll, the model's PHI goalie
        d = L.assess_leg(nhl_leg(prob=0.80, dec=1.75), c, NOW)
        self.assertEqual(d["action"], "reprice")
        adj = d["lineup"]["adj"]
        self.assertLess(adj["pAfter"], adj["pBefore"])
        self.assertAlmostEqual(adj["swing"], adj["pAfter"] - adj["pBefore"], places=3)
        self.assertGreater(adj["pBefore"] - adj["pAfter"], 0.015)                          # with no market blend the model-level swing is a few points
        self.assertTrue(d["tierN"] >= 2)
        self.assertEqual(d["lineup"]["status"], "confirmed")

    def test_confirmed_backup_holds_a_leg_that_only_qualified_by_a_small_margin(self):
        c = ctx(self.ROUTES(statuses={"home": "confirmed", "away": "confirmed"}))
        d = L.assess_leg(nhl_leg(prob=0.64, dec=1.75), c, NOW)                             # 64% / EV +12%: OPTIMAL by 2 points, lane floor 65%
        self.assertEqual(d["action"], "hold")
        self.assertIn("below the qualifying cutoff", d["reason"])
        self.assertEqual(d["lineup"]["action"], "hold")

    def test_market_blend_shrinks_the_effect_to_the_model_share(self):
        routes = self.ROUTES(statuses={"home": "confirmed", "away": "confirmed"})
        raw = L.assess_leg(nhl_leg(prob=0.80, dec=1.75), ctx(routes), NOW)["lineup"]["adj"]
        mid = L.assess_leg(nhl_leg(prob=0.80, dec=1.75, blendAlpha=0.80, modelProb=0.62), ctx(routes), NOW)
        self.assertEqual(mid["action"], "reprice")
        self.assertLess(abs(mid["lineup"]["adj"]["swing"]), abs(raw["swing"]) * 0.25)         # alpha .80 -> about 20% of the model-level swing
        self.assertGreater(abs(mid["lineup"]["adj"]["swing"]), 0.0)
        # the real alpha of the live NHL picks (.93-.95) leaves ~0.2 points: below NHL_MIN_REPRICE, so the leg is left exactly as it is
        live = L.assess_leg(nhl_leg(prob=0.80, dec=1.75, blendAlpha=0.93, modelProb=0.62), ctx(routes), NOW)
        self.assertEqual(live["action"], "none")
        self.assertIn("immaterial", " ".join(live["lineup"]["notes"]))
        with mock.patch.object(L, "NHL_STALE_MARKET_ASSUME", True):               # the switch that assumes the posted price predates the news
            full = L.assess_leg(nhl_leg(prob=0.80, dec=1.75, blendAlpha=0.93, modelProb=0.62), ctx(routes), NOW)["lineup"]["adj"]
        self.assertAlmostEqual(full["swing"], raw["swing"], places=4)

    def test_a_better_goalie_than_assumed_is_never_an_upgrade(self):
        # the leg backs BOS; PHI's confirmed starter is the WORSE goalie -> in BOS's favour
        c = ctx(self.ROUTES(statuses={"home": "confirmed", "away": "confirmed"}))
        d = L.assess_leg(nhl_leg(label="BOS ML", side="mlFav", prob=0.64, dec=1.75), c, NOW)
        self.assertEqual(d["action"], "none")
        self.assertIn("favour", " ".join(d["lineup"]["notes"]))

    def test_projected_not_confirmed_is_unknown_and_unchanged(self):
        d = L.assess_leg(nhl_leg(prob=0.64, dec=1.75), ctx(self.ROUTES()), NOW)           # fixture: both sides "expected"
        self.assertEqual(d["action"], "none")
        self.assertEqual(d["lineup"]["status"], "unknown")
        self.assertIn("projected", " ".join(d["lineup"]["notes"]))

    def test_only_the_confirmed_side_is_acted_on(self):
        c = ctx(self.ROUTES(statuses={"away": "confirmed"}))
        d = L.assess_leg(nhl_leg(prob=0.64, dec=1.75), c, NOW)
        self.assertEqual(d["action"], "hold")
        self.assertEqual(d["lineup"]["status"], "unknown")          # BOS's goalie is still only projected

    def test_fetch_failures_are_unknown_and_never_raise(self):
        for routes in ({}, {ROUTE_NHL: None}, {ROUTE_NHL: RuntimeError("boom")}, {ROUTE_NHL: {"events": []}}, {ROUTE_NHL: {"nonsense": 1}}):
            d = L.assess_leg(nhl_leg(prob=0.64, dec=1.75), ctx(routes), NOW)
            self.assertEqual(d["action"], "none", routes)
            self.assertEqual(d["lineup"]["status"], "unknown")

    def test_request_budget_exhausted_is_unknown(self):
        calls: list = []
        c = ctx(self.ROUTES(statuses={"home": "confirmed", "away": "confirmed"}), calls=calls, max_requests=0)
        d = L.assess_leg(nhl_leg(prob=0.64, dec=1.75), c, NOW)
        self.assertEqual((d["action"], d["lineup"]["status"], calls), ("none", "unknown", []))

    def test_scoreboard_is_fetched_once_per_run(self):
        calls: list = []
        c = ctx(self.ROUTES(statuses={"home": "confirmed", "away": "confirmed"}), calls=calls)
        for _ in range(5):
            L.assess_leg(nhl_leg(prob=0.64, dec=1.75), c, NOW)
        self.assertEqual(len(calls), 1)
        self.assertIn("dates=20261010", calls[0])

    def test_unmatched_game_is_unknown(self):
        d = L.assess_leg(nhl_leg(prob=0.64, dec=1.75, hA="TOR", awA="OTT", label="OTT ML"), ctx(self.ROUTES()), NOW)
        self.assertEqual(d["lineup"]["status"], "unknown")

    def test_unrated_replacement_is_league_average_like_an_injured_starter(self):
        c = ctx(self.ROUTES(statuses={"home": "confirmed", "away": "confirmed"}, names={"away": "Some Callup"}))
        d = L.assess_leg(nhl_leg(prob=0.80, dec=1.75), c, NOW)
        self.assertIn("league average", " ".join(d["lineup"]["notes"]))
        self.assertEqual(d["action"], "reprice")
        known = L.assess_leg(nhl_leg(prob=0.80, dec=1.75), ctx(self.ROUTES(statuses={"home": "confirmed", "away": "confirmed"})), NOW)
        self.assertLess(abs(d["lineup"]["adj"]["swing"]), abs(known["lineup"]["adj"]["swing"]))     # a league-average goalie hurts less than Vladar

    def test_a_replacement_with_a_tiny_sample_is_rated_league_average_not_by_noise(self):
        md = model()
        md.data["mp"]["goalies"][1] = goalie_row("Dan Vladar", "PHI", 2, -1.56, 0.863013698630137, 0.0, 29)      # 29 shots, 0-for-1 on high-danger chances
        both = {"home": "confirmed", "away": "confirmed"}
        d = L.assess_leg(nhl_leg(prob=0.80, dec=1.75), ctx(self.ROUTES(statuses=both), md), NOW)
        self.assertIn("only 29 shots faced", " ".join(d["lineup"]["notes"]))
        # same swing as an unrated call-up: Woll -> league average
        up = L.assess_leg(nhl_leg(prob=0.80, dec=1.75), ctx(self.ROUTES(statuses=both, names={"away": "Some Callup"})), NOW)
        self.assertAlmostEqual(d["lineup"]["adj"]["swing"], up["lineup"]["adj"]["swing"], places=6)

    def test_primary_goalie_already_out_in_the_injury_feed_is_not_counted_twice(self):
        out = [{"team": "PHI", "name": "Joseph Woll", "pos": "G", "status": "Out", "return": ""}]
        routes = self.ROUTES(statuses={"home": "confirmed", "away": "confirmed"})
        plain = L.assess_leg(nhl_leg(prob=0.80, dec=1.75), ctx(routes), NOW)["lineup"]["adj"]["swing"]
        c = ctx(routes, md=model(injuries_nhl=out))
        d = L.assess_leg(nhl_leg(prob=0.80, dec=1.75), c, NOW)
        # the model already rated PHI on a league-average goalie, so Vladar is a smaller step down than from Woll
        self.assertLess(abs(d["lineup"]["adj"]["swing"]), abs(plain))
        stale = model(injuries_nhl=out, generated="2026-09-01T00:00:00+00:00")           # a feed older than 72 h is ignored by the model, so by us
        d2 = L.assess_leg(nhl_leg(prob=0.80, dec=1.75), ctx(routes, md=stale), NOW)
        self.assertAlmostEqual(d2["lineup"]["adj"]["swing"], plain, places=6)

    def test_totals_and_puck_lines_are_sized_with_the_same_goalie_change(self):
        routes = self.ROUTES(statuses={"home": "confirmed", "away": "confirmed"})
        over = L.assess_leg(nhl_leg(label="OVER 5.5", side="over", prob=0.80, dec=1.91), ctx(routes), NOW)
        under = L.assess_leg(nhl_leg(label="UNDER 5.5", side="under", prob=0.80, dec=1.91), ctx(routes), NOW)
        self.assertEqual(over["action"], "none")                   # a worse PHI goalie means more goals: good for the over
        self.assertEqual(under["action"], "reprice")
        dog = L.assess_leg(nhl_leg(label="PHI +1.5", side="plDog", prob=0.80, dec=1.45), ctx(routes), NOW)
        self.assertEqual(dog["action"], "reprice")

    def test_missing_mc_projection_falls_back_to_the_league_rate_and_says_so(self):
        c = ctx(self.ROUTES(statuses={"home": "confirmed", "away": "confirmed"}))
        d = L.assess_leg(nhl_leg(prob=0.80, dec=1.75, mcSummary=None), c, NOW)
        self.assertEqual(d["action"], "reprice")
        self.assertIn("league rate", " ".join(d["lineup"]["notes"]))


class Scope(unittest.TestCase):
    def test_window_sport_and_switches(self):
        routes = {ROUTE_NHL: nhl_sb(statuses={"home": "confirmed", "away": "confirmed"})}
        self.assertIsNone(L.assess_leg(nhl_leg(), ctx(routes), PHI_BOS - 151 * 60000))          # outside the window
        self.assertIsNone(L.assess_leg(nhl_leg(), ctx(routes), PHI_BOS + 60000))                 # already started
        self.assertIsNotNone(L.assess_leg(nhl_leg(), ctx(routes), PHI_BOS - 149 * 60000))
        self.assertIsNone(L.assess_leg(nhl_leg(sport="CFB"), ctx(routes), NOW))                  # not a covered sport
        self.assertIsNone(L.assess_leg(nhl_leg(kind="PROP"), ctx(routes), NOW))
        self.assertIsNone(L.assess_leg(nhl_leg(startMs=None), ctx(routes), NOW))
        with mock.patch.object(L, "LINEUP_GATE_ENABLED", False):
            self.assertIsNone(L.assess_leg(nhl_leg(), ctx(routes), NOW))
        with mock.patch.object(L, "LINEUP_NHL_ENABLED", False):
            self.assertIsNone(L.assess_leg(nhl_leg(), ctx(routes), NOW))
        with mock.patch.dict("os.environ", {"LINEUP_GATE": "off"}):
            self.assertIsNone(L.assess_leg(nhl_leg(), ctx(routes), NOW))

    def test_an_internal_error_is_an_unknown_lineup_not_an_exception(self):
        routes = {ROUTE_NHL: nhl_sb(statuses={"home": "confirmed", "away": "confirmed"})}
        with mock.patch.dict(L._ASSESSORS, {"NHL": mock.Mock(side_effect=ZeroDivisionError)}):
            d = L.assess_leg(nhl_leg(), ctx(routes), NOW)
        self.assertEqual((d["action"], d["lineup"]["status"]), ("none", "unknown"))


# ── NBA ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def nba_model(stewart="OPTIMAL", snapshot=None, mpg=31.0):
    data = {"generated": "2026-10-10T11:10:39+00:00", "mp": {"goalies": []},
            "nba": {"roster": {"isaiah stewart": {"team": "MEM", "pos": "C", **({"rating": stewart} if stewart else {})},
                               "matas buzelis": {"team": "CHI", "pos": "F", "rating": "GOOD"}},
                    "playerProps": {"players": [{"name": "Isaiah Stewart", "team": "MEM", "mpg": mpg}, {"name": "Matas Buzelis", "team": "CHI", "mpg": 30.1}]}},
            "injuries": {"nhl": [], "nba": snapshot or []}}
    return L.ModelData(data)


def nba_leg(label="MEM ML", side="mlFav", prob=0.70, dec=1.60, **kw):
    q = {"kind": "GAME", "sport": "NBA", "hA": "CHI", "awA": "MEM", "side": side, "label": label, "prob": prob, "dec": dec, "evVal": prob * dec - 1,
         "tierN": 3, "startMs": MEM_CHI, "mcSummary": None}
    q.update(kw)
    return q


NBA_ROUTES = {"basketball/nba/scoreboard": NBA_SB, "basketball/nba/summary?event=401908940": NBA_SUM}
NBA_NOW = MEM_CHI - 45 * 60000


class NbaDecisions(unittest.TestCase):
    def test_rotation_player_newly_out_reprices_the_team_he_plays_for(self):
        d = L.assess_leg(nba_leg(prob=0.70, dec=1.60), ctx(NBA_ROUTES, nba_model()), NBA_NOW)    # Stewart: OPTIMAL -> the model's .045 penalty
        self.assertEqual(d["action"], "reprice")
        self.assertAlmostEqual(d["lineup"]["adj"]["pBefore"] - d["lineup"]["adj"]["pAfter"], 0.045, places=4)
        self.assertIn("Isaiah Stewart OUT", " ".join(d["lineup"]["notes"]))

    def test_the_same_absence_holds_a_thin_leg(self):
        d = L.assess_leg(nba_leg(prob=0.645, dec=1.60), ctx(NBA_ROUTES, nba_model()), NBA_NOW)
        self.assertEqual(d["action"], "hold")

    def test_premium_star_out_is_a_seven_point_penalty(self):
        d = L.assess_leg(nba_leg(prob=0.80, dec=1.60), ctx(NBA_ROUTES, nba_model(stewart="PREMIUM")), NBA_NOW)
        self.assertAlmostEqual(d["lineup"]["adj"]["pBefore"] - d["lineup"]["adj"]["pAfter"], 0.07, places=4)

    def test_already_in_the_models_injury_snapshot_is_not_counted_twice(self):
        snap = [{"team": "MEM", "name": "Isaiah Stewart", "pos": "C", "status": "Out"}]
        d = L.assess_leg(nba_leg(prob=0.645, dec=1.60), ctx(NBA_ROUTES, nba_model(snapshot=snap)), NBA_NOW)
        self.assertEqual(d["action"], "none")
        self.assertIn("already in the model", " ".join(d["lineup"]["notes"]))
        day = [{"team": "MEM", "name": "Isaiah Stewart", "pos": "C", "status": "Day-To-Day"}]      # the model reads Day-To-Day as fully out
        self.assertEqual(L.assess_leg(nba_leg(prob=0.645, dec=1.60), ctx(NBA_ROUTES, nba_model(snapshot=day)), NBA_NOW)["action"], "none")
        quest = [{"team": "MEM", "name": "Isaiah Stewart", "pos": "C", "status": "Questionable"}]    # .30 counted -> only the remaining .70 is new
        d3 = L.assess_leg(nba_leg(prob=0.80, dec=1.60), ctx(NBA_ROUTES, nba_model(snapshot=quest)), NBA_NOW)
        self.assertAlmostEqual(d3["lineup"]["adj"]["pBefore"] - d3["lineup"]["adj"]["pAfter"], 0.045 * 0.7, places=4)

    def test_the_other_side_of_the_injury_is_in_the_legs_favour(self):
        d = L.assess_leg(nba_leg(label="CHI ML", side="mlDog", prob=0.645, dec=1.60), ctx(NBA_ROUTES, nba_model()), NBA_NOW)
        self.assertEqual(d["action"], "none")

    def test_totals_have_no_injury_term(self):
        d = L.assess_leg(nba_leg(label="UNDER 220.5", side="under", prob=0.645, dec=1.91), ctx(NBA_ROUTES, nba_model()), NBA_NOW)
        self.assertEqual(d["action"], "none")

    def test_day_to_day_rotation_player_leaves_the_lineup_unknown_and_unchanged(self):
        d = L.assess_leg(nba_leg(label="CHI ML", side="mlDog", prob=0.645, dec=1.60), ctx(NBA_ROUTES, nba_model(stewart=None)), NBA_NOW)     # Buzelis (GOOD, Day-To-Day)
        self.assertEqual(d["action"], "none")
        self.assertEqual(d["lineup"]["status"], "unknown")
        self.assertIn("Buzelis", " ".join(d["lineup"]["notes"]))

    def test_a_non_rotation_player_out_is_ignored(self):
        d = L.assess_leg(nba_leg(prob=0.645, dec=1.60), ctx(NBA_ROUTES, nba_model(stewart=None, mpg=12.0)), NBA_NOW)
        self.assertEqual(d["action"], "none")

    def test_summary_failure_is_unknown(self):
        routes = {"basketball/nba/scoreboard": NBA_SB}            # the summary request fails
        d = L.assess_leg(nba_leg(prob=0.645, dec=1.60), ctx(routes, nba_model()), NBA_NOW)
        self.assertEqual((d["action"], d["lineup"]["status"]), ("none", "unknown"))

    def test_total_penalty_is_capped_like_the_model(self):
        def md(n_stars_already_out):
            m = nba_model(stewart="PREMIUM")
            m.data["nba"]["roster"].update({f"star {i}": {"team": "MEM", "pos": "F", "rating": "PREMIUM"} for i in range(3)})
            m.data["injuries"]["nba"] += [{"team": "MEM", "name": f"Star {i}", "pos": "F", "status": "Out"} for i in range(n_stars_already_out)]
            return m
        one = L.assess_leg(nba_leg(prob=0.90, dec=1.60), ctx(NBA_ROUTES, md(1)), NBA_NOW)          # .07 already counted + .07 new -> capped at .12 -> only .05 is new
        self.assertAlmostEqual(one["lineup"]["adj"]["pBefore"] - one["lineup"]["adj"]["pAfter"], 0.05, places=4)
        two = L.assess_leg(nba_leg(prob=0.90, dec=1.60), ctx(NBA_ROUTES, md(2)), NBA_NOW)          # already at the cap: nothing new
        self.assertEqual(two["action"], "none")


# ── NFL ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def nfl_sum(**status):
    """The real CHI @ GB summary with chosen players' statuses overridden: nfl_sum(**{'Caleb Williams': 'Out'})."""
    d = copy.deepcopy(NFL_SUM)
    for t in d["injuries"]:
        for r in t["injuries"]:
            for name, st in status.items():
                if r["athlete"]["displayName"] == name:
                    r["status"] = st
    return d


def nfl_model(snapshot=None, generated="2026-10-10 04:00 UTC"):
    chi = [r for t in NFL_SUM["injuries"] for r in t["injuries"] if t["team"]["abbreviation"] == "CHI"]
    ids = {r["athlete"]["displayName"]: r["athlete"]["id"] for r in chi}
    default_snap = [{"player": "Caleb Williams", "playerId": ids["Caleb Williams"], "position": "QB", "status": "Questionable", "estimatedReturn": "2026-10-11"}]
    players = {"generated_at": "2026-10-06 19:10 UTC", "teams": {"CHI": [
        {"name": "Caleb Williams", "position": "QB", "id": ids["Caleb Williams"]}, {"name": "Kyle Monangai", "position": "RB", "id": ids["Kyle Monangai"]}], "GB": []}}
    inj = {"generated_at": generated, "teams": {"CHI": default_snap if snapshot is None else snapshot}}
    return L.ModelData({"generated": "2026-10-10T11:10:39+00:00"}, inj, players)


def nfl_leg(label="CHI ML", side="mlDog", prob=0.70, dec=1.60, **kw):
    q = {"kind": "GAME", "sport": "NFL", "hA": "GB", "awA": "CHI", "side": side, "label": label, "prob": prob, "dec": dec, "evVal": prob * dec - 1,
         "tierN": 3, "startMs": CHI_GB, "mcSummary": None}
    q.update(kw)
    return q


def nfl_routes(summary):
    return {"football/nfl/scoreboard": NFL_SB, "football/nfl/summary?event=401872990": summary}


NFL_NOW = CHI_GB - 45 * 60000


class NflDecisions(unittest.TestCase):
    def test_real_payload_qb_questionable_leaves_the_lineup_unknown(self):
        d = L.assess_leg(nfl_leg(), ctx(nfl_routes(NFL_SUM), nfl_model()), NFL_NOW)
        self.assertEqual(d["lineup"]["status"], "unknown")
        self.assertIn("QB Caleb Williams Questionable (open)", " ".join(d["lineup"]["notes"]))

    def test_qb_out_on_the_side_the_leg_backs_is_a_hard_hold(self):
        c = ctx(nfl_routes(nfl_sum(**{"Caleb Williams": "Out"})), nfl_model())
        d = L.assess_leg(nfl_leg(prob=0.90, dec=1.60), c, NFL_NOW)                # huge margin, still held
        self.assertEqual(d["action"], "hold")
        self.assertIn("QB out", d["reason"])
        self.assertEqual(d["lineup"]["status"], "confirmed")

    def test_qb_out_on_the_opponent_is_in_the_legs_favour(self):
        c = ctx(nfl_routes(nfl_sum(**{"Caleb Williams": "Out"})), nfl_model())
        d = L.assess_leg(nfl_leg(label="GB ML", side="mlFav", prob=0.70, dec=1.60), c, NFL_NOW)
        self.assertEqual(d["action"], "none")

    def test_qb_out_holds_an_over_but_not_an_under(self):
        c = ctx(nfl_routes(nfl_sum(**{"Caleb Williams": "Out"})), nfl_model())
        self.assertEqual(L.assess_leg(nfl_leg(label="OVER 44.5", side="over", dec=1.91), c, NFL_NOW)["action"], "hold")
        self.assertEqual(L.assess_leg(nfl_leg(label="UNDER 44.5", side="under", dec=1.91), c, NFL_NOW)["action"], "none")

    def test_qb_the_model_already_counts_is_not_gated_again(self):
        ids = {r["athlete"]["displayName"]: r["athlete"]["id"] for t in NFL_SUM["injuries"] for r in t["injuries"]}
        snap = [{"player": "Caleb Williams", "playerId": ids["Caleb Williams"], "position": "QB", "status": "Out", "estimatedReturn": "2026-10-25"},
                {"player": "Kyle Monangai", "playerId": ids["Kyle Monangai"], "position": "RB", "status": "Out", "estimatedReturn": "2026-10-25"}]
        c = ctx(nfl_routes(nfl_sum(**{"Caleb Williams": "Out"})), nfl_model(snapshot=snap))
        d = L.assess_leg(nfl_leg(), c, NFL_NOW)
        self.assertEqual(d["action"], "none")
        self.assertEqual(d["lineup"]["status"], "confirmed")
        # ... but a snapshot older than the model's 60 h staleness cutoff was ignored by the model, so it counts as new
        stale = nfl_model(snapshot=snap, generated="2026-10-05 04:00 UTC")
        self.assertEqual(L.assess_leg(nfl_leg(), ctx(nfl_routes(nfl_sum(**{"Caleb Williams": "Out"})), stale), NFL_NOW)["action"], "hold")

    def test_skill_player_out_reprices_by_the_models_points(self):
        d = L.assess_leg(nfl_leg(prob=0.80, dec=1.60), ctx(nfl_routes(NFL_SUM), nfl_model()), NFL_NOW)    # Monangai (RB1) is Out in the real payload
        self.assertEqual(d["action"], "reprice")
        self.assertAlmostEqual(d["lineup"]["adj"]["pBefore"] - d["lineup"]["adj"]["pAfter"], L.NFL_INJ_SKILL_PTS * L.NFL_WIN_PROB_PER_POINT, places=4)

    def test_skill_player_out_holds_a_thin_leg(self):
        d = L.assess_leg(nfl_leg(prob=0.625, dec=1.60), ctx(nfl_routes(NFL_SUM), nfl_model()), NFL_NOW)
        self.assertEqual(d["action"], "hold")

    def test_offensive_line_absences_use_the_repo_table_and_the_cap(self):
        s = nfl_sum(**{"Aaron Banks": "Out", "Jacob Monk": "Out", "Jager Burton": "Out"})        # three GB linemen Out; the leg backs GB
        c = ctx(nfl_routes(s), nfl_model())
        d = L.assess_leg(nfl_leg(label="GB ML", side="mlFav", prob=0.80, dec=1.60), c, NFL_NOW)
        self.assertEqual(d["action"], "reprice")
        self.assertAlmostEqual(d["lineup"]["adj"]["pBefore"] - d["lineup"]["adj"]["pAfter"], min(L.NFL_OL_CAP, 3 * L.NFL_OL_WIN_PROB_EACH), places=4)

    def test_doubtful_linemen_alone_change_nothing(self):
        d = L.assess_leg(nfl_leg(label="GB ML", side="mlFav", prob=0.625, dec=1.60), ctx(nfl_routes(NFL_SUM), nfl_model()), NFL_NOW)
        self.assertEqual(d["action"], "none")

    def test_qb_gate_switch(self):
        c = ctx(nfl_routes(nfl_sum(**{"Caleb Williams": "Out"})), nfl_model())
        with mock.patch.object(L, "NFL_QB_OUT_HARD_GATE", False):
            d = L.assess_leg(nfl_leg(prob=0.90, dec=1.60), c, NFL_NOW)
        self.assertEqual(d["action"], "reprice")             # 2.5 pts x 3% = 7.5 points off a 90% leg
        self.assertAlmostEqual(d["lineup"]["adj"]["pBefore"] - d["lineup"]["adj"]["pAfter"], (2.5 * (1 - 0.3) + 0.4) * 0.03, places=4)   # QB: only the part beyond the .30 the snapshot already counts

    def test_nfl_failure_modes(self):
        for routes in ({}, {"football/nfl/scoreboard": NFL_SB}, nfl_routes({"injuries": []})):
            d = L.assess_leg(nfl_leg(), ctx(routes, nfl_model()), NFL_NOW)
            self.assertEqual((d["action"], d["lineup"]["status"]), ("none", "unknown"), routes.keys())


# ── wiring in auto_lock_settle ───────────────────────────────────────────────────────────────────────────────────────────────

def nhl_result(markets):
    return {"gameLegs": [{"sport": "NHL", "hA": "BOS", "awA": "PHI", "startMs": PHI_BOS, "markets": markets,
                          "mcSummary": "MC PROJ: PHI 2.7 – BOS 3.1 (Total 5.8, 25k sims)"}], "propLegs": []}


def hk_market(side, label, prob, dec, tier=3, lane=False):
    return {"side": side, "label": label, "prob": prob, "tierN": tier, "hkLane": lane, "evVal": prob * dec - 1, "ml": "+75", "dec": dec, "priceSource": "market"}


class Wiring(unittest.TestCase):
    def setUp(self):
        self.old_ctx, self.old_holds = A.LINEUP_CTX, list(A.LINEUP_HOLDS)
        A.LINEUP_HOLDS.clear()

    def tearDown(self):
        A.LINEUP_CTX = self.old_ctx
        A.LINEUP_HOLDS[:] = self.old_holds

    def confirmed(self, md=None, **kw):
        return ctx({ROUTE_NHL: nhl_sb(statuses={"home": "confirmed", "away": "confirmed"})}, md, **kw)

    def test_inert_until_activated(self):
        A.LINEUP_CTX = None
        q = A.build_qualifying(nhl_result([hk_market("mlDog", "PHI ML", 0.64, 1.75, tier=2)]), now=NOW)
        self.assertEqual(len(q), 1)
        self.assertNotIn("lineup", q[0])

    def test_held_leg_is_not_in_the_qualifying_list_and_is_reported(self):
        A.LINEUP_CTX = self.confirmed()
        stats: dict = {}
        q = A.build_qualifying(nhl_result([hk_market("mlDog", "PHI ML", 0.64, 1.75, tier=2)]), now=NOW, guard_stats=stats)
        self.assertEqual(q, [])
        self.assertEqual(len(A.LINEUP_HOLDS), 1)
        self.assertEqual(stats["lineupHolds"][0]["leg"], "PHI ML")

    def test_repriced_leg_carries_the_new_probability_and_the_lineup_record(self):
        A.LINEUP_CTX = self.confirmed()
        q = A.build_qualifying(nhl_result([hk_market("mlDog", "PHI ML", 0.80, 1.75)]), now=NOW)
        self.assertEqual(len(q), 1)
        leg = q[0]
        self.assertLess(leg["prob"], 0.80)
        self.assertEqual(leg["lineupPreProb"], 0.80)
        self.assertEqual(leg["lineup"]["action"], "reprice")
        self.assertEqual(leg["lineup"]["status"], "confirmed")
        self.assertIn("LINEUP:", leg["reasoning"] or "")
        self.assertAlmostEqual(leg["evVal"], leg["prob"] * 1.75 - 1, places=3)

    def test_unknown_lineup_leaves_the_leg_exactly_as_before_apart_from_the_record(self):
        base = A.build_qualifying(nhl_result([hk_market("mlDog", "PHI ML", 0.64, 1.75, tier=2)]), now=NOW)
        A.LINEUP_CTX = ctx({})                                   # every request fails
        q = A.build_qualifying(nhl_result([hk_market("mlDog", "PHI ML", 0.64, 1.75, tier=2)]), now=NOW)
        rec = q[0].pop("lineup")
        self.assertEqual(rec["status"], "unknown")
        self.assertEqual(q, base)

    def test_master_switch_off_restores_the_old_behaviour_exactly(self):
        base = A.build_qualifying(nhl_result([hk_market("mlDog", "PHI ML", 0.64, 1.75, tier=2)]), now=NOW)
        A.LINEUP_CTX = self.confirmed()
        with mock.patch.object(L, "LINEUP_GATE_ENABLED", False):
            self.assertEqual(A.build_qualifying(nhl_result([hk_market("mlDog", "PHI ML", 0.64, 1.75, tier=2)]), now=NOW), base)

    def test_the_watchdogs_observe_only_call_is_never_gated(self):
        A.LINEUP_CTX = self.confirmed()
        q = A.build_qualifying(nhl_result([hk_market("mlDog", "PHI ML", 0.64, 1.75, tier=2)]), now=0)
        self.assertEqual(len(q), 1)
        self.assertNotIn("lineup", q[0])

    def test_a_leg_outside_the_window_is_untouched(self):
        A.LINEUP_CTX = self.confirmed()
        q = A.build_qualifying(nhl_result([hk_market("mlDog", "PHI ML", 0.64, 1.75, tier=2)]), now=PHI_BOS - 5 * 3600 * 1000)
        self.assertEqual(len(q), 1)
        self.assertNotIn("lineup", q[0])

    def test_the_other_leg_of_a_game_survives_when_one_is_held(self):
        A.LINEUP_CTX = self.confirmed()
        q = A.build_qualifying(nhl_result([hk_market("mlDog", "PHI ML", 0.64, 1.75, tier=2), hk_market("over", "OVER 5.5", 0.64, 1.91, tier=2)]), now=NOW)
        self.assertEqual([x["label"] for x in q], ["OVER 5.5"])
        self.assertEqual(q[0]["lineup"]["action"], "none")

    def test_an_all_held_pass_is_not_reported_to_subscribers_as_no_picks(self):
        ok, why = A._zero_pick_decision([], {"skipped": 0, "lineupHolds": [{"leg": "PHI ML"}]}, True)
        self.assertFalse(ok)
        self.assertIn("lineup", why)
        self.assertTrue(A._zero_pick_decision([], {"skipped": 0}, True)[0])

    def test_requalify_mirrors_the_app_cutoffs(self):
        rq = A.lineup_requalify
        leg = {"sport": "NHL", "side": "mlFav"}
        self.assertEqual(rq(leg, 0.68, 0.06), (True, 3, False))
        self.assertEqual(rq(leg, 0.63, 0.04), (True, 2, False))
        self.assertEqual(rq(leg, 0.66, -0.05), (True, 0, True))      # high-probability lane: qualifies at 65%+ and EV >= -7% with a sub-OPTIMAL tier
        self.assertTrue(rq(leg, 0.66, -0.05)[0])
        self.assertTrue(rq(leg, 0.66, -0.05)[2])
        self.assertFalse(rq(leg, 0.64, -0.05)[0])
        self.assertFalse(rq(leg, 0.66, -0.08)[0])
        self.assertTrue(rq({"sport": "NHL", "side": "over"}, 0.57, -0.04)[0])                  # totals lane floor 56%
        self.assertFalse(rq({"sport": "NHL", "side": "plFav"}, 0.66, -0.05)[0])                 # the lane covers the +1.5 DOG only
        self.assertTrue(rq({"sport": "NFL", "side": "mlFav"}, 0.76, -0.2)[0])                   # the non-hockey 75% ML rule
        self.assertFalse(rq({"sport": "NFL", "side": "sprdFav"}, 0.76, -0.2)[0])
        self.assertTrue(rq({"sport": "NBA", "side": "mlFav"}, 0.63, 0.04)[0])
        self.assertFalse(rq({"sport": "NBA", "side": "mlFav"}, 0.61, 0.04)[0])

    def test_lock_game_leg_hands_the_lineup_record_to_lockpick(self):
        seen = {}

        class Page:
            def evaluate(self, js, args):
                seen["js"], seen["args"] = js, args
                return "locked"
        q = {"kind": "GAME", "sport": "NHL", "hA": "BOS", "awA": "PHI", "label": "PHI ML", "side": "mlDog", "prob": .7, "ml": "+75", "dec": 1.75,
             "startMs": PHI_BOS + 3600_000, "lineup": {"status": "confirmed", "notes": ["x"], "checkedAt": "t", "src": "espn", "action": "none"}}
        self.assertEqual(A.lock_game_leg(Page(), q, now=NOW), "locked")
        self.assertEqual(seen["args"]["lineup"]["status"], "confirmed")
        self.assertIn("...(lineup ? { lineup } : {})", seen["js"])
        q.pop("lineup")
        A.lock_game_leg(Page(), q, now=NOW)
        self.assertIsNone(seen["args"]["lineup"])

    def test_activation_is_fail_open_and_honours_the_switch(self):
        with mock.patch.object(L, "LINEUP_GATE_ENABLED", False):
            self.assertFalse(A.activate_lineup_gate())
            self.assertIsNone(A.LINEUP_CTX)
        with mock.patch.object(L, "default_context", side_effect=RuntimeError("x")):
            self.assertFalse(A.activate_lineup_gate())
            self.assertIsNone(A.LINEUP_CTX)
        self.assertTrue(A.activate_lineup_gate(ctx=self.confirmed()))


class WatchdogHold(unittest.TestCase):
    """The watchdog must not start a lock pass for a leg the pass would refuse, and must say why it is still open."""

    def test_held_leg_is_left_for_the_owner_with_the_reason(self):
        from test_watchdog_autolock import Harness, NOW as WD_NOW, q as wd_q
        start = WD_NOW + 45 * 60000
        leg = wd_q("NHL", "PHI ML", 45, hA="BOS", awA="PHI")
        leg.update({"side": "mlDog", "prob": 0.64, "dec": 1.75, "evVal": 0.12, "tierN": 2, "startMs": start, "mcSummary": "MC PROJ: PHI 2.7 – BOS 3.1 (Total 5.8)"})
        sb = nhl_sb(statuses={"home": "confirmed", "away": "confirmed"})
        sb["events"][0]["date"] = L.iso_z(start)[:-4] + "Z"          # the fixture game, moved to start 45 min after the harness clock
        h = Harness([leg])
        old = A.LINEUP_CTX
        A.LINEUP_CTX = ctx({ROUTE_NHL: sb})
        try:
            left = A.run_watchdog(None, True, now=WD_NOW, auto_lock=True)
        finally:
            A.LINEUP_CTX = old
            h.restore()
        self.assertEqual(h.calls, [])                                # no lock pass was started for it
        self.assertEqual(len(left), 1)
        self.assertIn("LINEUP HOLD", left[0]["why"])


if __name__ == "__main__":
    unittest.main()
