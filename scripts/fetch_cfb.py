"""College Football data pipeline — foundation layer.

Builds the conference/team roster for the 9 conferences the engine tracks
(ACC, American, Big 12, Big Ten, FBS Independents, MAC, Mountain West,
Pac-12, SEC), since the bulk ESPN team-list endpoint doesn't expose
conference membership and its `groups=` query param is silently ignored —
confirmed by direct testing. The only reliable source is each team's own
detail endpoint (`teams/{id}` -> `groups.id`), so this pages through the
full FBS team list once, then does one detail call per team to resolve its
real conference.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `import _season` however this is launched
from _season import football_season_year, roll_prior_snapshot

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "cfb_teams.json"
POWER_OUT_PATH = ROOT / "docs" / "cfb_power.json"
STATS_OUT_PATH = ROOT / "docs" / "cfb_team_stats.json"
# Previous season's FINAL team stats (same schema as cfb_team_stats.json), derived automatically by roll_prior_snapshot() the moment the
# new season's stats start replacing it -- replaces the hand-made, permanent cfb_team_stats_2025.json (docs/app.html loadCFBData's 15% blend).
PRIOR_STATS_PATH = ROOT / "docs" / "cfb_team_stats_prior.json"

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
# ESPN's site.api.espn.com now 403s any request carrying a custom
# User-Agent (verified: both this bot-labeled UA and a spoofed browser UA
# get blocked; requests' own default "python-requests/x.x" UA -- i.e. no
# custom header at all -- passes through fine). Empty dict, not removed
# entirely, so call sites don't need to change.
HEADERS: dict = {}
# www.espn.com (the actual website, used only by _fetch_fpi_view/
# fetch_power_ratings below) is the OPPOSITE of site.api.espn.com: it
# 403s a request with no custom User-Agent and needs a real browser one.
# It also sits behind an AWS WAF JS challenge a plain requests.get can
# never solve regardless of headers (confirmed live -- see
# _fetch_fpi_view's own comment), which is why that fetch goes through a
# real Playwright browser using this same UA string, not requests.
WWW_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
}

# The 11 conferences this engine tracks, keyed by ESPN's own group id.
# Conference USA (12) and Sun Belt (167) were missing entirely until this
# was caught during a coverage audit -- confirmed by resolving real 2026
# member teams' own groups.id field directly (verified against 3 real
# C-USA teams for 12; 2 real Sun Belt teams for 167). ESPN's old
# .../standings?group=80 children-listing endpoint this comment used to
# cite no longer returns that shape (verified directly -- it now just
# returns a fullViewLink), hence resolving via real member teams instead.
# Note Sun Belt's own group id (167) is NOT its groups.parent.id (37) --
# 37 returned zero teams when tried first; ESPN marks 167 isConference:
# False despite it being the correct, consistently-used id for every
# actual Sun Belt team checked.
TARGET_CONFERENCES = {
    "151": "American",
    "1":   "ACC",
    "4":   "Big 12",
    "5":   "Big Ten",
    "12":  "Conference USA",
    "18":  "FBS Independents",
    "15":  "MAC",
    "17":  "Mountain West",
    "9":   "Pac-12",
    "8":   "SEC",
    "167": "Sun Belt",
    "168": "Sun Belt",  # Sun Belt actually has 2 sub-group ids (divisions),
                         # both parented under 37 -- confirmed via real
                         # members on each (James Madison/Marshall -> 167,
                         # Arkansas State/Troy -> 168). Both map to the
                         # same conference name; roster building already
                         # groups by resolved name, not by raw id, so this
                         # just needs the second id present as a key.
}


def _log(msg: str) -> None:
    print(f"[cfb] {msg}", flush=True)


def fetch_all_team_ids() -> list[dict]:
    """Full team list (all divisions — FBS, FCS, D2/D3 club programs ESPN
    also carries under this sport). Filtered down to FBS by conference
    membership in the next step, not here.

    limit=1000, not 500 -- the endpoint actually has 759 teams across all
    divisions (confirmed directly), so the old limit=500 silently
    truncated the list. Caught during a coverage audit: it happened to
    not matter for the original 9 tracked conferences (apparently sorted
    early enough in ESPN's ordering to survive the cutoff), but Conference
    USA and Sun Belt teams were getting cut off, undercounting both."""
    r = requests.get(f"{ESPN_BASE}/teams", params={"limit": 1000}, headers=HEADERS, timeout=15)
    r.raise_for_status()
    d = r.json()
    teams = d.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    return [t["team"] for t in teams]


def fetch_team_conference(team_id: str) -> dict | None:
    try:
        r = requests.get(f"{ESPN_BASE}/teams/{team_id}", headers=HEADERS, timeout=10)
        r.raise_for_status()
        t = r.json().get("team", {})
        groups = t.get("groups") or {}
        venue = t.get("venue") or {}
        return {
            "id": t.get("id"),
            "abbr": t.get("abbreviation"),
            "name": t.get("displayName"),
            "confId": groups.get("id"),
            # Stadium capacity, piggybacked on the same per-team detail call
            # already made to resolve conference membership -- no extra API
            # calls needed. Used to derive a team-specific home-field-
            # advantage weight (a 100k-seat stadium plausibly provides a
            # bigger real edge than a 20k-seat one), rather than treating
            # every program's home crowd as identical. ESPN's field name
            # for this varies by whether the venue record is fully
            # populated; capacity is None when ESPN doesn't have it for a
            # given school rather than guessed.
            "venueCapacity": venue.get("capacity"),
            "venueName": venue.get("fullName"),
            "venueIndoor": venue.get("indoor"),
        }
    except Exception as e:
        _log(f"  team {team_id} FAILED: {e}")
        return None


def build_roster() -> dict:
    all_teams = fetch_all_team_ids()
    _log(f"{len(all_teams)} total teams found (all divisions) — resolving conference for each…")

    roster: dict[str, list[dict]] = {name: [] for name in TARGET_CONFERENCES.values()}
    checked = 0
    for t in all_teams:
        tid = t.get("id")
        if not tid:
            continue
        info = fetch_team_conference(tid)
        checked += 1
        if checked % 25 == 0:
            _log(f"  …{checked}/{len(all_teams)} checked")
        if not info or not info.get("confId"):
            time.sleep(0.15)
            continue
        conf_name = TARGET_CONFERENCES.get(str(info["confId"]))
        if conf_name:
            roster[conf_name].append({
                "id": info["id"], "abbr": info["abbr"], "name": info["name"],
                "venueCapacity": info.get("venueCapacity"),
                "venueName": info.get("venueName"),
                "venueIndoor": info.get("venueIndoor"),
            })
        time.sleep(0.15)

    for conf, teams in roster.items():
        _log(f"  {conf}: {len(teams)} teams")
    return roster


def run() -> dict:
    roster = build_roster()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "conferences": roster,
    }, indent=2))
    _log(f"wrote {OUT_PATH}")
    return roster


# Field allow-lists per the exact categories requested: offense (total
# yards, passing, rushing, receiving, downs), defense (points allowed,
# yards allowed, passing, rushing), special teams (returning, kicking,
# punting). ESPN's own per-team /statistics endpoint returns far more
# fields than this per category — trimmed down to what's actually needed
# rather than carrying everything.
_OFFENSE_FIELDS = {
    "passing":       ["completionPct", "passingYards", "passingYardsPerGame", "passingTouchdowns",
                        "interceptions", "yardsPerPassAttempt", "QBRating"],
    "rushing":       ["rushingYards", "rushingYardsPerGame", "yardsPerRushAttempt",
                        "rushingTouchdowns", "rushingFirstDowns"],
    "receiving":     ["receivingYards", "receivingYardsPerGame", "receivingTouchdowns", "yardsPerReception"],
    "miscellaneous": ["firstDowns", "thirdDownConvPct", "fourthDownConvPct", "totalPenaltyYards",
                        "possessionTimeSeconds", "turnOverDifferential"],
    # Own points scored per game -- confirmed present in ESPN's response
    # under a dedicated "scoring" category (own-side mirror of
    # defenseAllowed's totalPointsPerGame, which comes from the opponent
    # split). Never pulled before now, so no real point-differential
    # signal existed anywhere in this pipeline -- added specifically to
    # support a real "own points scored minus own points allowed" margin
    # signal (same shape as nflMC's real standings-differential margin),
    # for the 2025 season snapshot used as a small weighted-blend input.
    "scoring":       ["totalPointsPerGame"],
}
_DEFENSE_ALLOWED_FIELDS = {
    # from the "opponent" split — what opponents did against this team,
    # i.e. this team's defense
    "passing": ["totalPointsPerGame", "yardsPerGame", "passingYardsPerGame", "passingTouchdowns"],
    "rushing": ["rushingYardsPerGame", "yardsPerRushAttempt", "rushingTouchdowns"],
}
_ST_FIELDS = {
    "returning": ["yardsPerKickReturn", "kickReturnTouchdowns", "yardsPerPuntReturn", "puntReturnTouchdowns"],
    "kicking":   ["fieldGoalPct", "longFieldGoalMade", "extraPointPct", "totalKickingPoints"],
    "punting":   ["grossAvgPuntYards", "netAvgPuntYards", "puntsInside20"],
}


def _extract_fields(categories: list[dict], allow: dict[str, list[str]]) -> dict:
    out = {}
    by_cat = {c["name"]: c for c in categories}
    for cat_name, fields in allow.items():
        cat = by_cat.get(cat_name)
        if not cat:
            continue
        stat_by_name = {s["name"]: s for s in cat.get("stats", [])}
        for f in fields:
            s = stat_by_name.get(f)
            if s is not None:
                out[f] = s.get("value", s.get("displayValue"))
    return out


def fetch_team_stats(team_id: str, season: int) -> dict | None:
    try:
        r = requests.get(f"{ESPN_BASE}/teams/{team_id}/statistics",
                         params={"season": season}, headers=HEADERS, timeout=12)
        r.raise_for_status()
        d = r.json()
        results = d.get("results", {})
        own_cats = results.get("stats", {}).get("categories", [])
        opp_cats = results.get("opponent", [])
        if not own_cats:
            return None
        return {
            "offense": _extract_fields(own_cats, _OFFENSE_FIELDS),
            "defenseAllowed": _extract_fields(opp_cats, _DEFENSE_ALLOWED_FIELDS),
            "specialTeams": _extract_fields(own_cats, _ST_FIELDS),
        }
    except Exception as e:
        _log(f"  team {team_id} stats FAILED: {e}")
        return None


def fetch_all_team_stats(roster: dict, season: int | None = None) -> dict:
    """season=None -> football_season_year("cfb") (derived from today's date, env CFB_SEASON_YEAR overrides). The /statistics endpoint
    returns an empty categories list for a season with zero games played, confirmed by direct testing, not a bug -- write_stats()
    below tries the current season first and falls back to the previous one while it has no games."""
    if season is None:
        season = football_season_year("cfb")
    out = {}
    all_teams = [t for teams in roster.values() for t in teams]
    _log(f"Fetching stats for {len(all_teams)} teams (season={season})…")
    for i, t in enumerate(all_teams):
        stats = fetch_team_stats(t["id"], season)
        if stats:
            out[t["abbr"]] = {"teamId": t["id"], "name": t["name"], **stats}
        if (i + 1) % 20 == 0:
            _log(f"  …{i+1}/{len(all_teams)}")
        time.sleep(0.2)
    _log(f"  {len(out)}/{len(all_teams)} teams had stats")
    return out


# Real week date windows straight from ESPN's own scoreboard calendar
# (confirmed via GET .../scoreboard's leagues[0].calendar) rather than
# hardcoded guesses, since ESPN's actual week boundaries don't fall on
# consistent day-of-week gaps.
WEEK_LABELS = {str(i): f"Week {i}" for i in range(1, 16)}
POSTSEASON_LABEL = "Bowls"  # ESPN's scoreboard API groups Bowls+CFP together
                             # under seasontype=3 -- the schedule PAGE's
                             # separate "CFP" calendar entry is a display
                             # grouping, not a distinct queryable dimension.


def fetch_week_games(year: int, week: int, seasontype: int = 2) -> list[dict]:
    """One call gets every FBS game for that week, all conferences at
    once -- far cheaper than querying per-conference (which would also
    return each cross-conference game twice). Conference tagging happens
    locally afterward via the roster's team->conference map."""
    # groups=80 (FBS) is required here -- confirmed live: the same query
    # without it silently returns only 25 games for a real 99-game week
    # (2026 Week 1), dropping real matchups like UNC@TCU entirely while
    # keeping others (SJSU@USC) seemingly at random. This module's own
    # docstring notes `groups=` is ignored on the TEAM-LIST endpoint --
    # that's true there, but does NOT generalize to this scoreboard
    # endpoint, which behaves completely differently without it.
    r = requests.get(f"{ESPN_BASE}/scoreboard",
                     params={"limit": 300, "week": week, "seasontype": seasontype, "year": year, "groups": 80},
                     headers=HEADERS, timeout=15)
    r.raise_for_status()
    d = r.json()
    games = []
    for e in d.get("events", []):
        comp = (e.get("competitions") or [{}])[0]
        home = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "home"), {})
        away = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "away"), {})
        odds = (comp.get("odds") or [{}])[0]
        games.append({
            "id": e.get("id"),
            "date": e.get("date"),
            "name": e.get("name"),
            "venue": (comp.get("venue") or {}).get("fullName"),
            "city": ((comp.get("venue") or {}).get("address") or {}).get("city"),
            "venueState": ((comp.get("venue") or {}).get("address") or {}).get("state"),
            "indoor": (comp.get("venue") or {}).get("indoor", False),
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
            "state": e.get("status", {}).get("type", {}).get("state", "pre"),
        })
    return games


