"""NFL data pipeline — foundation layer, same architecture as fetch_cfb.py.

Phase 1: teams/schedule/standings/team-stats/injuries/transactions --
everything the Games/Model/Config tabs, weather, settlement, and the
injuries/transactions news feed need.

Phase 2 (player_stats mode): each team's real season-to-date leaders in
passing/rushing/receiving yards+TDs+receptions, via ESPN's team-detail
leaders array. This is deliberately scoped to season-to-date totals for
the players who'd actually have real prop markets (starting QB, lead
backs, top targets) -- NOT a full-roster or per-game-log scrape. A real
per-player game-log build (needed for true head-to-head/division-rival
history in the prop reasoning) is a bigger, separate pass; the frontend
prop model says so explicitly rather than pretending that history is
factored in.

Unlike CFB, NFL doesn't need a conference-membership discovery step --
every team's own detail endpoint directly exposes AFC/NFC + division,
no groups= workaround required.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
TEAMS_OUT = ROOT / "docs" / "nfl_teams.json"
SCHEDULE_OUT = ROOT / "docs" / "nfl_schedule.json"
STANDINGS_OUT = ROOT / "docs" / "nfl_standings.json"
STATS_OUT = ROOT / "docs" / "nfl_team_stats.json"
INJURIES_OUT = ROOT / "docs" / "nfl_injuries.json"
TRANSACTIONS_OUT = ROOT / "docs" / "nfl_transactions.json"
PLAYER_STATS_OUT = ROOT / "docs" / "nfl_player_stats.json"

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
ESPN_WEB_BASE = "https://site.web.api.espn.com/apis/v2/sports/football/nfl"
ESPN_CORE_BASE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
# ESPN's site.api.espn.com now 403s any request carrying a custom
# User-Agent (verified: both this bot-labeled UA and a spoofed browser UA
# get blocked; requests' own default "python-requests/x.x" UA -- i.e. no
# custom header at all -- passes through fine). Empty dict, not removed
# entirely, so call sites don't need to change.
HEADERS: dict = {}

# NFL regular season is 18 weeks; Thursday of week N through Monday of
# week N (Thu/Sun/Mon, per the user's explicit "Thursday night starts
# the week, Monday night ends it" framing) is one calendar window, not
# a Sun-Sat one. ESPN's own week=N indexing already groups games this
# way (a Thursday game shows under the SAME week number as the Sunday/
# Monday games right after it), so no extra bucketing logic is needed
# here beyond using ESPN's week param directly.
REGULAR_SEASON_WEEKS = list(range(1, 19))
POSTSEASON_ROUNDS = {1: "Wild Card", 2: "Divisional", 3: "Conference Championship", 5: "Super Bowl"}
# Preseason (seasontype=1) is 3 real game weeks for most teams (a 4th,
# "Hall of Fame Game" week, only involves 2 teams most years) -- included
# so preseason matchups show up in the engine as soon as they're
# scheduled, not just once the regular season starts.
PRESEASON_WEEKS = {0: "Hall of Fame Game", 1: "Preseason Week 1", 2: "Preseason Week 2", 3: "Preseason Week 3"}


def _log(msg: str) -> None:
    print(f"[nfl] {msg}", flush=True)


def fetch_all_teams() -> list[dict]:
    r = requests.get(f"{ESPN_BASE}/teams", params={"limit": 40}, headers=HEADERS, timeout=15)
    r.raise_for_status()
    d = r.json()
    teams = d.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    return [t["team"] for t in teams]


def fetch_team_detail(team_id: str) -> dict | None:
    """Conference (AFC/NFC) + division (North/South/East/West) come
    straight off each team's own detail endpoint's groups info -- unlike
    CFB, no bulk-endpoint filter workaround needed, NFL only has 32
    teams total so one detail call per team is cheap."""
    try:
        r = requests.get(f"{ESPN_BASE}/teams/{team_id}", headers=HEADERS, timeout=10)
        r.raise_for_status()
        t = r.json().get("team", {})
        venue = t.get("venue") or {}
        groups = t.get("groups") or {}
        parent = groups.get("parent") or {}
        return {
            "id": t.get("id"),
            "abbr": t.get("abbreviation"),
            "name": t.get("displayName"),
            "conference": parent.get("name"),  # "American Football Conference" / "National Football Conference"
            "division": groups.get("name"),    # e.g. "AFC North"
            "venueName": venue.get("fullName"),
            "venueCapacity": venue.get("capacity"),
            "venueIndoor": venue.get("indoor", False),
        }
    except Exception as exc:
        _log(f"  team {team_id} FAILED: {exc}")
        return None


def build_roster() -> dict:
    all_teams = fetch_all_teams()
    _log(f"{len(all_teams)} teams found — resolving conference/division for each…")
    roster = []
    for i, t in enumerate(all_teams):
        tid = t.get("id")
        if not tid:
            continue
        info = fetch_team_detail(tid)
        if info:
            roster.append(info)
        if (i + 1) % 10 == 0:
            _log(f"  …{i + 1}/{len(all_teams)}")
        time.sleep(0.15)
    return {"teams": roster}


def fetch_week_games(year: int, week: int, seasontype: int = 2) -> list[dict]:
    """One call per week gets every game — venue (for weather), city/
    state, real market spread/O-U/ML odds, and (once played) the final
    score used for settlement. seasontype: 1=preseason, 2=regular,
    3=postseason."""
    r = requests.get(
        f"{ESPN_BASE}/scoreboard",
        params={"limit": 100, "week": week, "seasontype": seasontype, "year": year},
        headers=HEADERS, timeout=15,
    )
    r.raise_for_status()
    d = r.json()
    games = []
    for e in d.get("events", []):
        comp = (e.get("competitions") or [{}])[0]
        home = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "home"), {})
        away = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "away"), {})
        odds = (comp.get("odds") or [{}])[0]
        venue = comp.get("venue") or {}
        address = venue.get("address") or {}
        status = e.get("status", {})
        games.append({
            "id": e.get("id"),
            "date": e.get("date"),
            "name": e.get("name"),
            "venue": venue.get("fullName"),
            "city": address.get("city"),
            "venueState": address.get("state"),
            "indoor": venue.get("indoor", False),
            "neutralSite": comp.get("neutralSite", False),
            "home": (home.get("team") or {}).get("abbreviation"),
            "homeName": (home.get("team") or {}).get("displayName"),
            "homeScore": int(home["score"]) if home.get("score") not in (None, "") else None,
            "away": (away.get("team") or {}).get("abbreviation"),
            "awayName": (away.get("team") or {}).get("displayName"),
            "awayScore": int(away["score"]) if away.get("score") not in (None, "") else None,
            "spread": odds.get("spread"),
            "spreadDetails": odds.get("details"),
            "overUnder": odds.get("overUnder"),
            "homeML": (odds.get("homeTeamOdds") or {}).get("moneyLine"),
            "awayML": (odds.get("awayTeamOdds") or {}).get("moneyLine"),
            "state": status.get("type", {}).get("state", "pre"),
            "week": week,
            "seasonType": seasontype,
        })
    return games


def fetch_full_schedule(year: int) -> dict:
    """Preseason (3-4 weeks), then weeks 1-18 regular season, then the 4
    postseason rounds (Wild Card/Divisional/Conf Champ/Super Bowl —
    ESPN's postseason week numbering skips 4, there's no "week 4"
    round). Thursday/Sunday/Monday games for a given regular-season week
    all come back from the same week=N call already, matching the
    user's own week-boundary framing -- no extra date-bucketing needed.
    Preseason weeks are inserted first so they sort chronologically
    ahead of "Week 1" in the schedule dict/week-filter dropdown."""
    schedule: dict[str, list[dict]] = {}
    for wk, label in PRESEASON_WEEKS.items():
        games = fetch_week_games(year, wk, seasontype=1)
        if games:
            schedule[label] = games
            _log(f"  {label}: {len(games)} games")
        time.sleep(0.2)
    for wk in REGULAR_SEASON_WEEKS:
        games = fetch_week_games(year, wk, seasontype=2)
        schedule[f"Week {wk}"] = games
        _log(f"  Week {wk}: {len(games)} games")
        time.sleep(0.2)
    for wk, label in POSTSEASON_ROUNDS.items():
        games = fetch_week_games(year, wk, seasontype=3)
        if games:
            schedule[label] = games
            _log(f"  {label}: {len(games)} games")
        time.sleep(0.2)
    return schedule


def fetch_standings(year: int) -> dict:
    """W-L-T, PF, PA, point differential, conference/division rank --
    the "surface stats" the user explicitly asked for, not full team
    stats (that's fetch_team_stats below)."""
    try:
        r = requests.get(
            f"{ESPN_WEB_BASE}/standings",
            params={"season": year, "level": 3},  # level 3 = division standings
            headers=HEADERS, timeout=15,
        )
        r.raise_for_status()
        d = r.json()
    except Exception as exc:
        _log(f"  standings FAILED: {exc}")
        return {"conferences": {}}

    conferences: dict[str, list[dict]] = {}
    for group in (d.get("children") or []):  # AFC / NFC
        conf_name = group.get("name") or group.get("abbreviation")
        for div in (group.get("children") or []):  # North/South/East/West
            div_name = div.get("name")
            entries = ((div.get("standings") or {}).get("entries") or [])
            for entry in entries:
                team = entry.get("team") or {}
                stats = {s.get("name"): s.get("value") for s in (entry.get("stats") or [])}
                row = {
                    "abbr": team.get("abbreviation"),
                    "name": team.get("displayName"),
                    "division": div_name,
                    "wins": stats.get("wins"),
                    "losses": stats.get("losses"),
                    "ties": stats.get("ties"),
                    "pointsFor": stats.get("pointsFor"),
                    "pointsAgainst": stats.get("pointsAgainst"),
                    "differential": stats.get("differential"),
                    "divisionRank": stats.get("divisionRank"),
                    "playoffSeed": stats.get("playoffSeed"),
                }
                conferences.setdefault(conf_name, []).append(row)
    return {"conferences": conferences}


# Team stats categories mirroring fetch_cfb.py's own _OFFENSE_FIELDS/
# _DEFENSE_ALLOWED_FIELDS/_ST_FIELDS shape, adapted to what the user
# explicitly asked for: total/passing/rushing/receiving/downs on
# offense; yards allowed/turnovers/passing/receiving/downs on defense;
# returning/kicking/punting on special teams.
_OFFENSE_FIELDS = {
    "total":      ["totalYards", "yardsPerGame", "totalPoints", "totalPointsPerGame", "turnOverDifferential"],
    "passing":    ["netPassingYards", "netPassingYardsPerGame", "passingTouchdowns",
                    "completionPct", "yardsPerPassAttempt", "interceptions"],
    "rushing":    ["rushingYards", "rushingYardsPerGame", "rushingTouchdowns", "yardsPerRushAttempt"],
    "receiving":  ["receivingYards", "receivingTouchdowns", "receivingYardsPerGame"],
    "downs":      ["thirdDownConvPct", "fourthDownConvPct", "firstDowns", "totalPenaltyYards"],
}
_DEFENSE_ALLOWED_FIELDS = {
    "yardsAllowed": ["totalYards", "yardsPerGame", "totalPointsPerGame"],
    "turnovers":    ["interceptions", "fumblesRecovered", "totalTakeaways"],
    "passing":      ["netPassingYardsPerGame", "passingTouchdowns", "interceptions"],
    # "rushing" was missing entirely -- defense radar/reasoning needs a
    # real rush-defense signal, not just pass/receiving, to be a genuine
    # offense/defense breakdown rather than a partial one.
    "rushing":      ["rushingYardsPerGame", "rushingTouchdowns"],
    # receivingYardsPerGame (ESPN's own per-game figure, when exposed)
    # is preferred over receivingYards -- the props model divides the
    # season total by games itself only when this native field is
    # missing, so it isn't guessing a per-game number that ESPN
    # already computed correctly.
    "receiving":    ["receivingYards", "receivingYardsPerGame", "receivingTouchdowns"],
    "downs":        ["thirdDownConvPct", "fourthDownConvPct"],
}
_ST_FIELDS = {
    "returning": ["yardsPerKickReturn", "kickReturnTouchdowns", "yardsPerPuntReturn", "puntReturnTouchdowns"],
    "kicking":   ["fieldGoalPct", "longFieldGoalMade", "extraPointPct", "totalKickingPoints"],
    "punting":   ["grossAvgPuntYards", "netAvgPuntYards", "puntsInside20"],
}


def _extract_fields(categories: list[dict], field_map: dict[str, list[str]]) -> dict:
    out: dict[str, float] = {}
    wanted = {f for fields in field_map.values() for f in fields}
    for cat in categories or []:
        for stat in cat.get("stats") or []:
            name = stat.get("name")
            if name in wanted:
                val = stat.get("value")
                if val is not None:
                    out[name] = val
    return out


def fetch_team_stats(team_id: str, season: int) -> dict | None:
    try:
        r = requests.get(
            f"{ESPN_BASE}/teams/{team_id}/statistics",
            params={"season": season}, headers=HEADERS, timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        own_cats = ((d.get("results") or {}).get("stats") or {}).get("categories") or []
        opp_cats = (d.get("results") or {}).get("opponent") or []
        offense = _extract_fields(own_cats, _OFFENSE_FIELDS)
        defense = _extract_fields(opp_cats, _DEFENSE_ALLOWED_FIELDS)
        # Real games-played for this exact stats snapshot, derived from
        # this team's own totalPoints/totalPointsPerGame ratio -- not
        # cross-referenced against the separately-scraped standings
        # file, which can be a snapshot or two out of sync. Any category
        # here that's a season total without a native ESPN PerGame
        # field (TDs allowed, etc.) divides by this number, so the
        # per-game figure used in the props model comes from the same
        # data pull it's being combined with, not a guess.
        games_played = None
        tp, tppg = offense.get("totalPoints"), offense.get("totalPointsPerGame")
        if tp and tppg:
            games_played = round(tp / tppg)
        if games_played:
            defense["gamesPlayed"] = games_played
        return {
            "offense": offense,
            "defenseAllowed": defense,
            "specialTeams": _extract_fields(own_cats, _ST_FIELDS),
        }
    except Exception as exc:
        _log(f"  team {team_id} stats FAILED: {exc}")
        return None


def fetch_all_team_stats(roster: list[dict], season: int) -> dict:
    stats = {}
    for i, t in enumerate(roster):
        abbr, tid = t.get("abbr"), t.get("id")
        if not abbr or not tid:
            continue
        s = fetch_team_stats(tid, season)
        if s:
            stats[abbr] = {"name": t.get("name"), **s}
        if (i + 1) % 8 == 0:
            _log(f"  stats …{i + 1}/{len(roster)}")
        time.sleep(0.2)
    return stats


# ESPN's team-detail endpoint exposes each team's own leaders per real
# stat category directly (team.leaders[]) -- exactly the skill players
# who'd have real prop markets anyway (QB1, lead backs, top targets).
# Season-to-date totals, not a per-game log -- see module docstring.
# Real gap, found via audit 2026-09-10: the old approach (fetch_team_leaders,
# below this comment in git history) read ESPN's team-level "leaders" field --
# that ONLY ever returns each team's statistical LEADER(s) per category (the
# #1 passer, #1 rusher, top 1-2 receivers), not a roster. Confirmed via the
# real git history: docs/nfl_player_stats.json has been 100% empty -- 0
# players, every team -- since its very first commit (2026-08-21), because
# ESPN doesn't populate "leaders" until enough real games have accumulated
# league-wide. Even once populated, it would only ever surface a handful of
# star players per team -- a WR2/WR3/RB2/backup TE would NEVER appear as a
# prop candidate no matter how much they played, since they're not their
# team's category leader. That's a leaderboard, not a roster.
#
# Fixed by using ESPN's real roster endpoint (/teams/{id}/roster, confirmed
# live to return ~80 real players per team across offense/defense/special-
# teams/IR/practice-squad groups, each with real name+position+athlete id)
# to get every ACTIVE offensive skill-position player (QB/RB/WR/TE -- the
# only positions _NFL_PROP_CATS in app.html builds props for), then fetching
# each of those players' own real season stats individually via ESPN's
# per-athlete stats endpoint (confirmed live: returns passing/rushing/
# receiving categories with a real per-player gamesPlayed count, more
# accurate than the old approach's team-level win+loss approximation).
_NFL_SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}


# Explicit request, after the user reviewed the real depth-chart data
# for all 32 teams and confirmed it accurate: only these 9 teams keep a
# real committee/2-back rotation worth a second RB prop pool -- every
# other team is single-RB-only. WR is capped at the top 2 slots
# (WR1/WR2) for every team; WR3 dropped everywhere.
_NFL_RB2_TEAMS = {"CAR", "CHI", "CLE", "DEN", "LAR", "MIN", "NE", "SEA", "WSH"}


def fetch_team_depth_chart(team_id: str) -> dict:
    """Real depth-chart order per position, via ESPN's live endpoint --
    verified structurally consistent across real teams before trusting
    it (a resolvable group found by searching every formation entry,
    not tied to one hardcoded formation name/label), and the resulting
    QB1/WR1-3/RB1-2/TE1 lists for all 32 teams were reviewed and
    confirmed accurate by the user directly. Returns
    {'QB': [ids], 'WR': [ids], 'RB': [ids], 'TE': [ids]} in depth
    order. WR merges the wr1/wr2/wr3 slot groups in slot order (each
    slot's own #1 first) -- ESPN structures WR depth as three separate
    ranked slots, not one flat list the way QB/RB/TE are. Empty lists
    (not an exception) if the fetch fails or a position has no
    resolvable group, so callers can fall back rather than silently
    dropping a position for that team."""
    try:
        r = requests.get(f"{ESPN_BASE}/teams/{team_id}/depthcharts", headers=HEADERS, timeout=15)
        r.raise_for_status()
        d = r.json()
    except Exception as exc:
        _log(f"  team {team_id} depth chart FAILED: {exc}")
        return {"QB": [], "WR": [], "RB": [], "TE": []}
    out: dict[str, list[str]] = {"QB": [], "WR": [], "RB": [], "TE": []}
    for chart in d.get("depthchart") or []:
        positions = chart.get("positions") or {}
        for key, label in (("qb", "QB"), ("rb", "RB"), ("te", "TE")):
            if key in positions and not out[label]:
                out[label] = [a.get("id") for a in (positions[key].get("athletes") or [])]
        for slot in ("wr1", "wr2", "wr3"):
            if slot in positions:
                athletes = positions[slot].get("athletes") or []
                if athletes:
                    aid = athletes[0].get("id")
                    if aid not in out["WR"]:
                        out["WR"].append(aid)
    return out


def fetch_team_roster(team_id: str, team_abbr: str) -> list[dict]:
    """Real active roster for one team, filtered to offensive skill
    positions only (props are only ever built for QB/RB/WR/TE) --
    excludes the injuredReserveOrOut/suspended/practiceSquad groups
    ESPN's roster response also returns, since those players aren't
    live game-day candidates. Each position is further filtered to its
    real depth-chart slice: QB1 only, WR1+WR2 only (WR3 dropped), RB1
    only except the 9 teams in _NFL_RB2_TEAMS which also keep RB2, and
    TE1 only."""
    try:
        r = requests.get(f"{ESPN_BASE}/teams/{team_id}/roster", headers=HEADERS, timeout=15)
        r.raise_for_status()
        d = r.json()
    except Exception as exc:
        _log(f"  team {team_id} roster FAILED: {exc}")
        return []
    depth = fetch_team_depth_chart(team_id)
    qb1_id = depth["QB"][0] if depth["QB"] else None
    wr_keep = set(depth["WR"][:2])
    rb_n = 2 if team_abbr in _NFL_RB2_TEAMS else 1
    rb_keep = set(depth["RB"][:rb_n])
    te1_id = depth["TE"][0] if depth["TE"] else None
    players = []
    for grp in d.get("athletes") or []:
        if grp.get("position") != "offense":
            continue
        for item in grp.get("items") or []:
            pos = (item.get("position") or {}).get("abbreviation")
            aid = item.get("id")
            if not aid or pos not in _NFL_SKILL_POSITIONS:
                continue
            # Each guard only applies when the depth chart actually
            # resolved for that position -- if the fetch itself failed
            # (empty list), fall back to every roster player at that
            # position rather than silently dropping it for this team.
            if pos == "QB" and qb1_id is not None and aid != qb1_id:
                continue
            if pos == "WR" and depth["WR"] and aid not in wr_keep:
                continue
            if pos == "RB" and depth["RB"] and aid not in rb_keep:
                continue
            if pos == "TE" and te1_id is not None and aid != te1_id:
                continue
            players.append({"id": aid, "name": item.get("fullName") or item.get("displayName"), "position": pos})
    return players


def fetch_player_season_stats(athlete_id: str, season: int) -> dict:
    """Real per-player season stats (passing/rushing/receiving), keyed
    to match _NFL_PROP_CATS' field names in app.html exactly, plus a
    real per-player gamesPlayed count. Empty dict (not an exception) if
    this player has no stats recorded for `season` yet -- normal for a
    player who hasn't played, not an error."""
    try:
        r = requests.get(
            f"https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{athlete_id}/stats",
            params={"season": season}, headers=HEADERS, timeout=15,
        )
        r.raise_for_status()
        d = r.json()
    except Exception:
        return {}
    out: dict = {}
    games = None
    keys = {"passingYards", "passingTouchdowns", "rushingYards", "rushingTouchdowns",
            "receivingYards", "receivingTouchdowns", "receptions"}
    for cat in d.get("categories") or []:
        names = cat.get("names") or []
        for entry in cat.get("statistics") or []:
            if (entry.get("season") or {}).get("year") != season:
                continue
            row = dict(zip(names, entry.get("stats") or []))
            # This endpoint's stats[] entries are display strings, not raw
            # numbers -- ESPN comma-formats anything >= 1000 (e.g. a
            # starting QB's season passing yards: "2,167"). float() throws
            # on the comma and the bare except silently dropped the whole
            # field, meaning passingYards (almost always 4 digits for a
            # real starter) was missing for essentially every QB, while
            # smaller same-shape fields like rushingYards parsed fine --
            # real bug, found while auditing why QB prop rows had no
            # Passing Yards market despite QBs clearly having real season
            # stats. Strip thousands separators before parsing.
            if "gamesPlayed" in row:
                try:
                    games = int(float(str(row["gamesPlayed"]).replace(",", "")))
                except (TypeError, ValueError):
                    pass
            for k in keys:
                if k in row:
                    try:
                        out[k] = float(str(row[k]).replace(",", ""))
                    except (TypeError, ValueError):
                        pass
    if games is not None:
        out["games"] = games
    return out


def fetch_all_player_stats(roster: list[dict], season: int) -> dict:
    """For every team, fetch the real active offensive-skill-position
    roster, then each of those players' own real season stats. Players
    with zero real stats yet (season just started, or a backup who
    hasn't touched the ball) are dropped -- same "no real signal ->
    don't fabricate a row" principle _nflBuildPropRow already applies
    in app.html, just enforced one layer earlier here."""
    out: dict[str, list[dict]] = {}
    for i, tm in enumerate(roster):
        abbr, tid = tm.get("abbr"), tm.get("id")
        if not abbr or not tid:
            continue
        players = fetch_team_roster(tid, abbr)
        rows = []
        for p in players:
            stats = fetch_player_season_stats(p["id"], season)
            time.sleep(0.1)
            if not stats.get("games"):
                continue
            rows.append({"name": p["name"], "position": p["position"], **stats})
        out[abbr] = rows
        _log(f"  {abbr}: {len(rows)}/{len(players)} skill players with real {season} stats  ({i + 1}/{len(roster)})")
        time.sleep(0.2)
    return out


def fetch_injuries() -> dict:
    """Per-team injury report: player, status, injury description,
    estimated return date when ESPN publishes one. Feeds both the
    per-team game-card context and the NFL news tab."""
    try:
        r = requests.get(f"{ESPN_BASE}/injuries", headers=HEADERS, timeout=15)
        r.raise_for_status()
        d = r.json()
    except Exception as exc:
        _log(f"  injuries FAILED: {exc}")
        return {"teams": {}}
    teams: dict[str, list[dict]] = {}
    for entry in d.get("injuries") or []:
        team = (entry.get("team") or {})
        abbr = team.get("abbreviation")
        if not abbr:
            continue
        rows = []
        for item in entry.get("injuries") or []:
            athlete = item.get("athlete") or {}
            details = item.get("details") or {}
            rows.append({
                "player": athlete.get("displayName"),
                "position": (athlete.get("position") or {}).get("abbreviation"),
                "status": item.get("status"),
                "injury": details.get("type") or item.get("shortComment"),
                "estimatedReturn": details.get("returnDate"),
                "comment": item.get("longComment") or item.get("shortComment"),
                "date": item.get("date"),
            })
        teams[abbr] = rows
    return {"teams": teams}


def fetch_transactions() -> dict:
    """Team, transaction type (trade/signing/release/waiver), date,
    description -- feeds the NFL news tab's transactions section."""
    try:
        r = requests.get(f"{ESPN_BASE}/transactions", headers=HEADERS, timeout=15)
        r.raise_for_status()
        d = r.json()
    except Exception as exc:
        _log(f"  transactions FAILED: {exc}")
        return {"items": []}
    items = []
    for t in d.get("transactions") or []:
        team = (t.get("team") or {})
        items.append({
            "team": team.get("abbreviation"),
            "teamName": team.get("displayName"),
            "date": t.get("date"),
            "description": t.get("description"),
        })
    return {"items": items}


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), **payload}
    path.write_text(json.dumps(payload, indent=2))
    _log(f"wrote {path}")


