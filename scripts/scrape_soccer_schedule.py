#!/usr/bin/env python3
"""
scrape_soccer_schedule.py — Daily same-day scoreboard/odds snapshot for
all 6 tracked soccer leagues (Champions League, Premier League, La Liga,
Bundesliga, MLS, Serie A).

Same convention as scrape_soccer_standings.py: standalone script, no
import dependency on clairvoyance_update.py. Writes docs/soccer_schedule.json
in the exact shape the frontend's own _fetchLeagueScoreboard() (docs/
app.html) already builds per-game, so it can seed the same
_leagueScoreCache that function's live browser fetch populates.

Why this exists: _fetchLeagueScoreboard() calls ESPN's public scoreboard
API directly from the browser at https://purple-wraith.github.io. Confirmed
by direct reproduction (headless browser hitting the live page) that this
call intermittently fails with a CORS error -- "No 'Access-Control-Allow-
Origin' header is present" -- even though the exact same endpoint returns
a normal 200 with a permissive `access-control-allow-origin: *` header
for a plain server-to-server request seconds later. This looks like
ESPN silently dropping the CORS header under some request-volume/abuse
heuristic rather than a permanent block, and it was the real cause of the
soccer-lock-early.yml pass occasionally gathering "0 games" for a whole
morning even on real matchdays (confirmed: the same 5 European leagues
had 9 real games that morning per direct ESPN API calls). A server-side
scrape has no CORS exposure at all (CORS is a browser-enforced policy,
never applied to this kind of request), so this file becomes the primary,
reliable source; the frontend's own live fetch still overwrites it with
fresher in-game data the moment a user actually opens a matches tab
(same pattern as loadSoccerStandingsSnapshot()/soccer_standings.json).

Usage:
  python3 scripts/scrape_soccer_schedule.py            # scrape + write, no push
  python3 scripts/scrape_soccer_schedule.py --push     # scrape + write + commit + push
  python3 scripts/scrape_soccer_schedule.py --tomorrow --push
      # Evening-prior lock support: scrapes TOMORROW's date instead of
      # today's, restricted to the 5 European leagues (CL/PL/La Liga/
      # Bundesliga/Serie A -- MLS excluded, it kicks off at normal US
      # evening times and doesn't have the early-kickoff problem this
      # exists for). Writes a SEPARATE file, docs/soccer_schedule_tomorrow.json,
      # so it never collides with or overwrites the live site's own
      # today-only docs/soccer_schedule.json. Consumed by
      # auto_lock_settle.py's --only-soccer-tomorrow lock pass (see
      # soccer-lock-evening.yml).
"""
from __future__ import annotations
import argparse, json, subprocess, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
OUT = ROOT / "docs" / "soccer_schedule.json"
OUT_TOMORROW = ROOT / "docs" / "soccer_schedule_tomorrow.json"
# "bl" (Bundesliga) removed 2026-09-23 -- retired from the evening-prior
# lock pipeline (see EURO_SOCCER_SPORTS' own comment in
# auto_lock_settle.py), so scraping tomorrow's Bundesliga schedule here
# would just be wasted work nothing downstream reads anymore. Left in
# ESPN_SOCCER_LEAGUES below (the regular daily, not tomorrow-specific,
# schedule scrape) since that file may still be read for general/
# historical display purposes.
EURO_LEAGUE_KEYS = ("cl", "pl", "liga", "ita")