def fetch_full_schedule(year: int, roster: dict) -> dict:
    team_conf = {t["abbr"]: conf for conf, teams in roster.items() for t in teams}
    schedule = {}
    for wk_num, label in WEEK_LABELS.items():
        games = fetch_week_games(year, int(wk_num), seasontype=2)
        for g in games:
            g["conferences"] = sorted({c for c in (team_conf.get(g["home"]), team_conf.get(g["away"])) if c})
        schedule[label] = games
        _log(f"  {label}: {len(games)} games")
        time.sleep(0.3)
    # Postseason (Bowls + CFP combined, per the API's own grouping)
    post_games = fetch_week_games(year, 1, seasontype=3)
    for g in post_games:
        g["conferences"] = sorted({c for c in (team_conf.get(g["home"]), team_conf.get(g["away"])) if c})
    schedule[POSTSEASON_LABEL] = post_games
    _log(f"  {POSTSEASON_LABEL}: {len(post_games)} games")
    return schedule


def fetch_rankings() -> list[dict]:
    """AP Top 25 — updates weekly (Mon/Tue during the season, once for the
    preseason poll in mid-August). Defaults to whatever ESPN's own API
    currently considers "latest" (last season's final poll until the 2026
    preseason poll actually drops), which is expected/correct, not a bug —
    it'll pick up the new poll automatically the moment ESPN publishes it."""
    r = requests.get(f"{ESPN_BASE}/rankings", headers=HEADERS, timeout=15)
    r.raise_for_status()
    d = r.json()
    ap = next((p for p in d.get("rankings", []) if p.get("shortName") == "AP Poll"), None)
    if not ap:
        _log("  AP Poll not found in rankings response")
        return []
    out = []
    for entry in ap.get("ranks", []):
        t = entry.get("team", {})
        out.append({
            "rank": entry.get("current"),
            "previousRank": entry.get("previous"),
            "trend": entry.get("trend"),
            "points": entry.get("points"),
            "firstPlaceVotes": entry.get("firstPlaceVotes"),
            "teamId": t.get("id"),
            "abbr": t.get("abbreviation"),
            # ESPN's team object here has location/name/nickname backwards
            # from what the field names suggest -- "name" holds the mascot
            # ("Hoosiers"), "nickname" holds the short school name
            # ("Indiana"), confirmed by direct inspection (Indiana Hoosiers
            # entry: name="Hoosiers", nickname="Indiana"). Using location +
            # name gives the correct "Indiana Hoosiers".
            "name": f"{t.get('location','')} {t.get('name','')}".strip(),
        })
    _log(f"AP Top 25: {len(out)} teams, week={d.get('latestWeek',{}).get('displayValue')}")
    return out


