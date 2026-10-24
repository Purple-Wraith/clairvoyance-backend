#!/usr/bin/env python3
"""NHL team Over/Under records from Flashscore's NHL standings "OVER/UNDER" tab -> docs/nhl_ou.json.

Flashscore publishes, for every team, how many of its games finished OVER / UNDER a goal line (the tab has one page per line).  This script keeps the four lines
the owner picked (2026-10-24): 4.5, 5.5, 6.5 and 7.5 -- for the team's OVERALL games and for its HOME and AWAY games separately -- plus last season's overall record, so
early-season counts (a dozen games) are steadied by a full prior season.  The counts are goals scored by both teams in regulation + overtime (the shootout-winning goal is not
counted, as sportsbooks settle totals -- checked against last season's real results in scripts/test_nhl_ou.py's fixture notes).

    python3 scripts/fetch_nhl_ou.py            # write docs/nhl_ou.json
    python3 scripts/fetch_nhl_ou.py --push     # ... and commit + push it (what the scheduled workflow does)

File shape (docs/nhl_ou.json):
    {"generated_at": "2026-10-24 12:10 UTC", "season": "2026-27", "lines": [4.5, 5.5, 6.5, 7.5],
     "teams": {"VGK": {"name": "Vegas Golden Knights", "fsId": "fTT0TsUg", "gf": 34, "ga": 55, "gm": 7.4,
                       "overall": {"gp": 12, "ou": {"4.5": [11, 1], "5.5": [10, 2], "6.5": [8, 4], "7.5": [5, 7]}},      # ou[line] = [over, under]
                       "home": {"gp": 6, "ou": {...}}, "away": {"gp": 6, "ou": {...}}}},
     "prev": {"season": "2025-26", "teams": {"VGK": {"gp": 82, "gm": 6.1, "ou": {...}}}}}
The scraping itself (rows, validation, in-page tab clicks) lives in scripts/_flashscore_ou.py, shared with the four European hockey scrapers.

Fail-open: a team whose rows do not add up (over + under != games, or counts that rise with the line) is dropped; fewer than 28 clean teams raises and the previous
committed file is left untouched.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from _flashscore_ou import LINES, SCOPES, scrape_teams

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "nhl_ou.json"
SCHEDULE = ROOT / "docs" / "nhl_schedule.json"
HOME = "https://www.flashscore.com/hockey/usa"
MIN_TEAMS = 28

# Flashscore team name -> the NHL-API three-letter code the rest of the app uses (LAK, NJD, SJS, TBL, UTA ...).  docs/nhl_schedule.json (ESPN names + codes) fills any name
# not listed, so a rename / relocation is picked up without a code change.
NAME_TO_CODE = {
    "Anaheim Ducks": "ANA", "Boston Bruins": "BOS", "Buffalo Sabres": "BUF", "Calgary Flames": "CGY", "Carolina Hurricanes": "CAR", "Chicago Blackhawks": "CHI",
    "Colorado Avalanche": "COL", "Columbus Blue Jackets": "CBJ", "Dallas Stars": "DAL", "Detroit Red Wings": "DET", "Edmonton Oilers": "EDM", "Florida Panthers": "FLA",
    "Los Angeles Kings": "LAK", "Minnesota Wild": "MIN", "Montreal Canadiens": "MTL", "Nashville Predators": "NSH", "New Jersey Devils": "NJD", "New York Islanders": "NYI",
    "New York Rangers": "NYR", "Ottawa Senators": "OTT", "Philadelphia Flyers": "PHI", "Pittsburgh Penguins": "PIT", "San Jose Sharks": "SJS", "Seattle Kraken": "SEA",
    "St. Louis Blues": "STL", "St Louis Blues": "STL", "Tampa Bay Lightning": "TBL", "Toronto Maple Leafs": "TOR", "Utah Mammoth": "UTA", "Utah Hockey Club": "UTA",
    "Vancouver Canucks": "VAN", "Vegas Golden Knights": "VGK", "Washington Capitals": "WSH", "Winnipeg Jets": "WPG", "Arizona Coyotes": "ARI",
}


def log(msg: str) -> None:
    print(f"[fetch_nhl_ou] {msg}", file=sys.stderr, flush=True)


def _code_map() -> dict:
    m = dict(NAME_TO_CODE)
    try:
        for g in json.loads(SCHEDULE.read_text()).get("games", []):
            for side in ("home", "away"):
                if g.get(f"{side}Name") and g.get(side):
                    m.setdefault(g[f"{side}Name"], g[side])
    except Exception:
        pass
    return m


def find_ou_base(page, standings_url: str) -> str | None:
    """The tournament-stage id changes every season, so read the Over/Under tab's own link off the standings page."""
    page.goto(standings_url, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(1500)
    hrefs = page.evaluate("[...document.querySelectorAll('a[href*=\"over_under\"]')].map(a=>a.getAttribute('href'))")
    for h in hrefs:
        m = re.match(r"^(/hockey/usa/[^/]+/standings/[A-Za-z0-9]+/over_under)/?", h or "")
        if m:
            return "https://www.flashscore.com" + m.group(1)
    return None


def to_codes(teams: dict, cmap: dict) -> dict:
    out = {}
    for fsid, t in teams.items():
        code = cmap.get(t["name"])
        if not code:
            log(f"  no NHL code for '{t['name']}' -- skipped")
            continue
        out[code] = {"name": t["name"], "fsId": fsid, **{k: v for k, v in t.items() if k != "name"}}
    return out


def season_labels(now: datetime) -> tuple[str, str, str, str]:
    y = now.year if now.month >= 7 else now.year - 1
    return f"{y}-{str(y + 1)[2:]}", f"{y - 1}-{str(y)[2:]}", f"nhl-{y - 1}-{y}", f"{y}"


def run() -> dict:
    now = datetime.now(timezone.utc)
    cur_label, prev_label, prev_slug, _ = season_labels(now)
    cmap = _code_map()
    prev_doc = {}
    try:
        prev_doc = json.loads(OUT.read_text())
    except Exception:
        pass
    reuse_prev = (prev_doc.get("prev") or {}).get("season") == prev_label and len((prev_doc.get("prev") or {}).get("teams") or {}) >= MIN_TEAMS
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(timezone_id="UTC", locale="en-US")
        page = context.new_page()
        base = find_ou_base(page, f"{HOME}/nhl/standings/")
        if not base:
            raise RuntimeError("could not find the NHL Over/Under tab link on Flashscore's standings page")
        log(f"current Over/Under base: {base}")
        cur = to_codes(scrape_teams(context, base, SCOPES, LINES, log), cmap)
        prev = (prev_doc.get("prev") or {}).get("teams") if reuse_prev else None
        if not prev:
            pbase = find_ou_base(page, f"{HOME}/{prev_slug}/standings/")
            if pbase:
                log(f"prior season ({prev_label}) base: {pbase}")
                pt = to_codes(scrape_teams(context, pbase, ("overall",), LINES, log), cmap)
                prev = {c: {"gp": t["overall"]["gp"], "gm": t["gm"], "ou": t["overall"]["ou"]} for c, t in pt.items()}
            else:
                log("prior season Over/Under tab not found -- prev left empty")
                prev = {}
        browser.close()
    if len(cur) < MIN_TEAMS:
        raise RuntimeError(f"only {len(cur)} clean NHL teams parsed (need {MIN_TEAMS}); keeping the existing {OUT.name}")
    doc = {"generated_at": now.strftime("%Y-%m-%d %H:%M UTC"), "season": cur_label, "lines": list(LINES), "teams": cur, "prev": {"season": prev_label, "teams": prev or {}}}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1))
    log(f"wrote {OUT} -- {len(cur)} teams, prior season {len(prev or {})} teams")
    return doc


def git_push(paths: list[str], message: str) -> None:
    subprocess.run(["git", "add", *paths], cwd=ROOT, check=True)
    r = subprocess.run(["git", "commit", "-m", message], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"nothing to commit ({r.stdout.strip()[:120]})")
        return
    from _scraper_health import park_health
    restore = park_health()
    try:
        for attempt in range(5):
            subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], cwd=ROOT, capture_output=True)
            push = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT, capture_output=True, text=True)
            if push.returncode == 0:
                log("pushed")
                return
            log(f"push attempt {attempt + 1}/5 failed, retrying: {push.stderr.strip()[:160]}")
            time.sleep(3 + attempt * 2)
        raise RuntimeError("git push failed after 5 retries")
    finally:
        restore()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()
    run()
    if args.push:
        git_push(["docs/nhl_ou.json"], "chore: refresh NHL over/under records (Flashscore)")