# Deliberate standalone copy of scrape_soccer_standings.py's
# ESPN_SOCCER_LEAGUES (no import dependency between the two scripts, same
# convention as scrape_opta_stats.py). If a league gets added/renamed/
# removed in one, update the other too, or they'll silently drift on
# which leagues get a schedule snapshot.
ESPN_SOCCER_LEAGUES: dict[str, dict] = {
    "cl":   {"name": "Champions League", "espn": "UEFA.champions"},
    "pl":   {"name": "Premier League",   "espn": "eng.1"},
    "liga": {"name": "La Liga",          "espn": "esp.1"},
    "bl":   {"name": "Bundesliga",       "espn": "ger.1"},
    "mls":  {"name": "MLS",              "espn": "usa.1"},
    "ita":  {"name": "Serie A",          "espn": "ita.1"},
}


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def fetch_scoreboard(espn_league: str, date_str: str) -> list[dict]:
    """Mirrors _fetchLeagueScoreboard()'s own field extraction exactly
    (docs/app.html) -- same games.push({...}) shape -- so the frontend can
    read this file as a drop-in substitute with zero mapping logic."""
    r = requests.get(
        f"https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_league}/scoreboard",
        params={"dates": date_str, "limit": 50},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    games: list[dict] = []
    for ev in data.get("events") or []:
        comps = ev.get("competitions") or []
        comp = comps[0] if comps else None
        if not comp:
            continue
        home = away = None
        for c in comp.get("competitors") or []:
            if c.get("homeAway") == "home":
                home = c
            elif c.get("homeAway") == "away":
                away = c
        if not home or not away:
            continue
        status = ((comp.get("status") or {}).get("type") or {}).get("state")
        clock = (comp.get("status") or {}).get("displayClock")
        h_score = int(home.get("score") or 0) if str(home.get("score") or "").strip() else 0
        a_score = int(away.get("score") or 0) if str(away.get("score") or "").strip() else 0
        odds_list = comp.get("odds") or []
        odds = odds_list[0] if odds_list else None

        def _parse_am(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        h_ml = a_ml = d_ml = None
        if odds:
            h_ml = ((odds.get("homeTeamOdds") or {}).get("moneyLine"))
            a_ml = ((odds.get("awayTeamOdds") or {}).get("moneyLine"))
            ml_block = odds.get("moneyline") or {}
            if h_ml is None and ml_block.get("home"):
                blk = ml_block["home"]
                h_ml = _parse_am(((blk.get("close") or {}).get("odds")) or ((blk.get("open") or {}).get("odds")))
            if a_ml is None and ml_block.get("away"):
                blk = ml_block["away"]
                a_ml = _parse_am(((blk.get("close") or {}).get("odds")) or ((blk.get("open") or {}).get("odds")))
            d_ml = (odds.get("drawOdds") or {}).get("moneyLine")
            if d_ml is None and ml_block.get("draw"):
                blk = ml_block["draw"]
                d_ml = _parse_am(((blk.get("close") or {}).get("odds")) or ((blk.get("open") or {}).get("odds")))

        games.append({
            "id": ev.get("id"),
            "home": (home.get("team") or {}).get("displayName"),
            "away": (away.get("team") or {}).get("displayName"),
            "hAbbr": (home.get("team") or {}).get("abbreviation"),
            "aAbbr": (away.get("team") or {}).get("abbreviation"),
            "hScore": h_score,
            "aScore": a_score,
            "status": status or "pre",
            "clock": clock or "",
            "date": ev.get("date"),
            "ou": odds.get("overUnder") if odds else None,
            "hML": h_ml,
            "aML": a_ml,
            "dML": d_ml,
            "venue": ((comp.get("venue") or {}).get("fullName")),
        })
    games.sort(key=lambda g: g.get("date") or "")
    return games


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--tomorrow", action="store_true",
                     help="Scrape TOMORROW's date instead of today's, restricted to the 5 "
                          "European leagues (no MLS). Writes docs/soccer_schedule_tomorrow.json "
                          "instead of docs/soccer_schedule.json.")
    args = ap.parse_args()

    # UTC date param, same convention _fetchLeagueScoreboard's own
    # _fmtESPN(new Date()) produces for the browser's local (Denver) date --
    # this script always runs under TZ=America/Denver in its workflow, so
    # datetime.now() without tz info reads the same wall-clock date.
    target_day = datetime.now() + timedelta(days=1) if args.tomorrow else datetime.now()
    date_str = target_day.strftime("%Y%m%d")
    leagues = {k: v for k, v in ESPN_SOCCER_LEAGUES.items() if (not args.tomorrow or k in EURO_LEAGUE_KEYS)}
    out_path = OUT_TOMORROW if args.tomorrow else OUT

    result: dict[str, list[dict]] = {}
    for key, cfg in leagues.items():
        try:
            games = fetch_scoreboard(cfg["espn"], date_str)
            result[key] = games
            log(f"{cfg['name']}: {len(games)} games")
        except Exception as exc:
            log(f"{cfg['name']}: FAILED {exc}")
            result[key] = []
        time.sleep(0.5)

    if not any(result.values()):
        log("no games for any league -- writing anyway (a real 0-fixture day is possible, e.g. no Monday matches)")

    out_path.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "date": date_str,
        "leagues": result,
    }, indent=2))
    log(f"wrote {out_path}")

    if args.push:
        subprocess.run(["git", "add", str(out_path.relative_to(ROOT))], cwd=ROOT, check=True)
        commit_msg = ("chore: refresh soccer schedule/odds snapshot (tomorrow)" if args.tomorrow
                      else "chore: refresh soccer schedule/odds snapshot")
        commit = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=ROOT, capture_output=True, text=True,
        )
        if commit.returncode != 0:
            log("nothing to commit")
            return
        for attempt in range(5):
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=ROOT, capture_output=True)
            push = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT, capture_output=True, text=True)
            if push.returncode == 0:
                log("pushed")
                return
            log(f"push attempt {attempt+1}/5 failed, retrying: {push.stderr.strip()[:160]}")
            time.sleep(3 + attempt * 2)
        raise RuntimeError("git push failed after 5 retries")


if __name__ == "__main__":
    main()