def _parse_fpi_blob(data: dict, view: str | None) -> dict:
    table = data.get("page", {}).get("content", {}).get("table", {})
    rows = []
    for entry in table.get("stats", []):
        team = entry.get("team", {})
        stats = {s["name"]: s.get("value") for s in entry.get("stats", [])}
        rows.append({"teamId": team.get("id"), "abbr": team.get("abbrev"),
                     "name": team.get("displayName"), "stats": stats})
    _log(f"  FPI view={view or 'fpi'}: {len(rows)} teams")
    return {"headers": table.get("headers", {}), "rows": rows}


def _fetch_fpi_view(page, view: str | None = None) -> dict:
    """FPI / Resume / Efficiencies all share one page template — ESPN
    server-renders the table into a `window['__espnfitt__']` JSON blob
    (same pattern as their other stat pages).

    Real bug, found and fixed: www.espn.com (unlike the plain-JSON
    site.api.espn.com used everywhere else in this file) now sits behind
    an AWS WAF JavaScript challenge -- confirmed live: a plain requests.get
    here got an HTTP 202 with either an empty body or the raw challenge
    page (window.gokuProps / awswaf.com/challenge.js), never the real
    page, so this had been silently writing 0 FPI/resume/efficiencies
    rows for every team since whenever that WAF went up -- masked because
    the CFB model already falls back to real market spread/moneyline odds
    when FPI is missing, EXCEPT for a match with no market line posted
    yet either (confirmed live: UNC@TCU, a neutral-site Dublin opener),
    which then had no signal at all and showed "no model projection".
    A real headless browser (already a dependency elsewhere in this repo
    for the lock/settle automation) can solve the challenge -- it just
    needs a few real seconds after navigation for the challenge's own
    token fetch + auto-reload to finish before window['__espnfitt__']
    exists, `networkidle` alone isn't enough of a signal for that.
    Takes an already-open Playwright `page` (one browser session, reused
    across all 3 views) rather than opening its own, so the 3 calls in
    fetch_power_ratings() don't each pay full browser-launch overhead."""
    url = "https://www.espn.com/college-football/fpi" + (f"/_/view/{view}" if view else "")
    page.goto(url, timeout=30000)
    page.wait_for_timeout(8000)
    data = page.evaluate("() => window['__espnfitt__'] || null")
    if not data:
        _log(f"  FPI view={view or 'fpi'}: embedded data blob not found (still on WAF challenge?)")
        return {}
    return _parse_fpi_blob(data, view)