def git_push(paths: list[str], message: str) -> None:
    subprocess.run(["git", "add", *paths], cwd=ROOT, check=True)
    r = subprocess.run(["git", "commit", "-m", message], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        _log(f"  nothing to commit ({r.stdout.strip()[:120]})")
        return
    # Main gets pushed to constantly (a live-score bot pushes every ~3min,
    # plus other scheduled scrapers) -- a bare push with no retry fails
    # instantly on any non-fast-forward race, which is exactly what was
    # happening here (this had zero pull/rebase at all, unlike fetch_cfb.py's
    # git_push). Rebase onto the latest remote and retry a few times before
    # giving up, same pattern as fetch_cfb.py.
    for attempt in range(5):
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=ROOT, capture_output=True)
        push = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT, capture_output=True, text=True)
        if push.returncode == 0:
            _log("  pushed")
            return
        _log(f"  push attempt {attempt+1}/5 failed, retrying: {push.stderr.strip()[:160]}")
        time.sleep(3 + attempt * 2)
    raise RuntimeError("git push failed after 5 retries")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["roster", "schedule", "standings", "stats", "player_stats", "injuries", "transactions", "all"], default="all")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--stats-season", type=int, default=None)
    ap.add_argument("--schedule-year", type=int, default=2026)
    args = ap.parse_args()

    did_roster = did_schedule = False
    roster_teams: list[dict] = []

    if args.mode in ("roster", "all"):
        result = build_roster()
        roster_teams = result["teams"]
        _write(TEAMS_OUT, result)
        did_roster = True

    if args.mode in ("schedule", "all"):
        sched = fetch_full_schedule(args.schedule_year)
        _write(SCHEDULE_OUT, {"weeks": sched, "year": args.schedule_year})
        did_schedule = True

    if args.mode in ("standings", "all"):
        standings = fetch_standings(args.schedule_year)
        _write(STANDINGS_OUT, standings)

    stats_season = None
    if args.mode in ("stats", "all"):
        if not roster_teams:
            if TEAMS_OUT.exists():
                roster_teams = json.loads(TEAMS_OUT.read_text()).get("teams", [])
            else:
                _log("  no roster available for stats — run --mode roster first")
        if roster_teams:
            # --stats-season=None (the default, used by the scheduled
            # workflow with no override) auto-detects: tries the real
            # current calendar year first, and only falls back to the
            # prior season if most teams come back with no stats at all
            # (i.e. this season's games genuinely haven't started yet).
            # Same real bug CFB had (fetch_cfb.py's write_stats() docstring)
            # -- this hardcoded --stats-season=2025 with no fallback logic
            # at all, so the scheduled weekly workflow would have kept
            # fetching 2025 forever even once 2026 games started this
            # week, with nothing ever automatically switching over. Found
            # and fixed in the same audit pass that caught CFB's version.
            if args.stats_season is not None:
                stats_season = args.stats_season
                stats = fetch_all_team_stats(roster_teams, stats_season)
            else:
                current_year = int(time.strftime("%Y", time.gmtime()))
                stats = fetch_all_team_stats(roster_teams, current_year)
                if len(stats) < len(roster_teams) * 0.5:
                    _log(f"  season={current_year} returned stats for only "
                         f"{len(stats)}/{len(roster_teams)} teams -- season hasn't "
                         f"started yet, falling back to {current_year - 1}")
                    stats_season = current_year - 1
                    stats = fetch_all_team_stats(roster_teams, stats_season)
                else:
                    stats_season = current_year
            _write(STATS_OUT, {"season": stats_season, "teams": stats})

    if args.mode in ("player_stats", "all"):
        if not roster_teams:
            if TEAMS_OUT.exists():
                roster_teams = json.loads(TEAMS_OUT.read_text()).get("teams", [])
            else:
                _log("  no roster available for player_stats — run --mode roster first")
        if roster_teams:
            # Reuses --stats-season, or stats_season already computed by
            # the team-stats block above in the same run (--mode all),
            # or auto-detects fresh the same way that block does --
            # tries the current year, falls back to the prior season if
            # nobody league-wide has real stats yet.
            if args.stats_season is not None:
                player_stats_season = args.stats_season
            elif stats_season is not None:
                player_stats_season = stats_season
            else:
                player_stats_season = int(time.strftime("%Y", time.gmtime()))
            player_stats = fetch_all_player_stats(roster_teams, player_stats_season)
            # Real bug, found via audit: an exact-zero check here missed
            # the actual early-season case -- once even ONE game has been
            # played (e.g. a Wednesday-night opener), a couple of that
            # game's players already have 1 real game of current-season
            # stats, so total_players is a small nonzero number instead of
            # 0, and the season never fell back even though the other 30+
            # teams still had nothing. Matches fetch_all_team_stats'
            # existing team-level proportional check instead of an exact
            # count: fall back unless at least half the LEAGUE's teams
            # have any real current-season data yet.
            teams_with_data = sum(1 for v in player_stats.values() if v)
            if (teams_with_data < len(roster_teams) * 0.5
                    and args.stats_season is None and stats_season is None):
                _log(f"  season={player_stats_season} returned real stats for only "
                     f"{teams_with_data}/{len(roster_teams)} teams -- season hasn't "
                     f"started yet, falling back to {player_stats_season - 1}")
                player_stats_season -= 1
                player_stats = fetch_all_player_stats(roster_teams, player_stats_season)
            _write(PLAYER_STATS_OUT, {"season": player_stats_season, "teams": player_stats})

    if args.mode in ("injuries", "all"):
        _write(INJURIES_OUT, fetch_injuries())

    if args.mode in ("transactions", "all"):
        _write(TRANSACTIONS_OUT, fetch_transactions())

    if args.push:
        paths = ["docs/nfl_teams.json", "docs/nfl_schedule.json", "docs/nfl_standings.json",
                  "docs/nfl_team_stats.json", "docs/nfl_player_stats.json", "docs/nfl_injuries.json", "docs/nfl_transactions.json"]
        # Each --mode run only writes its own file(s) -- `git add` on the others,
        # which don't exist yet on a fresh checkout, fails the whole pathspec and
        # aborts the script before anything (including the file that DID get
        # written) is ever committed. Only add files that actually exist.
        paths = [p for p in paths if (ROOT / p).exists()]
        if paths:
            git_push(paths, f"chore: refresh NFL {args.mode} data")
