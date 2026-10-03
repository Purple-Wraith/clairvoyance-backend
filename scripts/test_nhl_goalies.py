#!/usr/bin/env python3
"""Tests for scripts/_nhl_goalies.py and its wiring in scripts/fetch_nhl.py (no network).

    python3 scripts/test_nhl_goalies.py

Fixtures in scripts/fixtures/nhl_goalies/ are REAL responses captured Fri 2026-10-02 ~19:00 MT:
  espn_scoreboard_trimmed.json  ESPN site.api NHL scoreboard for 20261002 and 20261003, trimmed to the fields the scraper reads
                                (the 20261003 slate: 13 games, mostly "expected", WSH/NYI/STL "confirmed"; the 20261002 slate: five
                                 games, all "confirmed", three already in progress)
  mp_schedule_excerpt.json      MoneyPuck SeasonSchedule-20262027.json rows for game ids 2026020017..34
  mp_responses.json             MoneyPuck tweets/starting_goalies/*.csv and start_predictions/*.csv bodies + status codes
                                (404 where MoneyPuck had nothing -- including STL@COL's away side, which ESPN already showed
                                 confirmed while MoneyPuck did not)
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _nhl_goalies as G  # noqa: E402
import fetch_nhl as F  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "nhl_goalies"
ESPN = json.loads((FIX / "espn_scoreboard_trimmed.json").read_text())
MP_SCHED = json.loads((FIX / "mp_schedule_excerpt.json").read_text())
MP_RESP = json.loads((FIX / "mp_responses.json").read_text())
NOW = datetime(2026, 10, 3, 0, 55, tzinfo=timezone.utc)  # Fri 2026-10-02 18:55 MT


def espn_games(date):
    """ESPN fixture events -> (games list shaped like fetch_full_schedule's, {id: espn sides})."""
    games, sides = [], {}
    for ev in ESPN[date]["events"]:
        comp = ev["competitions"][0]
        home = next(c for c in comp["competitors"] if c["homeAway"] == "home")
        away = next(c for c in comp["competitors"] if c["homeAway"] == "away")
        games.append({"id": ev["id"], "date": ev["date"], "home": G.app_abbr(home["team"]["abbreviation"]),
                      "away": G.app_abbr(away["team"]["abbreviation"]), "state": comp["status"]["type"]["state"]})
        s = G.espn_probables(comp["competitors"])
        if s:
            sides[ev["id"]] = s
    return games, sides


class FakeResp:
    def __init__(self, status, body=""):
        self.status_code, self.text = status, body

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeMP:
    """Serves the real fixtures; counts requests; anything not captured is a 404 (like MoneyPuck)."""

    def __init__(self, overrides=None):
        self.calls, self.overrides = [], overrides or {}

    def __call__(self, url):
        path = url.split("/moneypuck/", 1)[1]
        self.calls.append(path)
        if path in self.overrides:
            return self.overrides[path]
        if path.startswith("OldSeasonScheduleJson/"):
            return FakeResp(200, json.dumps(MP_SCHED))
        r = MP_RESP.get(path)
        return FakeResp(r["status"], r["body"]) if r else FakeResp(404, "<html>404</html>")


class EspnProbables(unittest.TestCase):
    def test_real_slate_statuses(self):
        games, sides = espn_games("20261003")
        self.assertEqual(len(games), 13)
        self.assertEqual(len(sides), 13)  # ESPN names a starter on every side of every game
        conf = sorted((gid, s) for gid, d in sides.items() for s, v in d.items() if v["status"] == "confirmed")
        self.assertEqual(len(conf), 3)  # WSH (Lindgren), NYI (Sorokin), STL (Binnington) -- the rest are "expected"
        by_id = {g["id"]: g for g in games}
        wsh = sides["401891826"]
        self.assertEqual(wsh["away"], {"name": "Charlie Lindgren", "status": "confirmed"})
        self.assertEqual(wsh["home"], {"name": "Andrei Vasilevskiy", "status": "projected"})  # ESPN "expected" -> projected
        self.assertEqual((by_id["401891826"]["home"], by_id["401891826"]["away"]), ("TBL", "WSH"))  # ESPN's TB mapped to the app's TBL

    def test_started_games_keep_confirmed(self):
        games, sides = espn_games("20261002")
        self.assertEqual({g["state"] for g in games}, {"in", "pre"})
        self.assertTrue(all(v["status"] == "confirmed" for d in sides.values() for v in d.values()))
        self.assertEqual(sides["401891803"]["away"]["name"], "Dylan Garand")  # NYR, game in progress

    def test_unknown_status_skipped_and_reported(self):
        comps = [{"homeAway": "home", "probables": [{"name": "probableStartingGoalie", "athlete": {"fullName": "A B"},
                                                      "status": {"type": "doubtful"}}]},
                 {"homeAway": "away", "probables": [{"name": "probableStartingGoalie", "athlete": {"fullName": "C D"},
                                                      "status": {"type": "confirmed"}}]}]
        unknown = set()
        out = G.espn_probables(comps, unknown)
        self.assertEqual(out, {"away": {"name": "C D", "status": "confirmed"}})
        self.assertEqual(unknown, {"doubtful"})

    def test_no_probables_no_sides(self):
        self.assertEqual(G.espn_probables([{"homeAway": "home"}, {"homeAway": "away", "probables": []}]), {})
        self.assertEqual(G.espn_probables(None), {})


class MoneyPuckParsing(unittest.TestCase):
    def test_tweet_csv_real(self):
        v = G.parse_tweet_csv(MP_RESP["tweets/starting_goalies/2026020020H.csv"]["body"])
        # tweet 2026-10-02 11:35 AM ET (EDT, UTC-4) -> 15:35Z
        self.assertEqual(v, {"name": "Jake Oettinger", "status": "confirmed", "src": "moneypuck", "id": 8479979,
                             "by": "brucelevinepuck", "ts": "2026-10-02T15:35Z"})

    def test_tweet_csv_day_before(self):
        v = G.parse_tweet_csv(MP_RESP["tweets/starting_goalies/2026020024A.csv"]["body"])
        self.assertEqual((v["name"], v["id"], v["ts"]), ("Charlie Lindgren", 8479292, "2026-10-01T16:07Z"))

    def test_404_body_is_none(self):
        self.assertIsNone(G.parse_tweet_csv(MP_RESP["tweets/starting_goalies/2026020022H.csv"]["body"]))
        self.assertIsNone(G.parse_tweet_csv(""))
        self.assertIsNone(G.parse_tweet_csv("tweet_id,author_id,handle,created_at,found_at,goalie_id,goalie_name,text\n"))

    def test_prediction_csv_real(self):
        v = G.parse_prediction_csv(MP_RESP["start_predictions/2025020800H.csv"]["body"])
        self.assertEqual(v, {"name": "Connor Ingram", "status": "projected", "src": "moneypuck", "p": 0.51, "id": 8478971})
        self.assertIsNone(G.parse_prediction_csv(MP_RESP["start_predictions/2026020022H.csv"]["body"]))

    def test_schedule_index_and_match(self):
        unmapped = set()
        rows = MP_SCHED + [{"a": "XXX", "h": "TOR", "est": "20261003 19:00:00", "id": 9}]
        idx = G.mp_schedule_index(rows, unmapped)
        self.assertEqual(unmapped, {"XXX"})
        start = datetime(2026, 10, 3, 23, 0, tzinfo=timezone.utc)  # 7 PM ET
        self.assertEqual(G.mp_game_id(idx, "BUF", "CHI", start), 2026020022)
        self.assertIsNone(G.mp_game_id(idx, "BUF", "CHI", datetime(2026, 10, 5, 23, 0, tzinfo=timezone.utc)))  # wrong day
        self.assertIsNone(G.mp_game_id(idx, "TOR", "BUF", start))  # pairing does not exist

    def test_team_table_covers_all_32_and_skips_unknown(self):
        self.assertEqual(len({v for v in G.APP_ABBR.values()}), 32)
        self.assertEqual((G.app_abbr("TB"), G.app_abbr("UTAH"), G.app_abbr("LA")), ("TBL", "UTA", "LAK"))
        self.assertIsNone(G.app_abbr("XYZ"))
        self.assertIsNone(G.app_abbr(None))

    def test_norm_name(self):
        self.assertEqual(G.norm_name("Leevi Meriläinen"), G.norm_name("leevi  Merilainen"))


class MoneyPuckFetch(unittest.TestCase):
    def run_fetch(self, fake, games=None, sides=None):
        g3, s3 = espn_games("20261003")
        g2, s2 = espn_games("20261002")
        games = games if games is not None else (g2 + g3)
        sides = sides if sides is not None else {**s2, **s3}
        # the fixture's in-progress games must not be asked about
        fresh, meta = G.fetch_moneypuck(games, sides, "20262027", NOW, http_get=fake, sleep=lambda s: None)
        return fresh, meta, games, sides

    def test_only_unconfirmed_sides_are_requested(self):
        fake = FakeMP()
        fresh, meta, games, sides = self.run_fetch(fake)
        tw = [c for c in fake.calls if c.startswith("tweets/")]
        # 13 games x 2 sides, minus the 3 ESPN-confirmed sides on 10-03 (WSH away, NYI home, STL away) = 23 tweet probes,
        # and nothing for the 10-02 STL@DAL / ANA@VGK games (both sides ESPN-confirmed)
        self.assertEqual(len(tw), 23)
        self.assertFalse(any("2026020020" in c or "2026020021" in c for c in fake.calls))
        self.assertEqual(fake.calls[0], "OldSeasonScheduleJson/SeasonSchedule-20262027.json")
        self.assertEqual(meta["games"], 15)
        self.assertEqual(meta["error"], None)
        # circuit breaker: start_predictions probed on exactly 4 sides (all 404) then abandoned
        self.assertEqual(len([c for c in fake.calls if c.startswith("start_predictions/")]), 4)
        self.assertIs(meta["pred_published"], False)
        self.assertLessEqual(meta["requests"], 1 + 23 + 4)

    def test_espn_confirmed_but_moneypuck_silent_keeps_espn(self):
        fresh, meta, games, sides = self.run_fetch(FakeMP())
        stl_col = fresh["401892439"]
        self.assertEqual(stl_col["away"], {"name": "Jordan Binnington", "status": "confirmed"})  # MP had no tweet (404 in the fixture)
        self.assertEqual(stl_col["home"]["status"], "projected")
        self.assertNotIn("src", stl_col["away"])  # still plain ESPN

    def test_moneypuck_confirmation_upgrades_an_espn_projection(self):
        g3, s3 = espn_games("20261003")
        s3 = copy.deepcopy(s3)
        # NYI@NJD: pretend ESPN only had Sorokin as "expected"; MoneyPuck's real tweet file confirms him
        s3["401891818"]["home"] = {"name": "Ilya Sorokin", "status": "projected"}
        fake = FakeMP()
        fresh, meta, *_ = self.run_fetch(fake, games=g3, sides=s3)
        v = fresh["401891818"]["home"]
        self.assertEqual((v["name"], v["status"], v["src"], v["id"], v["by"]), ("Ilya Sorokin", "confirmed", "moneypuck", 8478009, "stefen_rosner"))
        self.assertEqual(v["ts"], "2026-10-02T15:36Z")
        self.assertEqual(fresh["401891818"]["away"]["name"], "Jake Allen")  # other side untouched ESPN projection
        self.assertEqual(meta["confirmed"], 1 + 0)

    def test_moneypuck_confirmed_and_espn_projected_differ_moneypuck_wins(self):
        g3, s3 = espn_games("20261003")
        s3 = copy.deepcopy(s3)
        s3["401891826"]["away"] = {"name": "Logan Thompson", "status": "projected"}  # ESPN thinks Thompson; MP tweet says Lindgren
        fresh, *_ = self.run_fetch(FakeMP(), games=g3, sides=s3)
        self.assertEqual(fresh["401891826"]["away"]["name"], "Charlie Lindgren")
        self.assertEqual(fresh["401891826"]["away"]["status"], "confirmed")

    def test_unmatched_game_is_skipped_not_guessed(self):
        g3, s3 = espn_games("20261003")
        g3[0]["date"] = "2026-10-03T15:00Z"  # 11 AM ET: no MoneyPuck game within 3h of that for CHI@BUF
        fresh, meta, *_ = self.run_fetch(FakeMP(), games=g3, sides=s3)
        self.assertEqual(meta["unmatched"], 1)
        self.assertNotIn(g3[0]["id"], fresh)  # nothing from MoneyPuck for it; attach_goalies keeps ESPN's value ({**espn, **mp})

    def test_http_failure_is_fail_open(self):
        def boom(url):
            raise OSError("network down")
        g3, s3 = espn_games("20261003")
        fresh, meta = G.fetch_moneypuck(g3, s3, "20262027", NOW, http_get=boom, sleep=lambda s: None)
        self.assertEqual(fresh, {})
        self.assertIn("network down", meta["error"])

    def test_schedule_fetch_http_error_is_fail_open(self):
        fake = FakeMP({"OldSeasonScheduleJson/SeasonSchedule-20262027.json": FakeResp(403, "blocked")})
        fresh, meta, *_ = self.run_fetch(fake)
        self.assertEqual(fresh, {})
        self.assertIn("403", meta["error"])
        self.assertEqual(len(fake.calls), 1)  # a blocked schedule fetch stops everything: no per-game hammering

    def test_request_cap(self):
        with mock.patch.object(G, "MP_MAX_REQUESTS", 5):
            fake = FakeMP()
            fresh, meta, *_ = self.run_fetch(fake)
        self.assertEqual(len(fake.calls), 5)
        self.assertIn("cap", meta["error"])

    def test_lookahead_window(self):
        g3, s3 = espn_games("20261003")
        fake = FakeMP()
        fresh, meta = G.fetch_moneypuck(g3, s3, "20262027", NOW, http_get=fake, sleep=lambda s: None, lookahead_hours=10)
        self.assertEqual(meta["games"], 0)  # 20:00 PM ET+ games are 22h+ away
        self.assertEqual(fake.calls, ["OldSeasonScheduleJson/SeasonSchedule-20262027.json"])


class MergeCarry(unittest.TestCase):
    def setUp(self):
        self.t1 = datetime(2026, 10, 2, 17, 0, tzinfo=timezone.utc)
        self.t2 = datetime(2026, 10, 2, 19, 0, tzinfo=timezone.utc)

    def test_first_observation_stamps_at(self):
        g = [{"id": "1"}]
        st = G.merge_with_previous(g, {"1": {"home": {"name": "A A", "status": "projected"}, "away": {"name": "B B", "status": "confirmed"}}}, {}, self.t1)
        self.assertEqual(g[0]["goalies"], {"home": {"name": "A A", "status": "projected"}, "away": {"name": "B B", "status": "confirmed"},
                                           "at": "2026-10-02T17:00Z", "src": "espn"})
        self.assertEqual((st["with_goalies"], st["confirmed_sides"], st["projected_sides"]), (1, 1, 1))

    def test_unchanged_game_keeps_at_so_no_diff(self):
        sides = {"1": {"home": {"name": "A A", "status": "projected"}}}
        g1 = [{"id": "1"}]
        G.merge_with_previous(g1, sides, {}, self.t1)
        g2 = [{"id": "1"}]
        G.merge_with_previous(g2, copy.deepcopy(sides), {"games": g1}, self.t2)
        self.assertEqual(g2[0]["goalies"], g1[0]["goalies"])
        self.assertEqual(g2[0]["goalies"]["at"], "2026-10-02T17:00Z")

    def test_change_restamps_at(self):
        g1 = [{"id": "1"}]
        G.merge_with_previous(g1, {"1": {"home": {"name": "A A", "status": "projected"}}}, {}, self.t1)
        g2 = [{"id": "1"}]
        G.merge_with_previous(g2, {"1": {"home": {"name": "A A", "status": "confirmed"}}}, {"games": g1}, self.t2)
        self.assertEqual(g2[0]["goalies"]["home"]["status"], "confirmed")
        self.assertEqual(g2[0]["goalies"]["at"], "2026-10-02T19:00Z")

    def test_missing_side_is_carried_from_previous(self):
        prev = {"games": [{"id": "1", "goalies": {"home": {"name": "A A", "status": "confirmed"}, "away": {"name": "B B", "status": "confirmed"},
                                                   "at": "2026-10-02T17:00Z", "src": "espn"}}]}
        g = [{"id": "1"}]
        st = G.merge_with_previous(g, {"1": {"away": {"name": "B B", "status": "confirmed"}}}, prev, self.t2)
        self.assertEqual(g[0]["goalies"]["home"], {"name": "A A", "status": "confirmed"})  # carried untouched
        self.assertEqual(g[0]["goalies"]["at"], "2026-10-02T17:00Z")
        self.assertEqual(st["carried_sides"], 1)

    def test_game_with_nothing_fresh_keeps_previous_entirely(self):
        old = {"home": {"name": "A A", "status": "confirmed"}, "at": "2026-10-02T17:00Z", "src": "espn"}
        g = [{"id": "1"}]
        G.merge_with_previous(g, {}, {"games": [{"id": "1", "goalies": old}]}, self.t2)
        self.assertEqual(g[0]["goalies"], old)

    def test_game_with_nothing_anywhere_has_no_key(self):
        g = [{"id": "1", "goalies": {"stale": True}}, {"id": "2"}]
        G.merge_with_previous(g, {}, {}, self.t1)
        self.assertNotIn("goalies", g[0])
        self.assertNotIn("goalies", g[1])

    def test_src_reflects_sides(self):
        g = [{"id": "1"}]
        G.merge_with_previous(g, {"1": {"home": {"name": "A A", "status": "confirmed", "src": "moneypuck", "id": 1, "by": "x", "ts": "2026-10-02T15:35Z"},
                                         "away": {"name": "B B", "status": "projected"}}}, {}, self.t1)
        self.assertEqual(g[0]["goalies"]["src"], "espn+moneypuck")


class FetchNhlWiring(unittest.TestCase):
    def fake_scoreboard(self):
        by_date = {d: ESPN[d] for d in ESPN}

        def get(url, params=None, headers=None, timeout=None):
            d = (params or {}).get("dates")
            body = by_date.get(d, {"events": []})
            return FakeResp(200, json.dumps(body))
        return get

    def run_full(self):
        with mock.patch.object(F.requests, "get", self.fake_scoreboard()), mock.patch.object(F.time, "sleep", lambda s: None):
            with mock.patch.object(F, "_nhl_current_season_id", return_value="20262027"):
                return F.fetch_full_schedule()

    def test_full_schedule_collects_probables_without_extra_requests(self):
        sched = self.run_full()
        self.assertEqual(len(sched["games"]), 18)
        self.assertEqual(len(sched["_goalie_sides"]), 18)
        # nothing leaks into the per-game dicts before attach_goalies runs
        self.assertTrue(all("goalies" not in g for g in sched["games"]))

    def test_attach_goalies_end_to_end_and_no_leak(self):
        sched = self.run_full()
        meta = F.attach_goalies(sched, {}, use_moneypuck=False, now=NOW)
        self.assertNotIn("_goalie_sides", sched)
        self.assertEqual(meta["with_goalies"], 18)
        self.assertEqual(meta["confirmed_sides"], 10 + 3)  # 10-02: 10 sides, 10-03: WSH/NYI/STL
        self.assertEqual(meta["error"], None)
        wsh = next(g for g in sched["games"] if g["id"] == "401891826")["goalies"]
        self.assertEqual(wsh["away"], {"name": "Charlie Lindgren", "status": "confirmed"})
        self.assertEqual((wsh["at"], wsh["src"]), ("2026-10-03T00:55Z", "espn"))
        # the JSON the file will contain is small
        self.assertLess(len(json.dumps(wsh)), 220)

    def test_attach_goalies_with_moneypuck_flag(self):
        sched = self.run_full()
        fake = FakeMP()
        with mock.patch.object(G, "MP_REQUEST_GAP_S", 0), mock.patch.object(G.time, "sleep", lambda s: None), \
                mock.patch("requests.get", side_effect=lambda url, headers=None, timeout=None: fake(url)):
            meta = F.attach_goalies(sched, {}, use_moneypuck=True, now=NOW)
        self.assertEqual(meta["moneypuck"]["error"], None)
        self.assertEqual(meta["with_goalies"], 18)

    def test_failure_in_goalie_step_keeps_previous_and_never_raises(self):
        sched = self.run_full()
        prev = {"games": [{"id": "401891826", "goalies": {"home": {"name": "X Y", "status": "confirmed"}, "at": "2026-10-02T17:00Z", "src": "espn"}}]}
        with mock.patch.object(G, "merge_with_previous", side_effect=RuntimeError("boom")):
            meta = F.attach_goalies(sched, prev, now=NOW)
        self.assertIn("boom", meta["error"])
        self.assertEqual(next(g for g in sched["games"] if g["id"] == "401891826")["goalies"], prev["games"][0]["goalies"])
        self.assertEqual(len(sched["games"]), 18)  # schedule itself untouched

    def test_moneypuck_failure_leaves_espn_goalies(self):
        sched = self.run_full()
        with mock.patch.object(G, "fetch_moneypuck", side_effect=RuntimeError("mp down")):
            meta = F.attach_goalies(sched, {}, use_moneypuck=True, now=NOW)
        # fetch_moneypuck itself is fail-open; if even that wrapper blew up, attach_goalies must still not raise
        self.assertIn("mp down", meta["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