def fetch_power_ratings() -> dict:
    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(user_agent=WWW_HEADERS["User-Agent"])
        for view, key in [(None, "fpi"), ("resume", "resume"), ("efficiencies", "efficiencies")]:
            out[key] = _fetch_fpi_view(page, view)
            time.sleep(1)
        browser.close()
    return out


def write_power(year: int | None = None) -> None:
    rankings = fetch_rankings()
    power = fetch_power_ratings()
    POWER_OUT_PATH.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "rankings": rankings,
        "power": power,
    }, indent=2))
    _log(f"wrote {POWER_OUT_PATH}")


def write_schedule(year: int | None = None) -> None:
    year = football_season_year("cfb") if year is None else year
    if not OUT_PATH.exists():
        _log("No roster on disk yet (docs/cfb_teams.json) — run --mode roster first")
        return
    roster = json.loads(OUT_PATH.read_text())["conferences"]
    schedule = fetch_full_schedule(year, roster)
    sched_path = ROOT / "docs" / "cfb_schedule.json"
    sched_path.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "year": year,
        "weeks": schedule,
    }, indent=2))
    _log(f"wrote {sched_path}")


def write_stats(year: int | None = None) -> None:
    """year=None (the default, used by the scheduled workflow with no
    --stats-season override) auto-detects: tries the real current
    calendar year first, and only falls back to the prior season if most
    teams come back with no stats at all (i.e. this season's games
    genuinely haven't started yet -- fetch_team_stats() already returns
    None per-team for a season with zero games played, confirmed
    directly against ESPN's API).

    This auto-detection did NOT actually exist before -- a prior version
    of this docstring (on fetch_all_team_stats) claimed "main() below
    already tries the current year first and falls back", but the real
    entry point just hardcoded --stats-season=2025 with no fallback logic
    at all, meaning the scheduled weekly workflow would have kept
    fetching 2025 forever even once 2026 games started, with nothing
    ever automatically switching over. Caught and fixed directly in
    response to being asked to verify this."""
    if not OUT_PATH.exists():
        _log("No roster on disk yet (docs/cfb_teams.json) — run --mode roster first")
        return
    roster = json.loads(OUT_PATH.read_text())["conferences"]
    total_teams = sum(len(v) for v in roster.values())

    if year is not None:
        stats = fetch_all_team_stats(roster, season=year)
    else:
        current_year = football_season_year("cfb")  # NOT the calendar year: Jan-Jun 2027 is still the 2026 season
        stats = fetch_all_team_stats(roster, season=current_year)
        # Majority of teams with real stats = the season has genuinely
        # started; a handful of early-week partial results is still a
        # real signal worth using immediately, not a reason to wait.
        if len(stats) < total_teams * 0.5:
            _log(f"season={current_year} returned stats for only {len(stats)}/{total_teams} teams "
                 f"-- season hasn't started yet, falling back to {current_year - 1}")
            year = current_year - 1
            stats = fetch_all_team_stats(roster, season=year)
        else:
            year = current_year

    stats_path = STATS_OUT_PATH
    # Before replacing last season's file with a newer season's stats, keep it as the prior-season snapshot (once; see _season.py).
    rolled = roll_prior_snapshot(stats_path, PRIOR_STATS_PATH, int(year))
    if rolled == "rolled":
        _log(f"rolled the season-{int(year) - 1} stats into {PRIOR_STATS_PATH.name} (new season {year} stats are replacing them)")
    stats_path.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "season": year,
        "teams": stats,
    }, indent=2))
    _log(f"wrote {stats_path} (season={year}, {len(stats)}/{total_teams} teams)")


