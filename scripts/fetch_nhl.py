"""NHL full-season schedule — real matchups, times, and market moneyline/
spread/total odds for all 32 teams, one file, same architecture as
fetch_nfl.py/fetch_cfb.py.

Explicit follow-up to a pre-season hockey audit: this app's only existing
NHL schedule fetch (fetch_nhl_today() in clairvoyance_update.py) is a
single-day-plus-tomorrow lookup, run once per pipeline pass for the live
game-card/lock flow -- there was no way to see the whole season's real
matchups/odds at once the way NFL/CFB already can. NHL plays close to
every day of its season (unlike NFL's week structure), so this iterates
one ESPN scoreboard call per calendar date across the regular season
rather than per-week.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_OUT = ROOT / "docs" / "nhl_schedule.json"

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl"
# Real gap, found live (and independently already documented in
# fetch_nfl.py -- this isn't new, just newly confirmed for this same
# domain from a hockey-specific script): site.api.espn.com 403s any
# request carrying a custom User-Agent, spoofed-browser or otherwise --
# requests' own default "python-requests/x.x" UA (i.e. no custom header
# at all) is what actually gets through. Empty dict, not removed
# entirely, so call sites don't need to change if that ever flips back.
HEADERS: dict = {}

# ESPN's own team abbreviations don't match this app's canonical NHL
# object for 5 of 32 teams -- confirmed live pulling every team from
# ESPN's own /teams endpoint and diffing against docs/app.html's NHL
# const keys. Normalized so a game's home/away fields always line up
# with NHL[abbr] lookups elsewhere in this app.
ESPN_ABBR_FIX = {"LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL", "UTAH": "UTA"}


def _log(msg: str) -> None:
    print(f"[nhl] {msg}", flush=True)


def _nhl_current_season_id() -> str:
    """NHL seasons run ~Oct-Jun/Jul, named startYear+startYear+1 (e.g.
    "20252026"). New season year-cycle begins ~August (draft/preseason
    ramp-up) -- matches the same boundary docs/app.html's own
    _nhlCurrentSeasonId() and clairvoyance_update.py's
    _nhl_current_season_id() use, kept in sync by convention since these
    are 3 independent small functions across 2 languages, not a shared
    import."""
    now = datetime.now(timezone.utc)
    start_year = now.year if now.month >= 8 else now.year - 1
    return f"{start_year}{start_year + 1}"


def fetch_full_schedule() -> dict:
    """
    Real regular-season schedule for all 32 teams, with real market
    moneyline/O-U odds where posted. Season boundaries computed from
    _nhl_current_season_id() rather than hardcoded, so this doesn't need
    updating by hand each year: real NHL regular seasons open in late
    September and run to mid-April, generously bounded here through
    April 20 (worst case the last few calls return zero games if the
    real season ends a little earlier, which costs nothing -- an empty
    scoreboard day is not an error).
    """
    _log("full regular-season schedule…")
    season_id = _nhl_current_season_id()
    start_year = int(season_id[:4])
    d = date(start_year, 9, 29)
    end = date(start_year + 1, 4, 20)
    games: list[dict] = []
    n_days_with_games = 0
    while d <= end:
        date_str = d.strftime("%Y%m%d")
        try:
            r = requests.get(
                f"{ESPN_BASE}/scoreboard",
                params={"dates": date_str, "limit": 50},
                headers=HEADERS, timeout=15,
            )
            r.raise_for_status()
            resp = r.json()
            day_count = 0
            for ev in (resp or {}).get("events", []):
                comp = (ev.get("competitions") or [{}])[0]
                home = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "home"), {})
                away = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "away"), {})
                status = comp.get("status", {})
                home_abbr = (home.get("team") or {}).get("abbreviation")
                away_abbr = (away.get("team") or {}).get("abbreviation")
                home_abbr = ESPN_ABBR_FIX.get(home_abbr, home_abbr)
                away_abbr = ESPN_ABBR_FIX.get(away_abbr, away_abbr)
                game = {
                    "id":        ev.get("id"),
                    "date":      ev.get("date"),
                    "home":      home_abbr,
                    "homeName":  (home.get("team") or {}).get("displayName"),
                    "homeScore": int(home["score"]) if home.get("score") not in (None, "") else None,
                    "away":      away_abbr,
                    "awayName":  (away.get("team") or {}).get("displayName"),
                    "awayScore": int(away["score"]) if away.get("score") not in (None, "") else None,
                    "venue":     (comp.get("venue") or {}).get("fullName"),
                    "state":     status.get("type", {}).get("state", "pre"),
                }
                odds = (comp.get("odds") or [{}])[0]
                if odds:
                    # Real schema gap, found live: unlike NFL's own
                    # odds.homeTeamOdds.moneyLine field, NHL's real
                    # moneyline lives nested under
                    # odds.moneyline.{home,away}.close.odds as a string
                    # ("-130"/"+110") -- homeTeamOdds/awayTeamOdds only
                    # carry favorite/underdog booleans here, no
                    # moneyLine field at all.
                    ml = odds.get("moneyline") or {}
                    home_ml_raw = ((ml.get("home") or {}).get("close") or {}).get("odds")
                    away_ml_raw = ((ml.get("away") or {}).get("close") or {}).get("odds")
                    try:
                        home_ml = int(home_ml_raw) if home_ml_raw not in (None, "") else None
                    except (TypeError, ValueError):
                        home_ml = None
                    try:
                        away_ml = int(away_ml_raw) if away_ml_raw not in (None, "") else None
                    except (TypeError, ValueError):
                        away_ml = None
                    # Real O/U prices (2026-10-03): the total line alone says nothing about the market's no-vig
                    # P(over) -- ESPN also carries the over/under prices (odds.total.over/under.close.odds, American
                    # strings like "+105"/"-125", same single book as the moneyline). docs/app.html's nhlEns uses
                    # them (with the ML) to blend the model toward the market (HOCKEY_MKT_BLEND_ALPHA).
                    tot = odds.get("total") or {}

                    def _am(side):
                        raw = ((tot.get(side) or {}).get("close") or {}).get("odds")
                        try:
                            return int(raw) if raw not in (None, "") else None
                        except (TypeError, ValueError):
                            return None

                    game.update({
                        "homeML":    home_ml,
                        "awayML":    away_ml,
                        "spread":    odds.get("spread"),
                        "overUnder": odds.get("overUnder"),
                        "ouOver":    _am("over"),
                        "ouUnder":   _am("under"),
                    })
                games.append(game)
                day_count += 1
            if day_count:
                n_days_with_games += 1
        except Exception as exc:
            _log(f"  {date_str}: {exc}")
        time.sleep(0.15)
        d += timedelta(days=1)
    _log(f"  {len(games)} games across {n_days_with_games} real game days (season {season_id})")
    return {"season": season_id, "games": games}


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
    # Same push-first-then-rebase-retry pattern already proven across
    # this repo's other fetch scripts -- main gets pushed to constantly
    # by multiple concurrent scheduled jobs, so a bare push failing once
    # is a normal race, not a real error.
    for attempt in range(5):
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=ROOT, capture_output=True)
        push = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT, capture_output=True, text=True)
        if push.returncode == 0:
            _log("  pushed")
            return
        _log(f"  push attempt {attempt + 1}/5 failed, retrying: {push.stderr.strip()[:160]}")
        time.sleep(3 + attempt * 2)
    raise RuntimeError("git push failed after 5 retries")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    sched = fetch_full_schedule()
    _write(SCHEDULE_OUT, sched)

    if args.push:
        git_push(["docs/nhl_schedule.json"], "chore: refresh NHL full-season schedule")