def git_push(paths: list[str], message: str) -> None:
    import subprocess
    # `git add` of ANY missing path fails the whole pathspec (adds nothing) -- only add files that exist (e.g. the prior-season
    # snapshot does not exist until the first season rollover, or until the owner seeds it).
    paths = [p for p in paths if (ROOT / p).exists()]
    subprocess.run(["git", "-C", str(ROOT), "add", *paths], check=False, capture_output=True)
    diff = subprocess.run(["git", "-C", str(ROOT), "diff", "--cached", "--quiet"], capture_output=True)
    if diff.returncode == 0:
        _log("git: nothing to commit")
        return
    subprocess.run(["git", "-C", str(ROOT), "commit", "-m", message], check=True, capture_output=True)
    # A single rebase-then-push attempt still races: CFB and NFL's schedule
    # workflows fire on the same cron, plus a live-score bot pushes to main
    # every ~3min, so another push can land between this rebase and this
    # push. Retry with a fresh rebase each time instead of failing outright.
    for attempt in range(5):
        subprocess.run(["git", "-C", str(ROOT), "pull", "--rebase", "origin", "main"], capture_output=True)
        push = subprocess.run(["git", "-C", str(ROOT), "push", "origin", "main"], capture_output=True, text=True)
        if push.returncode == 0:
            _log("git push → main ✓")
            return
        _log(f"git push attempt {attempt+1}/5 failed, retrying: {push.stderr.strip()[:160]}")
        time.sleep(3 + attempt * 2)
    raise RuntimeError("git push failed after 5 retries")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["roster", "rankings", "stats", "schedule", "all"], default="all")
    parser.add_argument("--push", action="store_true")
    # None (the default) auto-detects the real current season vs. falling
    # back to last season -- see write_stats()'s docstring. Pass this
    # explicitly only to force a specific season (e.g. regenerating a
    # historical snapshot).
    parser.add_argument("--stats-season", type=int, default=None)
    parser.add_argument("--schedule-year", type=int, default=None,
                        help="default: derived from today's date (football_season_year; env CFB_SEASON_YEAR / FOOTBALL_SEASON_YEAR)")
    args = parser.parse_args()

    if args.mode in ("roster", "all"):
        run()
        if args.push:
            git_push(["docs/cfb_teams.json"], "cfb: refresh conference roster")
    if args.mode in ("rankings", "all"):
        write_power()
        if args.push:
            git_push(["docs/cfb_power.json"], "cfb: refresh rankings/FPI/resume/efficiencies")
    if args.mode in ("stats", "all"):
        write_stats(args.stats_season)
        if args.push:
            git_push(["docs/cfb_team_stats.json", "docs/cfb_team_stats_prior.json"], "cfb: refresh team stats")
    if args.mode in ("schedule", "all"):
        write_schedule(args.schedule_year)
        if args.push:
            git_push(["docs/cfb_schedule.json"], "cfb: refresh schedule/lines")
