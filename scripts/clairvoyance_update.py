#!/usr/bin/env python3
from __future__ import annotations
"""
clairvoyance_update.py — Clairvoyance Master Data Refresh Engine v6.0
Fetches live stats, odds, schedules, standings, props, injuries, advanced
analytics across MLB, NBA, NHL, F1 then pushes to GitHub Pages.

Usage:
  python3 scripts/clairvoyance_update.py                  # full fetch + write
  python3 scripts/clairvoyance_update.py --push           # + git push
  python3 scripts/clairvoyance_update.py --mode live      # live-window loop (17:00–23:00 MT)
  python3 scripts/clairvoyance_update.py --mode props     # retired no-op (Linemate removed 2026-09-10)
  python3 scripts/clairvoyance_update.py --sport nhl      # single sport
  python3 scripts/clairvoyance_update.py --no-reference   # skip Baseball/Basketball/Hockey Ref
  python3 scripts/clairvoyance_update.py --verbose

Cron (MT times — TZ=America/Denver):
  0 8,12,16,20,0 * * *  full refresh + push
  0 17           * * *  live-window mode (self-terminates 23:00 MT)

Data sources (v6.0):
  ESPN APIs, NHL API, MoneyPuck, TennisAbstract Elo, Ergast F1,
  ESPN F1 scoreboard/standings, TennisAbstract Roland Garros, Sports-Reference
  (Baseball/Basketball/Hockey-Reference), FBref (Champions League/Premier
  League/La Liga/Bundesliga/MLS), Open-Meteo weather

IMPORTANT — Sports-Reference family lag (Baseball-Ref, Basketball-Ref,
Hockey-Ref, FBref, TennisAbstract): these sites finalize a given day's box
scores / xG / advanced stats the FOLLOWING calendar day, not same-day. A game
played June 23 won't show real numbers on any of these sites until June 24.
This is why the 09:00 MT run matters most — it's the first refresh that can
see yesterday's now-finalized numbers. A same-day evening refresh will still
be scraping incomplete/prior data for anything that happened that day.
"""

import argparse, csv, io, json, os, re, shutil, subprocess, sys, time
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

# ── load .env early (before any os.environ reads) ────────────────────────────
def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — no external deps required."""
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:   # don't override shell env
                os.environ[key] = val
    except Exception:
        pass

_load_dotenv(Path(__file__).parent.parent / ".env")

# ── dependency bootstrap ──────────────────────────────────────────────────────
try:
    import requests
    from bs4 import BeautifulSoup, Comment
except ImportError:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "requests", "beautifulsoup4", "lxml"],
        check=True, capture_output=True,
    )
    import requests
    from bs4 import BeautifulSoup, Comment

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `import _espn_injuries` however this is launched
import _espn_injuries  # pure ESPN injuries/roster parsers (see that module's docstring for the 2 silent bugs it fixes)
import _nhl_skaters    # pure NHL skater points/game table for the app's injury adjustment
import _nba_espn       # ESPN-sourced NBA team ratings + player-prop inputs (retry/health helpers, parsers, possession math)

# ── paths & config ────────────────────────────────────────────────────────────
ROOT     = Path(__file__).parent.parent
FE       = ROOT / "docs" / "app.html"          # Engine SPA — source of truth (index.html is a copy)
FE_DATA  = ROOT / "docs" / "data.json"        # Engine data pushed to docs/ → github.io
DATA     = ROOT / "data"
LOGS     = ROOT / "logs"

for _d in (DATA, LOGS):
    _d.mkdir(exist_ok=True)

BET_HISTORY_JSON = DATA / "bet_history.json"
BET_HISTORY_CSV  = DATA / "bet_history.csv"

# ESPN's site.api.espn.com now 403s any request carrying a custom
# User-Agent, browser-spoofed or not (verified directly: dropping the
# User-Agent header entirely -- keeping Accept/Accept-Language -- is what
# actually gets through; requests' own default "python-requests/x.x" UA
# is what a bare request without this header sends). _session below
# applies this dict to every ESPN call in the whole pipeline (MLB/NBA/
# NHL/WNBA schedules, scores, odds), so this one change unblocks all of
# them. Only REF_HEADERS (Sports-Reference, a different site entirely,
# unaffected by this) still sends a browser UA.
HEADERS = {
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
REF_HEADERS = {          # Sports-Reference sites want a real browser UA
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://www.google.com/",
}

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
NHL_API   = "https://api-web.nhle.com/v1"
NHL_STATS = "https://api.nhle.com/stats/rest/en"
MP_BASE = "https://moneypuck.com/moneypuck/playerData/seasonSummary"
YEAR      = 2026  # legacy/unused -- the WNBA fns derive their own from NOW_MT.year

# Explicit request, 2026-09-03: WNBA's real regular season has ended
# (confirmed live: zero real games 2026-09-01 through 09-05 on ESPN's
# own scoreboard; the next real games are the 2026-09-17/18 playoff
# openers) -- retired for the full offseason, through the 2027 season,
# not just this pre-playoff gap (explicit choice). Real ledger check:
# zero active WNBA subscribers right now, so this has no live customer
# impact today. Flip back to False once ready to cover the 2027 season
# -- every fetch_wnba*() function below short-circuits to its own empty
# default when this is True, rather than being removed or having its
# logic altered, so reactivating is exactly this one line, not a
# rewrite. Deliberately NOT the same "static offseason messaging"
# treatment an earlier pass gave NBA/NHL's own UI (docs/app.html) --
# that one accidentally gutted and orphaned real, still-referenced
# render functions along with the display copy; this only pauses the
# real, wasted daily fetches for a sport with no real games to fetch.
WNBA_OFFSEASON = True

# See the soccer_fbref roster block in main(): ESPN has no soccer injury feed, so the roster fetch that only existed to match
# injuries to clubs is off.  True = restore the (~96 requests/run) per-league roster fetch.
SOCCER_ROSTER_FETCH_ENABLED = False

NOW        = datetime.now(timezone.utc)
try:
    import zoneinfo
    _MT = zoneinfo.ZoneInfo("America/Denver")
    _ET = zoneinfo.ZoneInfo("America/New_York")
    NOW_MT = datetime.now(_MT)
    NOW_ET = datetime.now(_ET)
except Exception:
    NOW_MT = NOW - timedelta(hours=6)
    NOW_ET = NOW - timedelta(hours=4)

TODAY_MT   = NOW_MT.strftime("%Y%m%d")
TODAY_ET   = NOW_ET.strftime("%Y%m%d")   # MLB uses Eastern Time for scheduling
TODAY_ISO  = NOW_MT.strftime("%Y-%m-%d")
TS_DISPLAY = NOW_MT.strftime("%Y-%m-%d %H:%M MT")

# ── Seeded bets — always injected into data.json so mergeSeededBets() in the
#    frontend picks them up on every network-first data.json fetch (bypasses SW cache).
#    Bump seed_v when updating outcomes.
_FG1 = 1748995800000  # June 3 2026 8:30 PM ET
SEEDED_BETS: list[dict] = [
    # ── WCF G1 historical wins (May 18 2026) ──
    {"id":"prop_hist_wemby_pra_g1","sport":"NBA","betType":"PROP","betOn":"Victor Wembanyama OVER PTS+REB+AST 37.5","hA":"SA","awA":"OKC","ml":"-115","decOdds":1.869,"winProb":0.68,"wager":1,"outcome":"win","date":"2026-05-18","lockedAt":1747616400000,"note":"WCF G1: Wemby 41+24+4=69 total. SA won 122-115 2OT."},
    {"id":"prop_hist_jwill_pts_g1","sport":"NBA","betType":"PROP","betOn":"Jalen Williams OVER PTS 21.5","hA":"SA","awA":"OKC","ml":"-115","decOdds":1.869,"winProb":0.63,"wager":1,"outcome":"win","date":"2026-05-18","lockedAt":1747616400000,"note":"WCF G1 2OT: Williams scored 22+ pts."},
    # ── NBA Finals G1 results (June 3 2026) ──
    {"id":"finals_g1_wemby_pts","sport":"NBA","betType":"PROP","betOn":"Victor Wembanyama OVER PTS 24.5","hA":"SA","awA":"NY","ml":"-113","decOdds":1.885,"winProb":0.70,"wager":1,"outcome":"win","date":"2026-06-03","lockedAt":_FG1,"note":"NBA Finals G1 ✓ WIN. Wemby scored 40+ pts."},
    {"id":"finals_g1_wemby_pts_265","sport":"NBA","betType":"PROP","betOn":"Victor Wembanyama OVER PTS 26.5","hA":"SA","awA":"NY","ml":"-110","decOdds":1.909,"winProb":0.68,"wager":1,"outcome":"win","date":"2026-06-03","lockedAt":_FG1,"note":"NBA Finals G1 ✓ WIN. Wemby cleared 26.5 with ease."},
    {"id":"finals_g1_champagnie_pts","sport":"NBA","betType":"PROP","betOn":"Julian Champagnie OVER PTS 9.5","hA":"SA","awA":"NY","ml":"-125","decOdds":1.800,"winProb":0.61,"wager":1,"outcome":"win","date":"2026-06-03","lockedAt":_FG1,"note":"NBA Finals G1 ✓ WIN. Champagnie hit shots off Wemby gravity."},
    {"id":"finals_g1_wemby_reb","sport":"NBA","betType":"PROP","betOn":"Victor Wembanyama OVER REB 9.5","hA":"SA","awA":"NY","ml":"-115","decOdds":1.869,"winProb":0.65,"wager":1,"outcome":"win","date":"2026-06-03","lockedAt":_FG1,"note":"NBA Finals G1 ✓ WIN. Wemby dominated the glass."},
    {"id":"finals_g1_brunson_ast","sport":"NBA","betType":"PROP","betOn":"Jalen Brunson OVER AST 6.5","hA":"SA","awA":"NY","ml":"-128","decOdds":1.781,"winProb":0.68,"wager":1,"outcome":"loss","date":"2026-06-03","lockedAt":_FG1,"note":"NBA Finals G1 ✗ LOSS. Brunson held under 6.5 assists."},
    {"id":"finals_g1_castle_pts","sport":"NBA","betType":"PROP","betOn":"Stephon Castle OVER PTS 13.5","hA":"SA","awA":"NY","ml":"-115","decOdds":1.869,"winProb":0.64,"wager":1,"outcome":"win","date":"2026-06-03","lockedAt":_FG1,"note":"NBA Finals G1 ✓ WIN. Castle delivered a strong performance."},
    {"id":"finals_g1_brunson_pts","sport":"NBA","betType":"PROP","betOn":"Jalen Brunson OVER PTS 21.5","hA":"SA","awA":"NY","ml":"-118","decOdds":1.847,"winProb":0.63,"wager":1,"outcome":"win","date":"2026-06-03","lockedAt":_FG1,"note":"NBA Finals G1 ✓ WIN. Brunson cleared 21.5 pts."},
    {"id":"finals_g1_wemby_3pm","sport":"NBA","betType":"PROP","betOn":"Victor Wembanyama OVER 3PM 1.5","hA":"SA","awA":"NY","ml":"-170","decOdds":1.588,"winProb":0.72,"wager":1,"outcome":"win","date":"2026-06-03","lockedAt":_FG1,"note":"NBA Finals G1 ✓ WIN. Wemby hit 2+ threes."},
    {"id":"finals_g1_og_pts","sport":"NBA","betType":"PROP","betOn":"OG Anunoby OVER PTS 12.5","hA":"SA","awA":"NY","ml":"-112","decOdds":1.893,"winProb":0.62,"wager":1,"outcome":"win","date":"2026-06-03","lockedAt":_FG1,"note":"NBA Finals G1 ✓ WIN. OG cleared 12.5 pts."},
]

_verbose  = False
_changes: list[str] = []

# ── logging ───────────────────────────────────────────────────────────────────
def log(msg: str, level: str = "INFO") -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{level}] {msg}", flush=True)

def vlog(msg: str) -> None:
    if _verbose: log(msg, "DEBUG")

def note(msg: str) -> None:
    _changes.append(msg); log(msg)

def _notify(title: str, msg: str) -> None:
    """macOS system notification via osascript."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "Clairvoyance ⚡ {title}" sound name "Glass"'],
            capture_output=True, timeout=5
        )
    except Exception:
        pass  # non-macOS or osascript unavailable — silent fail

def _alert(title: str, msg: str, level: str = "info") -> None:
    """
    Local macOS notification (silently no-ops off macOS, including on
    GitHub Actions runners). Discord webhook alerting was never actually
    set up (DISCORD_WEBHOOK_URL was never configured as a secret) and has
    been retired -- email alerting via _gmail_email.py is the replacement
    path going forward, not built into this generic alert function yet.
    """
    _notify(title, msg)

def _check_source_health(label: str, count: int, prior_count: int | None = None) -> None:
    """
    Fires an alert when a data source comes back empty. Without prior-run
    history this can't distinguish "genuinely 0 games today" (real, common —
    e.g. an off-day) from "the scraper broke" for schedule-shaped sources, so
    it's used selectively — only for sources that should essentially never
    be legitimately empty (team/season-stats endpoints, not daily schedules).
    """
    if count == 0:
        _alert("Source Empty", f"{label} returned 0 results — scraper may be broken or blocked.", "warn")

# ── HTTP helpers ──────────────────────────────────────────────────────────────
_session = requests.Session()
_session.headers.update(HEADERS)
_ref_session = requests.Session()
_ref_session.headers.update(REF_HEADERS)

def fetch_json(url: str, timeout: int = 15, retries: int = 2, params: dict | None = None, quiet_404: bool = False) -> dict | list | None:
    """quiet_404: for requests where "not found" is an ordinary answer (e.g. a newly promoted club has no prior top-flight season): one INFO line, no retries, no WARN."""
    for attempt in range(retries + 1):
        try:
            r = _session.get(url, timeout=timeout, params=params)
            if quiet_404 and r.status_code == 404:
                log(f"not found (expected): {url}")
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries:
                log(f"FAILED {url}: {e}", "WARN"); return None
            time.sleep(2 ** attempt)

def fetch_html(url: str, timeout: int = 25, ref: bool = False) -> BeautifulSoup | None:
    sess = _ref_session if ref else _session
    try:
        r = sess.get(url, timeout=timeout)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        log(f"FAILED HTML {url}: {e}", "WARN"); return None

def fetch_csv_rows(url: str, timeout: int = 20) -> list[dict]:
    try:
        r = _session.get(url, timeout=timeout)
        r.raise_for_status()
        return list(csv.DictReader(io.StringIO(r.text)))
    except Exception as e:
        log(f"FAILED CSV {url}: {e}", "WARN"); return []

def _table_to_rows(soup: BeautifulSoup, table_id: str, limit: int = 60) -> list[dict]:
    """Extract a BeautifulSoup <table> (including commented-out SR tables) into row dicts."""
    table = soup.find("table", id=table_id)
    if not table:
        # Sports-Reference embeds tables in HTML comments
        for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
            if table_id in comment:
                fragment = BeautifulSoup(comment, "lxml")
                table = fragment.find("table", id=table_id)
                if table:
                    break
    if not table:
        return []
    thead = table.find("thead")
    cols  = [th.get("data-stat", th.get_text(strip=True)) for th in thead.find_all("th")] if thead else []
    rows  = []
    for tr in (table.find("tbody") or table).find_all("tr")[:limit]:
        if "thead" in (tr.get("class") or []):
            continue
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        row = {}
        for i, td in enumerate(cells):
            key = cols[i] if i < len(cols) else f"c{i}"
            row[key] = td.get_text(strip=True)
        if any(v for v in row.values()):
            rows.append(row)
    return rows

# ── ESPN generic helpers ──────────────────────────────────────────────────────
def _espn_odds(comp: dict, sport: str = "", league: str = "", event_id: str = "") -> dict:
    """Extract odds from ESPN competition dict.  Falls back to ESPN Core odds API when
    the scoreboard response has no odds (sport/league/event_id required for fallback)."""
    odds = (comp.get("odds") or [{}])[0]
    result = {
        "homeML":   odds.get("homeTeamOdds", {}).get("moneyLine"),
        "awayML":   odds.get("awayTeamOdds", {}).get("moneyLine"),
        "ou":       odds.get("overUnder"),
        "spread":   odds.get("spread"),
        "provider": (odds.get("provider") or {}).get("name", ""),
    }
    # If no odds came from scoreboard and we have sport/league/event info, try Core API fallback
    if (result["homeML"] is None and result["awayML"] is None
            and sport and league and event_id):
        try:
            url = (f"https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}"
                   f"/events/{event_id}/competitions/{event_id}/odds")
            fallback = fetch_json(url, timeout=10)
            items = (fallback or {}).get("items", [])
            if items:
                ref = items[0].get("$ref", "")
                if ref:
                    o = fetch_json(ref, timeout=10) or {}
                    home_o = o.get("homeTeamOdds", {})
                    away_o = o.get("awayTeamOdds", {})
                    if home_o.get("moneyLine") or away_o.get("moneyLine"):
                        result.update({
                            "homeML":   home_o.get("moneyLine"),
                            "awayML":   away_o.get("moneyLine"),
                            "ou":       o.get("overUnder"),
                            "spread":   o.get("spread"),
                            "provider": (o.get("provider") or {}).get("name", "ESPN-Core"),
                        })
                        vlog(f"  ESPN odds fallback used for event {event_id}")
        except Exception as exc:
            vlog(f"  ESPN odds fallback failed {event_id}: {exc}")
    return result

_ESPN_SPORT_LEAGUE: dict[str, tuple[str, str]] = {
    "MLB": ("baseball", "mlb"),
    "NBA": ("basketball", "nba"),
    "NHL": ("hockey", "nhl"),
}

def _espn_game(event: dict, sport: str) -> dict:
    comp  = (event.get("competitions") or [{}])[0]
    comps = comp.get("competitors") or []
    home  = next((c for c in comps if c.get("homeAway") == "home"), {})
    away  = next((c for c in comps if c.get("homeAway") == "away"), {})
    status = event.get("status") or {}
    state  = (status.get("type") or {}).get("state", "pre")
    event_id = event.get("id", "")
    espn_sport, espn_league = _ESPN_SPORT_LEAGUE.get(sport, ("", ""))
    g: dict = {
        "id":          event_id,
        "sport":       sport,
        "home":        (home.get("team") or {}).get("abbreviation", ""),
        "away":        (away.get("team") or {}).get("abbreviation", ""),
        "homeScore":   home.get("score") if state != "pre" else None,
        "awayScore":   away.get("score") if state != "pre" else None,
        "state":       state,
        "period":      status.get("period", 0),
        "displayClock": status.get("displayClock", ""),
        "venue":       (comp.get("venue") or {}).get("fullName", ""),
        "date":        event.get("date", ""),
        "network":     ((comp.get("broadcasts") or [{}])[0].get("names") or [""])[0],
        # ESPN season type of this event: 1=preseason, 2=regular, 3=postseason,
        # 5=play-in (None if ESPN omits it). Lets consumers tell NBA preseason
        # games (Oct 2026: ~Sep 30-Oct 20) from real regular-season games --
        # the rollover happens by itself, no code change on Oct 21.
        "seasonType":  (event.get("season") or {}).get("type"),
        "seasonYear":  (event.get("season") or {}).get("year"),
    }
    g.update(_espn_odds(comp, sport=espn_sport, league=espn_league, event_id=event_id))
    for note_obj in comp.get("notes") or []:
        h = note_obj.get("headline", "")
        if "Game" in h or "Series" in h:
            g["seriesNote"] = h; break
    return g

def fetch_espn_injuries(sport_path: str, sport_key: str) -> list[dict]:
    """Fetch ESPN injury report for a sport (e.g. 'baseball/mlb').

    Parsing lives in _espn_injuries.parse_injuries: ESPN's payload carries the team abbreviation on each
    athlete (athlete.team.abbreviation), NOT on the team entry any more -- reading the entry field
    (the old code) made `team` "" on every row of every sport.  Rows also carry the ESPN athlete `id`
    (additive) so consumers can match by identity instead of by name."""
    log(f"ESPN injuries {sport_key}…")
    url  = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/injuries"
    data = fetch_json(url)
    items = _espn_injuries.parse_injuries(data, sport_key, abbr_fix=_espn_injuries.NHL_ABBR_FIX if sport_key == "nhl" else None)
    vlog(f"  {sport_key} injuries: {len(items)}")
    return items

def fetch_espn_transactions(sport_path: str, sport_key: str, limit: int = 25) -> list[dict]:
    """Fetch ESPN transaction log for a league (e.g. 'baseball/mlb') — trades,
    signings, IL moves, call-ups. Single-page (ESPN paginates at 25/page by
    default); recent-transactions volume is what matters for freshness, not
    full history."""
    log(f"ESPN transactions {sport_key}…")
    url  = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/transactions"
    data = fetch_json(url)
    items: list[dict] = []
    for t in (data or {}).get("transactions") or []:
        team = t.get("team") or {}
        items.append({
            "date":        (t.get("date") or "")[:10],
            "description": t.get("description", ""),
            "team":        team.get("abbreviation", ""),
            "teamName":    team.get("displayName", ""),
            "sport":       sport_key,
        })
    vlog(f"  {sport_key} transactions: {len(items)}")
    return items[:limit]

def fetch_nba_roster() -> dict:
    """
    Real, current NBA roster for all 30 teams -- replaces the app's
    hand-embedded NBA_PLAYERS table (which only ever covered the
    handful of players relevant to whatever series/Finals matchup was
    current when it was last hand-edited) with a scraped source that
    reflects real offseason trades/signings automatically instead of
    needing another manual edit every time a player changes teams.

    One ESPN team-list call, then one roster call per team. Returns
    {"player name": {"team": "ABBR", "pos": "PG"}, ...}, keyed
    lowercase to match this codebase's existing name-lookup convention.
    """
    log("NBA rosters (ESPN)…")
    result: dict = {}
    try:
        teams_data = fetch_json("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams?limit=40")
        teams = ((teams_data or {}).get("sports") or [{}])[0].get("leagues", [{}])[0].get("teams", [])
        for t in teams:
            tm = t.get("team", {})
            team_id, abbr = tm.get("id"), tm.get("abbreviation", "")
            if not team_id or not abbr:
                continue
            try:
                time.sleep(0.2)
                roster = fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster")
                for p in (roster or {}).get("athletes", []):
                    pos = (p.get("position") or {}).get("abbreviation", "")
                    name = p.get("fullName", "")
                    if name:
                        result[name.lower()] = {"team": abbr, "pos": pos}
            except Exception as exc:
                log(f"NBA roster {abbr}: {exc}", "WARN")
        log(f"  NBA rosters: {len(result)} players across {len(teams)} teams")
    except Exception as exc:
        log(f"NBA rosters: {exc}", "WARN")
    return result

def fetch_nhl_roster() -> dict:
    """
    Real, current NHL roster for all 32 teams -- this codebase had NO
    NHL player-roster source at all before this (only the hand-curated
    4-team NHL object's implicit "goalie/skater" mentions). Same
    ESPN team-list -> per-team-roster pattern as fetch_nba_roster().
    Returns {"player name": {"team": "ABBR", "pos": "C", "id": "<ESPN athlete id>"}, ...},
    keyed lowercase.

    Bug fixed 2026-10-03: ESPN's NHL roster response groups `athletes` by position
    ([{"position": "Centers", "items": [...]}, ...]) instead of the flat list NBA returns, so the old
    flat loop raised `'str' object has no attribute 'get'` for 32 of 32 teams and nhl.roster was
    always {}.  Parsing now goes through _espn_injuries.parse_roster, which accepts both shapes.
    """
    log("NHL rosters (ESPN)…")
    result: dict = {}
    try:
        teams_data = fetch_json("https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/teams?limit=40")
        teams = ((teams_data or {}).get("sports") or [{}])[0].get("leagues", [{}])[0].get("teams", [])
        for t in teams:
            tm = t.get("team", {})
            team_id, abbr = tm.get("id"), tm.get("abbreviation", "")
            if not team_id or not abbr:
                continue
            try:
                time.sleep(0.2)
                roster = fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/teams/{team_id}/roster")
                result.update(_espn_injuries.parse_roster(roster, abbr, _espn_injuries.NHL_ABBR_FIX))
            except Exception as exc:
                log(f"NHL roster {abbr}: {exc}", "WARN")
        log(f"  NHL rosters: {len(result)} players across {len(teams)} teams")
    except Exception as exc:
        log(f"NHL rosters: {exc}", "WARN")
    return result

def fetch_wnba_roster() -> dict:
    """
    Real, current WNBA roster for all teams -- same pattern, closes the
    same gap fetch_nba_roster() closes for NBA (no scraped player-team
    source existed before this).
    """
    if WNBA_OFFSEASON:
        return {}
    log("WNBA rosters (ESPN)…")
    result: dict = {}
    try:
        teams_data = fetch_json("https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams?limit=20")
        teams = ((teams_data or {}).get("sports") or [{}])[0].get("leagues", [{}])[0].get("teams", [])
        for t in teams:
            tm = t.get("team", {})
            team_id, abbr = tm.get("id"), tm.get("abbreviation", "")
            if not team_id or not abbr:
                continue
            try:
                time.sleep(0.2)
                roster = fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/roster")
                for p in (roster or {}).get("athletes", []):
                    pos = (p.get("position") or {}).get("abbreviation", "")
                    name = p.get("fullName", "")
                    if name:
                        result[name.lower()] = {"team": abbr, "pos": pos}
            except Exception as exc:
                log(f"WNBA roster {abbr}: {exc}", "WARN")
        log(f"  WNBA rosters: {len(result)} players across {len(teams)} teams")
    except Exception as exc:
        log(f"WNBA rosters: {exc}", "WARN")
    return result

# ══════════════════════════════════════════════════════════════════════════════
# NBA season rollover — season derivation, Basketball-Reference team table,
# team ratings / Elo seed.   (added 2026-10-03 for the 2026-27 rollover)
#
# Background (verified live 2026-10-03):
#   * fetch_nba_team_advanced()/fetch_nba_four_factors() returned 0 teams on
#     EVERY run with no WARN. Root cause: Basketball-Reference renamed its
#     tables. The old code looked up `<td data-stat="team_id|team_name">`
#     (abbreviation text) in `advanced-team` and a `four_factors` table. The
#     live `advanced-team` table now carries `data-stat="team"` (full name,
#     "*" suffix for playoff teams, abbreviation only inside the <a href>) and
#     the four-factor columns (efg_pct/tov_pct/orb_pct/ft_rate/opp_*) live in
#     that SAME table -- there is no `four_factors` table any more. The
#     playoffs-page `misc_stats` table was renamed `advanced-team` as well.
#     Every row therefore hit `if not tm: continue` and the function logged
#     "0 teams" at INFO level only.
#   * BBRef team abbreviations (BRK/CHO/PHO/WAS/NOP/UTA...) differ from the
#     ESPN abbreviations the whole app keys on (BKN/CHA/PHX/WSH/NO/UTAH, plus
#     NY/GS/SA), and the old 4-entry ABBR_MAP did not cover them.
# ══════════════════════════════════════════════════════════════════════════════

# Min games played per team before the CURRENT season's Basketball-Reference
# page replaces last season's as the model prior (see select_nba_team_stats).
NBA_MIN_GAMES_FOR_CURRENT = 5
NBA_MIN_TEAMS_FOR_CURRENT = 24      # of 30 -- tolerate a few teams with a light early schedule

# Elo / rating seed parameters (see build_nba_team_ratings)
NBA_ELO_MEAN        = 1550   # == the app's own `NBA_ELO[x] || 1550` default
NBA_ELO_PER_POINT   = 28     # Elo points per point of scoring margin (538-style)
NBA_PRIOR_CARRY     = 0.75   # share of last season's margin carried into a new season
NBA_PRIOR_GAMES     = 20     # pseudo-games of weight the regressed prior gets vs current results
NBA_BBREF_DELAY     = 2      # seconds between Basketball-Reference requests (be polite); tests set 0
# Where team ratings come from: "espn" (default: ESPN primary, Basketball-Reference only fills teams ESPN lacks),
# "espn-only" (never touch Basketball-Reference), "bbref" (the pre-2026-10-03 behaviour: Basketball-Reference only).
NBA_TEAM_STATS_SOURCE = os.environ.get("NBA_TEAM_STATS_SOURCE", "espn").strip().lower() or "espn"

# Basketball-Reference full team name -> ESPN abbreviation (what data.json / app.html key on)
_NBA_NAME_TO_ESPN: dict[str, str] = {
    "Atlanta Hawks":"ATL","Boston Celtics":"BOS","Brooklyn Nets":"BKN","Charlotte Hornets":"CHA",
    "Chicago Bulls":"CHI","Cleveland Cavaliers":"CLE","Dallas Mavericks":"DAL","Denver Nuggets":"DEN",
    "Detroit Pistons":"DET","Golden State Warriors":"GS","Houston Rockets":"HOU","Indiana Pacers":"IND",
    "Los Angeles Clippers":"LAC","LA Clippers":"LAC","Los Angeles Lakers":"LAL","Memphis Grizzlies":"MEM",
    "Miami Heat":"MIA","Milwaukee Bucks":"MIL","Minnesota Timberwolves":"MIN","New Orleans Pelicans":"NO",
    "New York Knicks":"NY","Oklahoma City Thunder":"OKC","Orlando Magic":"ORL","Philadelphia 76ers":"PHI",
    "Phoenix Suns":"PHX","Portland Trail Blazers":"POR","Sacramento Kings":"SAC","San Antonio Spurs":"SA",
    "Toronto Raptors":"TOR","Utah Jazz":"UTAH","Washington Wizards":"WSH",
}
# Basketball-Reference team abbreviation (from /teams/XXX/2026.html hrefs) -> ESPN abbreviation
_NBA_BBREF_TO_ESPN: dict[str, str] = {
    "ATL":"ATL","BOS":"BOS","BRK":"BKN","BKN":"BKN","CHO":"CHA","CHA":"CHA","CHI":"CHI","CLE":"CLE",
    "DAL":"DAL","DEN":"DEN","DET":"DET","GSW":"GS","GS":"GS","HOU":"HOU","IND":"IND","LAC":"LAC",
    "LAL":"LAL","MEM":"MEM","MIA":"MIA","MIL":"MIL","MIN":"MIN","NOP":"NO","NO":"NO","NYK":"NY","NY":"NY",
    "OKC":"OKC","ORL":"ORL","PHI":"PHI","PHO":"PHX","PHX":"PHX","POR":"POR","SAC":"SAC","SAS":"SA","SA":"SA",
    "TOR":"TOR","UTA":"UTAH","UTAH":"UTAH","WAS":"WSH","WSH":"WSH",
}


def nba_season_end_year(today=None, override=None) -> int:
    """
    The NBA season's ending calendar year (2026-27 -> 2027). This is also the
    value ESPN uses for `season=` and Basketball-Reference for `NBA_<year>.html`.

    A season Y-1..Y starts in October: month >= 10 -> year+1, else year. So from
    Jul-Sep (offseason) this still returns the season that just ended, and flips
    on Oct 1 (preseason). Override with env var NBA_SEASON_END_YEAR (or the
    `override` argument) -- e.g. to pin the old season, or to flip early/late.
    """
    ov = override if override is not None else os.environ.get("NBA_SEASON_END_YEAR")
    if ov not in (None, ""):
        try:
            y = int(str(ov).strip())
            if 2000 <= y <= 2100:
                return y
        except ValueError:
            pass
        log(f"NBA_SEASON_END_YEAR={ov!r} is not a valid 4-digit year -- ignoring override", "WARN")
    d = today if today is not None else NOW_MT
    if isinstance(d, datetime):
        d = d.date()
    return d.year + 1 if d.month >= 10 else d.year


def nba_bbref_url(end_year: int) -> str:
    return f"https://www.basketball-reference.com/leagues/NBA_{end_year}.html"


def _bb_float(s):
    """Parse a Basketball-Reference cell ('+11.2', '.599', '1,234', '') -> float | None."""
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if s in ("", "-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _bbref_looks_blocked(text: str) -> bool:
    low = text[:6000].lower()
    return ("just a moment" in low or "cf-chl" in low or "challenge-platform" in low
            or "attention required" in low or "verify you are human" in low)


def _bbref_get(url: str, timeout: int = 25) -> tuple[str | None, str]:
    """GET a Basketball-Reference page. Returns (html | None, reason). Never raises."""
    try:
        r = _ref_session.get(url, timeout=timeout)
    except Exception as exc:
        return None, f"request failed: {exc}"
    status = getattr(r, "status_code", 0)
    if status != 200:
        cf = "cloudflare" in str((getattr(r, "headers", {}) or {}).get("server", "")).lower()
        return None, f"HTTP {status}" + (" (Cloudflare block/rate-limit)" if cf and status in (403, 429, 503) else "")
    text = r.text or ""
    if _bbref_looks_blocked(text):
        return None, "HTTP 200 but anti-bot challenge page"
    return text, "ok"


def _bb_find_table(soup, table_ids):
    """Find a table by id, including tables Sports-Reference wraps in HTML comments."""
    for tid in table_ids:
        t = soup.find("table", id=tid)
        if t is not None:
            return t, tid
    for cmt in soup.find_all(string=lambda t: isinstance(t, Comment)):
        for tid in table_ids:
            if tid in cmt:
                tbl = BeautifulSoup(cmt, "lxml").find("table", id=tid)
                if tbl is not None:
                    return tbl, tid
    return None, None


def parse_nba_advanced_team(soup) -> tuple[dict, str]:
    """
    Parse Basketball-Reference's league-page `advanced-team` table (also the
    playoffs page's) into {espn_abbr: row}. Returns (rows, diagnostic) --
    diagnostic is "" on success, otherwise the specific reason nothing parsed.

    Row keys: name, w, l, gp, mov, sos, srs, ortg, drtg, net_rtg, pace, ts_pct,
    efg_pct, tov_pct, orb_pct, ft_rate, opp_efg_pct, opp_tov_pct, drb_pct,
    opp_ft_rate. Values are floats or None (a new season's page has the table
    with empty stat cells until games are played -- those rows still parse,
    with w=l=0 and ortg=None).
    """
    tbl, tid = _bb_find_table(soup, ("advanced-team", "advanced_team"))
    if tbl is None:
        ids = [t.get("id") for t in soup.find_all("table") if t.get("id")]
        title = (soup.title.get_text(strip=True) if soup.title else "no <title>")[:80]
        return {}, f"no advanced-team table (page title {title!r}; table ids present: {ids[:12]})"
    body = tbl.find("tbody") or tbl
    out: dict = {}
    unmapped: list[str] = []
    n_rows = 0
    for tr in body.find_all("tr"):
        if "thead" in (tr.get("class") or []):
            continue
        cells = {}
        for td in tr.find_all(["td", "th"]):
            ds = td.get("data-stat")
            if ds and ds not in cells:
                cells[ds] = td
        tcell = cells.get("team") or cells.get("team_name") or cells.get("team_id")
        if tcell is None:
            continue
        name = re.sub(r"[*\s]+$", "", tcell.get_text(" ", strip=True)).strip()
        if not name or name.lower() in ("team", "league average"):
            continue
        n_rows += 1
        abbr = None
        a = tcell.find("a", href=True)
        if a:
            m = re.search(r"/teams/([A-Z]{2,4})/", a["href"])
            if m:
                abbr = _NBA_BBREF_TO_ESPN.get(m.group(1))
        if not abbr:
            abbr = _NBA_NAME_TO_ESPN.get(name) or _NBA_BBREF_TO_ESPN.get(name.upper())
        if not abbr:
            unmapped.append(name)
            continue
        g = lambda k: _bb_float(cells[k].get_text(strip=True)) if k in cells else None
        w, l = g("wins"), g("losses")
        ortg, drtg = g("off_rtg"), g("def_rtg")
        net = g("net_rtg")
        if net is None and ortg is not None and drtg is not None:
            net = round(ortg - drtg, 2)
        out[abbr] = {
            "name": name,
            "w": int(w) if w is not None else 0, "l": int(l) if l is not None else 0,
            "mov": g("mov"), "sos": g("sos"), "srs": g("srs"),
            "ortg": ortg, "drtg": drtg, "net_rtg": net, "pace": g("pace"),
            "ts_pct": g("ts_pct"), "efg_pct": g("efg_pct"), "tov_pct": g("tov_pct"),
            "orb_pct": g("orb_pct"), "ft_rate": g("ft_rate"),
            "opp_efg_pct": g("opp_efg_pct"), "opp_tov_pct": g("opp_tov_pct"),
            "drb_pct": g("drb_pct"), "opp_ft_rate": g("opp_ft_rate"),
        }
        out[abbr]["gp"] = out[abbr]["w"] + out[abbr]["l"]
    if unmapped:
        log(f"BBRef advanced-team: {len(unmapped)} team name(s) not in ESPN map: {unmapped[:5]}", "WARN")
    if not out:
        return {}, (f"table {tid!r} found but {n_rows} row(s) yielded no mappable team "
                    f"(unmapped: {unmapped[:5]}) -- column names likely changed")
    return out, ""


_NBA_STATS_CACHE: dict = {}      # (end_year) -> (rows, info);  (sel, cur, prior) -> selection


def fetch_nba_bbref_season(end_year: int) -> tuple[dict, dict]:
    """
    Fetch + parse one season's Basketball-Reference league page, once per run
    (cached -- teamAdv, fourFactors and teamRatings all share it). Returns
    (rows_by_espn_abbr, info). Always logs a WARN with the reason on failure.
    """
    key = ("page", end_year)
    if key in _NBA_STATS_CACHE:
        return _NBA_STATS_CACHE[key]
    url = nba_bbref_url(end_year)
    if NBA_BBREF_DELAY:
        time.sleep(NBA_BBREF_DELAY)
    html, reason = _bbref_get(url)
    info = {"year": end_year, "url": url, "ok": False, "reason": reason, "teams": 0}
    rows: dict = {}
    if html is None:
        log(f"NBA BBRef {end_year}: FETCH FAILED -- {reason} ({url})", "WARN")
    else:
        rows, diag = parse_nba_advanced_team(BeautifulSoup(html, "lxml"))
        if not rows:
            info["reason"] = diag
            log(f"NBA BBRef {end_year}: 0 teams parsed -- {diag} ({url})", "WARN")
        else:
            info.update(ok=True, reason="ok", teams=len(rows))
            if len(rows) != 30:
                log(f"NBA BBRef {end_year}: parsed {len(rows)} teams (expected 30)", "WARN")
    _NBA_STATS_CACHE[key] = (rows, info)
    return rows, info


def select_nba_team_stats(cur_rows: dict, prior_rows: dict, cur_year: int, prior_year: int,
                          min_games: int | None = None, min_teams: int | None = None) -> dict:
    """
    Decide which season's team stats the model should read. Pure function.

    Current season is used only once at least `min_teams` teams (default 24 of
    30) have >= `min_games` (default 5) games played AND carry real ratings;
    before that the whole league stays on last season's page as the prior. Once
    the current season is active, a team still under `min_games` keeps its own
    prior row. Returns {"teams": {abbr: row + season}, "seasonUsed", "mode"
    ("current"|"prior"|"mixed"|"none"), "curReady", "reason"}.
    """
    min_games = NBA_MIN_GAMES_FOR_CURRENT if min_games is None else min_games
    min_teams = NBA_MIN_TEAMS_FOR_CURRENT if min_teams is None else min_teams
    ready = {a: r for a, r in (cur_rows or {}).items()
             if (r.get("gp") or 0) >= min_games and r.get("ortg") is not None and r.get("drtg") is not None}
    teams: dict = {}
    if len(ready) >= min_teams:
        mode = "current"
        for a, r in cur_rows.items():
            if a in ready:
                teams[a] = {**r, "season": cur_year}
            elif prior_rows.get(a):
                teams[a] = {**prior_rows[a], "season": prior_year}
                mode = "mixed"
        for a, r in prior_rows.items():       # team missing from current page entirely
            if a not in teams:
                teams[a] = {**r, "season": prior_year}
                mode = "mixed"
        return {"teams": teams, "seasonUsed": cur_year if mode == "current" else f"{cur_year}/{prior_year}",
                "mode": mode, "curReady": len(ready),
                "reason": f"{len(ready)}/{len(cur_rows)} current-season teams have >= {min_games} GP"}
    why = (f"current-season page has only {len(ready)} team(s) with >= {min_games} GP "
           f"(need {min_teams}); using {prior_year} as prior")
    if prior_rows:
        teams = {a: {**r, "season": prior_year} for a, r in prior_rows.items()}
        return {"teams": teams, "seasonUsed": prior_year, "mode": "prior",
                "curReady": len(ready), "reason": why}
    if ready:   # prior page failed but a thin current sample exists -- better than nothing
        teams = {a: {**r, "season": cur_year} for a, r in ready.items()}
        return {"teams": teams, "seasonUsed": cur_year, "mode": "current",
                "curReady": len(ready), "reason": why + f"; prior {prior_year} UNAVAILABLE, using thin current sample"}
    return {"teams": {}, "seasonUsed": None, "mode": "none", "curReady": 0,
            "reason": why + f"; prior {prior_year} UNAVAILABLE -- no team stats at all"}


def _nba_http_get(url, params=None, timeout=20):
    """The one HTTP call every ESPN NBA fetch added 2026-10-03 goes through (tests patch this)."""
    return _session.get(url, params=params, timeout=timeout)


_NBA_CTX: "_nba_espn.Ctx | None" = None


def _nba_ctx(reset: bool = False) -> "_nba_espn.Ctx":
    """Per-run ESPN NBA context: retry/backoff (3 tries, 1s then 2s between), per-endpoint timing/health, logging via log()."""
    global _NBA_CTX
    if _NBA_CTX is None or reset:
        _NBA_CTX = _nba_espn.Ctx(lambda url, params=None, timeout=20: _nba_http_get(url, params=params, timeout=timeout),
                                 log=lambda m, lvl="INFO": log(m, lvl), sleep=lambda s: time.sleep(s))
    return _NBA_CTX


def _bbref_team_stats_selection() -> dict:
    """Basketball-Reference-only selection (the original behaviour; NBA_TEAM_STATS_SOURCE=bbref)."""
    cur = nba_season_end_year()
    prior = cur - 1
    cur_rows, cur_info = fetch_nba_bbref_season(cur)
    # The prior page is only needed until the current season has enough games, but
    # teamRatings wants last season's final numbers either way, so always fetch it.
    prior_rows, prior_info = fetch_nba_bbref_season(prior)
    sel = select_nba_team_stats(cur_rows, prior_rows, cur, prior)
    sel.update(curYear=cur, priorYear=prior, curRows=cur_rows, priorRows=prior_rows,
               curInfo=cur_info, priorInfo=prior_info)
    sel["origin"] = {"prior": {"season": prior, "espn": 0, "bbref": len(prior_rows)},
                     "current": {"season": cur, "espn": 0, "bbref": len(cur_rows)}}
    sel["bbref"] = "primary" if (prior_info["ok"] or cur_info["ok"]) else "blocked"
    sel["bbrefReason"] = "; ".join(f"{i['year']}: {i['reason']}" for i in (cur_info, prior_info) if not i["ok"])
    return sel


def _nba_fill_from_bbref(year: int, rows: dict, usable, label: str) -> tuple[dict, dict]:
    """Fill teams `usable(row)` rejects (or that are absent) from the Basketball-Reference page of `year`.
    Returns (rows, {"fetched": bool, "ok": bool, "filled": [abbr...], "reason": str}). Never replaces a usable ESPN row."""
    all30 = set(_NBA_NAME_TO_ESPN.values())
    need = sorted(a for a in all30 if not usable(rows.get(a)))
    info = {"fetched": False, "ok": False, "filled": [], "reason": "not needed"}
    if not need:
        return rows, info
    info["fetched"] = True
    bb_rows, bb_info = fetch_nba_bbref_season(year)
    info.update(ok=bb_info["ok"], reason=bb_info["reason"])
    if not bb_rows:
        log(f"NBA team stats {year}: ESPN missing {len(need)} team(s) {need[:6]} and Basketball-Reference fallback "
            f"unavailable ({bb_info['reason']})", "WARN")
        return rows, info
    rows = dict(rows)
    for a in need:
        if usable(bb_rows.get(a)):
            rows[a] = bb_rows[a]
            info["filled"].append(a)
    if info["filled"]:
        log(f"NBA team stats {year} ({label}): {len(info['filled'])} team(s) filled from Basketball-Reference because ESPN "
            f"lacked them: {info['filled'][:8]}", "WARN")
    return rows, info


def get_nba_team_stats_selection(standings_cur: dict | None = None, standings_prior: dict | None = None,
                                 allow_bbref: bool = True) -> dict:
    """
    Pick which season's team stats the model reads.  ESPN is PRIMARY (one `statistics/byteam` request per season ->
    ortg/drtg/pace/four factors for all 30 teams, see _nba_espn); Basketball-Reference is only consulted to fill a team ESPN could
    not supply (and never when NBA_TEAM_STATS_SOURCE=espn-only).  The pure selection rule is unchanged (select_nba_team_stats: last
    season is the prior until >= 24 teams have >= 5 games).  Cached per run.  The optional standings fill each row's w/l.

    Returns select_nba_team_stats' dict plus curYear/priorYear/curRows/priorRows/curInfo/priorInfo and
      origin  {"prior"|"current": {"season", "espn": n_teams, "bbref": n_teams_filled}}
      bbref   "skipped" (ESPN complete) | "ok" | "blocked" | "disabled" | "primary"     (+ bbrefReason)
    """
    cur = nba_season_end_year()
    prior = cur - 1
    key = ("sel", cur)
    if key in _NBA_STATS_CACHE:
        return _NBA_STATS_CACHE[key]
    if NBA_TEAM_STATS_SOURCE == "bbref":
        sel = _bbref_team_stats_selection()
        log(f"NBA team stats: using season {sel['seasonUsed']} [{sel['mode']}] (Basketball-Reference only) -- {sel['reason']}",
            "INFO" if sel["teams"] else "WARN")
        _NBA_STATS_CACHE[key] = sel
        return sel
    ctx = _nba_ctx()
    cur_rows, cur_espn = _nba_espn.fetch_team_rows(ctx, cur)
    prior_rows, prior_espn = _nba_espn.fetch_team_rows(ctx, prior)
    _nba_espn.attach_records(cur_rows, standings_cur)
    _nba_espn.attach_records(prior_rows, standings_prior)
    n_espn = {"prior": len(prior_rows), "current": len(cur_rows)}
    bb_state, bb_note = "skipped", ""
    if NBA_TEAM_STATS_SOURCE == "espn-only" or not allow_bbref:
        bb_state = "disabled"
    else:
        usable_prior = lambda r: bool(r) and r.get("ortg") is not None and r.get("drtg") is not None
        prior_rows, bb_p = _nba_fill_from_bbref(prior, prior_rows, usable_prior, "prior")
        # Before game 1 ESPN legitimately has no current-season table ("not_found") and neither does BBRef -- only ask BBRef
        # about the current season when ESPN really failed, or came back with a partial table.
        if cur_espn["hardFail"] or (cur_rows and len(cur_rows) < 30):
            cur_rows, bb_c = _nba_fill_from_bbref(cur, cur_rows, lambda r: bool(r), "current")
        else:
            bb_c = {"fetched": False, "ok": False, "filled": [], "reason": "not needed"}
        fetched = [b for b in (bb_p, bb_c) if b["fetched"]]
        if fetched:
            bb_state = "ok" if any(b["filled"] for b in fetched) else "blocked"
            bb_note = "; ".join(b["reason"] for b in fetched if not b["filled"])
    sel = select_nba_team_stats(cur_rows, prior_rows, cur, prior)
    sel.update(curYear=cur, priorYear=prior, curRows=cur_rows, priorRows=prior_rows, curInfo=cur_espn, priorInfo=prior_espn,
               origin={"prior": {"season": prior, "espn": n_espn["prior"], "bbref": len(prior_rows) - n_espn["prior"]},
                       "current": {"season": cur, "espn": n_espn["current"], "bbref": len(cur_rows) - n_espn["current"]}},
               bbref=bb_state, bbrefReason=bb_note)
    lvl = "INFO" if sel["teams"] else "WARN"
    log(f"NBA team stats: using season {sel['seasonUsed']} [{sel['mode']}] from ESPN "
        f"(prior {n_espn['prior']}/30, current {n_espn['current']}/30; BBRef {bb_state}) -- {sel['reason']}", lvl)
    _NBA_STATS_CACHE[key] = sel
    return sel


def fetch_nba_team_advanced() -> dict:
    """
    Team-level NBA advanced stats from Basketball Reference, keyed by ESPN
    abbreviation: ortg, drtg, pace, efg_pct, ts_pct, net_rtg (+ additive
    `season` and `gp` so the model/UI can tell which season a team's row is
    from). Which season is chosen by get_nba_team_stats_selection(): the
    current season once >= 24 teams have >= 5 GP, else last season as the prior.
    Used by calculate_best_bets and (via data.json nba.teamAdv) by the app's nbaMC.
    """
    log("NBA team advanced stats…")
    sel = get_nba_team_stats_selection()
    result: dict = {}
    for abbr, r in sel["teams"].items():
        if r.get("ortg") is None or r.get("drtg") is None:
            continue
        result[abbr] = {
            "ortg": r["ortg"], "drtg": r["drtg"], "pace": r.get("pace") or 0.0,
            "efg_pct": r.get("efg_pct") or 0.0, "ts_pct": r.get("ts_pct") or 0.0,
            "net_rtg": r["net_rtg"] if r.get("net_rtg") is not None else r["ortg"] - r["drtg"],
            "season": r["season"], "gp": r.get("gp", 0),
        }
    if len(result) < 30:
        log(f"NBA team advanced: only {len(result)}/30 teams (season {sel['seasonUsed']}, mode {sel['mode']})", "WARN")
    else:
        log(f"NBA team advanced: {len(result)} teams (season {sel['seasonUsed']}, mode {sel['mode']})")
    return result


def fetch_nba_four_factors() -> dict:
    """
    Dean Oliver's Four Factors (eFG%, TOV%, ORB%, FT/FGA, offense + defense),
    keyed by ESPN abbreviation. These columns live in the same Basketball-
    Reference `advanced-team` table as the ratings (there is no separate
    `four_factors` table any more) -- so this reuses the cached page fetch.
    Same season-selection rule as fetch_nba_team_advanced().
    """
    log("NBA four factors…")
    sel = get_nba_team_stats_selection()
    result: dict = {}
    keys = ("efg_pct", "tov_pct", "orb_pct", "ft_rate", "opp_efg_pct", "opp_tov_pct", "drb_pct", "opp_ft_rate")
    for abbr, r in sel["teams"].items():
        if any(r.get(k) is None for k in keys):
            continue
        result[abbr] = {k: r[k] for k in keys}
        result[abbr].update(season=r["season"], gp=r.get("gp", 0))
    if len(result) < 30:
        log(f"NBA four factors: only {len(result)}/30 teams (season {sel['seasonUsed']}, mode {sel['mode']})", "WARN")
    else:
        log(f"NBA four factors: {len(result)} teams (season {sel['seasonUsed']}, mode {sel['mode']})")
    return result


def _nba_mov_prior(prior_row: dict | None, espn_prior: dict | None) -> float | None:
    """Last season's strength (points of margin): BBRef SRS > MOV > net rating > ESPN avg differential."""
    if prior_row:
        for k in ("srs", "mov", "net_rtg"):
            if prior_row.get(k) is not None:
                return float(prior_row[k])
    if espn_prior and espn_prior.get("diff") is not None:
        return float(espn_prior["diff"])
    return None


def build_nba_team_ratings(prior_rows: dict, cur_rows: dict, espn_prior: dict, espn_cur: dict,
                           cur_year: int, prior_year: int, stats_sel: dict | None = None) -> dict:
    """
    All-30-teams rating block for the app (data.json `nba.teamRatings`, plus a
    flat `nba.eloSeed`). Pure function of its inputs.

    strength (pts of margin) = (gp*cur_mov + K*carry*prior_mov) / (gp + K)
        prior_mov = last season's SRS (fallback MOV / net rtg / ESPN avg diff)
        cur_mov   = ESPN standings avg point differential this season (fallback BBRef MOV)
        carry = NBA_PRIOR_CARRY (0.75), K = NBA_PRIOR_GAMES (20)
    elo = NBA_ELO_MEAN + NBA_ELO_PER_POINT * strength, clamped 1300..1850.
    A team with no data from any source gets elo = NBA_ELO_MEAN (source "default").
    """
    abbrs = sorted(set(prior_rows) | set(cur_rows) | set(espn_prior) | set(espn_cur))
    teams: dict = {}
    elo_seed: dict = {}
    for a in abbrs:
        pr, cr = prior_rows.get(a), cur_rows.get(a)
        ep, ec = espn_prior.get(a), espn_cur.get(a)
        # current record/margin: ESPN standings are real-time; BBRef lags a day
        cw = _bb_float((ec or {}).get("w"))
        cl = _bb_float((ec or {}).get("l"))
        if cw is None and cr:
            cw, cl = float(cr["w"]), float(cr["l"])
        cw, cl = int(cw or 0), int(cl or 0)
        gp = cw + cl
        cur_mov = _bb_float((ec or {}).get("diff"))
        if cur_mov is None and cr and cr.get("mov") is not None:
            cur_mov = cr["mov"]
        prior_mov = _nba_mov_prior(pr, ep)
        pw = pl = None
        if pr:
            pw, pl = pr["w"], pr["l"]
        elif ep:
            pw, pl = int(_bb_float(ep.get("w")) or 0), int(_bb_float(ep.get("l")) or 0)
        regressed = NBA_PRIOR_CARRY * prior_mov if prior_mov is not None else None
        if gp > 0 and cur_mov is not None and regressed is not None:
            strength, src = (gp * cur_mov + NBA_PRIOR_GAMES * regressed) / (gp + NBA_PRIOR_GAMES), "prior+current"
        elif gp > 0 and cur_mov is not None:
            strength, src = cur_mov * gp / (gp + NBA_PRIOR_GAMES), "current-only"
        elif regressed is not None:
            strength, src = regressed, "prior"
        else:
            strength, src = None, "default"
        elo = NBA_ELO_MEAN if strength is None else int(round(max(1300, min(1850, NBA_ELO_MEAN + NBA_ELO_PER_POINT * strength))))
        prior_pct = (pw / (pw + pl)) if (pw is not None and pl is not None and (pw + pl) > 0) else None
        teams[a] = {
            "name": (pr or cr or {}).get("name"),
            "prior": {
                "season": prior_year, "w": pw, "l": pl,
                "winPct": round(prior_pct, 3) if prior_pct is not None else None,
                "mov": (pr or {}).get("mov") if pr else _bb_float((ep or {}).get("diff")),
                "srs": (pr or {}).get("srs"),
                "netRtg": (pr or {}).get("net_rtg"), "ortg": (pr or {}).get("ortg"),
                "drtg": (pr or {}).get("drtg"), "pace": (pr or {}).get("pace"),
            },
            "current": {"season": cur_year, "w": cw, "l": cl, "gp": gp, "mov": cur_mov,
                        "netRtg": (cr or {}).get("net_rtg")},
            # last season's win% pulled halfway-plus to .500 -- a sane Bayes prior for a new season
            "priorWinPct": round(0.5 + NBA_PRIOR_CARRY * (prior_pct - 0.5), 3) if prior_pct is not None else None,
            "strength": round(strength, 2) if strength is not None else None,
            "elo": elo, "source": src,
        }
        elo_seed[a] = elo
    block = {
        "seasonCurrent": cur_year, "seasonPrior": prior_year,
        "statsSeasonUsed": (stats_sel or {}).get("seasonUsed"),
        "statsMode": (stats_sel or {}).get("mode"),
        "params": {"eloMean": NBA_ELO_MEAN, "eloPerPoint": NBA_ELO_PER_POINT, "priorCarry": NBA_PRIOR_CARRY,
                   "priorGames": NBA_PRIOR_GAMES, "minGamesForCurrent": NBA_MIN_GAMES_FOR_CURRENT},
        "generated": TODAY_ISO,
        "teams": teams,
    }
    return {"teamRatings": block, "eloSeed": elo_seed}


def _nba_carry_forward(label: str, fresh, prev_nba: dict, key: str):
    """If a fresh fetch came back empty, keep the previous data.json value (loudly)
    instead of publishing an empty block -- write_data_json() overwrites the file
    wholesale each run, so one blocked BBRef request used to blank the model's
    team ratings until the next good run."""
    if fresh:
        return fresh
    old = (prev_nba or {}).get(key)
    if old:
        log(f"NBA {label}: fresh fetch EMPTY -- carrying forward previous data.json nba.{key} "
            f"({len(old)} entries)", "WARN")
        return old
    log(f"NBA {label}: fresh fetch EMPTY and no previous data.json value to carry forward", "WARN")
    return fresh


def _nba_roster_with_carry(fresh: dict, prev: dict | None) -> tuple[dict, bool]:
    """fetch_nba_roster() logs and skips a team whose roster call failed (and returns {} if the team list failed), which used to
    drop that team's players from data.json.  Teams with NO fresh player are filled from the previous data.json roster
    (loudly).  Returns (roster, carried_any)."""
    if not prev:
        return fresh, False
    present = {v.get("team") for v in fresh.values()}
    missing = sorted(set(_NBA_NAME_TO_ESPN.values()) - present)
    if not missing:
        return fresh, False
    merged = dict(fresh)
    n = 0
    for name, v in prev.items():
        if v.get("team") in missing and name not in merged:
            merged[name] = v
            n += 1
    if n:
        log(f"NBA roster: no fresh players for {len(missing)} team(s) {missing[:8]} -- carried {n} player(s) forward "
            f"from the previous data.json", "WARN")
    return merged, n > 0


def collect_nba_season_data(no_reference: bool = False, prev_nba: dict | None = None) -> dict:
    """
    Everything season-dependent the NBA model needs, in one place (called from
    main()). Returns {season, standings, players, roster, teamAdv, fourFactors,
    teamRatings, eloSeed, playerProps, sources, health}. All season numbers derive
    from nba_season_end_year().

    ESPN is the primary source for everything (team ratings via statistics/byteam, rosters, player averages, game logs,
    injuries); Basketball-Reference is only an optional fallback for teams ESPN could not supply.  Each step is isolated
    (`safe`) and carries the previous data.json value forward, loudly, when its fresh result is empty, so one failing endpoint
    cannot blank another field.  `sources` records where each field came from; `health` the request/retry/timing/stale summary.
    """
    t_run = time.monotonic()
    ctx = _nba_ctx(reset=True)
    cur = nba_season_end_year()
    prior = cur - 1
    log(f"NBA season in use: {prior}-{str(cur)[2:]} (season end year {cur}; "
        f"override with env NBA_SEASON_END_YEAR); team stats source: {NBA_TEAM_STATS_SOURCE}")
    if prev_nba is None:
        try:
            prev_nba = json.loads(FE_DATA.read_text()).get("nba") or {}
        except Exception:
            prev_nba = {}
    stale: list[str] = []          # sources whose value this run is a carried-forward copy (or empty)
    src: dict = {}                 # per-field provenance -> data.json nba.sources

    def safe(label, fn, default):
        """One failing step must not blank the other NBA fields: log loudly, return `default`."""
        try:
            return fn()
        except Exception as exc:
            log(f"NBA {label}: step crashed -- {type(exc).__name__}: {exc}", "WARN")
            return default

    # standings: a real 0-0 table is normal in the preseason; carried forward only when the fetch is empty AND last run was this season
    standings = safe("standings", lambda: fetch_nba_standings(cur), {})
    src["standings"] = "espn"
    if not standings:
        if prev_nba.get("standings") and prev_nba.get("season") == cur:
            log("NBA standings: fresh fetch EMPTY -- carrying forward previous data.json nba.standings (same season)", "WARN")
            standings, src["standings"] = prev_nba["standings"], "carried"
        else:
            src["standings"] = "none"
        stale.append("standings")
    standings_prior = safe("standings(prior)", lambda: fetch_nba_standings(prior), {})
    stats_season = nba_player_stats_season(standings, cur)
    players = safe("players", lambda: fetch_nba_player_stats(stats_season), [])
    src["players"] = "espn"
    if not players:
        players = _nba_carry_forward("players", players, prev_nba, "players")
        src["players"] = "carried" if players else "none"
        stale.append("players")
    roster_fresh = safe("roster", fetch_nba_roster, {})
    roster, roster_carried = _nba_roster_with_carry(roster_fresh, prev_nba.get("roster"))
    src["roster"] = "espn" if roster_fresh and not roster_carried else ("carried" if roster_carried and not roster_fresh else
                                                                          ("espn+carried" if roster_carried else "none"))
    if roster_carried or not roster:
        stale.append("roster")
    tagged = apply_nba_player_tiers(roster, players)
    if roster and stats_season == cur:
        # current-season tiers only exist for players who already have NBA_TIER_MIN_GP games: keep everyone else on LAST season's tier instead of dropping to the fallback weight
        prior_players = safe("players(prior)", lambda: fetch_nba_player_stats(cur - 1), [])
        tagged += apply_nba_player_tiers(roster, prior_players, only_missing=True)
    log(f"  NBA roster: {len(roster)} players, {tagged} tiered (PREMIUM/OPTIMAL/GOOD) for injury weighting")
    if roster and not tagged:
        log("NBA roster: 0 players tiered -- ESPN player-stats fetch produced nothing usable", "WARN")

    # team ratings: ESPN primary (byteam), Basketball-Reference only fills teams ESPN lacks.  --no-reference stops BBRef, not ESPN.
    adv: dict = {}
    four: dict = {}
    sel = None
    if not (no_reference and NBA_TEAM_STATS_SOURCE == "bbref"):
        sel = safe("team stats", lambda: get_nba_team_stats_selection(standings, standings_prior, allow_bbref=not no_reference), None)
    if sel is not None:
        adv = _nba_carry_forward("teamAdv", safe("teamAdv", fetch_nba_team_advanced, {}), prev_nba, "teamAdv")
        four = _nba_carry_forward("fourFactors", safe("fourFactors", fetch_nba_four_factors, {}), prev_nba, "fourFactors")
        if adv is (prev_nba or {}).get("teamAdv") and adv:
            stale.append("teamAdv")
        if four is (prev_nba or {}).get("fourFactors") and four:
            stale.append("fourFactors")
    ratings = build_nba_team_ratings(
        (sel or {}).get("priorRows", {}), (sel or {}).get("curRows", {}),
        standings_prior, standings, cur, prior, sel)
    real = sum(1 for t in ratings["teamRatings"]["teams"].values() if t["source"] != "default")
    ratings_carried = False
    if real < 28:
        log(f"NBA teamRatings: only {real}/30 teams have real data (rest default to Elo {NBA_ELO_MEAN})", "WARN")
        if real == 0:
            ratings = {"teamRatings": _nba_carry_forward("teamRatings", {}, prev_nba, "teamRatings"),
                       "eloSeed": _nba_carry_forward("eloSeed", {}, prev_nba, "eloSeed")}
            ratings_carried = True
            stale.append("teamRatings")
    else:
        log(f"NBA teamRatings: {real} teams, season {cur} (prior {prior}), "
            f"stats season used {ratings['teamRatings']['statsSeasonUsed']}")
    origin = (sel or {}).get("origin") or {}
    n_bb = sum(v.get("bbref", 0) for v in origin.values())
    n_es = sum(v.get("espn", 0) for v in origin.values())
    src["teamRatings"] = ("carried" if ratings_carried else "none" if real == 0 else
                          "bbref" if n_es == 0 and n_bb else "espn+bbref" if n_bb else "espn")
    src["teamStats"] = origin
    src["bbref"] = (sel or {}).get("bbref", "disabled")
    if (sel or {}).get("bbrefReason") and src["bbref"] == "blocked":
        src["bbrefReason"] = sel["bbrefReason"]

    # player-prop inputs (ESPN only): season averages + last-5 form + stdev from game logs + injury status
    injuries: list = []

    def _inj():
        data, st = ctx.get_json(_nba_espn.INJURIES_URL, None, "espn.injuries")
        return _espn_injuries.parse_injuries(data, "nba") if data else []
    injuries = safe("injuries", _inj, [])
    src["injuries"] = "espn" if injuries else "none"
    props_info: dict = {}

    def _props():
        return _nba_espn.fetch_player_props(ctx, stats_season, roster, injuries, prev_nba.get("playerProps"), TODAY_ISO)
    props, props_info = safe("playerProps", _props, ({}, {}))
    src["playerProps"] = "espn"
    if not props:
        old = prev_nba.get("playerProps")
        if old and old.get("players"):
            log(f"NBA playerProps: fresh build EMPTY -- carrying forward previous data.json nba.playerProps "
                f"({len(old['players'])} players)", "WARN")
            props, src["playerProps"] = old, "carried"
        else:
            props, src["playerProps"] = {}, "none"
        stale.append("playerProps")
    elif props_info.get("gamelogFailed") or props_info.get("gamelogSkippedBudget"):
        stale.append("playerProps.form(partial)")
    props_kb = round(len(json.dumps(props, separators=(",", ":"))) / 1024, 1) if props else 0
    if props_kb > 150:
        log(f"NBA playerProps: {props_kb} KB exceeds the 150 KB budget", "WARN")

    gps = sorted(int(_bb_float(v.get("w")) or 0) + int(_bb_float(v.get("l")) or 0) for v in (standings or {}).values())
    src["generatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tot = ctx.totals()
    health = {"teams_with_ratings": real, "games_played_median": gps[len(gps) // 2] if gps else None,
              "stale_sources": sorted(set(stale)), "playerPropsKB": props_kb,
              "requests": tot["requests"], "retries": tot["retries"], "failures": tot["failures"],
              "seconds": round(time.monotonic() - t_run, 1), "endpoints": ctx.summary()}
    log(f"NBA collect: {tot['requests']} ESPN requests ({tot['retries']} retries, {tot['failures']} failed), "
        f"{health['seconds']}s; teams with ratings {real}/30; stale: {health['stale_sources'] or 'none'}; "
        f"BBRef {src['bbref']}; playerProps {props.get('n', 0)} players ({props_kb} KB)",
        "WARN" if health["stale_sources"] else "INFO")
    for label, v in ctx.summary().items():
        log(f"  NBA source {label}: {v['req']} req, {v['retries']} retries, {v['sec']}s -- {v['last']}")
    return {"season": cur, "standings": standings, "players": players, "roster": roster,
            "teamAdv": adv, "fourFactors": four,
            "teamRatings": ratings["teamRatings"], "eloSeed": ratings["eloSeed"],
            "playerProps": props, "sources": src, "health": health}


_TEAM_NAME_TO_ABBR: dict[str, str] = {
    # MLB
    "Arizona Diamondbacks":"ARI","Atlanta Braves":"ATL","Baltimore Orioles":"BAL",
    "Boston Red Sox":"BOS","Chicago Cubs":"CHC","Chicago White Sox":"CWS",
    "Cincinnati Reds":"CIN","Cleveland Guardians":"CLE","Colorado Rockies":"COL",
    "Detroit Tigers":"DET","Houston Astros":"HOU","Kansas City Royals":"KC",
    "Los Angeles Angels":"LAA","Los Angeles Dodgers":"LAD","Miami Marlins":"MIA",
    "Milwaukee Brewers":"MIL","Minnesota Twins":"MIN","New York Mets":"NYM",
    "New York Yankees":"NYY","Oakland Athletics":"OAK","Athletics":"OAK",
    "Philadelphia Phillies":"PHI","Pittsburgh Pirates":"PIT","San Diego Padres":"SD",
    "San Francisco Giants":"SF","Seattle Mariners":"SEA","St. Louis Cardinals":"STL",
    "Tampa Bay Rays":"TB","Texas Rangers":"TEX","Toronto Blue Jays":"TOR",
    "Washington Nationals":"WSH",
    # NBA
    "Atlanta Hawks":"ATL","Boston Celtics":"BOS","Brooklyn Nets":"BKN",
    "Charlotte Hornets":"CHA","Chicago Bulls":"CHI","Cleveland Cavaliers":"CLE",
    "Dallas Mavericks":"DAL","Denver Nuggets":"DEN","Detroit Pistons":"DET",
    "Golden State Warriors":"GSW","Houston Rockets":"HOU","Indiana Pacers":"IND",
    "Los Angeles Clippers":"LAC","Los Angeles Lakers":"LAL","Memphis Grizzlies":"MEM",
    "Miami Heat":"MIA","Milwaukee Bucks":"MIL","Minnesota Timberwolves":"MIN",
    "New Orleans Pelicans":"NOP","New York Knicks":"NYK","Oklahoma City Thunder":"OKC",
    "Orlando Magic":"ORL","Philadelphia 76ers":"PHI","Phoenix Suns":"PHX",
    "Portland Trail Blazers":"POR","Sacramento Kings":"SAC","San Antonio Spurs":"SAS",
    "Toronto Raptors":"TOR","Utah Jazz":"UTA","Washington Wizards":"WSH",
    # NHL
    "Anaheim Ducks":"ANA","Arizona Coyotes":"ARI","Boston Bruins":"BOS",
    "Buffalo Sabres":"BUF","Calgary Flames":"CGY","Carolina Hurricanes":"CAR",
    "Chicago Blackhawks":"CHI","Colorado Avalanche":"COL","Columbus Blue Jackets":"CBJ",
    "Dallas Stars":"DAL","Detroit Red Wings":"DET","Edmonton Oilers":"EDM",
    "Florida Panthers":"FLA","Los Angeles Kings":"LAK","Minnesota Wild":"MIN",
    "Montreal Canadiens":"MTL","Nashville Predators":"NSH","New Jersey Devils":"NJD",
    "New York Islanders":"NYI","New York Rangers":"NYR","Ottawa Senators":"OTT",
    "Philadelphia Flyers":"PHI","Pittsburgh Penguins":"PIT","San Jose Sharks":"SJS",
    "Seattle Kraken":"SEA","St. Louis Blues":"STL","Tampa Bay Lightning":"TB",
    "Toronto Maple Leafs":"TOR","Utah Hockey Club":"UTA","Vancouver Canucks":"VAN",
    "Vegas Golden Knights":"VGK","Washington Capitals":"WSH","Winnipeg Jets":"WPG",
    # WNBA
    "Atlanta Dream":"ATL","Chicago Sky":"CHI","Connecticut Sun":"CON",
    "Dallas Wings":"DAL","Golden State Valkyries":"GS","Indiana Fever":"IND",
    "Las Vegas Aces":"LV","Los Angeles Sparks":"LA","Minnesota Lynx":"MIN",
    "New York Liberty":"NY","Phoenix Mercury":"PHX","Seattle Storm":"SEA",
    "Washington Mystics":"WSH",
    # NFL
    "Arizona Cardinals":"ARI","Atlanta Falcons":"ATL","Baltimore Ravens":"BAL",
    "Buffalo Bills":"BUF","Carolina Panthers":"CAR","Chicago Bears":"CHI",
    "Cincinnati Bengals":"CIN","Cleveland Browns":"CLE","Dallas Cowboys":"DAL",
    "Denver Broncos":"DEN","Detroit Lions":"DET","Green Bay Packers":"GB",
    "Houston Texans":"HOU","Indianapolis Colts":"IND","Jacksonville Jaguars":"JAX",
    "Kansas City Chiefs":"KC","Las Vegas Raiders":"LV","Los Angeles Chargers":"LAC",
    "Los Angeles Rams":"LAR","Miami Dolphins":"MIA","Minnesota Vikings":"MIN",
    "New England Patriots":"NE","New Orleans Saints":"NO","New York Giants":"NYG",
    "New York Jets":"NYJ","Philadelphia Eagles":"PHI","Pittsburgh Steelers":"PIT",
    "San Francisco 49ers":"SF","Seattle Seahawks":"SEA","Tampa Bay Buccaneers":"TB",
    "Tennessee Titans":"TEN","Washington Commanders":"WSH",
}

def fetch_best_odds(sport: str, game_list: list, name_resolver=None) -> dict:
    """
    Per-game odds map keyed 'home_key:away_key' -> {homeML, awayML, ou, book} built from the ESPN odds already attached to
    game_list (the NBA/NHL scoreboard objects).

    The Odds API path was REMOVED 2026-10-03: the key returned 401 (free tier = 500 credits/month, ~20 credits/run x 3 runs/day),
    the workflows no longer pass ODDS_API_KEY, and every consumer is covered by ESPN / Flashscore prices.  Nothing in this
    pipeline makes a the-odds-api.com request any more.  `sport` / `name_resolver` are kept only so the call sites and the
    data.json bestOdds/bestOddsExt shape stay unchanged; sports whose game_list is empty simply yield {}.
    """
    best: dict = {}
    for g in game_list:
        key = f"{g.get('home','')}:{g.get('away','')}"
        best[key] = {"homeML": g.get("homeML"), "awayML": g.get("awayML"),
                     "ou": g.get("ou"), "book": "ESPN"}
    return best


def fetch_nba_scoreboard(date: str = TODAY_ET) -> tuple[list, list]:
    """Fetch NBA scoreboard. Uses Eastern Time date since NBA game times are listed in ET."""
    log(f"NBA scoreboard {date} (ET)…")
    data = fetch_json(f"{ESPN_BASE}/basketball/nba/scoreboard?dates={date}&limit=20")
    if not data: return [], []
    games    = [_espn_game(e, "NBA") for e in (data.get("events") or [])]
    tom      = (datetime.strptime(date, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
    data2    = fetch_json(f"{ESPN_BASE}/basketball/nba/scoreboard?dates={tom}&limit=20")
    tomorrow = [_espn_game(e, "NBA") for e in ((data2 or {}).get("events") or [])]
    vlog(f"  NBA: {len(games)} today, {len(tomorrow)} tomorrow")
    return games, tomorrow

def fetch_nba_standings(season: int | None = None) -> dict:
    """
    ESPN NBA regular-season standings for `season` (ESPN's season = the year the
    season ENDS: 2026-27 is 2027). Defaults to nba_season_end_year(), so it rolls
    over on Oct 1 automatically. Before a team's first regular-season game every
    value is 0-0 (preseason games do not count) -- that is real, not an error.
    Entry keys: w, l, pct, gb, rs, ra, diff (avg point differential, "+8.2").
    """
    season = season if season is not None else nba_season_end_year()
    log(f"NBA standings (ESPN season={season})…")
    data = fetch_json(
        "https://site.web.api.espn.com/apis/v2/sports/basketball/nba/standings"
        # seasontype=2 (regular season), NOT type=2: ESPN ignores `type` here and returns PRESEASON W-L / points (checked live 2026-10-05: MIA 1-0 +24 before any regular-season game),
        # which then leaked into teamRatings.current, eloSeed, nbaGetBayes and the median-GP rule for player tiers. seasontype=2 returns a clean 0-0 until real games are played.
        f"?region=us&lang=en&season={season}&seasontype=2"
    )
    if not data:
        log(f"NBA standings season={season}: no response from ESPN", "WARN")
        return {}
    out: dict = {}
    for conf in data.get("children") or []:
        for entry in (conf.get("standings") or {}).get("entries") or []:
            team  = entry.get("team") or {}
            abbr  = team.get("abbreviation", "")
            stats = {s["name"]: s.get("displayValue", s.get("value",""))
                     for s in (entry.get("stats") or [])}
            out[abbr] = {
                "w": stats.get("wins","0"), "l": stats.get("losses","0"),
                "pct": stats.get("winPercent",".000"), "gb": stats.get("gamesBehind","—"),
                "rs": stats.get("avgPointsFor","0"), "ra": stats.get("avgPointsAgainst","0"),
                "diff": stats.get("differential","0"),
            }
    if len(out) != 30:
        log(f"NBA standings season={season}: {len(out)} teams (expected 30)", "WARN")
    vlog(f"  NBA standings: {len(out)} teams")
    return out


def nba_playoffs_year(today=None) -> int:
    """Year of the playoffs the NBA 'current' postseason pages should show: this
    season's once April arrives, otherwise the most recent completed one."""
    end = nba_season_end_year(today)
    d = today if today is not None else NOW_MT
    if isinstance(d, datetime):
        d = d.date()
    return end if (d.year, d.month) >= (end, 4) else end - 1


def nba_in_playoff_window(today=None) -> bool:
    """True April-June (NBA play-in + playoffs).  NBA_PLAYOFFS_FORCE=1 forces a fetch attempt."""
    if os.environ.get("NBA_PLAYOFFS_FORCE") == "1":
        return True
    d = today if today is not None else NOW_MT
    if isinstance(d, datetime):
        d = d.date()
    return 4 <= d.month <= 6


def fetch_nba_playoff_bracket() -> dict:
    """Fetch NBA playoff bracket from ESPN (most recent playoffs, see nba_playoffs_year)."""
    yr = nba_playoffs_year()
    if not nba_in_playoff_window():
        log(f"NBA playoff bracket: outside the playoff window (April-June) -- not requested (set NBA_PLAYOFFS_FORCE=1 to force)")
        return {}
    log(f"NBA playoff bracket (ESPN season={yr})…")
    data = fetch_json(f"{ESPN_BASE}/basketball/nba/playoffs?season={yr}", quiet_404=True)
    if not data: return {}
    return {"raw": data, "fetchedAt": TODAY_ISO, "season": yr}


def nba_player_stats_season(espn_cur: dict, cur_year: int, min_games: int | None = None) -> int:
    """Season whose per-player numbers should feed injury-impact tiers: the current
    one once the median team has >= min_games played (ESPN standings), else last season.
    min_games defaults to NBA_TIER_MIN_GP (15), NOT the 5 the team ratings switch at: a player is only tiered with >= 15 games (apply_nba_player_tiers), so flipping at 5 left
    ~2-3 weeks (until players reached 15 GP) with nobody tiered and every star weighted at the fallback in the app's injury impact."""
    min_games = NBA_TIER_MIN_GP if min_games is None else min_games
    gps = sorted((int(_bb_float(v.get("w")) or 0) + int(_bb_float(v.get("l")) or 0)) for v in (espn_cur or {}).values())
    if gps and gps[len(gps) // 2] >= min_games:
        return cur_year
    return cur_year - 1


def fetch_nba_player_stats(season: int | None = None, pages: int = 2) -> list[dict]:
    """
    Top NBA scorers (by points per game) for `season`: [{name, team, gp, mpg, ppg}].

    The old source (site.api.espn.com/.../nba/leaders?season=2026&seasontype=3)
    404s -- that path does not exist on ESPN any more, so this returned [] on
    every run. ESPN's common/v3 `statistics/byathlete` endpoint is the working
    equivalent. `season` defaults to last season until the current one is
    underway (callers pass nba_player_stats_season()). Regular season only
    (seasontype=2). Used to tier players for the app's injury weighting.
    """
    season = season if season is not None else nba_season_end_year() - 1
    log(f"NBA player stats (ESPN byathlete season={season})…")
    players: list[dict] = []
    for page in range(1, pages + 1):
        data = fetch_json(
            "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/statistics/byathlete",
            params={"region": "us", "lang": "en", "contentorigin": "espn", "isqualified": "false",
                    "page": page, "limit": 75, "sort": "offensive.avgPoints:desc",
                    "season": season, "seasontype": 2},
        )
        if not data:
            log(f"NBA player stats season={season} page {page}: no response from ESPN", "WARN")
            break
        cats = {c.get("name"): c.get("names") or [] for c in data.get("categories") or []}
        for row in data.get("athletes") or []:
            ath = row.get("athlete") or {}
            vals = {}
            for c in row.get("categories") or []:
                for nm, v in zip(cats.get(c.get("name"), []), c.get("values") or []):
                    vals.setdefault(nm, v)
            name = ath.get("displayName", "")
            if not name or vals.get("avgPoints") is None:
                continue
            players.append({"name": name, "team": ath.get("teamShortName", ""),
                            "gp": int(vals.get("gamesPlayed") or 0),
                            "mpg": round(float(vals.get("avgMinutes") or 0), 1),
                            "ppg": round(float(vals.get("avgPoints") or 0), 1),
                            "season": season})
        time.sleep(0.3)
    if not players:
        log(f"NBA player stats season={season}: 0 players parsed", "WARN")
    return players


NBA_TIER_PPG = ((24.0, "PREMIUM"), (18.0, "OPTIMAL"), (12.0, "GOOD"))   # ppg -> tier used by app's computeInjuryImpact
NBA_TIER_MIN_GP = 15


def apply_nba_player_tiers(roster: dict, players: list[dict], only_missing: bool = False) -> int:
    """Add `rating` (PREMIUM/OPTIMAL/GOOD) and `ppg` to roster entries (in place) for
    players whose last/current-season scoring clears a tier. Returns how many were tagged.
    only_missing: leave entries that already have a rating alone (used to fill the gaps from LAST season's numbers once the current season is underway)."""
    n = 0
    for p in players or []:
        ent = roster.get((p.get("name") or "").lower())
        if not ent or (p.get("gp") or 0) < NBA_TIER_MIN_GP:
            continue
        if only_missing and ent.get("rating"):
            continue
        for thr, tier in NBA_TIER_PPG:
            if (p.get("ppg") or 0) >= thr:
                ent["rating"] = tier
                ent["ppg"] = p["ppg"]
                n += 1
                break
    return n


def _bb_table_rows(soup, table_ids, limit: int = 60) -> list[dict]:
    """Rows of a Basketball-Reference table (also if comment-wrapped) keyed by each
    cell's data-stat -- robust to multi-row headers, unlike _table_to_rows()."""
    tbl, _ = _bb_find_table(soup, table_ids)
    if tbl is None:
        return []
    rows = []
    for tr in (tbl.find("tbody") or tbl).find_all("tr"):
        if "thead" in (tr.get("class") or []):
            continue
        row: dict = {}
        for td in tr.find_all(["td", "th"]):
            ds = td.get("data-stat")
            if ds and ds != "DUMMY" and ds not in row:
                row[ds] = td.get_text(strip=True)
        if any(v for v in row.values()):
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def fetch_basketball_reference() -> dict:
    """
    Scrape Basketball-Reference's most recent NBA PLAYOFF team tables (see
    nba_playoffs_year). Table ids were renamed by BBRef (old `playoffs_per_game`
    / `playoffs_advanced` ... no longer exist -> every list came back empty with
    no warning); current ids are per_game-team, per_poss-team, advanced-team,
    shooting-team, per_game-opponent. Logs a WARN naming each table that is empty.
    """
    yr = nba_playoffs_year()
    log(f"Basketball Reference playoff stats ({yr})…")
    result: dict = {"perGame": [], "per100": [], "advanced": [], "shooting": [], "opponentPerGame": [],
                    "fetchedAt": TODAY_ISO, "season": yr}
    base = f"https://www.basketball-reference.com/playoffs/NBA_{yr}.html"
    table_map = [
        ("perGame",        ("per_game-team",)),
        ("per100",         ("per_poss-team",)),
        ("advanced",       ("advanced-team",)),
        ("shooting",       ("shooting-team",)),
        ("opponentPerGame",("per_game-opponent",)),
    ]
    if NBA_BBREF_DELAY:
        time.sleep(NBA_BBREF_DELAY)
    html, reason = _bbref_get(base)
    if html is None:
        log(f"Basketball Reference playoffs {yr}: FETCH FAILED -- {reason} ({base})", "WARN")
    else:
        soup = BeautifulSoup(html, "lxml")
        for key, ids in table_map:
            rows = _bb_table_rows(soup, ids, limit=60)
            result[key] = rows
            vlog(f"  BBRef {key}: {len(rows)} rows")
            if not rows:
                log(f"Basketball Reference playoffs {yr}: table {ids[0]!r} missing/empty (layout change?)", "WARN")

    # Series-level stats: these two URLs are specific 2026 series pages (no way to
    # derive future series URLs), so they are only fetched while the playoffs shown are 2026's.
    result["series"] = {}
    series_urls = [
        ("east_finals", "https://www.basketball-reference.com/playoffs/2026-nba-eastern-conference-finals-cavaliers-vs-knicks.html"),
        ("west_finals", "https://www.basketball-reference.com/playoffs/2026-nba-western-conference-finals-spurs-vs-thunder.html"),
    ] if yr == 2026 else []
    for label, url in series_urls:
        if NBA_BBREF_DELAY:
            time.sleep(NBA_BBREF_DELAY)
        html2, reason2 = _bbref_get(url)
        if html2 is None:
            log(f"Basketball Reference series {label}: FETCH FAILED -- {reason2}", "WARN")
            continue
        soup2 = BeautifulSoup(html2, "lxml")
        series_data: dict = {}
        for key, tbl_id in [("perGame", "per_game"), ("advanced", "advanced")]:
            rows = _bb_table_rows(soup2, (tbl_id,), limit=20)
            if rows: series_data[key] = rows
        result["series"][label] = series_data
        vlog(f"  BBRef series {label}: {len(series_data)} tables")
    return result


def _fetch_nhl_game_odds(event_id: str) -> dict:
    """Fetch NHL game odds from ESPN Core API (returns ML, puck line, O/U)."""
    try:
        url  = (f"http://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl"
                f"/events/{event_id}/competitions/{event_id}/odds")
        data = fetch_json(url, timeout=10)
        items = (data or {}).get("items", [])
        if not items: return {}
        ref = items[0].get("$ref", "")
        if not ref: return {}
        o = fetch_json(ref, timeout=10) or {}
        home = o.get("homeTeamOdds", {})
        away = o.get("awayTeamOdds", {})
        return {
            "homeML":      home.get("moneyLine"),
            "awayML":      away.get("moneyLine"),
            "ou":          o.get("overUnder"),
            "spread":      o.get("spread"),
            "homePL":      home.get("spreadOdds"),   # puck line odds
            "awayPL":      away.get("spreadOdds"),
            "details":     o.get("details", ""),
            "provider":    o.get("provider", {}).get("name", ""),
        }
    except Exception as exc:
        vlog(f"NHL odds {event_id}: {exc}")
        return {}

def _espn_nhl_event_ids(date: str) -> dict[str, str]:
    """Return {home_abbr: event_id} for NHL games on a given date (YYYYMMDD)."""
    try:
        data = fetch_json(
            f"https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl"
            f"/events?dates={date}&limit=20"
        )
        ids: dict[str, str] = {}
        for item in (data or {}).get("items", []):
            ref = item.get("$ref", "")
            eid = ref.split("/events/")[-1].split("?")[0] if "/events/" in ref else ""
            if eid: ids[eid] = eid   # we'll resolve home team after fetching
        return ids
    except Exception:
        return {}

def fetch_nhl_today() -> tuple[list, list]:
    """Fetch NHL schedule for today, trying both MT and ET dates to avoid empty results."""
    mt_date = NOW_MT.strftime("%Y-%m-%d")
    et_date = NOW_ET.strftime("%Y-%m-%d")
    result = fetch_nhl_schedule(mt_date)
    today_games, tom_games = result
    if not today_games and mt_date != et_date:
        log(f"NHL: MT date {mt_date} returned no games, trying ET date {et_date}…")
        result = fetch_nhl_schedule(et_date)
        today_games, tom_games = result
    return today_games, tom_games

def fetch_nhl_schedule(date: str = TODAY_ISO) -> tuple[list, list]:
    log(f"NHL schedule {date}…")
    data = fetch_json(f"{NHL_API}/schedule/{date}")
    if not data: return [], []

    def parse_games(day: dict) -> list[dict]:
        out = []
        for g in day.get("games") or []:
            home  = g.get("homeTeam") or {}
            away  = g.get("awayTeam") or {}
            state = g.get("gameState", "FUT")
            out.append({
                "id":         g.get("id"),
                "sport":      "NHL",
                "home":       home.get("abbrev",""),
                "away":       away.get("abbrev",""),
                "homeScore":  home.get("score") if state not in ("FUT","PRE") else None,
                "awayScore":  away.get("score") if state not in ("FUT","PRE") else None,
                "state":      state,
                "period":     g.get("period", 0),
                "clock":      (g.get("clock") or {}).get("timeRemaining",""),
                "venue":      (g.get("venue") or {}).get("default",""),
                "date":       g.get("startTimeUTC",""),
                "network":    ", ".join(b.get("network","") for b in (g.get("tvBroadcasts") or [])),
                "gameType":   g.get("gameType", 2),
                "seriesStatus": (g.get("seriesSummary") or {}).get("seriesStatusShort",""),
            })
        return out

    week = data.get("gameWeek") or []
    today_games    = parse_games(week[0] if week else {})
    tomorrow_games = parse_games(week[1] if len(week) > 1 else {})

    # Enrich today's games with odds from ESPN Core API
    today_str = date.replace("-", "")
    event_ids = _espn_nhl_event_ids(today_str)
    if event_ids and today_games:
        # Map event IDs to games by fetching each event
        for eid in list(event_ids.keys())[:len(today_games)]:
            odds = _fetch_nhl_game_odds(eid)
            if not odds: continue
            # Match by the "details" field which has "HOME -175" style text
            detail = odds.get("details", "")
            fav_abbr = detail.split()[0] if detail else ""
            for g in today_games:
                if g.get("homeML") is not None: continue   # already has odds
                if fav_abbr in (g.get("home",""), g.get("away","")):
                    g.update(odds); break
            else:
                # Fallback: attach to first game without odds
                for g in today_games:
                    if g.get("homeML") is None:
                        g.update(odds); break

    vlog(f"  NHL: {len(today_games)} today, {len(tomorrow_games)} tomorrow")
    return today_games, tomorrow_games

def fetch_nhl_standings() -> dict:
    log("NHL standings…")
    data = fetch_json(f"{NHL_API}/standings/now")
    if not data: return {}
    out: dict = {}
    for t in data.get("standings") or []:
        abbr = (t.get("teamAbbrev") or {}).get("default","")
        out[abbr] = {
            "w": t.get("wins",0), "l": t.get("losses",0), "otl": t.get("otLosses",0),
            "pts": t.get("points",0), "gf": t.get("goalFor",0), "ga": t.get("goalAgainst",0),
            "gd": t.get("goalDifferential",0), "row": t.get("regulationOrOvertimeWins",0),
            "div": t.get("divisionName",""), "conf": t.get("conferenceName",""),
        }
    vlog(f"  NHL standings: {len(out)} teams")
    return out

def nhl_season_end_year(today=None, override=None) -> int:
    """
    The NHL season's ending calendar year (2026-27 -> 2027), same style as nba_season_end_year(): a season Y-1..Y starts in
    Oct, so month >= 9 -> year+1 else year (Jul-Aug still name the season that just ended).  Override with env NHL_SEASON_END_YEAR.
    """
    ov = override if override is not None else os.environ.get("NHL_SEASON_END_YEAR")
    if ov not in (None, ""):
        try:
            y = int(str(ov).strip())
            if 2000 <= y <= 2100:
                return y
        except ValueError:
            pass
        log(f"NHL_SEASON_END_YEAR={ov!r} is not a valid 4-digit year -- ignoring override", "WARN")
    d = today if today is not None else NOW_MT
    if isinstance(d, datetime):
        d = d.date()
    return d.year + 1 if d.month >= 9 else d.year


def nhl_in_playoff_window(today=None) -> bool:
    """True April-June (NHL postseason runs ~mid-April to mid-June).  NHL_PLAYOFFS_FORCE=1 forces a fetch attempt."""
    if os.environ.get("NHL_PLAYOFFS_FORCE") == "1":
        return True
    d = today if today is not None else NOW_MT
    if isinstance(d, datetime):
        d = d.date()
    return 4 <= d.month <= 6


def fetch_nhl_playoff_bracket(today=None) -> dict:
    """
    NHL playoff bracket from ESPN for the CURRENT season (was hardcoded season=2026, which 404s and logged a FAILED-WARN storm
    all year).  Only fetched inside the playoff window (April-June); otherwise one INFO line and the present-but-empty {} the bundle
    key (nhl.bracket) has always carried.  A single non-retrying request: a 404 (no bracket published yet) is a quiet INFO, not a WARN.
    Display-only data: nothing in docs/app.html reads nhl.bracket (renderBracket() is a static placeholder) and no simulation uses it.
    """
    if not nhl_in_playoff_window(today):
        log("NHL playoff bracket: skipped (outside the April-June playoff window)", "INFO")
        return {}
    yr = nhl_season_end_year(today)
    url = f"{ESPN_BASE}/hockey/nhl/playoffs?season={yr}"
    log(f"NHL playoff bracket (ESPN season={yr})…")
    try:
        r = _session.get(url, timeout=15)
        if r.status_code == 404:
            log(f"NHL playoff bracket: ESPN has no bracket for season={yr} yet (404) -- leaving it empty", "INFO")
            return {}
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log(f"NHL playoff bracket season={yr}: {exc} -- leaving it empty", "INFO")
        return {}
    if not data:
        return {}
    return {"raw": data, "fetchedAt": TODAY_ISO, "season": yr}


def _nhl_api_stats(endpoint: str, cayenne: str, limit: int = 50) -> list[dict]:
    url = (f"{NHL_STATS}/{endpoint}?isAggregate=false&isGame=false"
           f"&sort=%5B%7B%22property%22:%22points%22,%22direction%22:%22DESC%22%7D%5D"
           f"&start=0&limit={limit}&cayenneExp={requests.utils.quote(cayenne)}")
    data = fetch_json(url)
    return (data or {}).get("data") or []

# teamId -> this app's own NHL team abbreviations. Real gap, found via a
# pre-season hockey audit: team/percentages (unlike goalie/skater
# endpoints) carries no team-abbreviation field at all -- only
# teamFullName/teamId -- confirmed live against the real API. teamId is
# used, not teamFullName, because NHL's own name strings don't reliably
# match this app's: confirmed live, the real API returns "Montréal
# Canadiens" (accented), this app's own static data uses "Montreal
# Canadiens" for the few teams that even have a real name filled in (see
# the _syntheticEntry finding below) -- a name-string match would
# silently miss exactly that team every time.
_NHL_TEAM_ID_MAP = {
    24: "ANA", 6: "BOS", 7: "BUF", 20: "CGY", 12: "CAR", 16: "CHI", 21: "COL",
    29: "CBJ", 25: "DAL", 17: "DET", 22: "EDM", 13: "FLA", 26: "LAK", 30: "MIN",
    8: "MTL", 18: "NSH", 1: "NJD", 2: "NYI", 3: "NYR", 9: "OTT", 4: "PHI",
    5: "PIT", 28: "SJS", 55: "SEA", 19: "STL", 14: "TBL", 10: "TOR", 68: "UTA",
    23: "VAN", 54: "VGK", 15: "WSH", 52: "WPG",
}


def _nhl_current_season_id() -> str:
    """NHL seasons run ~Oct-Jun/Jul, named startYear+startYear+1 (e.g.
    "20252026"). New season year-cycle begins ~August (draft/preseason
    ramp-up), matching the same boundary docs/app.html's own
    _nhlCurrentSeasonId() uses."""
    ov = os.environ.get("NHL_SEASON_START_YEAR", "").strip()
    if ov.isdigit() and 2000 <= int(ov) <= 2100:
        return f"{int(ov)}{int(ov) + 1}"
    now = datetime.now(timezone.utc)
    start_year = now.year if now.month >= 8 else now.year - 1
    return f"{start_year}{start_year + 1}"


_NHL_PRIOR_SEASON_WEIGHT = 0.25  # explicit direction: 2025-26 (and going forward, "last season") counts for a fixed 25% of every rate stat feeding the NHL MC sims, permanently -- not a sample-size-adaptive shrinkage that fades out once the new season has enough games.

def _nhl_prior_season_id(season: str) -> str:
    start = int(season[:4]) - 1
    return f"{start}{start + 1}"

def _nhl_blend(current, prior, w_prior: float = _NHL_PRIOR_SEASON_WEIGHT):
    """current*0.75 + prior*0.25, degrading to whichever side is real
    when only one exists (e.g. a rookie goalie with no 2025-26 NHL
    record, or a team stat before the new season has logged any real
    games yet)."""
    if current is None and prior is None:
        return None
    if current is None:
        return prior
    if prior is None:
        return current
    return round(current * (1 - w_prior) + prior * w_prior, 4)

def _nhl_fetch_goalie_season(season: str) -> tuple[dict, dict]:
    """One season's real goalie/summary: (best starter per team, every
    goalie's stats by name). The by-name lookup is what lets a blend
    find THIS SAME GOALIE's prior-season numbers even if he was on a
    different team, or find nothing at all for a rookie -- never another
    goalie's numbers standing in for his."""
    try:
        r = requests.get(
            f"{NHL_STATS}/goalie/summary",
            params={"isAggregate": "false", "isGame": "false", "start": 0, "limit": 200,
                    "sort": "gamesPlayed", "cayenneExp": f"seasonId={season} and gameTypeId=2"},
            headers=HEADERS, timeout=15,
        )
        rows = (r.json() or {}).get("data") or [] if r.ok else []
    except Exception as exc:
        log(f"NHL Edge goalies {season}: {exc}", "WARN")
        rows = []
    best_by_team: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for g in rows:
        abbr = g.get("teamAbbrevs", "")
        name = g.get("goalieFullName", "")
        if not name:
            continue
        stat = {"sv": g.get("savePct"), "gaa": g.get("goalsAgainstAverage"), "gp": g.get("gamesPlayed", 0) or 0}
        by_name.setdefault(name, stat)  # first row wins if a name repeats across a mid-season trade's split rows
        # A goalie traded mid-season also gets a combined "EDM,PIT"-style
        # row (confirmed live) -- skip only THAT one for the team map so
        # it doesn't sit as a dead key matching no real 3-letter abbr;
        # the by-name lookup above still benefits from his real per-team
        # stint rows regardless.
        if abbr and "," not in abbr:
            if abbr not in best_by_team or stat["gp"] > best_by_team[abbr]["gp"]:
                best_by_team[abbr] = {**stat, "name": name}
    return best_by_team, by_name

def _nhl_fetch_team_percentages(season: str) -> dict:
    try:
        r = requests.get(
            f"{NHL_STATS}/team/percentages",
            params={"isAggregate": "false", "isGame": "false", "start": 0, "limit": 50,
                    "cayenneExp": f"seasonId={season} and gameTypeId=2"},
            headers=HEADERS, timeout=15,
        )
        rows = (r.json() or {}).get("data") or [] if r.ok else []
    except Exception as exc:
        log(f"NHL Edge zone starts {season}: {exc}", "WARN")
        rows = []
    out = {}
    for t in rows:
        abbr = _NHL_TEAM_ID_MAP.get(t.get("teamId"))
        zs = t.get("zoneStartPct5v5")
        if abbr and zs is not None:
            out[abbr] = zs * 100
    return out

def _nhl_fetch_team_summary(season: str) -> dict:
    """Real per-team goalsForPerGame/goalsAgainstPerGame/powerPlayPct/
    penaltyKillPct -- gf60/ga60/pp/pk in this app's own naming. These were
    never actually pipeline-fetched before this: nhlMC's own gf60/ga60
    (its single dominant offense/defense term) and pp/pk came only from
    the static, hand-typed NHL[] table in docs/app.html, refreshed by
    hand or not at all. Real, live, all 32 teams, both seasons for the
    25%-weight blend."""
    try:
        r = requests.get(
            f"{NHL_STATS}/team/summary",
            params={"isAggregate": "false", "isGame": "false", "start": 0, "limit": 50,
                    "cayenneExp": f"seasonId={season} and gameTypeId=2"},
            headers=HEADERS, timeout=15,
        )
        rows = (r.json() or {}).get("data") or [] if r.ok else []
    except Exception as exc:
        log(f"NHL Edge team rates {season}: {exc}", "WARN")
        rows = []
    out = {}
    for t in rows:
        abbr = _NHL_TEAM_ID_MAP.get(t.get("teamId"))
        if not abbr:
            continue
        out[abbr] = {
            "gf60": t.get("goalsForPerGame"),
            "ga60": t.get("goalsAgainstPerGame"),
            "pp":   t.get("powerPlayPct"),
            "pk":   t.get("penaltyKillPct"),
        }
    return out

def fetch_nhl_edge() -> dict:
    """Real per-team goalie quality (save%, GAA), 5v5 zone-start rate, and
    scoring/special-teams rates (gf60/ga60/pp/pk) -- every secondary and
    primary signal nhlMC/nhlEns (docs/app.html) read from either a live
    fetch or, for gf60/ga60/pp/pk, a static hand-typed table until now.
    Every OTHER field this function used to also fetch (xG%, Corsi%,
    high-danger chances, skating speed) was confirmed dead via a full
    consumer grep across docs/app.html -- zero real read sites for any of
    it, and several of those sub-fetches were independently broken anyway
    (an invalid sort property causing an outright 400 on team/realtime;
    the skater/skating endpoint 500s from the real API regardless of
    parameters, apparently retired). This app's real xG/Corsi signal
    already comes from the separate, working MoneyPuck integration
    (NHL[abbr].mp) -- not duplicated here.

    Explicit direction: 2025-26 season data should carry a fixed 25%
    weight in the NHL MC sims going forward, permanently (not just an
    early-season stopgap). Every stat here is now fetched for BOTH the
    real current season and 2025-26, then blended 75/25 via _nhl_blend()
    -- degrading cleanly to 100% of whichever season is real when the
    other has no data yet (e.g. a rookie goalie, or before the new
    season has any real games logged).

    Real architecture bug, found and fixed separately: this data used to
    ALSO be fetched a second time, directly from the browser
    (docs/app.html's fetchNHLEdge()) straight to api.nhle.com. Confirmed
    live: that API sends no Access-Control-Allow-Origin header at all,
    so every one of those browser-side calls was blocked by the
    browser's own same-origin policy on arrival -- not fixable by
    correcting field names or seasons, since CORS is enforced before the
    response body is ever readable. This is now the only real fetch:
    server-side (not a browser, not subject to CORS), written into
    docs/data.json, read same-origin by the browser -- the same pattern
    every other sport's team/schedule data in this app already uses.
    docs/app.html's own fetchNHLEdge() now reads this instead of
    re-fetching live.
    """
    log("NHL Edge stats (goalies + zone starts + team rates, current + 25%% 2025-26)…")
    current_season = _nhl_current_season_id()
    prior_season = _nhl_prior_season_id(current_season)
    out: dict = {"season": current_season, "priorSeason": prior_season,
                 "priorSeasonWeight": _NHL_PRIOR_SEASON_WEIGHT,
                 "goalies": {}, "zoneStart": {}, "teamRates": {}}

    # Goalies: blend by the SAME PERSON's prior-season row, not just
    # whichever goalie has the team's job this year vs. last year.
    cur_g_team, cur_g_name = _nhl_fetch_goalie_season(current_season)
    pri_g_team, pri_g_name = _nhl_fetch_goalie_season(prior_season)
    for abbr in set(cur_g_team) | set(pri_g_team):
        cur = cur_g_team.get(abbr)
        if cur:
            prior_stat = pri_g_name.get(cur["name"])
            out["goalies"][abbr] = {
                "name": cur["name"],
                "sv":  _nhl_blend(cur["sv"],  prior_stat["sv"]  if prior_stat else None),
                "gaa": _nhl_blend(cur["gaa"], prior_stat["gaa"] if prior_stat else None),
                "gp":  cur["gp"],
            }
        else:
            # No current-season games logged for this team yet -- use
            # last season's own starter at full weight until real
            # current-season games exist to blend against.
            pri = pri_g_team[abbr]
            out["goalies"][abbr] = {"name": pri["name"], "sv": pri["sv"], "gaa": pri["gaa"], "gp": 0}

    # Zone starts and team rates: team-level, no identity-matching needed.
    cur_zs, pri_zs = _nhl_fetch_team_percentages(current_season), _nhl_fetch_team_percentages(prior_season)
    for abbr in set(cur_zs) | set(pri_zs):
        blended = _nhl_blend(cur_zs.get(abbr), pri_zs.get(abbr))
        if blended is not None:
            out["zoneStart"][abbr] = round(blended, 1)

    cur_tr, pri_tr = _nhl_fetch_team_summary(current_season), _nhl_fetch_team_summary(prior_season)
    for abbr in set(cur_tr) | set(pri_tr):
        c, p = cur_tr.get(abbr, {}), pri_tr.get(abbr, {})
        out["teamRates"][abbr] = {
            "gf60": _nhl_blend(c.get("gf60"), p.get("gf60")),
            "ga60": _nhl_blend(c.get("ga60"), p.get("ga60")),
            "pp":   _nhl_blend(c.get("pp"),   p.get("pp")),
            "pk":   _nhl_blend(c.get("pk"),   p.get("pk")),
        }

    vlog(f"  NHL Edge ({current_season} + 25% {prior_season}): {len(out['goalies'])} team goalies, "
         f"{len(out['zoneStart'])} zone-starts, {len(out['teamRates'])} team rates")
    return out


_MP_TEAM_FIELDS = ("xgfPct","xgf60","xga60","cfPct","hdcfPct","gf","ga","shots","hdgf","hdga","scgf","scga")
_MP_GOALIE_BLEND_FIELDS = ("gsaa","savePct","xSavePct","hdSavePct","mdSavePct","ldSavePct")

def _mp_load_teams(year: int) -> dict:
    rows = fetch_csv_rows(f"{MP_BASE}/{year}/regular/teams.csv")
    out: dict = {}
    for row in rows:
        situation = row.get("situation","")
        team = row.get("team","")
        if not team: continue
        try:
            out.setdefault(team, {})[situation] = {
                "xgfPct":    float(row.get("xGoalsPercentage") or 0),
                "xgf60":     float(row.get("xGoalsForPer60") or row.get("xGoalsFor") or 0),
                "xga60":     float(row.get("xGoalsAgainstPer60") or row.get("xGoalsAgainst") or 0),
                "cfPct":     float(row.get("corsiPercentage") or 0),
                "hdcfPct":   float(row.get("highDangerShotsForPercentage") or row.get("highDangerShotsFor") or 0),
                "gf":        float(row.get("goalsFor") or 0),
                "ga":        float(row.get("goalsAgainst") or 0),
                "shots":     float(row.get("shotsOnGoalFor") or 0),
                "hdgf":      float(row.get("highDangerGoalsFor") or 0),
                "hdga":      float(row.get("highDangerGoalsAgainst") or 0),
                "scgf":      float(row.get("scoreAdjustedShotsAttemptsFor") or 0),
                "scga":      float(row.get("scoreAdjustedShotsAttemptsAgainst") or 0),
            }
        except (ValueError, TypeError):
            pass
    return out

def _mp_load_goalies(year: int) -> dict:
    """Keyed by (name, situation) -- MoneyPuck's goalies.csv has one row
    per goalie per situation (all/5v5/4v5/...), same as teams.csv.

    Real bug, found 2026-09-16 auditing why every NHL team but 4 (a
    hand-curated static fallback) had zero real goaltending signal in
    the model: this used to look up columns (savePct, goalsAboveAverage,
    highDangerSavePct, shotsOnGoalAgainst, goalsAgainst, xGoalsAgainst --
    all copy-pasted from _mp_load_teams' own camelCase convention) that
    don't exist on MoneyPuck's real goalies.csv at all. row.get(...) on a
    missing column returns None, and every field here was wrapped in
    `or 0`/`or 0.0`, so the lookup failing was silent -- confirmed live,
    every one of 490 real goalie rows had gsaa/savePct/hdSavePct/shots/
    ga/xga sitting at exactly 0.0 despite games_played (the one field
    that WAS spelled correctly) coming through with real values. Only
    the hand-typed static 4-team table was ever contributing real
    goaltending data to nhlEns()'s composite as a result.

    MoneyPuck's goalies.csv doesn't publish save%/GSAx as columns at
    all -- those are derived stats -- it publishes the raw counting
    fields (shots faced, goals allowed, expected goals against, and the
    same breakdown per danger zone) that save%/GSAx are computed from
    elsewhere on their own site. Deriving them here from those raw
    fields instead of a nonexistent pre-computed column:
      gsaa (goals saved above average, i.e. GSAx) = xGoals - goals
      savePct = (shots - goals) / shots
      xSavePct = (shots - xGoals) / shots
      {hd,md,ld}SavePct = (zoneShots - zoneGoals) / zoneShots per danger tier
    Best-effort against MoneyPuck's documented raw schema -- not yet
    verified against a live CSV pull (network to moneypuck.com is
    blocked from the environment this fix was written in); the next
    real scheduled run of this script is the first real check that
    these are the correct raw column names."""
    rows = fetch_csv_rows(f"{MP_BASE}/{year}/regular/goalies.csv")
    out: dict = {}
    def _safe_div(num: float, den: float) -> float:
        return num / den if den else 0.0
    for row in rows:
        name = row.get("name","")
        situation = row.get("situation","all")
        if not name: continue
        try:
            shots = float(row.get("ongoal") or 0)
            goals = float(row.get("goals") or 0)
            xga = float(row.get("xGoals") or 0)
            hdShots = float(row.get("highDangerShots") or 0)
            hdGoals = float(row.get("highDangerGoals") or 0)
            mdShots = float(row.get("mediumDangerShots") or 0)
            mdGoals = float(row.get("mediumDangerGoals") or 0)
            ldShots = float(row.get("lowDangerShots") or 0)
            ldGoals = float(row.get("lowDangerGoals") or 0)
            out[(name, situation)] = {
                "team":      row.get("team",""),
                "gp":        int(row.get("games_played") or 0),
                "gsaa":      xga - goals,
                "savePct":   _safe_div(shots - goals, shots),
                "xSavePct":  _safe_div(shots - xga, shots),
                "hdSavePct": _safe_div(hdShots - hdGoals, hdShots),
                "mdSavePct": _safe_div(mdShots - mdGoals, mdShots),
                "ldSavePct": _safe_div(ldShots - ldGoals, ldShots),
                "shots":     int(shots),
                "ga":        goals,
                "xga":       xga,
            }
        except (ValueError, TypeError):
            pass
    return out

def fetch_moneypuck() -> dict:
    """MoneyPuck advanced stats — 5v5, 5v4, 4v5, all — teams and goalies.

    Explicit direction: 2025-26 season data should carry a fixed 25%
    weight in the NHL MC sims going forward (docs/app.html's nhlEns()
    reads MONEYPUCK.teams' xgfPct live for its mpBoost term). Fetches
    both the real current season and 2025-26 and blends every numeric
    team field via _nhl_blend() (75% current / 25% prior), same policy
    and same helper as fetch_nhl_edge(). MoneyPuck doesn't publish a
    season's folder until its own pipeline starts ingesting that
    season's real games (confirmed live: .../2026/regular/teams.csv
    404s in September, before puck drop) -- the blend degrades cleanly
    to 100% of 2025-26 through that pre-season gap, same as any other
    team/goalie missing from the current season's file so far.
    """
    log("MoneyPuck stats (current + 25% 2025-26)…")
    out: dict = {"teams": {}, "goalies": [], "skaters": []}
    current_yr = int(_nhl_current_season_id()[:4])
    prior_yr = current_yr - 1

    cur_teams, pri_teams = _mp_load_teams(current_yr), _mp_load_teams(prior_yr)
    for team in set(cur_teams) | set(pri_teams):
        out["teams"][team] = {}
        c_sit, p_sit = cur_teams.get(team, {}), pri_teams.get(team, {})
        for situation in set(c_sit) | set(p_sit):
            c, p = c_sit.get(situation, {}), p_sit.get(situation, {})
            out["teams"][team][situation] = {f: _nhl_blend(c.get(f), p.get(f)) for f in _MP_TEAM_FIELDS}

    # Goalies: blend the SAME goalie's prior-season row when it exists
    # (matched by name+situation, same principle as fetch_nhl_edge's
    # goalie blend) -- never mixed with a different goalie's numbers.
    # gp/team/shots/ga/xga stay current-season-only (real current usage/
    # counting stats, not rate stats meant to be blended); only the rate
    # fields in _MP_GOALIE_BLEND_FIELDS get the 75/25 treatment.
    cur_g, pri_g = _mp_load_goalies(current_yr), _mp_load_goalies(prior_yr)
    for key in set(cur_g) | set(pri_g):
        name, situation = key
        c, p = cur_g.get(key), pri_g.get(key)
        if c:
            blended = {**c, **{f: _nhl_blend(c.get(f), p.get(f) if p else None) for f in _MP_GOALIE_BLEND_FIELDS}}
        else:
            blended = p  # no current-season row yet for this goalie -- 100% 2025-26 until one exists
        out["goalies"].append({"name": name, "situation": situation, **blended})

    out["goalies"].sort(key=lambda g: g["gsaa"], reverse=True)
    vlog(f"  MoneyPuck ({current_yr} + 25% {prior_yr}): {len(out['teams'])} teams, {len(out['goalies'])} goalies")
    return out

# RETIRED 2026-10-03: fetch_hockeyviz(), fetch_hockey_reference(), fetch_hockey_reference_team_stats().
#   * HockeyViz: both pages it scraped (/txt/shotRatesByScore4, /txt/teamStats4) now return 404, so nhl.hockeyviz was
#     always {"teams": {}} while every refresh paid two failing requests + two log warnings.  Nothing in docs/app.html
#     (or any script) ever read it (grep: the only hits are the bundle key and a section-header comment).
#   * Hockey-Reference: scraped two hardcoded 2026 conference-finals series pages (columns misaligned, last season's
#     finalists) and the NHL_2026 season page (LAST season; the live one is NHL_2027), 3 requests with 2s sleeps each;
#     nhl.hockeyRef was stale playoff junk and nhl.hockeyRefTeams was always {teams:{}}.  Also never read anywhere.
#   The bundle keys below stay present-but-empty so any defensive reader keeps working.  NHL team-level shot/possession
#   quality already comes from MoneyPuck (fetch_moneypuck) and the NHL stats API (fetch_nhl_edge) -- the live consumers.

def fetch_nhl_skater_value() -> dict:
    """Per-skater points-per-game table for the app's NHL injury adjustment (docs/app.html nhlMC / NHL_INJ_*).

    One request per season (current + prior) to the NHL stats API's skater/summary (every skater in one response, ~940 rows).
    Estimation/shrinkage rules are in scripts/_nhl_skaters.py's docstring.  Fail-open: any failure returns an empty
    `players` dict and the app applies no skater adjustment."""
    log("NHL skater value (stats API, current + prior season)…")
    current = _nhl_current_season_id()
    prior = _nhl_prior_season_id(current)
    out: dict = {"season": current, "priorSeason": prior, "shrinkGames": _nhl_skaters.SHRINK_GAMES, "players": {}}
    def rows(season: str) -> list[dict]:
        try:
            r = requests.get(
                f"{NHL_STATS}/skater/summary",
                params={"isAggregate": "false", "isGame": "false", "start": 0, "limit": -1,
                        "cayenneExp": f"seasonId={season} and gameTypeId=2"},
                headers=HEADERS, timeout=25,
            )
            return ((r.json() or {}).get("data") or []) if r.ok else []
        except Exception as exc:
            log(f"NHL skater value {season}: {exc}", "WARN")
            return []
    cur_rows = rows(current)
    time.sleep(0.5)
    pri_rows = rows(prior)
    if not pri_rows:          # no prior season = cannot anchor the shrinkage; ship nothing rather than a current-season-only table
        log("NHL skater value: prior season unavailable -- skipping (no skater injury adjustment)", "WARN")
        return out
    out["players"] = _nhl_skaters.build_skater_value(cur_rows, pri_rows)
    vlog(f"  NHL skater value: {len(out['players'])} skaters ({current} now + {prior} prior)")
    return out


def fetch_futures_odds() -> dict:
    """
    RETIRED 2026-10-03 (The Odds API removed: 401 / free-tier quota; the futures panel is unused).  Makes NO network request.
    Returns the present-but-empty shape docs/app.html's renderFuturesOdds() reads defensively (source "none").
    """
    return {"mlb": [], "nba": [], "nhl": [], "golf": [], "source": "none"}


# ═══════════════════════════════════════════════════════════════════════════════
# F1
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_f1() -> dict:
    result: dict = {"schedule":[], "driverStandings":[], "constructorStandings":[], "nextRace":None}
    year = NOW_MT.year

    # Ergast schedule (short timeout — site often slow/down)
    try:
        data  = fetch_json(f"https://api.jolpi.ca/ergast/f1/{year}.json?limit=25", timeout=6)
        races = (data.get("MRData",{}).get("RaceTable",{}).get("Races") or [])
        for race in races:
            rd = race.get("date","")
            entry = {
                "round":   int(race.get("round",0)),
                "name":    race.get("raceName",""),
                "circuit": race.get("Circuit",{}).get("circuitName",""),
                "country": race.get("Circuit",{}).get("Location",{}).get("country",""),
                "date":    rd, "time": race.get("time",""),
                "past":    rd < TODAY_ISO,
            }
            result["schedule"].append(entry)
            if rd >= TODAY_ISO and result["nextRace"] is None:
                result["nextRace"] = entry
    except Exception as exc: log(f"F1 schedule: {exc}", "WARN")

    # Driver standings
    try:
        data = fetch_json(f"https://api.jolpi.ca/ergast/f1/{year}/driverStandings.json", timeout=6)
        for s in (data.get("MRData",{}).get("StandingsTable",{})
                      .get("StandingsLists",[{}])[0].get("DriverStandings") or [])[:20]:
            drv  = s.get("Driver",{})
            ctor = (s.get("Constructors") or [{}])[0]
            result["driverStandings"].append({
                "pos":  int(s.get("position",99)),
                "name": f"{drv.get('givenName','')} {drv.get('familyName','')}".strip(),
                "code": drv.get("code",""), "team": ctor.get("name",""),
                "pts":  float(s.get("points",0)), "wins": int(s.get("wins",0)),
            })
    except Exception as exc: log(f"F1 driver standings: {exc}", "WARN")

    # Constructor standings
    try:
        data = fetch_json(f"https://api.jolpi.ca/ergast/f1/{year}/constructorStandings.json", timeout=6)
        for s in (data.get("MRData",{}).get("StandingsTable",{})
                      .get("StandingsLists",[{}])[0].get("ConstructorStandings") or [])[:10]:
            ctor = s.get("Constructor",{})
            result["constructorStandings"].append({
                "pos":  int(s.get("position",99)),
                "name": ctor.get("name",""),
                "pts":  float(s.get("points",0)), "wins": int(s.get("wins",0)),
            })
    except Exception as exc: log(f"F1 constructor standings: {exc}", "WARN")

    log(f"F1: {len(result['schedule'])} races | next: {result['nextRace'] and result['nextRace']['name']}")
    return result

def fetch_f1_analytics() -> dict:
    """Fetch F1 race analysis from f1datastop.com."""
    url    = "https://f1datastop.com/race-analysis"
    result = {"source":"f1datastop.com", "fetchedAt":TODAY_ISO, "data":[], "error":None}
    try:
        r = _ref_session.get(url, timeout=20)
        if r.ok:
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.find_all(["article","section","div"],
                                  class_=lambda c: c and any(k in c for k in ["race","analysis","driver","lap","pace"]))
            for card in cards[:20]:
                txt = card.get_text(separator=" ", strip=True)
                if len(txt) > 30: result["data"].append({"text": txt[:300], "tag": card.name})
            for tbl in soup.find_all("table")[:5]:
                for row in tbl.find_all("tr")[:15]:
                    cells = [td.get_text(strip=True) for td in row.find_all(["td","th"])]
                    if cells: result["data"].append({"row": cells})
        else:
            result["error"] = f"HTTP {r.status_code}"
    except Exception as exc:
        result["error"] = str(exc)
        log(f"F1 analytics: {exc}", "WARN")
    return result

def fetch_f1_tracing_insights() -> dict:
    """Fetch race data index from TracingInsights/2026 GitHub repo."""
    log("F1 TracingInsights GitHub…")
    result: dict = {"source":"TracingInsights/2026", "races":[], "fetchedAt":TODAY_ISO}
    try:
        api   = "https://api.github.com/repos/TracingInsights/2026/contents"
        hdrs  = {"Accept": "application/vnd.github.v3+json", "User-Agent": "clairvoyance-engine"}
        r     = requests.get(api, headers=hdrs, timeout=15)
        if r.ok:
            contents = r.json()
            for item in contents:
                if item.get("type") == "dir":
                    race_r = requests.get(item["url"], headers=hdrs, timeout=10)
                    files  = [f["name"] for f in (race_r.json() if race_r.ok else [])
                              if f.get("type") == "file"]
                    result["races"].append({"race": item["name"], "files": files})
        else:
            result["error"] = f"HTTP {r.status_code}"
    except Exception as exc:
        log(f"TracingInsights: {exc}", "WARN")
        result["error"] = str(exc)
    log(f"F1 TracingInsights: {len(result['races'])} race dirs found")
    return result

def fetch_f1_calendar_datastop() -> list[dict]:
    """Fetch F1 calendar from f1datastop.com."""
    log("F1 datastop calendar…")
    items: list[dict] = []
    try:
        soup = fetch_html("https://f1datastop.com/calendar", timeout=15)
        if soup:
            for row in soup.select("tr"):
                cells = row.find_all(["td","th"])
                if len(cells) >= 2:
                    items.append({
                        "event": cells[0].get_text(strip=True),
                        "date":  cells[1].get_text(strip=True) if len(cells)>1 else "",
                        "venue": cells[2].get_text(strip=True) if len(cells)>2 else "",
                    })
    except Exception as exc: log(f"F1 calendar datastop: {exc}", "WARN")
    return items[:25]

def fetch_f1_data() -> dict:
    """Fetch comprehensive F1 data: ESPN scoreboard/standings + Ergast.
    Returns nextRace, driverStandings, constructorStandings, recentResults,
    qualifyingGrid, raceBets."""
    log("F1 comprehensive data (ESPN + Ergast)…")
    result: dict = {
        "nextRace": None,
        "driverStandings": [],
        "constructorStandings": [],
        "recentResults": [],
        "qualifyingGrid": [],
        "raceBets": [],
        "schedule": [],
        "fetchedAt": TODAY_ISO,
    }

    # ESPN F1 scoreboard
    try:
        sb = fetch_json("https://site.api.espn.com/apis/site/v2/sports/racing/f1/scoreboard")
        for ev in (sb or {}).get("events") or []:
            comp  = (ev.get("competitions") or [{}])[0]
            comps = comp.get("competitors") or []
            st    = ev.get("status") or {}
            state = (st.get("type") or {}).get("state", "pre")
            entry = {
                "id":      ev.get("id",""),
                "name":    ev.get("name",""),
                "date":    ev.get("date",""),
                "state":   state,
                "results": [],
            }
            for c in comps[:10]:
                athlete = (c.get("athlete") or c.get("team") or {})
                entry["results"].append({
                    "pos":  c.get("order") or c.get("position",""),
                    "name": athlete.get("displayName","") or athlete.get("name",""),
                    "team": (c.get("team") or {}).get("displayName",""),
                    "time": c.get("displayValue",""),
                })
            if state == "pre" and result["nextRace"] is None:
                result["nextRace"] = {"name": ev.get("name",""), "date": ev.get("date",""),
                                       "shortName": ev.get("shortName",""), "id": ev.get("id","")}
            if state == "post":
                result["recentResults"].append(entry)
                # Try to get qualifying grid from same event
                for note_obj in (comp.get("notes") or []):
                    h = note_obj.get("headline","")
                    if "qual" in h.lower() or "grid" in h.lower():
                        entry["qualNote"] = h
        # Qualifying grid: check if scoreboard has a separate qualifying event
        for ev in (sb or {}).get("events") or []:
            if "qualifying" in str(ev.get("name","")).lower():
                comp  = (ev.get("competitions") or [{}])[0]
                for c in (comp.get("competitors") or [])[:10]:
                    athlete = (c.get("athlete") or c.get("team") or {})
                    result["qualifyingGrid"].append({
                        "pos":  c.get("order") or c.get("position",""),
                        "name": athlete.get("displayName","") or athlete.get("name",""),
                        "team": (c.get("team") or {}).get("displayName",""),
                        "time": c.get("displayValue",""),
                    })
    except Exception as exc:
        log(f"F1 ESPN scoreboard: {exc}", "WARN")

    # ESPN F1 standings
    try:
        st_data = fetch_json("https://site.api.espn.com/apis/site/v2/sports/racing/f1/standings")
        for entry in (st_data or {}).get("standings", {}).get("entries") or []:
            stats = {s.get("name",""): s.get("displayValue","") for s in (entry.get("stats") or [])}
            ath = entry.get("athlete") or {}
            ctor = entry.get("team") or {}
            result["driverStandings"].append({
                "pos":   stats.get("rank",""),
                "name":  ath.get("displayName",""),
                "code":  ath.get("abbreviation",""),
                "team":  ctor.get("displayName","") or ctor.get("abbreviation",""),
                "pts":   stats.get("points",""),
                "wins":  stats.get("wins","0"),
            })
    except Exception as exc:
        log(f"F1 ESPN standings: {exc}", "WARN")

    # Fall back to Ergast if ESPN standings is empty
    if not result["driverStandings"]:
        ergast = fetch_f1()
        result["driverStandings"]     = ergast.get("driverStandings", [])
        result["constructorStandings"] = ergast.get("constructorStandings", [])
        if not result["schedule"]:
            result["schedule"]  = ergast.get("schedule", [])
        if not result["nextRace"]:
            result["nextRace"]  = ergast.get("nextRace")
    else:
        # Also grab constructor standings and schedule from Ergast
        try:
            ergast = fetch_f1()
            result["constructorStandings"] = ergast.get("constructorStandings", [])
            result["schedule"]  = ergast.get("schedule", [])
            if not result["nextRace"]:
                result["nextRace"] = ergast.get("nextRace")
        except Exception as exc:
            log(f"F1 Ergast fallback: {exc}", "WARN")

    # Generate race bets from championship standings + pole position model
    standings = result["driverStandings"]
    if standings and result["nextRace"]:
        race_name = result["nextRace"].get("name", "Next Race")
        qual_grid = result["qualifyingGrid"]
        # Pole sitter wins ~35% of races
        pole = qual_grid[0] if qual_grid else None
        if pole:
            pole_name = pole.get("name", "")
            # Adjust by championship position
            champ_pos = next((int(s.get("pos",99)) for s in standings
                              if s.get("name","") == pole_name), 10)
            pole_prob = 0.35 * (1.0 + max(0, (10 - champ_pos)) * 0.02)
            pole_prob = min(0.55, pole_prob)
            ev_pct = round((pole_prob * 2.50 - 1) * 100, 1)  # assume +150 winner market
            if ev_pct > 0:
                result["raceBets"].append({
                    "sport":      "F1",
                    "game":       race_name,
                    "pick":       f"{pole_name} Race Winner",
                    "prob":       round(pole_prob * 100, 1),
                    "ev":         ev_pct,
                    "evGrade":    _ev_grade(ev_pct),
                    "confidence": _confidence(pole_prob, ev_pct, 2),
                    "ml":         "+150",
                    "grade":      "GOOD" if ev_pct > 4 else "INFO",
                    "note":       f"Pole position · P{champ_pos} in championship",
                    "date":       TODAY_ISO,
                })
        # Top-3 finish bets for P2/P3 in championship if strong
        for i, drv in enumerate(standings[:3]):
            pos = int(str(drv.get("pos","99")))
            if pos > 3: continue
            prob = max(0.50, 0.70 - pos * 0.08)
            ev_pct = round((prob * 1.60 - 1) * 100, 1)  # assume -167 podium
            if ev_pct > 2:
                result["raceBets"].append({
                    "sport":      "F1",
                    "game":       race_name,
                    "pick":       f"{drv.get('name','')} Podium Finish",
                    "prob":       round(prob * 100, 1),
                    "ev":         ev_pct,
                    "evGrade":    _ev_grade(ev_pct),
                    "confidence": _confidence(prob, ev_pct, 1),
                    "ml":         "-167",
                    "grade":      "INFO",
                    "note":       f"P{pos} championship · strong form",
                    "date":       TODAY_ISO,
                })

    log(f"F1 comprehensive: {len(result['driverStandings'])} drivers, "
        f"{len(result['qualifyingGrid'])} grid, {len(result['raceBets'])} bets")
    return result

# ═══════════════════════════════════════════════════════════════════════════════
# Weather
# ═══════════════════════════════════════════════════════════════════════════════
_MLB_COORDS: dict[str, tuple[float, float, str]] = {
    "NYY":(40.8296,-73.9262,"Bronx NY"), "NYM":(40.7571,-73.8458,"Queens NY"),
    "BOS":(42.3467,-71.0972,"Boston MA"), "CHC":(41.9484,-87.6553,"Chicago IL"),
    "CHW":(41.8300,-87.6338,"Chicago IL"), "CLE":(41.4962,-81.6852,"Cleveland OH"),
    "DET":(42.3390,-83.0485,"Detroit MI"), "KC":(39.0517,-94.4803,"Kansas City MO"),
    "LAA":(33.8003,-117.8827,"Anaheim CA"), "LAD":(34.0739,-118.2400,"Los Angeles CA"),
    "PHI":(39.9061,-75.1665,"Philadelphia PA"), "PIT":(40.4469,-80.0057,"Pittsburgh PA"),
    "CIN":(39.0975,-84.5066,"Cincinnati OH"), "STL":(38.6226,-90.1928,"St. Louis MO"),
    "WSH":(38.8730,-77.0074,"Washington DC"), "BAL":(39.2838,-76.6218,"Baltimore MD"),
    "SF":(37.7786,-122.3893,"San Francisco CA"), "SD":(32.7076,-117.1570,"San Diego CA"),
    "COL":(39.7559,-104.9942,"Denver CO"), "OAK":(37.7516,-122.2005,"Oakland CA"),
    "ATH":(37.7516,-122.2005,"Oakland CA"),
}
_INDOOR = {"MIN","TOR","TB","MIA","TEX","HOU","ARI","ATL","MIL","SEA"}
_WMO = {0:"Clear",1:"Mainly Clear",2:"Partly Cloudy",3:"Overcast",
         45:"Fog",51:"Drizzle",61:"Rain",71:"Snow",80:"Showers",95:"Thunderstorm"}

def _wmo_desc(code: int) -> str:
    for k in sorted(_WMO, reverse=True):
        if code >= k: return _WMO[k]
    return "Unknown"

def fetch_f1_unchained() -> dict:
    """F1 Unchained track guides — overtaking spots, DRS zones, racing lines."""
    log("F1 Unchained track guide…")
    result: dict = {"tracks": {}, "source": "unchained"}
    try:
        soup = fetch_html("https://www.unchainedmediainc.com/track-guide")
        if not soup:
            return result
        articles = soup.find_all(["article","div"], class_=re.compile(r"track|guide|circuit", re.I))
        for a in articles[:20]:
            title_el = a.find(["h1","h2","h3","h4"])
            if not title_el: continue
            title = title_el.get_text(strip=True)
            text_el = a.find("p")
            text = text_el.get_text(strip=True) if text_el else ""
            if title and len(title) < 60:
                result["tracks"][title] = {"description": text[:300]}
        vlog(f"  F1 Unchained: {len(result['tracks'])} tracks")
    except Exception as e:
        log(f"F1 Unchained error: {e}", "WARN")
    return result

def fetch_weather(home_team: str) -> dict | None:
    if home_team in _INDOOR:
        return {"condition":"Dome/Retractable","temp":None,"wind":None,"indoor":True}
    coords = _MLB_COORDS.get(home_team)
    if not coords: return None
    lat, lon, city = coords
    data = fetch_json(
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,wind_speed_10m,wind_direction_10m,weather_code"
        f"&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=auto",
        timeout=10
    )
    if not data: return None
    c = data.get("current") or {}
    return {
        "condition": _wmo_desc(int(c.get("weather_code",0))),
        "temp":      round(float(c.get("temperature_2m",0))),
        "wind":      round(float(c.get("wind_speed_10m",0))),
        "windDir":   round(float(c.get("wind_direction_10m",0))),
        "city":      city, "indoor": False,
    }

# MLS club home-city coordinates (lat, lon, city) — same Open-Meteo pattern
# fetch_weather() already uses for MLB, extended to soccer since wind/rain
# meaningfully suppress O/U totals in open-air stadiums, same as baseball.
_MLS_COORDS: dict[str, tuple[float, float, str]] = {
    "atlanta united":(33.7554,-84.4008,"Atlanta GA"), "austin fc":(30.2747,-97.7211,"Austin TX"),
    "charlotte fc":(35.2258,-80.8528,"Charlotte NC"), "chicago fire fc":(41.8623,-87.6167,"Chicago IL"),
    "fc cincinnati":(39.1102,-84.5203,"Cincinnati OH"), "colorado rapids":(39.8035,-104.8927,"Commerce City CO"),
    "columbus crew":(39.9689,-83.0173,"Columbus OH"), "d.c. united":(38.8678,-77.0113,"Washington DC"),
    "fc dallas":(33.1538,-96.8355,"Frisco TX"), "houston dynamo fc":(29.7521,-95.3527,"Houston TX"),
    "inter miami cf":(26.1584,-80.1397,"Fort Lauderdale FL"), "los angeles football club":(34.0132,-118.2856,"Los Angeles CA"),
    "lafc":(34.0132,-118.2856,"Los Angeles CA"), "la galaxy":(33.8644,-118.2611,"Carson CA"),
    "minnesota united fc":(44.9535,-93.1614,"St. Paul MN"), "cf montréal":(45.5089,-73.5533,"Montréal QC"),
    "cf montreal":(45.5089,-73.5533,"Montréal QC"), "nashville sc":(36.1329,-86.7734,"Nashville TN"),
    "new england revolution":(42.0909,-71.2643,"Foxborough MA"), "new york city football club":(40.8296,-73.9262,"Bronx NY"),
    "nycfc":(40.8296,-73.9262,"Bronx NY"), "new york red bulls":(40.7351,-74.1503,"Harrison NJ"),
    "red bull new york":(40.7351,-74.1503,"Harrison NJ"), "orlando city sc":(28.5416,-81.3893,"Orlando FL"),
    "philadelphia union":(39.8328,-75.3782,"Chester PA"), "portland timbers":(45.5215,-122.6919,"Portland OR"),
    "real salt lake":(40.5829,-111.8933,"Sandy UT"), "san diego fc":(32.7157,-117.1611,"San Diego CA"),
    "san jose earthquakes":(37.3520,-121.9250,"San Jose CA"), "seattle sounders fc":(47.5952,-122.3316,"Seattle WA"),
    "sporting kansas city":(38.8225,-94.8203,"Kansas City KS"), "st. louis city sc":(38.6413,-90.2529,"St. Louis MO"),
    "toronto fc":(43.6332,-79.4186,"Toronto ON"), "vancouver whitecaps fc":(49.2765,-123.1028,"Vancouver BC"),
}
_MLS_DOME = {"atlanta united", "minnesota united fc"}  # retractable/covered roofs

def fetch_soccer_weather(team_name: str) -> dict | None:
    """Weather for a soccer club's home city — matched loosely by name since
    team names vary in casing/punctuation across sources (FBref/ESPN/mlssoccer.com)."""
    if not team_name:
        return None
    key = team_name.strip().lower()
    if key in _MLS_DOME:
        return {"condition": "Dome/Retractable", "temp": None, "wind": None, "indoor": True}
    coords = _MLS_COORDS.get(key)
    if not coords:
        for k, v in _MLS_COORDS.items():
            if k in key or key in k:
                coords = v; break
    if not coords:
        return None
    lat, lon, city = coords
    data = fetch_json(
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,wind_speed_10m,wind_direction_10m,weather_code,precipitation_probability"
        f"&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=auto",
        timeout=10
    )
    if not data: return None
    c = data.get("current") or {}
    return {
        "condition": _wmo_desc(int(c.get("weather_code", 0))),
        "temp":      round(float(c.get("temperature_2m", 0))),
        "wind":      round(float(c.get("wind_speed_10m", 0))),
        "windDir":   round(float(c.get("wind_direction_10m", 0))),
        "precipProb": c.get("precipitation_probability"),
        "city":      city, "indoor": False,
    }

# ═══════════════════════════════════════════════════════════════════════════════
# Linemate props / trends / cheatsheets — REMOVED 2026-09-10.
# Every per-league page on linemate.io (confirmed for /nfl, /nba, /nhl,
# and /mlb, both bare and /trends) now redirects an unauthenticated
# request straight back to the marketing homepage — a site-wide login
# wall, not an off-season or NFL-specific gap. This scraper had nothing
# real left to find for any sport; the real production bundle showed
# zero props/trends/form for every sport still in this loop before
# removal. NFL and NBA now source props from their own real ESPN-stats
# + Monte Carlo sim generators (docs/app.html's renderNFLModelProps /
# renderNBAProps); NHL already had that same live fallback built in.
# validate_props_against_schedule() is kept below since nothing else
# in this file references fetch_linemate_props's removed team-tag bug
# it guarded against, but no caller passes it real data anymore either.
# ═══════════════════════════════════════════════════════════════════════════════
def validate_props_against_schedule(props: list[dict], todays_games: list[dict]) -> list[dict]:
    """Drop any prop whose parsed team abbreviation doesn't belong to a team
    actually playing today per the real ESPN schedule for that sport.
    If we have no schedule to validate against yet, don't drop anything —
    an empty schedule means "unknown", not "invalid"."""
    valid_teams = set()
    for g in todays_games or []:
        if g.get("home"): valid_teams.add(str(g["home"]).upper())
        if g.get("away"): valid_teams.add(str(g["away"]).upper())
    if not valid_teams:
        return props
    kept, dropped = [], 0
    for p in props:
        team = str(p.get("team") or "").upper()
        if not team or team in valid_teams:
            kept.append(p)
        else:
            dropped += 1
    if dropped:
        log(f"  Prop-matchup validation: dropped {dropped}/{len(props)} props (team not in today's schedule)")
    return kept

# ═══════════════════════════════════════════════════════════════════════════════
# Sports news + injuries
# ═══════════════════════════════════════════════════════════════════════════════
_INJURY_KEYWORDS = [
    "injured","out","doubtful","questionable","day-to-day","IR","scratch",
    "suspended","illness","flu","knee","ankle","shoulder","back","concussion",
    "unavailable","game-time","sidelined","hamstring","wrist","hand","elbow",
]

# ═══════════════════════════════════════════════════════════════════════════════
# Soccer (Champions League / Premier League / La Liga / Bundesliga / Serie A)
# ═══════════════════════════════════════════════════════════════════════════════
# FBref scraping (fbref.com, part of the Sports-Reference family) was retired
# 2026-08-22: its Cloudflare bot-check 403s every request from both
# residential and GitHub Actions-runner IPs, with no header/UA workaround, and
# had done so for the whole time this pipeline existed. It was never a real
# fallback source in practice, just a guaranteed-403 request plus a 2-second
# rate-limit sleep on every run before falling through to fetch_espn_soccer_
# league() anyway. See that function for the actual live data path.
# "mls" removed 2026-10-01 -- real bug found + fixed alongside the dedicated
# fetch_mls_team_stats()/fetch_mls_standings()/fetch_mls_schedule()/
# fetch_mls_rosters() calls near the bottom of main(): MLS was fully retired
# 2026-09-27, but this tuple still fed fetch_soccer_team_stats_all() a live
# ESPN fetch for MLS every "soccer"/"all" run and wrote the result into
# soccer_fbref["mls"]/docs/soccer_fbref.json, a second independent live-fetch
# path for the same retired league the dedicated-feed removal alone didn't
# touch. "bl" (Bundesliga) stays -- also retired as its own product/tab, but
# its ESPN data is still a real, needed input to the Champions League
# domestic-form blend for German clubs (see _clDomesticContext/
# _CL_DOMESTIC_LEAGUES in docs/app.html), unlike MLS which no MLS club plays
# in UEFA competitions and therefore never consumes.
_SOCCER_LEAGUES: tuple[str, ...] = ("cl", "pl", "liga", "bl", "ita")

# World Cup country name -> 3-letter code, ported directly from the frontend's
# WC26_GROUPS (docs/app.html) so it stays a single source of truth for the
# codes the engine already uses everywhere else (WC26_SCHEDULE's hc/ac
# fields, lockPick calls, etc). The Odds API returns full country names for
# its World Cup market, so this is what actually resolves them to something
# the rest of the engine can match against.
_WC26_COUNTRY_TO_CODE: dict[str, str] = {
    "Mexico": "MEX", "South Africa": "RSA", "South Korea": "KOR", "Czechia": "CZE",
    "Canada": "CAN", "Bosnia-Herzegovina": "BIH", "Qatar": "QAT", "Switzerland": "SUI",
    "Brazil": "BRA", "Morocco": "MAR", "Haiti": "HAI", "Scotland": "SCO",
    "United States": "USA", "Paraguay": "PAR", "Australia": "AUS", "Türkiye": "TUR",
    "Turkey": "TUR", "Germany": "GER", "Curaçao": "CUW", "Ivory Coast": "CIV",
    "Côte d'Ivoire": "CIV", "Ecuador": "ECU", "Netherlands": "NED", "Japan": "JPN",
    "Sweden": "SWE", "Tunisia": "TUN", "Belgium": "BEL", "Egypt": "EGY", "Iran": "IRN",
    "New Zealand": "NZL", "Spain": "ESP", "Cape Verde": "CPV", "Saudi Arabia": "KSA",
    "Uruguay": "URU", "France": "FRA", "Senegal": "SEN", "Iraq": "IRQ", "Norway": "NOR",
    "Argentina": "ARG", "Algeria": "ALG", "Austria": "AUT", "Jordan": "JOR",
    "Portugal": "POR", "Congo DR": "COD", "DR Congo": "COD", "Uzbekistan": "UZB",
    "Colombia": "COL", "England": "ENG", "Croatia": "CRO", "Ghana": "GHA", "Panama": "PAN",
}

def _wc_name_to_abbr(name: str) -> str:
    return _WC26_COUNTRY_TO_CODE.get(name, name[:3].upper())

# Club leagues (PL/La Liga/Bundesliga/MLS) key their team data
# (docs/soccer_fbref.json) by lowercased full club name, not a 3-letter
# code — see fetch_espn_soccer_league()'s `out["teams"][tname.lower()]` — so
# odds resolution for these just needs to normalize both sides the same way
# rather than needing a hand-built abbreviation table like the other
# sports. Strips the generic corporate-entity suffixes ("FC", "CF", "AFC",
# "SC") that FBref/Odds API disagree on including, so "Inter Miami CF"
# and "Inter Miami" normalize to the same key.
_SOCCER_CLUB_SUFFIXES = (" fc", " cf", " afc", " sc", " cd", " ud")

def _soccer_club_key(name: str) -> str:
    n = (name or "").strip().lower()
    for suf in _SOCCER_CLUB_SUFFIXES:
        if n.endswith(suf):
            n = n[: -len(suf)]
    return n.strip()

# _soccer_club_key() only strips short abbreviation suffixes (" fc", " sc",
# etc); it doesn't help when one source spells the suffix out ("Football
# Club" vs "FC") or uses a pure acronym for the whole name (ESPN's "lafc"
# vs mlssoccer.com's "Los Angeles Football Club" -- neither a substring of
# the other, no suffix to strip off "lafc" at all). Used to merge MLS's
# real mlssoccer.com stats with ESPN-sourced matchLog/recentForm/homeSplit/
# awaySplit data, which are keyed by each source's own naming convention.
_MLS_NAME_SUFFIXES = (" football club", " soccer club", " fc", " cf", " afc", " sc", " cd", " ud")

def _mls_name_strip(name: str) -> str:
    n = (name or "").strip().lower()
    for suf in _MLS_NAME_SUFFIXES:
        if n.endswith(suf):
            return n[: -len(suf)].strip()
    return n

def _fuzzy_mls_name_lookup(target: str, candidates: dict):
    """Find target's entry in a dict keyed by a differently-formatted name
    for the same clubs. Exact match, then suffix-stripped exact/substring
    match, then an acronym fallback (does one side's un-spaced letters
    match the initials of the other side's words) for the LAFC case
    neither of the first two resolves."""
    if target in candidates:
        return candidates[target]
    t_stripped = _mls_name_strip(target)
    for k, v in candidates.items():
        k_stripped = _mls_name_strip(k)
        if k_stripped == t_stripped or k_stripped in t_stripped or t_stripped in k_stripped:
            return v
    t_flat = target.replace(" ", "")
    for k, v in candidates.items():
        k_flat = k.replace(" ", "")
        k_initials = "".join(w[0] for w in k.split() if w)
        t_initials = "".join(w[0] for w in target.split() if w)
        if k_initials == t_flat or t_initials == k_flat:
            return v
    return None

# NOTE: scripts/scrape_soccer_standings.py has its own standalone copy of
# this exact dict (deliberately -- it's a separate daily cron with no
# import dependency on this file, same convention as scrape_opta_stats.py).
# If a league gets added/renamed/removed here, update it there too, or the
# two will silently drift on which leagues get a standings snapshot.
ESPN_SOCCER_LEAGUES: dict[str, dict] = {
    "cl":   {"name": "Champions League", "espn": "UEFA.champions"},
    "pl":   {"name": "Premier League",   "espn": "eng.1"},
    "liga": {"name": "La Liga",          "espn": "esp.1"},
    "bl":   {"name": "Bundesliga",       "espn": "ger.1"},
    "mls":  {"name": "MLS",              "espn": "usa.1"},
    "ita":  {"name": "Serie A",          "espn": "ita.1"},
}

def _espn_team_season_stats(espn_league: str, tid: str, season_year: int) -> tuple[dict, int]:
    """
    Fetch one team's per-season aggregate stats from ESPN's core API and
    return (flat_stats_dict, games_played). games_played is derived from
    wins+draws+losses (not "appearances", which is aggregated across the
    whole roster on this endpoint, not the team's own match count) --
    returns 0 (not the old mp=1 fallback) when the team has no record for
    this season at all, e.g. a club that wasn't in the top flight yet
    (newly promoted) or a season that hasn't started. Callers decide what
    a 0 means; folding a silent "assume 1 game" fallback in here made it
    indistinguishable from a team that's actually played exactly 1 game.
    """
    stats = fetch_json(
        f"https://sports.core.api.espn.com/v2/sports/soccer/leagues/{espn_league}/seasons/{season_year}/types/1/teams/{tid}/statistics",
        quiet_404=True,   # 404 = this club has no record that season (newly promoted / season not started): documented above, not a failure
    )
    cats = ((stats or {}).get("splits") or {}).get("categories") or []
    flat: dict = {}
    for cat in cats:
        for s in cat.get("stats", []):
            flat[s.get("name")] = s.get("value")
    gp = (flat.get("wins", 0) or 0) + (flat.get("draws", 0) or 0) + (flat.get("losses", 0) or 0)
    return flat, int(gp)


def _espn_team_match_log(espn_league: str, tid: str, seasons: list[int]) -> list[dict]:
    """
    Compact completed-match history for one team across the given season
    years, oldest first: date, opponent name, home/away, goals for/against.
    This one flat list is the single source that recent-form, home/away
    splits, and head-to-head all derive from -- computed here once per
    team (cheap: one list-filter each) or left raw for the frontend to
    filter by a specific opponent at render time (head-to-head is
    fixture-specific -- precomputing every possible opponent pairing
    server-side would be O(teams^2) per league for no benefit, since only
    one specific matchup needs to be looked up per card rendered).
    """
    games: list[dict] = []
    for season in seasons:
        data = fetch_json(
            f"https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_league}/teams/{tid}/schedule",
            params={"season": season},
        )
        for ev in (data or {}).get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            status = (comp.get("status") or {}).get("type") or {}
            if not status.get("completed"):
                continue
            competitors = comp.get("competitors") or []
            me  = next((c for c in competitors if str(c.get("team", {}).get("id")) == str(tid)), None)
            opp = next((c for c in competitors if c is not me), None)
            if not me or not opp:
                continue
            gf = (me.get("score") or {}).get("value")
            ga = (opp.get("score") or {}).get("value")
            if gf is None or ga is None:
                continue
            games.append({
                "date": ev.get("date", ""),
                "opponent": (opp.get("team") or {}).get("displayName", ""),
                "homeAway": me.get("homeAway", ""),
                "gf": gf,
                "ga": ga,
                "season": season,
            })
    games.sort(key=lambda g: g["date"])
    return games


def _summarize_match_log(games: list[dict], cur_year: int, w_cur: float, w_prev: float) -> dict:
    """Derive recent-form (last 5, any venue) and home/away splits from a
    team's match log. All three of the model's new form-based signals come
    from this one summary; head-to-head is the only one that needs the raw
    per-opponent match log itself (kept separately, see matchLog below).

    recentForm is deliberately NOT season-blended -- it's the literal last
    5 games in chronological order regardless of season boundary, correct
    for tracking a streak (form can carry across a season transition), not
    a "how much do we trust this season's sample yet" question.

    homeSplit/awaySplit DO blend current-season vs previous-season using
    the same w_cur/w_prev weight the caller already computed for this
    team's goals/xG numbers -- without this, a team's home-fortress
    reading stayed an unweighted flat pool of 2 full seasons even once
    the rest of that team's profile had shifted to mostly-current-season,
    silently keeping the split stale-season-heavy well past the point the
    goals blend had already floored at _SEASON_BLEND_FLOOR_WEIGHT.
    """
    def _agg(rows: list[dict]) -> dict:
        n = len(rows)
        if not n:
            return {"games": 0, "w": 0, "d": 0, "l": 0, "gf": 0.0, "ga": 0.0}
        w = sum(1 for g in rows if g["gf"] > g["ga"])
        d = sum(1 for g in rows if g["gf"] == g["ga"])
        l = n - w - d
        return {
            "games": n, "w": w, "d": d, "l": l,
            "gf": round(sum(g["gf"] for g in rows) / n, 3),
            "ga": round(sum(g["ga"] for g in rows) / n, 3),
        }

    def _agg_blended(rows: list[dict]) -> dict:
        cur_agg = _agg([g for g in rows if g.get("season") == cur_year])
        prev_agg = _agg([g for g in rows if g.get("season") != cur_year])
        if not cur_agg["games"]:
            return prev_agg
        if not prev_agg["games"]:
            return cur_agg
        return {
            "games": cur_agg["games"] + prev_agg["games"],
            "w": cur_agg["w"] + prev_agg["w"], "d": cur_agg["d"] + prev_agg["d"], "l": cur_agg["l"] + prev_agg["l"],
            "gf": round(cur_agg["gf"] * w_cur + prev_agg["gf"] * w_prev, 3),
            "ga": round(cur_agg["ga"] * w_cur + prev_agg["ga"] * w_prev, 3),
        }

    last5 = games[-5:]
    home = [g for g in games if g["homeAway"] == "home"]
    away = [g for g in games if g["homeAway"] == "away"]
    return {"recentForm": _agg(last5), "homeSplit": _agg_blended(home), "awaySplit": _agg_blended(away)}


# Season-blend tuning for the 4 major European leagues (not Champions
# League -- its "current" season is still last season's completed
# tournament until the new one starts in September, so there's nothing new
# to blend in yet; not MLS -- it has its own dedicated real-xG source,
# fetch_mls_team_stats(), that overwrites this function's output entirely).
# A team's stats blend current-season with last-season, ramping from 100%
# last-season at 0 games played this year down to a 15% floor by 10 games
# played -- and staying at that 15% floor for the rest of the season, not
# dropping to zero. Explicit direction: prior-season form should always
# still count for something, just progressively less as the current
# season's own sample grows, never fully discarded.
_SEASON_BLEND_LEAGUES = {"pl", "liga", "bl", "ita"}
_SEASON_BLEND_RAMP_GAMES = 10
_SEASON_BLEND_FLOOR_WEIGHT = 0.15

def fetch_espn_soccer_league(key: str) -> dict:
    """
    Primary (and only, as of 2026-08-22 -- see below) source for one
    league's soccer team stats. ESPN's soccer.core API doesn't expose true
    xG, so 'xg'/'npxg' here are goals-per-game proxies rather than
    shot-quality models -- weaker signal than real xG, but a real, live one.

    FBref scraping was retired entirely: FBref's Cloudflare bot-check 403s
    every scrape attempt from both residential and GitHub Actions-runner
    IPs, with no header/UA workaround, and had done so for the whole time
    this pipeline existed -- there was no live FBref data path to fall back
    from, just permanent dead weight (a guaranteed-to-fail request, a
    2-second rate-limit sleep, and an HTML table parser) on every run. See
    fetch_soccer_team_stats_all() for what replaced it.

    Season handling: the per-team statistics endpoint needs an explicit
    season year and does NOT default to "current" -- this used to be
    hardcoded to 2025 (the 2025-26 season), which meant every returning
    club's stats were permanently frozen at their final 2025-26 numbers no
    matter how far the 2026-27 season actually progressed, and every newly
    promoted club (not in the 2025-26 top flight at all) came back as an
    essentially-empty record. Now reads the actual current season year off
    the standings response (already being fetched for team IDs) instead of
    a fixed number, so this keeps advancing every future season with no
    further code changes.

    Season blending: see _SEASON_BLEND_* constants above. A newly-promoted
    team has no meaningful "last season" top-flight record to blend with
    (its prior-season fetch comes back with 0 games played at this level),
    so it just runs on however many current-season games it has, unblended,
    rather than blending in a false last-season reading of a division it
    wasn't even competing in.
    """
    cfg = ESPN_SOCCER_LEAGUES.get(key)
    if not cfg:
        return {}
    log(f"ESPN soccer fallback {cfg['name']}…")
    out: dict = {"league": cfg["name"], "fetchedAt": TODAY_ISO, "teams": {}}
    try:
        standings = fetch_json(f"https://site.api.espn.com/apis/v2/sports/soccer/{cfg['espn']}/standings")
        if not standings:
            return out
        cur_year = (standings.get("season") or {}).get("year") or datetime.now(timezone.utc).year
        prev_year = cur_year - 1
        blend_league = key in _SEASON_BLEND_LEAGUES
        team_ids: dict[str, str] = {}
        for child in (standings.get("children") or [standings]):
            for entry in ((child.get("standings") or {}).get("entries") or []):
                tid = entry.get("team", {}).get("id")
                tname = entry.get("team", {}).get("displayName")
                if tid and tname:
                    team_ids[tid] = tname
        for tid, tname in team_ids.items():
            try:
                time.sleep(0.3)
                cur, mp_cur = _espn_team_season_stats(cfg["espn"], tid, cur_year)

                # Always pull last season for blend-eligible leagues, not
                # just early in the season -- the 15% floor applies for the
                # whole season, not only before some games-played cutoff.
                prev, mp_prev = ({}, 0)
                if blend_league:
                    time.sleep(0.3)
                    prev, mp_prev = _espn_team_season_stats(cfg["espn"], tid, prev_year)

                def _rate(flat: dict, field: str, mp: int) -> float:
                    return (flat.get(field, 0) or 0) / mp if mp else 0.0

                if mp_prev > 0 and blend_league:
                    # Ramp from 100% last-season at 0 games played this
                    # season down to the 15% floor at _SEASON_BLEND_RAMP_GAMES
                    # (10) games played, then hold at that floor -- never
                    # fully drops to 0% weight on last season.
                    ramp = min(mp_cur, _SEASON_BLEND_RAMP_GAMES) / _SEASON_BLEND_RAMP_GAMES
                    w_prev = max(_SEASON_BLEND_FLOOR_WEIGHT, 1 - (1 - _SEASON_BLEND_FLOOR_WEIGHT) * ramp)
                    w_cur = 1 - w_prev
                    def blend(field: str) -> float:
                        return _rate(cur, field, mp_cur) * w_cur + _rate(prev, field, mp_prev) * w_prev
                    gf_pg  = blend("totalGoals")
                    ga_pg  = blend("goalsConceded")
                    xag_pg = blend("goalAssists")
                    shots_pg = blend("totalShots")
                    sot_pg   = blend("shotsOnTarget")
                    poss = (cur.get("possessionPct") or 0)*w_cur + (prev.get("possessionPct") or 0)*w_prev
                    src_tag = "espn+prior-season-blend"
                else:
                    # Not a blend-eligible league, or no usable prior-season
                    # record (newly promoted) -- current season only.
                    w_cur, w_prev = 1.0, 0.0
                    gf_pg  = _rate(cur, "totalGoals", mp_cur)
                    ga_pg  = _rate(cur, "goalsConceded", mp_cur)
                    xag_pg = _rate(cur, "goalAssists", mp_cur)
                    shots_pg = _rate(cur, "totalShots", mp_cur)
                    sot_pg   = _rate(cur, "shotsOnTarget", mp_cur)
                    poss = cur.get("possessionPct") or 0
                    src_tag = "espn"

                # Match log (current + previous season) -- the single
                # source recent form, home/away splits, and head-to-head
                # all derive from. Kept raw (not just the summary) so the
                # frontend can filter it against a specific opponent for
                # head-to-head at render time.
                time.sleep(0.3)
                match_log = _espn_team_match_log(cfg["espn"], tid, [cur_year, prev_year])
                # homeSplit/awaySplit blend with the SAME w_cur/w_prev
                # weight the goals/xG numbers above just used, so a team's
                # home-fortress reading doesn't stay pooled across 2 full
                # seasons flat while everything else about that team has
                # already shifted to mostly-current-season. recentForm
                # deliberately does NOT get this treatment -- it's the
                # literal last 5 games in chronological order regardless
                # of season boundary, which is correct for tracking a
                # streak (form can carry across a season transition), not
                # a "how much do we trust this season's sample yet" question.
                form = _summarize_match_log(match_log, cur_year, w_cur, w_prev)

                # Store as season totals at the real current games-played
                # (not a nominal 1) so mp reflects reality everywhere it's
                # displayed; a still-winless week-1 team with mp=0 gets
                # mp=1 purely so downstream code that divides by mp doesn't
                # divide by zero -- the totals below already bake in the
                # blended per-game rate regardless of what mp says.
                eff_mp = max(mp_cur, 1)
                out["teams"][tname.lower()] = {
                    "mp": int(eff_mp),
                    "poss": round(poss, 2),
                    "gf": round(gf_pg * eff_mp, 2),
                    # Season totals, NOT per-game — this is what the
                    # frontend's _socXGFromFBref() expects: it divides by mp
                    # itself (t.xg/mp) to get the per-game rate.
                    "xg": round(gf_pg * eff_mp, 2),     # proxy, not true xG
                    "npxg": round(gf_pg * eff_mp, 2),   # proxy, not true xG
                    "xag": round(xag_pg * eff_mp, 2),
                    "prg_passes": cur.get("accuratePasses", 0) or 0,
                    "ga": round(ga_pg * eff_mp, 2),
                    "xga": round(ga_pg * eff_mp, 2),    # proxy, not true xG
                    "shots_pg": round(shots_pg, 2),
                    "sot_pg": round(sot_pg, 2),
                    "gamesPlayedThisSeason": mp_cur,
                    "recentForm": form["recentForm"],
                    "homeSplit": form["homeSplit"],
                    "awaySplit": form["awaySplit"],
                    "matchLog": match_log,
                    "src": src_tag,
                }
            except Exception as exc:
                vlog(f"  ESPN soccer team {tname}: {exc}")
        log(f"  ESPN soccer fallback {cfg['name']}: {len(out['teams'])} teams (season {cur_year})")
    except Exception as exc:
        log(f"ESPN soccer fallback {cfg['name']}: {exc}", "WARN")
    return out

def fetch_soccer_team_stats_all() -> dict:
    """
    Fetch every league in _SOCCER_LEAGUES' team stats via fetch_espn_soccer_
    league() -- see that function's docstring for the season-blend logic and
    why FBref (formerly tried first here) was retired entirely rather than
    kept as a dead-weight first attempt. (5 leagues as of 2026-10-01 -- MLS
    removed from _SOCCER_LEAGUES itself; see that tuple's own comment.)
    """
    result: dict = {}
    for key in _SOCCER_LEAGUES:
        result[key] = fetch_espn_soccer_league(key)
        time.sleep(3)  # courtesy delay between leagues on top of per-call sleeps
    return result


MLS_COMPETITION_ID = "MLS-COM-000001"
MLS_SEASON_ID      = "MLS-SEA-0001KA"   # 2026 MLS regular season

def fetch_mls_team_stats() -> dict:
    """
    Full-season MLS club statistics straight from mlssoccer.com's own stats
    API (stats-api.mlssoccer.com) — unauthenticated, undocumented but public,
    the same endpoint the site's own club-stats page calls. One request
    returns all 144 stat fields (general/passing/attacking/defending are all
    the same payload client-side — the site's stat_type tabs just re-filter
    the same response) for all 30 clubs, including real xG (not a proxy like
    the ESPN soccer fallback uses for the other leagues), shot locations,
    pass completion by distance band, aerials, interceptions, and more —
    this is the primary MLS data source now; ESPN/FBref remain fallbacks.
    """
    log("MLS club stats (mlssoccer.com)…")
    result: dict = {"fetchedAt": TODAY_ISO, "teams": {}}
    try:
        data = fetch_json(
            f"https://stats-api.mlssoccer.com/statistics/clubs/competitions/{MLS_COMPETITION_ID}/seasons/{MLS_SEASON_ID}",
            params={"per_page": 50},
        )
        for t in (data or {}).get("team_statistics", []):
            name = t.get("team_name")
            if not name:
                continue
            mp = t.get("matches_played") or 1
            result["teams"][name.lower()] = {
                "team_id": t.get("team_id"),
                "code": t.get("three_letter_code"),
                "mp": mp,
                "goals": t.get("goals", 0),
                "goals_conceded": t.get("goals_conceded", 0),
                "xG": t.get("xG", 0),
                "xG_per_game": round((t.get("xG") or 0) / mp, 2),
                "xG_efficiency": t.get("xG_efficiency"),
                "shots": t.get("shots_at_goal_sum", 0),
                "shots_on_target": t.get("shots_on_target", 0),
                "shots_conversion_rate": t.get("shots_conversion_rate"),
                "shots_faced": t.get("shots_faced", 0),
                "goalkeeper_saves": t.get("goalkeeper_saves", 0),
                "clean_sheets": t.get("clean_sheets", 0),
                "possession_ratio": t.get("possession_ratio"),
                "passes_sum": t.get("passes_sum", 0),
                "passes_conversion_rate": t.get("passes_conversion_rate"),
                "passes_from_open_play_short_successful_ratio": t.get("passes_from_open_play_short_successful_ratio"),
                "passes_from_open_play_medium_successful_ratio": t.get("passes_from_open_play_medium_successful_ratio"),
                "passes_from_open_play_long_successful_ratio": t.get("passes_from_open_play_long_successful_ratio"),
                "crosses_conversion_rate": t.get("crosses_conversion_rate"),
                "corner_kicks_sum": t.get("corner_kicks_sum", 0),
                "free_kicks_sum": t.get("free_kicks_sum", 0),
                "penalties_sum": t.get("penalties_sum", 0),
                "penalty_conversion_rate": t.get("penalty_conversion_rate"),
                "penalties_saved": t.get("penalties_saved", 0),
                "fouls_sum": t.get("fouls_sum", 0),
                "fouls_suffered": t.get("fouls_suffered", 0),
                "cards_yellow": t.get("cards_yellow", 0),
                "cards_red": t.get("cards_red", 0),
                "offsides": t.get("offsides", 0),
                "tackling_games_air_won": t.get("tackling_games_air_won", 0),
                "tackling_games_air_sum": t.get("tackling_games_air_sum", 0),
                "interceptions_sum": t.get("interceptions_sum", 0),
                "defensive_clearances": t.get("defensive_clearances", 0),
                "counter_attacks": t.get("counter_attacks", 0),
                "chances": t.get("chances", 0),
                "sitters": t.get("sitters", 0),
                "goal_opportunities": t.get("goal_opportunities", 0),
                "assists": t.get("assists", 0),
                "distance_covered": t.get("distance_covered"),
                "src": "mlssoccer",
            }
        log(f"  MLS club stats: {len(result['teams'])} teams")
    except Exception as exc:
        log(f"MLS club stats: {exc}", "WARN")
    return result

def fetch_mls_standings() -> list[dict]:
    """MLS regular-season table (both conferences combined) from mlssoccer.com."""
    log("MLS standings (mlssoccer.com)…")
    out: list[dict] = []
    try:
        data = fetch_json(f"https://stats-api.mlssoccer.com/competitions/{MLS_COMPETITION_ID}/seasons/{MLS_SEASON_ID}/standings")
        for table in (data or {}).get("tables", []):
            for e in table.get("entries", []):
                out.append({
                    "position": e.get("position"), "team": e.get("team"), "team_id": e.get("team_id"),
                    "code": e.get("team_three_letter_code"),
                    "gp": e.get("games_played"), "w": e.get("wins"), "d": e.get("draws"), "l": e.get("losses"),
                    "gf": e.get("goals_scored"), "ga": e.get("goals_against"), "gd": e.get("goals_difference"),
                    "pts": e.get("points"), "ppg": e.get("points_per_game"),
                })
        log(f"  MLS standings: {len(out)} teams")
    except Exception as exc:
        log(f"MLS standings: {exc}", "WARN")
    return out

def fetch_mls_rosters() -> dict:
    """
    Soccer injury-integration, scoped to MLS only: of the 5 tracked leagues,
    MLS is the only one actually in-season right now (CL/PL/La Liga/
    Bundesliga are all confirmed empty on injuries — offseason — so building
    150-team roster coverage across all 5 for zero real signal isn't worth
    it yet; revisit once those leagues resume in August). ESPN's team-
    roster endpoint gives name/team/position for all 30 MLS clubs in one
    request per team — no separate stat-based rating needed,
    computeInjuryImpact()'s soccer branch will key off position (GK
    highest impact, then DEF/MID/FWD).
    """
    log("MLS rosters (ESPN)…")
    result: dict = {}
    try:
        teams_data = fetch_json("https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/teams?limit=40")
        teams = ((teams_data or {}).get("sports") or [{}])[0].get("leagues", [{}])[0].get("teams", [])
        for t in teams:
            tm = t.get("team", {})
            team_id, abbr = tm.get("id"), tm.get("abbreviation", "")
            if not team_id or not abbr:
                continue
            try:
                time.sleep(0.2)
                roster = fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/teams/{team_id}/roster")
                for p in (roster or {}).get("athletes", []):
                    name = p.get("fullName", "")
                    pos = (p.get("position") or {}).get("abbreviation", "") or (p.get("position") or {}).get("name", "")
                    if name:
                        result[name.lower()] = {"team": abbr, "pos": pos}
            except Exception as exc:
                log(f"MLS roster {abbr}: {exc}", "WARN")
        log(f"  MLS rosters: {len(result)} players across {len(teams)} teams")
    except Exception as exc:
        log(f"MLS rosters: {exc}", "WARN")
    return result

def fetch_espn_soccer_rosters(espn_path: str, league_name: str) -> dict:
    """
    Generic version of fetch_mls_rosters() for the other 4 tracked soccer
    leagues (Champions League/Premier League/La Liga/Bundesliga) — same
    ESPN team-list -> per-team-roster pattern, just parameterized on the
    league's ESPN path instead of hardcoded to usa.1. These leagues already
    have a real win-probability model (_soccerMC's xG/Poisson engine in
    app.html, same one MLS uses) — the only gap was roster data for
    computeInjuryImpact() to key off, which this closes.
    """
    log(f"{league_name} rosters (ESPN)…")
    result: dict = {}
    try:
        teams_data = fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_path}/teams?limit=40")
        teams = ((teams_data or {}).get("sports") or [{}])[0].get("leagues", [{}])[0].get("teams", [])
        for t in teams:
            tm = t.get("team", {})
            team_id, abbr = tm.get("id"), tm.get("abbreviation", "")
            if not team_id or not abbr:
                continue
            try:
                time.sleep(0.2)
                roster = fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_path}/teams/{team_id}/roster")
                for p in (roster or {}).get("athletes", []):
                    name = p.get("fullName", "")
                    pos = (p.get("position") or {}).get("abbreviation", "") or (p.get("position") or {}).get("name", "")
                    if name:
                        result[name.lower()] = {"team": abbr, "pos": pos}
            except Exception as exc:
                log(f"  {league_name} roster {abbr}: {exc}", "WARN")
        log(f"  {league_name} rosters: {len(result)} players across {len(teams)} teams")
    except Exception as exc:
        log(f"{league_name} rosters: {exc}", "WARN")
    return result

def fetch_mls_schedule(days_forward: int = 20, days_back: int = 3) -> list[dict]:
    """
    MLS schedule from mlssoccer.com, mirroring how the site's own Schedule &
    Scores page paginates — one match_date at a time (the API's range query
    param is broken/unsupported server-side; single-date is the only mode
    that returns real data), rolled up into a 7-ish-day-forward window plus
    a short lookback so recently-completed results stay visible for model
    calibration. competition_id is pinned to MLS regular season only, so
    MLS NEXT Pro reserve-team matches (a separate competition on the same
    API) don't leak into the first-team schedule.
    """
    log(f"MLS schedule ({days_back}d back, {days_forward}d forward, mlssoccer.com)…")
    out: list[dict] = []
    start = NOW.date() - timedelta(days=days_back)
    for i in range(days_back + days_forward + 1):
        d = (start + timedelta(days=i)).isoformat()
        try:
            # A plain requests call (not fetch_json) on purpose: a 404 here
            # just means "no MLS match that day" (e.g. the World Cup break),
            # not a real failure, so it shouldn't retry or log a WARN for
            # every quiet day in the window.
            r = _session.get(
                f"https://stats-api.mlssoccer.com/matches/seasons/{MLS_SEASON_ID}",
                params={"match_date": d, "competition_id": MLS_COMPETITION_ID, "per_page": 20},
                timeout=15,
            )
            data = r.json() if r.status_code == 200 else {}
            for m in (data or {}).get("schedule", []):
                out.append({
                    "match_id": m.get("match_id"), "date": d,
                    "kickoff": m.get("planned_kickoff_time"),
                    "home": m.get("home_team_name"), "away": m.get("away_team_name"),
                    "home_id": m.get("home_team_id"), "away_id": m.get("away_team_id"),
                    "home_code": m.get("home_team_three_letter_code"), "away_code": m.get("away_team_three_letter_code"),
                    "status": m.get("match_status"), "result": m.get("result"),
                    "home_goals": m.get("home_team_goals"), "away_goals": m.get("away_team_goals"),
                    "match_day": m.get("match_day"), "match_type": m.get("match_type"),
                    "stadium": m.get("stadium_name"), "city": m.get("stadium_city"),
                })
            time.sleep(0.25)
        except Exception as exc:
            vlog(f"  MLS schedule {d}: {exc}")
    log(f"  MLS schedule: {len(out)} matches across {days_back+days_forward+1} days")
    return out

def fetch_ncaa_baseball() -> dict:
    """Fetch NCAA Men's Baseball scoreboard, rankings, standings, 14-day schedule from ESPN."""
    log("NCAA Baseball scoreboard…")
    result = {"today": [], "rankings": [], "schedule": [], "weekSchedule": [], "standings": [], "conferenceStandings": {}}
    try:
        data = fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/baseball/college-baseball/scoreboard?dates={TODAY_ET}&limit=25")
        for ev in (data or {}).get("events", []):
            g = _espn_game(ev, "NCAAB")
            if g: result["today"].append(g)
        log(f"NCAA Baseball today: {len(result['today'])} games")
    except Exception as e: log(f"NCAA Baseball scoreboard: {e}", "WARN")
    try:
        rdata = fetch_json("https://site.api.espn.com/apis/site/v2/sports/baseball/college-baseball/rankings")
        for poll in ((rdata or {}).get("rankings") or [])[:1]:
            for r in (poll.get("ranks") or [])[:25]:
                t = r.get("team", {})
                result["rankings"].append({
                    "rank": r.get("current", 0),
                    "team": t.get("displayName", ""),
                    "abbr": t.get("abbreviation", ""),
                    "record": r.get("recordSummary", ""),
                    "logo": (t.get("logos") or [{}])[0].get("href","") if t.get("logos") else "",
                })
        log(f"NCAA Baseball rankings: {len(result['rankings'])}")
    except Exception as e: log(f"NCAA Baseball rankings: {e}", "WARN")
    try:
        # Conference standings
        sdata = fetch_json("https://site.api.espn.com/apis/site/v2/sports/baseball/college-baseball/standings")
        for conf in ((sdata or {}).get("children") or []):
            cname = conf.get("name","")
            entries = []
            for entry in (conf.get("standings",{}).get("entries") or []):
                t = entry.get("team",{})
                stats = {s["name"]:s.get("displayValue","") for s in entry.get("stats",[])}
                entries.append({
                    "team": t.get("displayName",""), "abbr": t.get("abbreviation",""),
                    "w": stats.get("wins","0"), "l": stats.get("losses","0"),
                    "pct": stats.get("winPercent",""), "confW": stats.get("conferenceWins",""),
                    "confL": stats.get("conferenceLosses",""),
                })
            if entries:
                result["conferenceStandings"][cname] = entries
                result["standings"].extend(entries)
        log(f"NCAA Baseball conf standings: {len(result['conferenceStandings'])} conferences")
    except Exception as e: log(f"NCAA Baseball standings: {e}", "WARN")
    try:
        # 14-day schedule
        from datetime import datetime, timedelta
        for i in range(14):
            d = (datetime.now() + timedelta(days=i)).strftime("%Y%m%d")
            sdata = fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/baseball/college-baseball/scoreboard?dates={d}&limit=15")
            for ev in (sdata or {}).get("events", [])[:8]:
                comp = (ev.get("competitions") or [{}])[0]
                comps = comp.get("competitors") or []
                h = next((c for c in comps if c.get("homeAway")=="home"), {})
                a = next((c for c in comps if c.get("homeAway")=="away"), {})
                ht = h.get("team") or {}; at = a.get("team") or {}
                entry = {
                    "date": d,
                    "home": ht.get("displayName",""), "homeAbbr": ht.get("abbreviation",""),
                    "homeRecord": (h.get("records") or [{}])[0].get("summary","") if h.get("records") else "",
                    "away": at.get("displayName",""), "awayAbbr": at.get("abbreviation",""),
                    "awayRecord": (a.get("records") or [{}])[0].get("summary","") if a.get("records") else "",
                    "state": ev.get("status",{}).get("type",{}).get("state","pre"),
                    "homeScore": h.get("score",""), "awayScore": a.get("score",""),
                    "venue": (comp.get("venue") or {}).get("fullName",""),
                    "network": (comp.get("broadcasts") or [{}])[0].get("names",[""])[0] if comp.get("broadcasts") else "",
                }
                result["weekSchedule"].append(entry)
                if i < 7:
                    result["schedule"].append(entry)
            time.sleep(0.15)
        log(f"NCAA Baseball 14d schedule: {len(result['weekSchedule'])} games")
    except Exception as e: log(f"NCAA Baseball schedule: {e}", "WARN")
    return result


def fetch_wnba_player_stats() -> list:
    """Scrape WNBA 2026 per-game + advanced player stats from Basketball Reference.
    BBRef WNBA uses table id='per_game' and player name is in <th data-stat='player'><a>.
    """
    if WNBA_OFFSEASON:
        return []
    YEAR = NOW_MT.year  # WNBA season year (retired via WNBA_OFFSEASON; derived so reactivating needs no edit)
    players: dict[str, dict] = {}

    def _parse_wnba_table(soup, tbl_id: str) -> list[dict]:
        """Parse a BBRef WNBA player table using th[data-stat=player] + td[data-stat]."""
        tbl = soup.find("table", id=tbl_id)
        if not tbl:
            log(f"  Table {tbl_id!r} not found", "WARN")
            return []
        rows_out = []
        for row in tbl.find_all("tr"):
            # Player name is in <th data-stat='player'><a>
            th = row.find("th", attrs={"data-stat": "player"})
            if not th:
                continue
            a = th.find("a")
            name = a.get_text(strip=True) if a else th.get_text(strip=True)
            # Skip header rows
            if not name or name in ("Player", "Rk", ""):
                continue
            row_data: dict = {"player": name}
            for td in row.find_all("td"):
                stat = td.get("data-stat", "")
                val  = td.get_text(strip=True)
                if stat:
                    row_data[stat] = val
            rows_out.append(row_data)
        return rows_out

    # ── Per-game stats ──────────────────────────────────────────────────────
    try:
        log("WNBA per-game (BBRef)…")
        time.sleep(2)
        soup = fetch_html(
            f"https://www.basketball-reference.com/wnba/years/{YEAR}_per_game.html",
            ref=True,
        )
        if soup:
            for r in _parse_wnba_table(soup, "per_game"):
                name = r["player"]
                try:
                    players[name] = {
                        "name":     name,
                        "team":     r.get("team", ""),
                        "pos":      r.get("pos", ""),
                        "g":        int(float(r.get("g", 0) or 0)),
                        "mp":       float(r.get("mp_per_g", 0) or 0),
                        "pts":      float(r.get("pts_per_g", 0) or 0),
                        "reb":      float(r.get("trb_per_g", 0) or 0),
                        "ast":      float(r.get("ast_per_g", 0) or 0),
                        "stl":      float(r.get("stl_per_g", 0) or 0),
                        "blk":      float(r.get("blk_per_g", 0) or 0),
                        "tov":      float(r.get("tov_per_g", 0) or 0),
                        "fg_pct":   float(r.get("fg_pct", 0) or 0),
                        "fg3_pct":  float(r.get("fg3_pct", 0) or 0),
                        "ft_pct":   float(r.get("ft_pct", 0) or 0),
                        "ts_pct":   0.0,
                        "usg_pct":  0.0,
                    }
                except (ValueError, TypeError):
                    continue
        log(f"WNBA per-game: {len(players)} players")
    except Exception as e:
        log(f"WNBA per-game: {e}", "WARN")

    # ── Advanced stats (PER, TS%, USG%, eFG%, BPM) ─────────────────────────
    try:
        log("WNBA advanced (BBRef)…")
        time.sleep(2.5)
        soup2 = fetch_html(
            f"https://www.basketball-reference.com/wnba/years/{YEAR}_advanced.html",
            ref=True,
        )
        if soup2:
            for r in _parse_wnba_table(soup2, "advanced"):
                name = r["player"]
                if name not in players:
                    continue
                try:
                    players[name]["per"]     = float(r.get("per", 0) or 0)
                    players[name]["ts_pct"]  = float(r.get("ts_pct", 0) or 0)
                    players[name]["usg_pct"] = float(r.get("usg_pct", 0) or 0)
                    players[name]["efg_pct"] = float(r.get("efg_pct", 0) or 0)
                    players[name]["bpm"]     = float(r.get("bpm", 0) or 0)
                    players[name]["obpm"]    = float(r.get("obpm", 0) or 0)
                    players[name]["dbpm"]    = float(r.get("dbpm", 0) or 0)
                except (ValueError, TypeError):
                    continue
        log(f"WNBA advanced: enriched {sum(1 for p in players.values() if p.get('ts_pct',0)>0)} players")
    except Exception as e:
        log(f"WNBA advanced: {e}", "WARN")

    # Rating tier — closes the WNBA injury-integration gap the same way MLB's
    # batter rosters did: computeInjuryImpact() needs a PREMIUM/OPTIMAL/GOOD
    # tier per player to weight an injury's win-probability impact, and
    # unlike NBA_PLAYERS (hand-curated, static) this is computed directly
    # from the real per-game/advanced stats already scraped above. WNBA
    # scoring/usage run lower than NBA, so thresholds are scaled down rather
    # than reusing NBA's cutoffs verbatim.
    for p in players.values():
        ppg, usg, per = p.get("pts", 0), p.get("usg_pct", 0), p.get("per", 0)
        if ppg >= 18 and usg >= 26:
            p["rating"] = "PREMIUM"
        elif ppg >= 13 or per >= 16:
            p["rating"] = "OPTIMAL"
        elif ppg >= 7:
            p["rating"] = "GOOD"
        else:
            p["rating"] = "FAIR"

    result = list(players.values())
    log(f"WNBA players total: {len(result)}")
    return result

def fetch_wnba_team_stats() -> dict:
    """Scrape WNBA 2026 team stats from BBRef using correct 2026 table IDs.
    Main page: advanced-team (ORtg, DRtg, NRtg, Pace, eFG%, TOV%, ORB%)
               per_game-team (pts, fg%, 3p%, ft%)
               per_game-opponent (opp pts, opp fg%)
    """
    if WNBA_OFFSEASON:
        return {}
    YEAR = NOW_MT.year  # WNBA season year (retired via WNBA_OFFSEASON)
    result: dict = {}
    try:
        log("WNBA team stats (BBRef 2026)…")
        time.sleep(2)
        soup = fetch_html(
            f"https://www.basketball-reference.com/wnba/years/{YEAR}.html",
            ref=True,
        )
        if not soup:
            log("WNBA team stats: soup is None", "WARN")
            return result

        # ── Advanced team table (ORtg, DRtg, NRtg, Pace, eFG%, etc.) ──────
        adv = soup.find("table", id="advanced-team")
        if adv:
            for row in adv.find_all("tr"):
                cells = {
                    c.get("data-stat", ""): c.get_text(strip=True)
                    for c in row.find_all(["th", "td"])
                    if c.get("data-stat")
                }
                team_name = cells.get("team", "")
                if not team_name or team_name in ("Team", ""):
                    continue
                # Derive abbreviation from anchor href
                team_a = row.find("td", {"data-stat": "team"})
                a_tag  = team_a.find("a") if team_a else None
                abbr   = a_tag["href"].split("/")[3].upper() if (a_tag and "href" in a_tag.attrs) else team_name[:3].upper()
                try:
                    entry = {
                        "name":     team_name,
                        "abbr":     abbr,
                        "w":        int(cells.get("wins", 0) or 0),
                        "l":        int(cells.get("losses", 0) or 0),
                        "mov":      float(cells.get("mov", 0) or 0),
                        "srs":      float(cells.get("srs", 0) or 0),
                        "ortg":     float(cells.get("off_rtg", 0) or 0),
                        "drtg":     float(cells.get("def_rtg", 0) or 0),
                        "net_rtg":  float((cells.get("net_rtg") or "0").replace("+", "") or 0),
                        "pace":     float(cells.get("pace", 0) or 0),
                        "ts_pct":   float(cells.get("ts_pct", 0) or 0),
                        "efg_pct":  float(cells.get("efg_pct", 0) or 0),
                        "tov_pct":  float(cells.get("tov_pct", 0) or 0),
                        "orb_pct":  float(cells.get("orb_pct", 0) or 0),
                        "ft_rate":  float(cells.get("fta_per_fga_pct", 0) or 0),
                        "opp_efg":  float(cells.get("opp_efg_pct", 0) or 0),
                        "pts_pg":   0.0,
                        "opp_pts":  0.0,
                        "fg_pct":   0.0,
                    }
                    if entry["ortg"] > 0:
                        result[abbr] = entry
                except (ValueError, TypeError):
                    continue
        log(f"WNBA team adv stats: {len(result)} teams")

        # ── Per-game team scoring ──────────────────────────────────────────
        pg_team = soup.find("table", id="per_game-team")
        if pg_team:
            for row in pg_team.find_all("tr"):
                cells = {c.get("data-stat", ""): c.get_text(strip=True)
                         for c in row.find_all(["th", "td"]) if c.get("data-stat")}
                team_a2 = row.find("td", {"data-stat": "team"})
                a2 = team_a2.find("a") if team_a2 else None
                abbr2 = a2["href"].split("/")[3].upper() if (a2 and "href" in a2.attrs) else ""
                if not abbr2 or abbr2 not in result:
                    continue
                try:
                    result[abbr2]["pts_pg"] = float(cells.get("pts_per_g", 0) or 0)
                    result[abbr2]["fg_pct"] = float(cells.get("fg_pct", 0) or 0)
                    result[abbr2]["fg3_pct"]= float(cells.get("fg3_pct", 0) or 0)
                    result[abbr2]["ft_pct"] = float(cells.get("ft_pct", 0) or 0)
                except (ValueError, TypeError):
                    continue

        # ── Opponent per-game ──────────────────────────────────────────────
        pg_opp = soup.find("table", id="per_game-opponent")
        if pg_opp:
            for row in pg_opp.find_all("tr"):
                cells = {c.get("data-stat", ""): c.get_text(strip=True)
                         for c in row.find_all(["th", "td"]) if c.get("data-stat")}
                team_a3 = row.find("td", {"data-stat": "team"})
                a3 = team_a3.find("a") if team_a3 else None
                abbr3 = a3["href"].split("/")[3].upper() if (a3 and "href" in a3.attrs) else ""
                if not abbr3 or abbr3 not in result:
                    continue
                try:
                    result[abbr3]["opp_pts"] = float(cells.get("pts_per_g", 0) or 0)
                    result[abbr3]["opp_fg"]  = float(cells.get("fg_pct", 0) or 0)
                except (ValueError, TypeError):
                    continue

        log(f"WNBA team stats complete: {len(result)} teams")
    except Exception as e:
        log(f"WNBA team stats: {e}", "WARN")
    return result

def fetch_wnba() -> dict:
    """Fetch WNBA scoreboard, standings, schedule from ESPN + BBRef player/team stats."""
    result = {"today": [], "standings": {}, "schedule": [], "players": [], "teamStats": {}}
    if WNBA_OFFSEASON:
        return result
    log("WNBA scoreboard…")
    try:
        data = fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard?dates={TODAY_ET}&limit=15")
        for ev in (data or {}).get("events", []):
            g = _espn_game(ev, "WNBA")
            if g: result["today"].append(g)
        log(f"WNBA today: {len(result['today'])} games")
    except Exception as e: log(f"WNBA scoreboard: {e}", "WARN")
    try:
        sdata = fetch_json("https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/standings")
        for conf in ((sdata or {}).get("children") or []):
            cname = conf.get("name","")
            for entry in (conf.get("standings",{}).get("entries") or []):
                t = entry.get("team",{})
                stats = {s["name"]:s.get("displayValue","") for s in entry.get("stats",[])}
                result["standings"][t.get("abbreviation","")] = {
                    "name": t.get("displayName",""), "conf": cname,
                    "w": stats.get("wins","0"), "l": stats.get("losses","0"),
                    "pct": stats.get("winPercent",""),
                }
    except Exception as e: log(f"WNBA standings: {e}", "WARN")
    try:
        from datetime import datetime, timedelta
        for i in range(7):
            d = (datetime.now() + timedelta(days=i)).strftime("%Y%m%d")
            sdata = fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard?dates={d}&limit=8")
            for ev in (sdata or {}).get("events", [])[:4]:
                comp = (ev.get("competitions") or [{}])[0]
                comps = comp.get("competitors") or []
                h = next((c for c in comps if c.get("homeAway")=="home"), {})
                a = next((c for c in comps if c.get("homeAway")=="away"), {})
                odds = (comp.get("odds") or [{}])[0]
                result["schedule"].append({
                    "date": d,
                    "home": (h.get("team") or {}).get("abbreviation",""),
                    "away": (a.get("team") or {}).get("abbreviation",""),
                    "homeName": (h.get("team") or {}).get("displayName",""),
                    "awayName": (a.get("team") or {}).get("displayName",""),
                    "homeML": (odds.get("homeTeamOdds") or {}).get("moneyLine"),
                    "awayML": (odds.get("awayTeamOdds") or {}).get("moneyLine"),
                    "state": ev.get("status",{}).get("type",{}).get("state","pre"),
                })
            time.sleep(0.2)
    except Exception as e: log(f"WNBA schedule: {e}", "WARN")
    result["players"] = fetch_wnba_player_stats()
    result["teamStats"] = fetch_wnba_team_stats()
    return result


def fetch_pwhl() -> dict:
    """Fetch PWHL scoreboard and standings from ESPN."""
    log("PWHL data…")
    result = {"today": [], "standings": {}, "schedule": []}
    try:
        data = fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/hockey/pwhl/scoreboard?dates={TODAY_ET}&limit=10")
        for ev in (data or {}).get("events", []):
            g = _espn_game(ev, "PWHL")
            if g: result["today"].append(g)
    except Exception as e: log(f"PWHL scoreboard: {e}", "WARN")
    try:
        sdata = fetch_json("https://site.api.espn.com/apis/site/v2/sports/hockey/pwhl/standings")
        for conf in ((sdata or {}).get("children") or []):
            for entry in (conf.get("standings",{}).get("entries") or []):
                t = entry.get("team",{})
                stats = {s["name"]:s.get("displayValue","") for s in entry.get("stats",[])}
                result["standings"][t.get("abbreviation","")] = {
                    "name": t.get("displayName",""),
                    "w": stats.get("wins","0"), "l": stats.get("losses","0"),
                    "otl": stats.get("otLosses","0"), "pts": stats.get("points","0"),
                }
    except Exception as e: log(f"PWHL standings: {e}", "WARN")
    try:
        from datetime import datetime, timedelta
        for i in range(7):
            d = (datetime.now() + timedelta(days=i)).strftime("%Y%m%d")
            sdata = fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/hockey/pwhl/scoreboard?dates={d}&limit=5")
            for ev in (sdata or {}).get("events",[])[:3]:
                comp=(ev.get("competitions") or [{}])[0]; comps=comp.get("competitors") or []
                h=next((c for c in comps if c.get("homeAway")=="home"),{}); a=next((c for c in comps if c.get("homeAway")=="away"),{})
                result["schedule"].append({"date":d,"home":(h.get("team") or {}).get("abbreviation",""),"away":(a.get("team") or {}).get("abbreviation",""),"homeName":(h.get("team") or {}).get("displayName",""),"awayName":(a.get("team") or {}).get("displayName",""),"state":ev.get("status",{}).get("type",{}).get("state","pre")})
            time.sleep(0.2)
    except Exception as e: log(f"PWHL schedule: {e}", "WARN")
    return result

def fetch_week_schedule(sport_path: str, sport_key: str, limit_per_day: int = 8) -> list[dict]:
    """Fetch 7-day schedule for any ESPN sport."""
    from datetime import datetime, timedelta
    schedule = []
    for i in range(7):
        d = (datetime.now() + timedelta(days=i)).strftime("%Y%m%d")
        try:
            data = fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard?dates={d}&limit={limit_per_day}")
            for ev in (data or {}).get("events", [])[:limit_per_day]:
                comp = (ev.get("competitions") or [{}])[0]
                comps = comp.get("competitors") or []
                h = next((c for c in comps if c.get("homeAway")=="home"), {})
                a = next((c for c in comps if c.get("homeAway")=="away"), {})
                odds = (comp.get("odds") or [{}])[0]
                schedule.append({
                    "date": d, "sport": sport_key,
                    "home": (h.get("team") or {}).get("abbreviation",""),
                    "away": (a.get("team") or {}).get("abbreviation",""),
                    "homeName": (h.get("team") or {}).get("displayName",""),
                    "awayName": (a.get("team") or {}).get("displayName",""),
                    "time": ev.get("date",""),
                    "network": ((comp.get("broadcasts") or [{}])[0].get("names") or [""])[0],
                    "homeML": (odds.get("homeTeamOdds") or {}).get("moneyLine"),
                    "awayML": (odds.get("awayTeamOdds") or {}).get("moneyLine"),
                    "ou": odds.get("overUnder"),
                    "state": ev.get("status",{}).get("type",{}).get("state","pre"),
                    "venue": (comp.get("venue") or {}).get("fullName",""),
                    "seasonType": (ev.get("season") or {}).get("type"),
                })
            time.sleep(0.15)
        except Exception as e:
            log(f"Week schedule {sport_key} {d}: {e}", "WARN")
    return schedule


# Shared across news/injuries/transactions — every league ESPN exposes a
# site.api.espn.com/apis/site/v2/sports/{path} feed for, that this engine
# tracks somewhere (game data, standings, or a model). "ncaab" keeps its
# existing (slightly misleading — it's NCAA *Baseball*, not basketball)
# key name for backward compat with the frontend; "cbb" is the real NCAA
# men's basketball addition.
ESPN_LEAGUE_PATHS: dict[str, str] = {
    "mlb":      "baseball/mlb",
    "nhl":      "hockey/nhl",
    "nba":      "basketball/nba",
    "wnba":     "basketball/wnba",
    "ncaab":    "baseball/college-baseball",   # NCAA Baseball (existing key/convention)
    "cbb":      "basketball/mens-college-basketball",
    "nfl":      "football/nfl",
    "cfb":      "football/college-football",
    "f1":       "racing/f1",
    "mls":      "soccer/usa.1",
    "cl":       "soccer/UEFA.champions",
    "pl":       "soccer/eng.1",
    "liga":     "soccer/esp.1",
    "bl":       "soccer/ger.1",
    "ita":      "soccer/ita.1",
}

# Leagues the product no longer covers (MLB + WNBA 2026-09-08, college baseball purged, MLS 2026-09-27): their ESPN news / injuries / transactions feeds were still being called on every
# run (and returned 500s / dead data nothing reads). The keys stay in the bundle, empty, so any defensive reader of them keeps working.  Bundesliga ("bl") is NOT here: its data still feeds
# the Champions League domestic-form blend for German clubs.
RETIRED_ESPN_KEYS = frozenset({"mlb", "wnba", "ncaab", "mls"})

def fetch_sports_news() -> dict:
    """Fetch latest news articles for all sports/leagues from ESPN."""
    news: dict = {}
    sport_map = dict(ESPN_LEAGUE_PATHS)
    sport_map["football"] = sport_map.pop("nfl")  # keep the pre-existing "football" key the frontend already reads
    for sport_key, espn_path in sport_map.items():
        if sport_key in RETIRED_ESPN_KEYS:
            news[sport_key] = []
            continue
        try:
            url = f"https://site.api.espn.com/apis/site/v2/sports/{espn_path}/news"
            articles = (fetch_json(url) or {}).get("articles", [])
            items = []
            for a in articles[:20]:
                title = a.get("headline", "")
                if not title: continue
                pub = a.get("published", "")
                items.append({
                    "headline": title,
                    "summary": (a.get("description") or a.get("story",""))[:250],
                    "published": pub,
                    "date": pub[:10] if pub else TODAY_ISO,
                    "link": a.get("links", {}).get("web", {}).get("href", ""),
                    "image": ((a.get("images") or [{}])[0]).get("url",""),
                    "sport": sport_key,
                    "category": a.get("categories", [{}])[0].get("description","") if a.get("categories") else "",
                })
            news[sport_key] = items[:15]
            log(f"News {sport_key}: {len(items)} articles")
        except Exception as exc:
            log(f"News {sport_key}: {exc}", "WARN"); news[sport_key] = []
    return news

# ═══════════════════════════════════════════════════════════════════════════
# SUPABASE BET LEDGER — the real bet ledger lives in the browser
# (localStorage/IndexedDB); docs/app.html's syncBetsToSupabase() mirrors every
# locked/settled bet to this table on a debounce. This gives the scheduled
# GitHub Actions run (this script, 3x/day) something real to read for
# automation that previously could only run client-side while a browser tab
# was open: stale-pending detection, and settling bets against the box
# scores this same run already fetched.
# ═══════════════════════════════════════════════════════════════════════════
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://vhwkbeblforpnliowpam.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

def _supabase_headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }

def fetch_bets_from_supabase() -> list[dict]:
    """All bets currently mirrored from the frontend. Read-only — the
    publishable key's RLS policy allows insert/update but this call only
    ever GETs. Paginates in case the ledger grows past PostgREST's default
    page size."""
    if not SUPABASE_KEY:
        log("SUPABASE_KEY not set — skipping Supabase bet-ledger automation", "WARN")
        return []
    rows: list[dict] = []
    offset = 0
    page = 1000
    try:
        while True:
            r = _session.get(
                f"{SUPABASE_URL}/rest/v1/bets",
                headers={**_supabase_headers(), "Range": f"{offset}-{offset+page-1}"},
                params={"select": "*", "order": "date.desc"},
                timeout=15,
            )
            if r.status_code not in (200, 206):
                log(f"Supabase fetch: HTTP {r.status_code} {r.text[:200]}", "WARN")
                break
            batch = r.json()
            rows.extend(batch)
            if len(batch) < page:
                break
            offset += page
        log(f"Supabase: fetched {len(rows)} bets")
    except Exception as exc:
        log(f"Supabase fetch: {exc}", "WARN")
    return rows

def _supabase_patch_outcome(bet_id: str, outcome: str, h_score: int, a_score: int) -> bool:
    try:
        r = _session.patch(
            f"{SUPABASE_URL}/rest/v1/bets",
            headers={**_supabase_headers(), "Prefer": "return=minimal"},
            params={"id": f"eq.{bet_id}"},
            json={"outcome": outcome, "settled_at": int(time.time() * 1000)},
            timeout=10,
        )
        return r.status_code in (200, 204)
    except Exception as exc:
        log(f"Supabase patch {bet_id}: {exc}", "WARN")
        return False

# Every league a pick can actually be locked under — mirrors
# ESPN_SPORT_PATHS in generate_social_cards.py. NBA/MLB/NHL are handled
# via the pre-fetched today/tomorrow lists this run already has (no
# extra API calls); everything else here gets a direct, on-demand ESPN
# scoreboard fetch below, since this script never otherwise pulls
# CFB/NFL/WNBA/CBB/NCAAH/soccer schedules.
_SUPABASE_SETTLE_ESPN_PATHS = {
    "NFL": "football/nfl", "CFB": "football/college-football",
    "WNBA": "basketball/wnba", "CBB": "basketball/mens-college-basketball",
    "NCAAH": "hockey/mens-college-hockey",
    "BL": "soccer/ger.1", "LIGA": "soccer/esp.1", "MLS": "soccer/usa.1",
    "PL": "soccer/eng.1", "SERIEA": "soccer/ita.1", "CL": "soccer/UEFA.champions",
}

def _fetch_espn_games_for_date(path: str, date_str: str) -> list[dict]:
    """Generic ESPN scoreboard fetch for one league/date, normalized to
    the same {home,away,state,homeScore,awayScore} shape game_index()
    already expects from the NBA/MLB/NHL pre-fetched lists — so both
    sources can be merged into one index below without special-casing."""
    try:
        r = _session.get(
            f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard",
            params={"dates": date_str.replace("-", ""), "limit": 300}, timeout=15,
        )
        r.raise_for_status()
        events = r.json().get("events", [])
    except Exception as exc:
        log(f"  ESPN scoreboard fetch failed for {path} {date_str}: {exc}", "WARN")
        return []
    out = []
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        comps = comp.get("competitors") or []
        home = next((c for c in comps if c.get("homeAway") == "home"), {})
        away = next((c for c in comps if c.get("homeAway") == "away"), {})
        state = ((comp.get("status") or {}).get("type") or {}).get("state", "pre")
        out.append({
            "home": (home.get("team") or {}).get("abbreviation", ""),
            "away": (away.get("team") or {}).get("abbreviation", ""),
            "homeScore": home.get("score") if state != "pre" else None,
            "awayScore": away.get("score") if state != "pre" else None,
            "state": state,
        })
    return out

def _settle_bet_outcome(bet_type: str, raw: dict, game: dict) -> str | None:
    """Same win/loss/push logic as the local auto_settle(), shared here so
    Supabase settlement covers every bet type that function does (ML,
    SPREAD/RL/PL, OU) instead of ML-only. Returns None (leave pending)
    for anything it can't verify — never a guess."""
    hs, as_ = int(game.get("homeScore") or 0), int(game.get("awayScore") or 0)
    home, away = game.get("home", ""), game.get("away", "")
    if bet_type == "ML":
        # Pred objects never carry a bare "team" field — every lock*()
        # path (lockPick, lockCFBGame, etc.) stores the picked side as
        # betOn itself for ML bets (e.g. betOn: hA), not a separate
        # "team" key. Checking raw.get("team") — which no bet has ever
        # actually had — meant this branch silently matched nothing.
        picked = str(raw.get("betOn", "")).strip()
        winner = home if hs > as_ else away
        if picked not in (home, away):
            return None  # betOn wasn't a bare team code for this pick — don't guess
        return "win" if picked == winner else "loss"
    if bet_type in ("RL", "PL", "RUNLINE", "PUCKLINE", "SPREAD"):
        bet_on = str(raw.get("betOn", ""))
        m = re.search(r"(-?\d+\.?\d*)\s*$", bet_on)
        if not m:
            return None
        line = float(m.group(1))
        fav_team = bet_on.split(" ")[0]
        if fav_team not in (home, away):
            return None
        is_home = fav_team == home
        diff = (hs - as_) if is_home else (as_ - hs)
        covered = diff + line
        return "win" if covered > 0 else ("push" if covered == 0 else "loss")
    if bet_type in ("OU", "OVER_UNDER", "TOTAL"):
        total = hs + as_
        try:
            lv = float(raw.get("ou") if raw.get("ou") is not None else raw.get("line") or 0)
        except (TypeError, ValueError):
            return None
        if not lv:
            return None
        over = "OVER" in str(raw.get("betOn", "")).upper()
        if total == lv:
            return "push"
        return "win" if (total > lv) == over else "loss"
    return None  # PROP and anything else: no verifiable server-side source here

def run_supabase_automation(nba_final: list, mlb_final: list, nhl_final: list) -> dict:
    """
    Server-side counterpart to the client-side Stale Pending Audit /
    auto-settle — the difference is this actually runs on the 3x/day
    schedule regardless of whether a browser is open. Two jobs:
      1. Settle any pending bet whose game already has a final score —
         NBA/MLB/NHL from the box scores this same run already fetched,
         every other league (CFB/NFL/WNBA/CBB/NCAAH/soccer — previously
         not covered server-side at all) via an on-demand ESPN scoreboard
         fetch, deduped per (league, date) pair actually needed. Handles
         ML/SPREAD/OU, not just ML (previously ML-only, and that ML match
         itself was checking a field — raw.team — that no bet has ever
         actually had, so it never matched anything).
      2. Flag bets that are still pending with a date >24h in the past —
         these didn't match a final score above, so either the game
         hasn't gone final yet or something's off; surfaced in the run
         log rather than silently sitting unresolved.
    """
    bets = fetch_bets_from_supabase()
    if not bets:
        return {"settled": 0, "stale": 0}

    def game_index(games: list) -> dict:
        idx = {}
        for g in games:
            h, a = g.get("home", ""), g.get("away", "")
            state = str(g.get("state", ""))
            if state in ("post", "FINAL", "OFF", "7", "F", "OT", "SO") and h and a:
                idx[(h, a)] = g; idx[(a, h)] = g
        return idx

    all_idx: dict = {}
    all_idx.update(game_index(nba_final))
    all_idx.update(game_index(mlb_final))
    all_idx.update(game_index(nhl_final))

    # Pull in every other league that actually has a pending bet, one
    # scoreboard fetch per distinct (league, date) pair — bounded by
    # however many distinct pairs are really in the pending set, not a
    # fixed fan-out across every league regardless of whether it's used.
    pending_bets = [b for b in bets if b.get("outcome", "pending") == "pending"]
    other_pairs: set[tuple[str, str]] = set()
    for bet in pending_bets:
        raw = bet.get("raw") or {}
        league = str(raw.get("league") or bet.get("sport") or raw.get("sport") or "").upper()
        path = _SUPABASE_SETTLE_ESPN_PATHS.get(league)
        bet_date = bet.get("date")
        if path and bet_date:
            other_pairs.add((league, bet_date))
    for league, bet_date in other_pairs:
        games = _fetch_espn_games_for_date(_SUPABASE_SETTLE_ESPN_PATHS[league], bet_date)
        all_idx.update(game_index(games))

    settled_count = 0
    stale = []
    now_ms = int(time.time() * 1000)
    for bet in bets:
        if bet.get("outcome", "pending") != "pending":
            continue
        raw = bet.get("raw") or {}
        hA, awA = raw.get("hA", "") or bet.get("ha", ""), raw.get("awA", "") or bet.get("awa", "")
        game = all_idx.get((hA, awA))
        bet_type = str(bet.get("bet_type", "ML")).upper()
        if game:
            outcome = _settle_bet_outcome(bet_type, raw, game)
            if outcome and _supabase_patch_outcome(bet["id"], outcome, int(game.get("homeScore") or 0), int(game.get("awayScore") or 0)):
                settled_count += 1
                log(f"  Supabase auto-settle: {bet.get('bet_on', bet['id'])} -> {outcome.upper()}")
                continue
        # Not matched to a final — check staleness
        bet_date = bet.get("date")
        if bet_date:
            try:
                bet_ts = datetime.strptime(bet_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000
                if now_ms - bet_ts > 24 * 3600 * 1000:
                    stale.append(bet.get("bet_on", bet["id"]))
            except Exception:
                pass

    if stale:
        log(f"Supabase: {len(stale)} bets pending >24h past their date and unmatched to a final score", "WARN")
    log(f"Supabase automation: settled {settled_count}, {len(stale)} still stale")
    return {"settled": settled_count, "stale": len(stale), "bets": bets}

def supabase_bets_to_history(bets: list[dict]) -> list[dict]:
    """
    Normalize Supabase's row schema (snake_case, PostgREST column names)
    into the same shape build_overall_stats()/compute_calibration_metrics()
    already expect from the local bet_history.json path (camelCase,
    matching what the frontend's lockPick() writes). This is what actually
    connects overallStats/calibration to the REAL 346-bet ledger — before
    this, both were computed from local bet_history.json, which stays
    empty now that the real ledger lives in Supabase (mirrored from the
    browser), so every settled-bet-derived stat in the scheduled bundle
    was silently running against zero real bets.
    """
    out = []
    # 2026-10-02: this history feeds PUBLIC files (docs/data.json overallStats/betHistory, docs/bet_history.csv, data/
    # bet_history.csv), so it is PRE-START LOCKS ONLY -- a pick KNOWN to have been locked after its game started (the owner's
    # manual late locks) is dropped, via the same shared classifier every public figure uses (scripts/lock_timing.py).
    # Unknown-timing picks stay. Retired-sport history (MLB/tennis/...) has no start data, so it is unfiltered by necessity.
    import lock_timing as _lt
    _lt_idx = _lt.load_index()
    _late_dropped = 0
    for row in bets:
        if row.get("outcome") not in ("win", "loss", "push"):
            continue
        raw = row.get("raw") or {}
        _pk = {**raw, "id": row.get("id") or raw.get("id")}
        for _k in ("sport", "date"):
            if not _pk.get(_k):
                _pk[_k] = row.get(_k)
        if _lt.is_parlay(_pk):
            continue        # parlays are out of the engine (owner decision 2026-10-04): they never feed overallStats / calibration / the public history files
        if _lt.is_known_late(_pk, _lt_idx):
            _late_dropped += 1
            continue
        settled_at = row.get("settled_at")
        if settled_at is not None and not isinstance(settled_at, str):
            # Supabase's settled_at column is a bigint (epoch ms) — every
            # other settledAt in this pipeline (local bet_history.json,
            # merge_settled_to_history's NOW.isoformat() writes) is a
            # string, and build_overall_stats()/compute_streak() both
            # slice/compare it as one. Left as a raw int, the very first
            # scheduled run after wiring in the real Supabase ledger
            # crashed on `settledAt[:10]` with "'int' object is not
            # subscriptable" — normalizing here, once, is simpler than
            # making every downstream consumer defensive.
            try:
                settled_at = datetime.fromtimestamp(settled_at / 1000, tz=timezone.utc).isoformat()
            except Exception:
                settled_at = None
        outcome = row.get("outcome")
        # Unit-normalized PnL (1 unit staked per bet, regardless of the
        # `wager` field) — matches the frontend's own calcPerf() exactly
        # (win: decOdds-1, loss: -1, push: 0), which is the convention
        # behind every "+187.4u"-style number already shown in the app.
        # Nothing writes a `pnl` field at all today - not Supabase rows,
        # not local bet_history.json, not the frontend's own bet objects -
        # so compute_roi()'s totalPnl/roi has always silently been 0
        # through this pipeline. Computing it here, once, at the same
        # place settledAt already gets normalized.
        dec_odds = row.get("dec_odds") if row.get("dec_odds") is not None else raw.get("decOdds")
        try:
            dec_odds = float(dec_odds) if dec_odds is not None else None
        except (TypeError, ValueError):
            dec_odds = None  # a handful of real rows have junk in dec_odds (e.g. a venue string) — fall through to ml
        if dec_odds is None:
            ml = row.get("ml") or raw.get("ml")
            try:
                dec_odds = _ml_to_dec(float(ml))
            except Exception:
                dec_odds = 2.0
        pnl = raw.get("pnl")
        if pnl is None:
            if outcome == "win":
                pnl = dec_odds - 1
            elif outcome == "loss":
                pnl = -1.0
            else:
                pnl = 0.0
        out.append({
            "id":        row.get("id"),
            "sport":     row.get("sport") or raw.get("sport"),
            "betType":   raw.get("betType") or row.get("bet_type"),
            "betOn":     raw.get("betOn") or row.get("bet_on"),
            "winProb":   row.get("win_prob") if row.get("win_prob") is not None else raw.get("winProb"),
            "ml":        row.get("ml") or raw.get("ml"),
            "decOdds":   dec_odds,
            "outcome":   outcome,
            "date":      row.get("date") or raw.get("date"),
            "settledAt": settled_at or raw.get("settledAt"),
            "wager":     row.get("wager", 100),
            "pnl":       pnl,
        })
    if _late_dropped:
        log(f"History: dropped {_late_dropped} known-late (locked after game start) settled pick(s) -- public figures are pre-start locks only")
    return out

def fetch_injuries_all() -> dict:
    """
    Dedicated injury fetcher for all sports. fetch_espn_injuries() is
    generic (any ESPN sport_path works). As of the injury-integration work
    done in app.html's buildInjuryRoster(), MLB (batters + starting
    pitchers), NBA, WNBA, NHL, and MLS all have real roster maps so these
    injury reports actually apply a win-probability penalty via
    computeInjuryImpact() — not just fetched-and-displayed. Champions
    League/Premier League/La Liga/Bundesliga, tennis, F1, and CFB/NFL/CBB
    still have no roster coverage (soccer's non-MLS leagues were
    confirmed offseason/empty; the others simply don't have a roster
    dataset built yet) — this list is fetched for those too but has
    nothing to match against.
    """
    log("ESPN injury reports…")
    # Every team-based league — tennis (ATP/WTA) and F1 are excluded since
    # ESPN's /injuries endpoint schema is team-roster-shaped and doesn't
    # apply to individual-athlete sports (a tour withdrawal isn't a "team
    # injury report").
    result: dict = {}
    for key, path in ESPN_LEAGUE_PATHS.items():
        if key == "f1":
            continue
        if key in RETIRED_ESPN_KEYS:
            result[key] = []
            continue
        result[key] = fetch_espn_injuries(path, key)
    return result

def fetch_transactions_all() -> dict:
    """
    Transactions for every team-based league (same exclusions as
    fetch_injuries_all — no meaningful "transaction log" for individual-
    athlete tennis/F1). New feed, previously not tracked at all anywhere
    in the pipeline.
    """
    log("ESPN transaction logs…")
    result: dict = {}
    for key, path in ESPN_LEAGUE_PATHS.items():
        if key == "f1":
            continue
        if key in RETIRED_ESPN_KEYS:
            result[key] = []
            continue
        result[key] = fetch_espn_transactions(path, key)
    return result

# ═══════════════════════════════════════════════════════════════════════════════
# Best Bets Calculator  (EV + Confidence scoring)
# ═══════════════════════════════════════════════════════════════════════════════
def _ml_to_prob(ml) -> float | None:
    try:
        ml = int(ml)
        return abs(ml)/(abs(ml)+100) if ml < 0 else 100/(ml+100)
    except: return None

def _ml_to_dec(ml) -> float:
    try:
        ml = int(ml)
        return (100/abs(ml)+1) if ml < 0 else (ml/100+1)
    except: return 1.91

def _ev(prob: float, dec: float) -> float:
    return prob * dec - 1

def _ev_grade(ev_pct: float) -> str:
    if ev_pct >= 12: return "A+"
    if ev_pct >= 8:  return "A"
    if ev_pct >= 4:  return "B"
    if ev_pct >= 1:  return "C"
    return "D"

def _confidence(prob: float, ev_pct: float, extra_signals: int = 0) -> int:
    """Return 0–100 confidence score."""
    base = int(prob * 70)             # max 70 from implied prob
    ev_bonus = min(20, int(ev_pct))   # max 20 from EV
    signal_bonus = min(10, extra_signals * 3)
    return min(100, base + ev_bonus + signal_bonus)

def _pyth_win_pct(rs: float, ra: float, exp: float = 1.83) -> float:
    """Pythagorean win expectation from runs scored/allowed."""
    try:
        if rs + ra == 0: return 0.5
        return rs**exp / (rs**exp + ra**exp)
    except: return 0.5

def _estimate_prob_from_standings(home: str, away: str, standings: dict,
                                   hfa: float = 0.04) -> tuple[float, float, int]:
    """
    Estimate win probabilities from standings when no book odds available.
    Uses Pythagorean expectation (RS/RA) + home field advantage.
    Returns (home_prob, away_prob, ml_estimate) — ml_estimate for home.
    """
    hs = standings.get(home, {})
    as_ = standings.get(away, {})
    if not hs or not as_:
        return 0.5 + hfa, 0.5 - hfa, -110
    try:
        h_pyth = _pyth_win_pct(float(hs.get("rs", 200)), float(hs.get("ra", 200)))
        a_pyth = _pyth_win_pct(float(as_.get("rs", 200)), float(as_.get("ra", 200)))
        # Log5 formula: P(A beats B) = (A - A*B) / (A + B - 2*A*B)
        if h_pyth + a_pyth == 0: return 0.54, 0.46, -120
        h_prob_raw = (h_pyth - h_pyth * a_pyth) / (h_pyth + a_pyth - 2 * h_pyth * a_pyth)
        h_prob = min(0.82, max(0.18, h_prob_raw + hfa))
        a_prob = 1 - h_prob
        # Convert to American ML
        if h_prob >= 0.5:
            ml = -int(round(h_prob / (1 - h_prob) * 100))
        else:
            ml = int(round((1 - h_prob) / h_prob * 100))
        return h_prob, a_prob, ml
    except:
        return 0.54, 0.46, -120

def calculate_best_bets(
    nba_today: list, mlb_today: list, nhl_today: list,
    weather: dict, mp: dict | None = None, nhl_edge: dict | None = None,
    nhl_props: list | None = None, nhl_trends: list | None = None,
    mlb_sabre: dict | None = None, best_odds: dict | None = None,
    nba_adv: dict | None = None, mlb_standings: dict | None = None,
) -> list[dict]:
    log("Calculating best bets…")
    picks: list[dict] = []

    def add(sport, game_str, pick_str, prob, ml, grade, note="", extra_signals=0):
        dec     = _ml_to_dec(ml)
        ev_pct  = round(_ev(prob, dec) * 100, 1)
        conf    = _confidence(prob, ev_pct, extra_signals)
        ev_letter = _ev_grade(ev_pct)
        picks.append({
            "sport":      sport,
            "game":       game_str,
            "pick":       pick_str,
            "prob":       round(prob * 100, 1),
            "ev":         ev_pct,
            "evGrade":    ev_letter,
            "confidence": conf,
            "ml":         f"+{ml}" if isinstance(ml,int) and ml>0 else str(ml),
            "grade":      grade,
            "note":       note,
            "date":       TODAY_ISO,
        })

    # NBA advanced stats helper
    _nba_adv = nba_adv or {}

    def _nba_net_rtg_adj(home: str, away: str) -> tuple[float, str]:
        """
        Return (home_prob_adj, note) from net rating differential.
        Net Rating gap of 10 pts → ~4% win probability swing.
        """
        ht = _nba_adv.get(home, {}); at = _nba_adv.get(away, {})
        if not ht or not at: return 0.0, ""
        h_net = ht.get("net_rtg", 0.0)
        a_net = at.get("net_rtg", 0.0)
        diff  = h_net - a_net
        adj   = diff * 0.004       # 10pt gap → +4% for home
        parts = []
        if abs(adj) > 0.005:
            parts.append(f"NetRtg: {home} {h_net:+.1f} / {away} {a_net:+.1f}")
        h_efg = ht.get("efg_pct", 0); a_efg = at.get("efg_pct", 0)
        efg_adj = (h_efg - a_efg) * 0.5  # 2% eFG gap → ~1% prob swing
        if abs(efg_adj) > 0.005:
            adj += efg_adj
            parts.append(f"eFG%: {home} {h_efg*100:.1f} / {away} {a_efg*100:.1f}")
        return adj, "  ".join(parts)

    # NBA moneylines
    _nba_pre_skipped = 0
    for g in nba_today:
        if g.get("state") != "pre": continue
        # Preseason (ESPN seasonType 1, ~Sep 30-Oct 20): the model is not built for
        # exhibition games (rotations, minutes limits) and they must not become
        # graded picks -- same exclusion NFL preseason already gets. Flips off by
        # itself when ESPN labels the games seasonType 2 on opening night.
        if g.get("seasonType") == 1:
            _nba_pre_skipped += 1
            continue
        hml, aml  = g.get("homeML"), g.get("awayML")
        hprob_raw, aprob_raw = _ml_to_prob(hml), _ml_to_prob(aml)
        home, away = g.get("home",""), g.get("away","")
        game_str   = f"{away} @ {home}"
        adv_adj, adv_note = _nba_net_rtg_adj(home, away)
        extra_sig  = 1 if adv_note else 0
        hprob = max(0.05, min(0.95, hprob_raw + adv_adj)) if hprob_raw else None
        aprob = max(0.05, min(0.95, aprob_raw - adv_adj)) if aprob_raw else None
        if hprob and hprob > 0.62:
            add("NBA", game_str, f"{home} ML {hml}", hprob, hml,
                "LOCK" if hprob>0.67 else "GOOD",
                note=(g.get("seriesNote","") + ("  " + adv_note if adv_note else "")).strip(),
                extra_signals=extra_sig)
        elif aprob and aprob > 0.62:
            add("NBA", game_str, f"{away} ML {aml}", aprob, aml,
                "LOCK" if aprob>0.67 else "GOOD",
                note=(g.get("seriesNote","") + ("  " + adv_note if adv_note else "")).strip(),
                extra_signals=extra_sig)
        # O/U info
        ou = g.get("ou")
        if ou and hml and aml:
            # Pace-adjusted O/U lean
            h_pace = _nba_adv.get(home, {}).get("pace", 0)
            a_pace = _nba_adv.get(away, {}).get("pace", 0)
            avg_pace = (h_pace + a_pace) / 2 if h_pace and a_pace else 0
            pace_note = f"Pace: {home} {h_pace:.1f} / {away} {a_pace:.1f}" if avg_pace else ""
            picks.append({
                "sport":"NBA","game":game_str,"pick":f"O/U {ou}","prob":52.0,
                "ev":0.0,"evGrade":"D","confidence":52,"ml":"-110","grade":"INFO",
                "note":f"Line: {home} {hml} / {away} {aml}" + (f"  {pace_note}" if pace_note else ""),
                "date":TODAY_ISO,
            })

    if _nba_pre_skipped:
        log(f"  NBA best bets: skipped {_nba_pre_skipped} preseason game(s) (seasonType=1)")

    # ── MLB sabermetric lookup helpers ──────────────────────────────────────
    _sabre = mlb_sabre or {}
    _best  = best_odds or {}

    # Build abbr→full-name reverse map from whatever keys exist in _sabre
    # Sabre data may be keyed by full name ("Tampa Bay Rays") or abbr ("TB")
    _ABBR_TO_FULL: dict[str, str] = {v: k for k, v in _TEAM_NAME_TO_ABBR.items()}

    def _sabre_lookup(abbr: str) -> dict:
        """Look up sabre stats by ESPN abbr, tolerating full-name or abbr keys."""
        d = _sabre.get(abbr)
        if d: return d
        full = _ABBR_TO_FULL.get(abbr, "")
        return _sabre.get(full, {})

    def _sabre_edge(home: str, away: str) -> tuple[float, float, str]:
        """
        Return (home_adj, away_adj, note) capped at ±4% total.
        Uses OPS+ (offense) and ERA- (pitching) — both scaled to 100 = league avg.
        OPS+ typical range: 70–140. ERA- typical range: 65–140.
        Values outside these ranges are treated as data errors and clamped.
        """
        hs = _sabre_lookup(home); as_ = _sabre_lookup(away)
        if not hs and not as_:
            return 0.0, 0.0, ""

        def _clamp_ops(v) -> float:
            try: v = float(v or 100)
            except: v = 100.0
            return max(50.0, min(160.0, v))  # clip outliers

        def _clamp_era(v) -> float:
            try: v = float(v or 100)
            except: v = 100.0
            return max(50.0, min(160.0, v))  # clip outliers

        h_ops = _clamp_ops(hs.get("ops_plus", 100))
        a_ops = _clamp_ops(as_.get("ops_plus", 100))
        h_era = _clamp_era(hs.get("era_minus", 100))
        a_era = _clamp_era(as_.get("era_minus", 100))

        # 10pt OPS+ gap → ~1.5% win prob swing; 10pt ERA- gap → ~1% swing
        ops_adj = (h_ops - a_ops) * 0.0015
        era_adj = (a_era - h_era) * 0.001   # lower ERA- is better for pitching

        total = max(-0.04, min(0.04, ops_adj + era_adj))  # hard cap ±4%
        parts = []
        if abs(ops_adj) > 0.005:
            parts.append(f"OPS+: {home} {h_ops:.0f} / {away} {a_ops:.0f}")
        if abs(era_adj) > 0.005:
            parts.append(f"ERA-: {home} {h_era:.0f} / {away} {a_era:.0f}")
        note = "  ".join(parts)
        return total, -total, note

    _standings = mlb_standings or {}

    # MLB — wind-adjusted O/U + sabermetric ML model
    for g in mlb_today:
        if g.get("state") != "pre": continue
        home, away = g.get("home",""), g.get("away","")
        game_str   = f"{away} @ {home}"
        w          = weather.get(home,{})
        # Best available odds: Odds API (real book lines) → ESPN → estimated
        bk       = _best.get(f"{home}:{away}", {})
        hml      = bk.get("homeML") or g.get("homeML")
        aml      = bk.get("awayML") or g.get("awayML")
        ou       = bk.get("ou") or g.get("ou")
        book_src = bk.get("book", "")
        hbook    = bk.get("homeBook", book_src)
        abook    = bk.get("awayBook", book_src)
        # Fallback: estimate from standings when no book odds
        using_estimated_odds = False
        if hml is None and _standings:
            h_est, a_est, hml_est = _estimate_prob_from_standings(home, away, _standings)
            aml_est = (-int(round(a_est/(1-a_est)*100)) if a_est >= 0.5
                       else int(round((1-a_est)/a_est*100)))
            hml, aml = hml_est, aml_est
            using_estimated_odds = True
        # Wind-adjusted O/U
        if w and not w.get("indoor"):
            wind     = w.get("wind",0) or 0
            wind_dir = w.get("windDir",0) or 0
            blowing_out = 45 <= wind_dir <= 135
            if wind >= 12 and ou:
                pick_dir = "OVER" if blowing_out else "UNDER"
                prob     = 0.58 if wind >= 18 else 0.54
                add("MLB", game_str,
                    f"{pick_dir} {ou} (wind {wind}mph {'out' if blowing_out else 'in'})",
                    prob, -110, "GOOD" if prob > 0.56 else "INFO",
                    note=f"{w.get('condition')}, {w.get('temp')}°F", extra_signals=1)
        # Independent model probability + sabermetric edge vs book odds
        # Step 1: Independent win probability from Pythagorean + Log5
        h_model, a_model, _ = _estimate_prob_from_standings(home, away, _standings)
        # Step 2: Sabermetric edge shifts the model probability
        h_adj, a_adj, sabre_note = _sabre_edge(home, away)
        h_model = max(0.10, min(0.90, h_model + h_adj))
        a_model = max(0.10, min(0.90, a_model + a_adj))
        extra_sig = 1 if sabre_note else 0
        real_odds = not using_estimated_odds and hml is not None
        est_note  = " [model est.]" if using_estimated_odds else (f" [{hbook}]" if hbook else "")

        # Step 3: EV = model_prob × book_decimal - 1
        # Positive when our model thinks team more likely to win than book implies
        if hml is not None:
            hdec = _ml_to_dec(hml)
            hev  = round(_ev(h_model, hdec) * 100, 1)
            # Fire when: model prob ≥ 53%, EV ≥ 1.5% with real odds / ≥ 3% with estimated
            ev_min = 1.5 if real_odds else 3.0
            if h_model >= 0.53 and hev >= ev_min:
                grade = "LOCK" if hev >= 8 else ("GOOD" if hev >= 4 else "LEAN")
                note_parts = []
                if sabre_note: note_parts.append(sabre_note)
                note_parts.append(f"model {h_model*100:.1f}% vs book {(_ml_to_prob(hml) or 0)*100:.1f}%")
                if est_note.strip(): note_parts.append(est_note.strip())
                add("MLB", game_str, f"{home} ML {hml}", h_model, hml,
                    grade, note="  ".join(note_parts), extra_signals=extra_sig)

        if aml is not None:
            adec = _ml_to_dec(aml)
            aev  = round(_ev(a_model, adec) * 100, 1)
            ev_min = 1.5 if real_odds else 3.0
            if a_model >= 0.53 and aev >= ev_min:
                grade = "LOCK" if aev >= 8 else ("GOOD" if aev >= 4 else "LEAN")
                book_n = f" [{abook}]" if abook and not using_estimated_odds else est_note.strip()
                note_parts = []
                if sabre_note: note_parts.append(sabre_note)
                note_parts.append(f"model {a_model*100:.1f}% vs book {(_ml_to_prob(aml) or 0)*100:.1f}%")
                if book_n: note_parts.append(book_n)
                add("MLB", game_str, f"{away} ML {aml}", a_model, aml,
                    grade, note="  ".join(note_parts), extra_signals=extra_sig)

    # NHL — moneyline + puck line + O/U (pre-game and live)
    mp_teams = (mp or {}).get("teams",{}) if mp else {}
    nhl_edge_teams = (nhl_edge or {}).get("teams",{}) if nhl_edge else {}
    for g in nhl_today:
        if g.get("state") not in ("FUT","PRE","LIVE","CRIT"): continue
        home, away  = g.get("home",""), g.get("away","")
        # Best odds: Odds API → ESPN fallback
        nhl_bk = _best.get(f"{home}:{away}", {})
        hml  = nhl_bk.get("homeML") or g.get("homeML")
        aml  = nhl_bk.get("awayML") or g.get("awayML")
        game_str    = f"{away} @ {home}"
        series_note = g.get("seriesStatus","") or g.get("details","")
        is_live     = g.get("state") in ("LIVE","CRIT")
        live_tag    = " [LIVE]" if is_live else ""

        # MoneyPuck 5v5 xGF% → model win probability (home-adjusted)
        home_xgf_raw = float((mp_teams.get(home,{}).get("5on5") or {}).get("xgfPct") or 0.50)
        away_xgf_raw = float((mp_teams.get(away,{}).get("5on5") or {}).get("xgfPct") or 0.50)
        # Normalize so they sum to 1, then apply small home-ice bump (+3%)
        total_xgf   = home_xgf_raw + away_xgf_raw if (home_xgf_raw + away_xgf_raw) > 0 else 1
        model_home  = min(0.80, max(0.20, home_xgf_raw / total_xgf + 0.03))
        model_away  = 1.0 - model_home
        xgf_edge    = 1 if abs(home_xgf_raw - away_xgf_raw) > 0.04 else 0
        xgf_note    = (f"xGF%: {home} {home_xgf_raw*100:.1f} / {away} {away_xgf_raw*100:.1f}"
                       f"  model: {model_home*100:.1f}% / {model_away*100:.1f}%")

        # PDO regression: PDO = sh% + sv% (league avg ≈ 1.000 in 5v5)
        # High PDO (>1.025) → likely to regress negatively; Low PDO (<0.975) → positive regression
        pdo_adj  = 0.0
        pdo_note = ""
        h_edge = nhl_edge_teams.get(home, {})
        a_edge = nhl_edge_teams.get(away, {})
        if h_edge and a_edge:
            h_sf60 = float(h_edge.get("sf60") or 0)
            h_gf60 = float(h_edge.get("gf60") or 0)
            h_sa60 = float(h_edge.get("sa60") or 0)
            h_ga60 = float(h_edge.get("ga60") or 0)
            a_sf60 = float(a_edge.get("sf60") or 0)
            a_gf60 = float(a_edge.get("gf60") or 0)
            a_sa60 = float(a_edge.get("sa60") or 0)
            a_ga60 = float(a_edge.get("ga60") or 0)
            # sh% = gf / sf (avoid div/0)
            h_sh = h_gf60 / h_sf60 if h_sf60 > 0 else 0.08
            a_sh = a_gf60 / a_sf60 if a_sf60 > 0 else 0.08
            # sv% = 1 - ga/sa (goalie; lower ga/sa = better)
            h_sv = 1 - (h_ga60 / h_sa60) if h_sa60 > 0 else 0.915
            a_sv = 1 - (a_ga60 / a_sa60) if a_sa60 > 0 else 0.915
            h_pdo = h_sh + h_sv
            a_pdo = a_sh + a_sv
            # PDO above 1.050 is unusually lucky; adjust model_home
            # Each 0.010 PDO above 1.025 reduces win prob by ~1.5%
            h_pdo_adj = -max(0, (h_pdo - 1.025)) * 1.5  # negative if home over-performing
            a_pdo_adj = -max(0, (a_pdo - 1.025)) * 1.5
            # Home benefiting from away's negative regression = positive adj for home
            pdo_adj = h_pdo_adj - a_pdo_adj
            pdo_adj = max(-0.06, min(0.06, pdo_adj))  # cap at ±6%
            if abs(pdo_adj) > 0.01:
                pdo_note = (f"PDO: {home} {h_pdo:.3f}{'↓' if h_pdo>1.025 else ''} / "
                            f"{away} {a_pdo:.3f}{'↓' if a_pdo>1.025 else ''}")
                xgf_edge += 1  # extra signal strength

        # Apply PDO adjustment to model probabilities
        model_home = min(0.80, max(0.20, model_home + pdo_adj))
        model_away = 1.0 - model_home
        if pdo_note:
            xgf_note = f"{xgf_note}  {pdo_note}"

        # Market-implied probs
        hprob, aprob = _ml_to_prob(hml), _ml_to_prob(aml)

        # ── Moneyline — pick side with highest positive EV ───────────────
        h_ev = _ev(model_home, _ml_to_dec(hml)) * 100 if hml else -99
        a_ev = _ev(model_away, _ml_to_dec(aml)) * 100 if aml else -99
        if h_ev > a_ev and h_ev > 1.0:
            grade = "LOCK" if h_ev > 8 else "GOOD" if h_ev > 4 else "INFO"
            add("NHL", game_str, f"{home} ML {hml}{live_tag}",
                model_home, hml, grade,
                note=f"{series_note}  {xgf_note}".strip(), extra_signals=xgf_edge)
        elif a_ev > 1.0:
            grade = "LOCK" if a_ev > 8 else "GOOD" if a_ev > 4 else "INFO"
            add("NHL", game_str, f"{away} ML {aml}{live_tag}",
                model_away, aml, grade,
                note=f"{series_note}  {xgf_note}".strip(), extra_signals=xgf_edge)

        # ── Puck Line ────────────────────────────────────────────────────
        hpl_odds = g.get("homePL")   # e.g. +142 for COL -1.5
        apl_odds = g.get("awayPL")   # e.g. -170 for VGK +1.5
        spread   = g.get("spread")   # e.g. -1.5 (home favored)
        if spread is not None and hprob:
            # Underdog puck line (+1.5) is worth flagging when favourite is heavy
            if apl_odds and _ml_to_prob(-abs(int(apl_odds or 110))) and hprob > 0.60:
                apl_prob = _ml_to_prob(-abs(int(apl_odds)))
                add("NHL", game_str, f"{away} +1.5 ({apl_odds}){live_tag}",
                    apl_prob or 0.55, int(apl_odds or -170), "GOOD",
                    note=f"Puck line · {series_note}".strip(), extra_signals=xgf_edge)
            # Favourite -1.5 only if very strong
            if hpl_odds and hprob > 0.68:
                add("NHL", game_str, f"{home} -1.5 ({hpl_odds}){live_tag}",
                    0.45, int(hpl_odds or 140), "INFO",
                    note=f"Puck line · {series_note}".strip())

        # ── Over / Under ─────────────────────────────────────────────────
        ou = g.get("ou")
        if ou:
            # Pull goalie save% from MoneyPuck to tilt O/U
            mp_goalies = (mp or {}).get("goalies",[])
            def best_goalie_sv(team):
                gl = [g2 for g2 in mp_goalies if g2.get("team") == team]
                return max((g2.get("savePct",0) for g2 in gl), default=0)
            h_sv = best_goalie_sv(home)
            a_sv = best_goalie_sv(away)
            avg_sv = (h_sv + a_sv) / 2 if (h_sv and a_sv) else 0
            # High combined save% → lean UNDER
            if avg_sv > 0.915:
                add("NHL", game_str, f"UNDER {ou}{live_tag}", 0.56, -115, "GOOD",
                    note=f"Goalie SV%: {home} {h_sv:.3f} / {away} {a_sv:.3f}")
            elif avg_sv < 0.900 and avg_sv > 0:
                add("NHL", game_str, f"OVER {ou}{live_tag}", 0.54, -115, "INFO",
                    note=f"Goalie SV%: {home} {h_sv:.3f} / {away} {a_sv:.3f}")
            else:
                picks.append({
                    "sport":"NHL","game":game_str,"pick":f"O/U {ou}{live_tag}",
                    "prob":52.0,"ev":0.0,"evGrade":"D","confidence":52,
                    "ml":"-110","grade":"INFO",
                    "note":f"Line: {home} {hml} / {away} {aml}","date":TODAY_ISO,
                })

    # ── NHL Player Props (from Linemate trends) ──────────────────────────────
    if nhl_trends:
        for trend in nhl_trends:
            player    = trend.get("player", "")
            category  = trend.get("category", "")
            direction = trend.get("direction", "neutral")
            l5_str    = trend.get("l5", "")
            l10_str   = trend.get("l10", "")
            if not player or not category:
                continue
            # Parse "X/Y" hit-rate strings
            def _parse_rate(s):
                m = re.match(r"(\d+)/(\d+)", s or "")
                return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            l5_hit, l5_tot  = _parse_rate(l5_str)
            l10_hit, l10_tot = _parse_rate(l10_str)
            # Score: require at least 4/5 or 7/10 hit rate to qualify
            strong_l5  = l5_tot >= 5  and l5_hit / l5_tot  >= 0.80
            strong_l10 = l10_tot >= 8 and l10_hit / l10_tot >= 0.70
            if not (strong_l5 or strong_l10):
                continue
            hit_rate   = (l5_hit / l5_tot) if l5_tot else (l10_hit / l10_tot if l10_tot else 0.5)
            bet_dir    = ("OVER" if direction in ("hot","up") else
                          "UNDER" if direction in ("cold","down") else
                          ("OVER" if hit_rate >= 0.8 else "UNDER"))
            grade      = "LOCK" if (strong_l5 and l5_hit == l5_tot) else "GOOD" if strong_l5 else "INFO"
            # Build note with trend streaks
            note_parts = []
            if l5_str:  note_parts.append(f"L5: {l5_str}")
            if l10_str: note_parts.append(f"L10: {l10_str}")
            if trend.get("lineMove"): note_parts.append(f"Line: {trend['lineMove']}")
            trend_note = "  ".join(note_parts) if note_parts else "Linemate trend"
            # Estimated prob from hit rate (regress to the mean slightly)
            prop_prob = max(0.52, min(0.85, hit_rate * 0.88 + 0.10))
            picks.append({
                "sport":      "NHL",
                "game":       player,
                "pick":       f"{bet_dir} {category}",
                "prob":       round(prop_prob * 100, 1),
                "ev":         round((prop_prob * 1.909 - 1) * 100, 1),  # assume -110
                "evGrade":    _ev_grade(round((prop_prob * 1.909 - 1) * 100, 1)),
                "confidence": _confidence(prop_prob, round((prop_prob * 1.909 - 1) * 100, 1), 1),
                "ml":         "-110",
                "grade":      grade,
                "note":       trend_note,
                "date":       TODAY_ISO,
                "betType":    "PROP",
                "propPlayer": player,
                "propStat":   category,
                "propDir":    bet_dir,
                "propHitRate": f"{l5_hit}/{l5_tot}" if l5_tot else f"{l10_hit}/{l10_tot}",
            })

    # ── BUG 2 FIX: Filter Linemate date-as-game garbage ─────────────────────
    # ── WNBA moneylines ──────────────────────────────────────────────────────
    wnba_games = (best_odds or {}).get("_wnba_today", [])
    for g in wnba_games:
        if g.get("state") != "pre": continue
        home, away = g.get("home",""), g.get("away","")
        game_str   = f"{away} @ {home}"
        hml, aml   = g.get("homeML"), g.get("awayML")
        hprob_raw, aprob_raw = _ml_to_prob(hml), _ml_to_prob(aml)
        if hprob_raw and hprob_raw > 0.60:
            add("WNBA", game_str, f"{home} ML {hml}", hprob_raw, hml, "GOOD",
                note=f"WNBA home advantage")
        if aprob_raw and aprob_raw > 0.60:
            add("WNBA", game_str, f"{away} ML {aml}", aprob_raw, aml, "GOOD",
                note=f"WNBA road value")

    # ── NCAA Baseball — top-25 ranked team picks ──────────────────────────────
    ncaa_games = (best_odds or {}).get("_ncaa_today", [])
    for g in ncaa_games:
        if g.get("state") != "pre": continue
        home, away = g.get("home",""), g.get("away","")
        game_str   = f"{away} @ {home}"
        hml, aml   = g.get("homeML"), g.get("awayML")
        hprob_raw  = _ml_to_prob(hml)
        aprob_raw  = _ml_to_prob(aml)
        if hprob_raw and hprob_raw > 0.62:
            add("NCAAB", game_str, f"{home} ML {hml}", hprob_raw, hml, "GOOD",
                note="NCAA Baseball — ranked home team")
        elif aprob_raw and aprob_raw > 0.62:
            add("NCAAB", game_str, f"{away} ML {aml}", aprob_raw, aml, "GOOD",
                note="NCAA Baseball — ranked away team")

    # Linemate scraper sometimes returns the date string (e.g. "12/11/25") as
    # the "game" field or propPlayer field.  Remove those entries.
    _date_like = re.compile(r"^\d{2}/\d{2}/\d{2,4}$")
    def _is_valid_pick(p: dict) -> bool:
        game_field = str(p.get("game", ""))
        if _date_like.match(game_field):
            return False
        # For PROP bets: propPlayer must exist and must not be a date string
        if p.get("betType") == "PROP" or p.get("propPlayer") is not None:
            player_field = str(p.get("propPlayer", ""))
            if not player_field or _date_like.match(player_field):
                return False
        return True
    picks = [p for p in picks if _is_valid_pick(p)]

    grade_order = {"LOCK":0,"GOOD":1,"INFO":2}
    picks.sort(key=lambda p: (grade_order.get(p["grade"],9), -p.get("confidence",0)))
    top = picks[:20]   # bumped cap to 20 to include room for props
    (DATA / "best_bets.json").write_text(json.dumps(top, indent=2))
    log(f"Best bets: {len(top)} picks | top EV grade: {top[0]['evGrade'] if top else '—'}")
    return top

# ═══════════════════════════════════════════════════════════════════════════════
# Auto-settle (enhanced — checks ML, RL, OU, Spread, Puck Line, Run Line)
# ═══════════════════════════════════════════════════════════════════════════════
def auto_settle(nba_final: list, mlb_final: list, nhl_final: list) -> list[dict]:
    locked_path  = DATA / "locked_props.json"
    settled_path = DATA / "settled.json"
    if not locked_path.exists():
        vlog("No locked_props.json — skipping server-side auto-settle"); return []

    locked:   list[dict] = json.loads(locked_path.read_text())
    existing: list[dict] = json.loads(settled_path.read_text()) if settled_path.exists() else []
    settled_ids = {s.get("id","") for s in existing}
    new_settled  = list(existing)

    def game_index(games: list) -> dict:
        idx = {}
        for g in games:
            h, a  = g.get("home",""), g.get("away","")
            state = str(g.get("state",""))
            if state in ("post","FINAL","OFF","7","F","OT","SO") and h and a:
                idx[(h,a)] = g; idx[(a,h)] = g
        return idx

    all_idx: dict = {}
    all_idx.update(game_index(nba_final))
    all_idx.update(game_index(mlb_final))
    all_idx.update(game_index(nhl_final))

    for bet in locked:
        if bet.get("outcome","pending") != "pending": continue
        bet_id = bet.get("id","")
        if bet_id in settled_ids: continue
        hA, awA = bet.get("hA","") or bet.get("home",""), bet.get("awA","") or bet.get("away","")
        game = all_idx.get((hA, awA))
        if not game: continue
        hs, as_ = int(game.get("homeScore") or 0), int(game.get("awayScore") or 0)
        bet_type = str(bet.get("betType","ML")).upper()
        outcome  = None

        if bet_type == "ML":
            winner  = game.get("home") if hs > as_ else game.get("away")
            outcome = "win" if bet.get("team","") == winner else "loss"

        elif bet_type in ("RL","PL","RUNLINE","PUCKLINE","SPREAD"):
            line    = float(str(bet.get("line","1.5")).replace("+","").replace("−","-") or 1.5)
            is_home = bet.get("team","") == game.get("home","")
            diff    = (hs - as_) if is_home else (as_ - hs)
            outcome = "win" if diff+line > 0 else ("push" if diff+line == 0 else "loss")

        elif bet_type in ("OU","OVER_UNDER","TOTAL"):
            total   = hs + as_
            lv      = float(bet.get("ou") or bet.get("line") or 0)
            over    = "OVER" in str(bet.get("betOn","")).upper() or bet.get("over", True)
            outcome = ("win" if total > lv else ("push" if total == lv else "loss")) if over else \
                      ("win" if total < lv else ("push" if total == lv else "loss"))

        elif bet_type == "PROP":
            continue   # props need player stat data — skip server-side settle

        if outcome:
            sb = {**bet, "outcome": outcome, "settledAt": NOW.isoformat(),
                  "hScore": hs, "aScore": as_}
            new_settled.append(sb)
            settled_ids.add(bet_id)
            log(f"  SETTLED: {bet.get('betOn',bet.get('pick',''))} → {outcome.upper()}")

    settled_path.write_text(json.dumps(new_settled, indent=2))
    log(f"Auto-settle: {len(new_settled)} total settled")
    return new_settled

# ═══════════════════════════════════════════════════════════════════════════════
# Bet History (persistent log + CSV export)
# ═══════════════════════════════════════════════════════════════════════════════
def load_bet_history() -> list[dict]:
    if BET_HISTORY_JSON.exists():
        return json.loads(BET_HISTORY_JSON.read_text())
    return []

def merge_settled_to_history(settled: list[dict]) -> list[dict]:
    history     = load_bet_history()
    existing_ids = {b.get("id","") for b in history}
    new_bets    = [b for b in settled if b.get("id","") and b.get("id","") not in existing_ids]
    history.extend(new_bets)
    BET_HISTORY_JSON.write_text(json.dumps(history, indent=2))
    if new_bets: note(f"Bet history: +{len(new_bets)} new records ({len(history)} total)")
    return history

def export_bet_history_csv(history: list[dict]) -> None:
    if not history: return
    fields = [
        "id","sport","game","betOn","betType","pick","line","ou","prob",
        "ev","evGrade","confidence","ml","grade","outcome","settledAt",
        "hScore","aScore","note","date",
    ]
    with open(BET_HISTORY_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(history)
    docs_csv = ROOT / "docs" / "bet_history.csv"
    shutil.copy(BET_HISTORY_CSV, docs_csv)
    note(f"Bet history CSV: {len(history)} rows → docs/bet_history.csv")

def build_overall_stats(history: list[dict]) -> dict:
    """Aggregate win/loss/push from bet history for Overall tab."""
    from collections import defaultdict
    total: dict = {"w":0,"l":0,"push":0,"total":0}
    by_sport: dict = defaultdict(lambda: {"w":0,"l":0,"push":0,"total":0})
    by_type:  dict = defaultdict(lambda: {"w":0,"l":0,"push":0,"total":0})
    by_date:  dict = defaultdict(lambda: {"w":0,"l":0,"push":0,"total":0})

    # outcome -> bucket key. Was `o if o!="win" else "w"`, which correctly
    # mapped "win"->"w" but left "loss" mapping to itself ("loss") instead
    # of "l" - every bucket's "l" count silently stayed 0 forever, on both
    # local and real Supabase-sourced history, since this ran regardless
    # of data source. Only just discovered because this was the first time
    # real settled-bet data (with actual losses in it) flowed through here.
    _outcome_key = {"win": "w", "loss": "l", "push": "push"}
    for b in history:
        o = b.get("outcome","pending")
        if o not in ("win","loss","push"): continue
        sport = b.get("sport","?")
        btype = b.get("betType","ML")
        day   = (b.get("settledAt","") or b.get("date",""))[:10]
        k = _outcome_key[o]
        for bucket in (total, by_sport[sport], by_type[btype], by_date[day]):
            bucket[k] = bucket.get(k,0) + 1
            bucket["total"] = bucket.get("total",0) + 1

    def pct(d): return round(d["w"]/d["total"]*100,1) if d["total"] else 0
    return {
        "total":     {**total, "pct": pct(total)},
        "bySport":   {k: {**v,"pct":pct(v)} for k,v in by_sport.items()},
        "byBetType": {k: {**v,"pct":pct(v)} for k,v in by_type.items()},
        "byDate":    dict(sorted(by_date.items())[-30:]),   # last 30 days
        "fetchedAt": TODAY_ISO,
        "roi":       compute_roi(history),
        "streak":    compute_streak(history),
        "calibration": compute_calibration_metrics(history),
    }

def compute_roi(history: list[dict]) -> dict:
    """
    ROI and units (PnL) from settled bet history. `pnl` on each bet is
    unit-normalized (1 unit staked per bet - win: decOdds-1, loss: -1,
    push: 0 - see supabase_bets_to_history()), the same convention behind
    every "+187.4u"-style number already shown in the frontend. ROI is
    therefore computed as units won/lost per unit staked (pnl / n_bets),
    not against the dollar-style `wager` field - `totalWagered` is still
    reported for reference, but mixing it into the roi ratio itself would
    be off by ~100x (wager defaults to 100 "dollars" per bet; pnl is
    scaled in ~1s).
    """
    settled = [b for b in history if b.get("outcome") in ("win","loss","push")]
    if not settled:
        return {"roi": 0.0, "totalWagered": 0.0, "totalPnl": 0.0, "avgOdds": 0.0, "n": 0}
    wagered = sum(float(b.get("wager", 100)) for b in settled)
    pnl = sum(float(b.get("pnl") or 0) for b in settled)
    odds_sum = 0; odds_n = 0
    for b in settled:
        ml = b.get("ml") or b.get("line")
        try:
            dec = _ml_to_dec(float(ml))
            odds_sum += dec; odds_n += 1
        except Exception:
            pass
    return {
        "roi":          round(pnl / len(settled) * 100, 2),
        "totalWagered": round(wagered, 2),
        "totalPnl":     round(pnl, 2),
        "avgOdds":      round(odds_sum / odds_n, 3) if odds_n else 0.0,
        "n":            len(settled),
    }

def compute_streak(history: list[dict]) -> dict:
    """Current and longest win/loss streaks from settled history."""
    settled = sorted(
        [b for b in history if b.get("outcome") in ("win","loss")],
        key=lambda b: b.get("settledAt","") or b.get("date","")
    )
    if not settled:
        return {"current": 0, "direction": "W", "longestW": 0, "longestL": 0}
    cur = 1; cur_dir = "W" if settled[-1]["outcome"]=="win" else "L"
    for i in range(len(settled)-2, -1, -1):
        d = "W" if settled[i]["outcome"]=="win" else "L"
        if d == cur_dir: cur += 1
        else: break
    lw = ll = run = 0
    run_d = None
    for b in settled:
        d = "W" if b["outcome"]=="win" else "L"
        if d == run_d: run += 1
        else: run = 1; run_d = d
        if d == "W": lw = max(lw, run)
        else: ll = max(ll, run)
    return {"current": cur, "direction": cur_dir, "longestW": lw, "longestL": ll}

def compute_calibration_metrics(history: list[dict]) -> dict:
    """
    Brier score and log-loss — the actual quantitative "is this model any
    good" answer, computed from settled bets against the model's own
    winProb at lock time. Existing calibration UI in the frontend buckets
    predicted-vs-actual into bands (CALIBRATED/SLIGHT DRIFT/etc) but never
    surfaced a real proper-scoring-rule number; this fills that gap.

    Brier score = mean((predicted_prob - actual_outcome)^2). 0 = perfect,
    0.25 = a coin flip guessed at 50%, 1.0 = maximally wrong. Lower is
    better, and unlike raw win%, it punishes overconfidence directly (a
    bet locked at 90% that loses costs 0.81; the same loss at 55% only
    costs 0.30).

    Log-loss = mean(-log(p_actual_outcome)), clipped away from 0/1 to
    avoid -inf. Also lower-is-better; punishes confident wrong calls even
    more sharply than Brier (a 95% "sure thing" that loses costs ~3.0,
    versus Brier's 0.9).

    Also returns a 10-bucket reliability-diagram table (predicted prob
    decile -> actual win rate in that decile) — the same shape as the
    frontend's existing bucket UI, but computed server-side from the full
    settled history so it isn't limited to whatever's in this browser's
    localStorage.
    """
    import math
    settled = [
        b for b in history
        if b.get("outcome") in ("win", "loss")
        and b.get("winProb") is not None
    ]
    if not settled:
        return {"n": 0, "brier": None, "logLoss": None, "bySport": {}, "reliability": []}

    def _score(bets: list[dict]) -> dict:
        n = len(bets)
        if not n:
            return {"n": 0, "brier": None, "logLoss": None}
        brier_sum = 0.0
        ll_sum = 0.0
        eps = 1e-9
        for b in bets:
            p = max(0.0, min(1.0, float(b["winProb"])))
            y = 1.0 if b["outcome"] == "win" else 0.0
            brier_sum += (p - y) ** 2
            p_actual = p if y == 1.0 else (1.0 - p)
            ll_sum += -math.log(max(eps, min(1 - eps, p_actual)))
        return {"n": n, "brier": round(brier_sum / n, 4), "logLoss": round(ll_sum / n, 4)}

    from collections import defaultdict
    by_sport: dict = defaultdict(list)
    for b in settled:
        by_sport[b.get("sport", "?")].append(b)

    # 10-bucket reliability diagram: predicted-prob decile -> actual win rate
    buckets = [[] for _ in range(10)]
    for b in settled:
        p = max(0.0, min(0.999, float(b["winProb"])))
        buckets[int(p * 10)].append(b)
    reliability = []
    for i, bucket in enumerate(buckets):
        if not bucket:
            continue
        wins = sum(1 for b in bucket if b["outcome"] == "win")
        reliability.append({
            "predictedLow": i / 10, "predictedHigh": (i + 1) / 10,
            "predictedMid": round((i + 0.5) / 10, 2),
            "actualWinRate": round(wins / len(bucket), 4),
            "n": len(bucket),
        })

    return {
        **_score(settled),
        "bySport": {k: _score(v) for k, v in by_sport.items()},
        "reliability": reliability,
        "fetchedAt": TODAY_ISO,
    }

def surface_best_bets_for_day(best_bets: list[dict], n: int = 6) -> list[dict]:
    """Top N deduped picks by composite EV+confidence score for the home tab hero section.
    Always filters to TODAY_ISO and excludes entries with date-like game fields."""
    _date_like_sbd = re.compile(r"^\d{2}/\d{2}/\d{2,4}$")
    seen_games: set = set()
    ranked = sorted(
        [b for b in best_bets
         if b.get("date", TODAY_ISO) == TODAY_ISO
         and not _date_like_sbd.match(str(b.get("game", "")))],
        key=lambda b: (b.get("ev", 0) or 0) * 0.6 + (b.get("confidence", 0) or 0) * 0.4,
        reverse=True
    )
    out = []
    for b in ranked:
        game_key = (b.get("sport",""), b.get("home","") or b.get("game",""))
        if game_key in seen_games: continue
        seen_games.add(game_key)
        out.append(b)
        if len(out) >= n: break
    return out

def compute_live_win_prob(game: dict, sport: str) -> dict:
    """
    Estimate in-game win probability from score + game state.
    MLB: score diff + innings remaining via logit model.
    NBA: point diff + time remaining (linear regression).
    NHL: goal diff + period + time remaining.
    """
    h = game.get("hScore", 0) or 0
    a = game.get("aScore", 0) or 0
    diff = h - a
    import math

    if sport == "mlb":
        inning = game.get("inning", 5) or 5
        innings_left = max(0, 9 - inning)
        # Run expectancy: ~0.5 runs/inning regression to mean
        # logit(p) = diff * 0.45 - 0.015 * innings_left * abs(diff)
        logit_val = diff * 0.45 - 0.015 * innings_left * abs(diff)
        home_wp = round(1 / (1 + math.exp(-logit_val)), 3)
    elif sport == "nba":
        quarter = game.get("period", 4) or 4
        mins_elapsed = (quarter - 1) * 12 + (game.get("minutesElapsed") or ((quarter-1)*12))
        mins_left = max(0, 48 - mins_elapsed)
        # ~2.5 pts per minute variance; logit from FiveThirtyEight-style model
        sigma = math.sqrt(mins_left * 2.5)
        logit_val = diff / (sigma + 1e-9) * 1.5
        home_wp = round(1 / (1 + math.exp(-logit_val)), 3)
    elif sport == "nhl":
        period = game.get("period", 3) or 3
        mins_left_est = max(0, (3 - period) * 20)
        # ~0.15 goals/min base; logit from goal diff + time
        logit_val = diff * 0.55 - 0.008 * mins_left_est * abs(diff)
        home_wp = round(1 / (1 + math.exp(-logit_val)), 3)
    else:
        home_wp = 0.5

    return {
        "homeWinProb": home_wp,
        "awayWinProb": round(1 - home_wp, 3),
        "hScore": h,
        "aScore": a,
        "gameId": game.get("id",""),
        "home": game.get("home",""),
        "away": game.get("away",""),
        "state": game.get("state",""),
    }

# ═══════════════════════════════════════════════════════════════════════════════
# data.json writer + HTML timestamp patcher
# ═══════════════════════════════════════════════════════════════════════════════
def write_data_json(bundle: dict) -> None:
    payload = json.dumps(bundle, indent=2)
    FE_DATA.write_text(payload)
    note(f"data.json written ({len(payload)//1024} KB) → docs/ (github.io)")
    # Write version.json — mobile PWA reads this to detect when a new build is deployed
    version_payload = json.dumps({"built": TODAY_ISO.replace("-","")[:8]+"-"+datetime.now().strftime("%H%M"), "ts": int(time.time())}, indent=2)
    (ROOT / "docs" / "version.json").write_text(version_payload)
    note("version.json updated for mobile freshness check")

def patch_html_timestamp() -> None:
    # FE is now docs/app.html — source of truth.
    # index.html is kept as an identical copy (GitHub Pages serves index.html at root).
    if not FE.exists(): return
    html   = FE.read_text(encoding="utf-8")
    ts_pat = r"(LAST_AUTO_UPDATE\s*=\s*['\"])([^'\"]*?)(['\"])"
    if re.search(ts_pat, html):
        html = re.sub(ts_pat, rf"\g<1>{TS_DISPLAY}\g<3>", html)
    else:
        html = html.replace("<script>",
            f'<script>\nconst LAST_AUTO_UPDATE = "{TS_DISPLAY}";\n', 1)
    FE.write_text(html, encoding="utf-8")
    # Mirror to index.html (GitHub Pages root) — same content
    index_html = FE.parent / "index.html"
    index_html.write_text(html, encoding="utf-8")
    vlog(f"HTML timestamp patched → {TS_DISPLAY}")

# ═══════════════════════════════════════════════════════════════════════════════
# Git push
# ═══════════════════════════════════════════════════════════════════════════════
def git_push(summary: str = "") -> bool:
    try:
        subprocess.run(["git","-C",str(ROOT),"add",
            # engine SPA + data — served at purple-wraith.github.io/clairvoyance-backend/
            "docs/index.html",
            "docs/app.html",
            "docs/data.json",
            "docs/live_data.json",
            "docs/card.png",
            "docs/social_copy.json",
            "docs/bet_history.csv",
            "docs/soccer_fbref.json",
            "docs/mls_stats.json",
            "docs/mls_schedule.json",
            # persistent records
            "data/bet_history.json",
            "data/bet_history.csv",
            "data/soccer_fbref.json",
            "data/mls_stats.json",
            "data/mls_schedule.json",
        ], capture_output=True, check=False)
        diff = subprocess.run(["git","-C",str(ROOT),"diff","--cached","--quiet"],
                              capture_output=True)
        if diff.returncode == 0:
            log("git: nothing to commit"); return True
        msg = f"data: {TS_DISPLAY} auto-refresh\n\n{summary}\n\nhttps://purple-wraith.github.io/clairvoyance-backend/app.html"
        subprocess.run(["git","-C",str(ROOT),"commit","-m",msg], check=True, capture_output=True)
        try:
            subprocess.run(["git","-C",str(ROOT),"push","origin","main"], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            # Real bug, found and fixed in run_live_window's own push (this
            # repo also gets pushed to by GitHub Actions workflows running
            # concurrently, so a non-fast-forward rejection here isn't rare)
            # -- previously this just alerted and gave up for the whole run,
            # leaving the rejected commit to alert again identically on the
            # NEXT scheduled run hours later, since nothing ever reconciled
            # with origin in between. One fetch+rebase retry lets a
            # transient race self-heal within the same run instead.
            subprocess.run(["git","-C",str(ROOT),"fetch","origin","main"], check=True, capture_output=True)
            rebase_res = subprocess.run(["git","-C",str(ROOT),"rebase","origin/main"], capture_output=True)
            if rebase_res.returncode != 0:
                subprocess.run(["git","-C",str(ROOT),"rebase","--abort"], capture_output=True)
                raise
            subprocess.run(["git","-C",str(ROOT),"push","origin","main"], check=True, capture_output=True)
        note("git push → main ✓")
        return True
    except Exception as exc:
        log(f"git push failed: {exc}", "WARN")
        _alert("Push Failed", f"git push → main failed: {exc}", "error")
        return False

def verify_deployment(retries: int = 3, delay_sec: int = 20) -> bool:
    """
    Post-deploy safety net: after a successful push, GitHub Pages takes a
    little while to rebuild, so poll the LIVE data.json a few times and
    confirm it (a) actually loads, (b) parses as JSON, and (c) has
    non-trivial content — catches the class of failure validate.py can't:
    a commit that was locally valid but somehow deployed broken/empty (CDN
    issue, a genuinely empty bestBets from a bad scrape that slipped past
    other checks, etc). Alerts on failure rather than auto-reverting —
    reverting automatically risks compounding a bad situation if the
    failure is transient (Pages still rebuilding, a flaky CDN edge) rather
    than a real bad deploy.
    """
    url = "https://purple-wraith.github.io/clairvoyance-backend/data.json"
    for attempt in range(retries):
        try:
            time.sleep(delay_sec)
            r = requests.get(f"{url}?_v={int(time.time())}", timeout=15)
            r.raise_for_status()
            d = r.json()
            games_total = (len(d.get("nba", {}).get("today", []))
                           + len(d.get("nhl", {}).get("today", []))
                           + len(d.get("pwhl", {}).get("today", [])))
            bets_total = len(d.get("bestBets", [])) + len(d.get("heroPicksForDay", []))
            has_content = bool(d.get("generated")) and (games_total > 0 or bets_total > 0)
            if has_content:
                note(f"deployment verified ✓ ({games_total} games/matches today, {bets_total} best bets)")
                return True
            log(f"verify_deployment: live data.json parses but looks empty (attempt {attempt+1}/{retries})", "WARN")
        except Exception as exc:
            log(f"verify_deployment attempt {attempt+1}/{retries} failed: {exc}", "WARN")
    _alert("Deploy Verification Failed",
           f"Live data.json at {url} failed to verify after {retries} attempts post-push — "
           "site may be serving stale or broken data. Check manually.", "error")
    return False

# ═══════════════════════════════════════════════════════════════════════════════
# Live Window  (17:00–23:00 MT continuous refresh)
# ═══════════════════════════════════════════════════════════════════════════════
def run_live_window(push: bool = True, interval_sec: int = 120) -> None:
    log("=== LIVE WINDOW MODE STARTED ===")
    live_data_fe   = ROOT / "docs" / "live_data.json"        # served at github.io

    while True:
        try:
            now_mt = datetime.now().astimezone()  # uses system TZ (set to America/Denver in cron)
        except Exception:
            now_mt = datetime.now()
        hour = now_mt.hour
        if hour >= 23 or hour < 16:
            log("=== LIVE WINDOW END (outside 16:00-23:00 MT) ==="); break

        log(f"Live refresh {now_mt.strftime('%H:%M')}…")
        try:
            # MLB retired 2026-09-08 -- no longer fetched here.
            nba_t, _  = fetch_nba_scoreboard()
            nhl_t, _  = fetch_nhl_today()
            live_bundle = {
                "generatedMT": now_mt.isoformat(),
                "ts":          now_mt.strftime("%H:%M MT"),
                "nbaLive":     [g for g in nba_t  if g.get("state") == "in"],
                "nhlLive":     [g for g in nhl_t  if g.get("state") in ("LIVE","CRIT","IN")],
                "nbaAll":      nba_t,
                "nhlAll":      nhl_t,
            }
            live_probs = {"nba":[], "nhl":[]}
            for g in live_bundle["nbaLive"]:
                live_probs["nba"].append(compute_live_win_prob(g, "nba"))
            for g in live_bundle["nhlLive"]:
                live_probs["nhl"].append(compute_live_win_prob(g, "nhl"))
            live_bundle["liveProbs"] = live_probs
            live_bundle["autoSettled"] = auto_settle([], live_bundle["nbaAll"], live_bundle["nhlAll"])
            live_data_fe.write_text(json.dumps(live_bundle, indent=2))

            if push:
                subprocess.run(["git","-C",str(ROOT),"add",
                    "docs/live_data.json"], capture_output=True)
                diff = subprocess.run(["git","-C",str(ROOT),"diff","--cached","--quiet"],
                                      capture_output=True)
                if diff.returncode != 0:
                    subprocess.run(["git","-C",str(ROOT),"commit","-m",
                        f"live: {now_mt.strftime('%H:%M')} MT scores"], capture_output=True)
                    # Real bug, found and fixed: this push had NO error
                    # checking at all (no check=True, no try/except, no
                    # alert) -- unlike git_push() above, which does. A
                    # rejected push (non-fast-forward -- this repo also
                    # gets pushed to by GitHub Actions workflows running
                    # concurrently) failed COMPLETELY SILENTLY, every
                    # 120s, forever: no exception, no log line, no alert.
                    # Confirmed live: 41 local commits piled up unpushed
                    # over several hours with zero visible sign anything
                    # was wrong. Fixed with a fetch+rebase retry so a
                    # rejection self-heals on the very next push instead
                    # of accumulating, plus an actual alert if it still
                    # can't recover.
                    push_res = subprocess.run(["git","-C",str(ROOT),"push","origin","main"],
                                   capture_output=True, text=True)
                    if push_res.returncode != 0:
                        subprocess.run(["git","-C",str(ROOT),"fetch","origin","main"], capture_output=True)
                        rebase_res = subprocess.run(["git","-C",str(ROOT),"rebase","origin/main"], capture_output=True)
                        if rebase_res.returncode == 0:
                            retry_res = subprocess.run(["git","-C",str(ROOT),"push","origin","main"],
                                           capture_output=True, text=True)
                            if retry_res.returncode != 0:
                                log(f"live push retry failed: {retry_res.stderr}", "WARN")
                                _alert("Live Push Failed", f"git push (after rebase retry) failed: {retry_res.stderr[:300]}", "error")
                        else:
                            subprocess.run(["git","-C",str(ROOT),"rebase","--abort"], capture_output=True)
                            log(f"live push failed, rebase also failed: {push_res.stderr}", "WARN")
                            _alert("Live Push Failed", f"git push failed and rebase onto origin/main failed too -- local commits are accumulating unpushed: {push_res.stderr[:300]}", "error")
        except Exception as exc:
            log(f"Live refresh error: {exc}", "WARN")
        time.sleep(interval_sec)

# ═══════════════════════════════════════════════════════════════════════════════
# Main orchestrator
# ═══════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
# NHL core refresh (--only-nhl-core) -- the fast NHL pieces of docs/data.json, for scripts/lock_prep.py's "data-nhl" job
# ═══════════════════════════════════════════════════════════════════════════════
# The full run above lands 3x/day and GitHub delays it 3-6h, so a lock pass could read NHL standings that miss a quarter of the
# finished games.  This entry point re-fetches ONLY standings / edge / MoneyPuck / skaterValue / NHL injuries with the SAME fetch
# functions and merges them into the existing docs/data.json (read-modify-write, every other key untouched, atomic replace).
# Fail-open: any error (or a suspiciously small/empty fetch) leaves the corresponding part -- or the whole file -- as it was.
# merge rules + atomic write live in scripts/_nhl_core.py (pure, unit-tested in test_lock_prep_data.py)


def run_nhl_core_refresh(path: Path | None = None, dry_run: bool = False) -> int:
    """Refresh the fast NHL parts of `path` (default docs/data.json).  ALWAYS returns 0 (fail-open, like lock_prep's other jobs)."""
    from concurrent.futures import ThreadPoolExecutor
    from _nhl_core import atomic_write_text, merge_nhl_core
    path = path or FE_DATA
    t0 = time.time()
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except Exception as exc:
        log(f"NHL core: cannot read {path} ({exc}) -- leaving it untouched", "WARN")
        return 0
    jobs = {"standings": fetch_nhl_standings, "edge": fetch_nhl_edge, "mp": fetch_moneypuck,
            "skater_value": fetch_nhl_skater_value,
            "injuries": lambda: fetch_espn_injuries(ESPN_LEAGUE_PATHS["nhl"], "nhl")}
    res: dict = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {k: ex.submit(fn) for k, fn in jobs.items()}
        for k, f in futs.items():
            try:
                res[k] = f.result()
            except Exception as exc:
                log(f"NHL core: {k} failed ({exc}) -- keeping the existing value", "WARN")
                res[k] = None
    try:
        data, changed = merge_nhl_core(data, res.get("standings") or {}, res.get("edge") or {}, res.get("mp") or {},
                                       res.get("skater_value") or {}, res.get("injuries") or [], NOW.isoformat())
    except Exception as exc:
        log(f"NHL core: merge failed ({exc}) -- {path.name} untouched", "WARN")
        return 0
    if not changed:
        log(f"NHL core: nothing usable fetched -- {path.name} untouched ({time.time() - t0:.0f}s)", "WARN")
        return 0
    if dry_run:
        log(f"NHL core: dry run -- would replace {', '.join(changed)} in {path} ({time.time() - t0:.0f}s)")
        return 0
    try:
        atomic_write_text(path, json.dumps(data, indent=2))
    except Exception as exc:
        log(f"NHL core: write failed ({exc}) -- {path.name} untouched", "WARN")
        return 0
    log(f"NHL core: refreshed {', '.join(changed)} in {path} in {time.time() - t0:.0f}s")
    return 0


def main() -> None:
    global _verbose

    parser = argparse.ArgumentParser(description="Clairvoyance v6.0 data refresh")
    parser.add_argument("--push",          action="store_true", help="Commit + push to GitHub")
    parser.add_argument("--dry-run",       action="store_true", help="Fetch only, no writes")
    parser.add_argument("--no-linemate",   action="store_true", help="No-op — Linemate scraper removed 2026-09-10 (kept so scheduled-refresh.yml/manual-sync.yml, which still pass this flag, don't fail argparse)")
    parser.add_argument("--no-reference",  action="store_true", help="Skip Baseball/Basketball/Hockey Reference")
    parser.add_argument("--mode",          choices=["full","live","props"], default="full")
    parser.add_argument("--sport",         choices=["nba","nhl","nfl","soccer","all"], default="all")
    parser.add_argument("--verbose","-v",  action="store_true")
    parser.add_argument("--only-nhl-core", action="store_true",
                        help="Refresh ONLY the fast NHL pieces (standings/edge/MoneyPuck/skaterValue/NHL injuries) inside the existing "
                             "data.json and exit 0 even on failure (scripts/lock_prep.py's data-nhl job). Writes nothing else.")
    parser.add_argument("--data-json", default=None, help="With --only-nhl-core: the data.json to merge into (default docs/data.json)")
    args    = parser.parse_args()
    _verbose = args.verbose

    if args.only_nhl_core:
        sys.exit(run_nhl_core_refresh(Path(args.data_json) if args.data_json else None, dry_run=args.dry_run))

    # ── live-window short-circuit ────────────────────────────────────────────
    if args.mode == "live":
        run_live_window(push=args.push)
        return

    log("=" * 60)
    log(f"Clairvoyance v6.0 — {TS_DISPLAY}")
    log(f"Mode: {args.mode} | Sport: {args.sport}")
    log("=" * 60)

    S = args.sport  # shorthand

    # ── props-only mode — RETIRED 2026-09-10 ─────────────────────────────────
    # This mode existed solely to run the Linemate scraper (fetch_linemate_
    # props/trends/cheatsheet) for NBA/NHL/NFL's daily props workflows.
    # linemate.io now login-walls every per-league page site-wide (see the
    # removed scraper's own comment, just above validate_props_against_
    # schedule() near the top of this file), so there is nothing left for
    # this mode to fetch. NFL/NBA/NHL props all now come from their own
    # real ESPN-stats + Monte Carlo generators, wired directly into
    # docs/app.html's render functions rather than through this pipeline.
    # Kept as a recognized (no-op) mode rather than removed outright since
    # daily-player-stats-refresh.yml still invokes it for all three sports.
    if args.mode == "props":
        log("props-only mode retired 2026-09-10 — Linemate is login-walled site-wide; NFL/NBA/NHL props now come from their own ESPN+MC-sim generators. No-op.", "WARN")
        return

    # ── full fetch phase ─────────────────────────────────────────────────────
    # Schedule accuracy: log the exact dates used per sport to confirm alignment
    log(f"Schedule dates → MLB/NBA: {TODAY_ET} (ET) · NHL/F1: {TODAY_ISO} (MT ISO)")
    # MLB is no longer tracked in the engine — retired 2026-09-08, purged
    # from the daily fetch (same pattern as F1/NCAA baseball below). Bundle
    # keys are kept (empty) so any remaining d.get('mlb', ...) reads don't
    # need matching changes.
    mlb_today: list      = []
    mlb_tom: list        = []
    mlb_standings: dict  = {}
    mlb_week: list       = []
    mlb_ref: dict        = {}
    mlb_bullpen: dict    = {}
    mlb_sabre: dict      = {}
    mlb_fielding: dict   = {}
    mlb_batters: dict    = {}
    mlb_statcast: dict   = {}
    mlb_nrfi: list       = []

    nba_today, nba_tom   = fetch_nba_scoreboard()         if S in ("nba","all") else ([],[])
    if S in ("nba","all"):
        _by_type: dict = {}
        for _g in nba_today + nba_tom:
            _by_type[_g.get("seasonType")] = _by_type.get(_g.get("seasonType"), 0) + 1
        log(f"NBA games today+tomorrow by ESPN seasonType (1=pre 2=reg 3=post 5=play-in): {_by_type or 'none'}")
    # Season-dependent NBA data (standings, player tiers, roster, BBRef team stats,
    # team ratings / Elo seed) -- all keyed off nba_season_end_year(), see above.
    _nba_sd = collect_nba_season_data(no_reference=args.no_reference) if S in ("nba","all") else {}
    nba_standings        = _nba_sd.get("standings", {})
    nba_players          = _nba_sd.get("players", [])
    nba_bracket          = fetch_nba_playoff_bracket()    if S in ("nba","all") else {}
    nba_ref              = (fetch_basketball_reference()  if not args.no_reference else {}) if S in ("nba","all") else {}
    nba_adv              = _nba_sd.get("teamAdv", {})
    nba_four_factors     = _nba_sd.get("fourFactors", {})
    nba_roster           = _nba_sd.get("roster", {})
    nba_team_ratings     = _nba_sd.get("teamRatings", {})
    nba_elo_seed         = _nba_sd.get("eloSeed", {})

    nhl_today, nhl_tom   = fetch_nhl_today()               if S in ("nhl","all") else ([],[])
    nhl_standings        = fetch_nhl_standings()          if S in ("nhl","all") else {}
    nhl_roster           = fetch_nhl_roster()             if S in ("nhl","all") else {}
    nhl_bracket          = fetch_nhl_playoff_bracket()    if S in ("nhl","all") else {}
    nhl_edge             = fetch_nhl_edge()               if S in ("nhl","all") else {}
    mp                   = fetch_moneypuck()              if S in ("nhl","all") else {}
    nhl_skater_value     = fetch_nhl_skater_value()       if S in ("nhl","all") else {}
    # hockeyviz / hockey-reference fetches retired 2026-10-03 (404 / stale + never read) -- see the RETIRED note above
    # fetch_nhl_skater_value().  The bundle keys stay present-but-empty below.
    hockeyviz: dict        = {"teams": {}}
    hockey_ref: dict       = {}
    hockey_ref_teams: dict = {}

    # F1 is no longer tracked in the engine — purged from the daily fetch.
    # Bundle keys are kept (empty) below so the frontend's d.get('f1',...)
    # reads don't need matching changes.
    f1_data: dict          = {}
    f1_analytics: dict     = {}
    f1_tracing: dict       = {}
    f1_calendar: list      = []
    f1_comprehensive: dict = {}
    f1_unchained: dict     = {}

    futures_odds     = fetch_futures_odds()

    # Weather (was MLB home teams -- MLB retired 2026-09-08, purged along
    # with its fetch above). Bundle key kept (empty) for the same reason.
    weather: dict = {}

    # Linemate — scraper removed 2026-09-10 (linemate.io login-walls every
    # per-league page site-wide now; see the removed fetch_linemate_props/
    # trends/cheatsheet's replacement comment near validate_props_against_
    # schedule() above). Dicts kept as always-empty so the bundle["linemate"]
    # shape below and every downstream .get("nba"/"nhl", []) reader stays
    # unchanged rather than needing an audit of every consumer for missing-
    # key safety. NFL/NBA/NHL props all come from their own real ESPN+MC-sim
    # generators now (docs/app.html's renderNFLModelProps/renderNBAProps/
    # renderNHLPropsLive), independent of this bundle key entirely.
    lm_props:  dict = {"nba":[],"mlb":[],"nhl":[],"wnba":[],"nfl":[]}
    lm_trends: dict = {"nba":[],"mlb":[],"nhl":[],"wnba":[],"nfl":[]}
    lm_form:   dict = {"nba":[],"mlb":[],"nhl":[],"wnba":[],"nfl":[]}

    # NCAA Baseball + WNBA + PWHL
    # NCAA baseball is no longer tracked in the engine — purged from the
    # daily fetch. Bundle key kept (empty) below for the same reason as F1.
    # WNBA was fully retired 2026-09-08 (same reason) -- removed from
    # app.html and this pipeline entirely, not merely kept off paid
    # products (see auto_lock_settle.py's PRODUCT_SPORTS comment).
    ncaa_baseball: dict = {}
    wnba: dict          = {}
    # PWHL: no reader anywhere (docs/app.html, scripts/, workflows) -- the scoreboard/standings calls only produced 400/500 WARNs. Bundle key kept (empty); fetch_pwhl() stays defined.
    pwhl: dict          = {}

    # Soccer — Champions League / Premier League / La Liga / Bundesliga / MLS
    # / Serie A. Written to its own file (docs/soccer_fbref.json) rather than
    # folded into the giant bundle, since the frontend only needs to fetch
    # this one small file to replace/refresh its static xG fallback table.
    soccer_fbref = fetch_soccer_team_stats_all() if S in ("soccer","all") else {}
    # Injury-integration roster coverage for the 4 non-MLS leagues — these
    # already have a real win-probability model (_soccerMC's xG/Poisson
    # engine, same one MLS uses), the roster map was the only missing piece
    # for computeInjuryImpact() to key off. Stored alongside each league's
    # existing "teams" xG data in soccer_fbref.json so the frontend only
    # needs the one file it already fetches (loadSoccerFBref()).
    # DISABLED 2026-10-03 (SOCCER_ROSTER_FETCH_ENABLED): rosters existed ONLY so computeInjuryImpact() could map an injured
    # player's name to a club -- and ESPN publishes NO soccer injuries: /soccer/{eng.1,esp.1,ger.1,ita.1,UEFA.champions,usa.1}/
    # injuries returns {"injuries": []} for every league (verified live 2026-10-03, in season), and the roster endpoint's per-player
    # `injuries` arrays are empty too.  So ~96 team-roster requests per run and ~1 MB of soccer_fbref.json (3,400 players, the
    # file the app downloads) fed an injury impact that is always 0.  Nothing else reads `rosters` (grep).  Flip the constant
    # back to True only if ESPN ever starts publishing soccer injuries; the app-side readers are still in place and fail open.
    if S in ("soccer","all") and SOCCER_ROSTER_FETCH_ENABLED:
        for _lkey, _lcfg in ESPN_SOCCER_LEAGUES.items():
            if _lkey == "mls":
                continue  # MLS fully retired 2026-09-27 -- no roster fetch at all
                          # anymore (fetch_mls_rosters() itself is no longer called
                          # either, see the mls_rosters comment near the bottom of
                          # main()), this isn't "fetched elsewhere" like it used to be
            _rosters = fetch_espn_soccer_rosters(_lcfg["espn"], _lcfg["name"])
            if _rosters:
                soccer_fbref.setdefault(_lkey, {"league": _lcfg["name"], "fetchedAt": TODAY_ISO, "teams": {}})
                soccer_fbref[_lkey]["rosters"] = _rosters
            time.sleep(0.3)
    # MLS fully retired 2026-09-27 (see auto_lock_settle.py's PRODUCT_SPORTS
    # comment) -- real bug found + fixed here 2026-10-01: this block kept
    # calling fetch_mls_team_stats()/fetch_mls_standings()/fetch_mls_schedule()/
    # fetch_mls_rosters() live against mlssoccer.com/ESPN on every single
    # "soccer"/"all" scoped run, then wrote the results straight to
    # docs/mls_stats.json and docs/mls_schedule.json -- MLS had been removed
    # from app.html's nav/tabs/products/news feeds, but nobody had actually
    # stopped fetching or publishing its live data here. Hardcoded empty the
    # same way WNBA/NCAA baseball/MLB are elsewhere in this function, so the
    # bundle shape below is unchanged for any downstream .get() reader, and
    # the two JSON files simply stop being refreshed (the file-write blocks
    # below are already gated on these being non-empty, and the old
    # _check_source_health calls that used to assume MLS always has 30
    # in-season clubs were removed with the fetch, so retirement doesn't
    # trip a false "scraper may be broken" alert on every run).
    mls_stats: dict = {}
    mls_standings: list = []
    mls_schedule: list = []
    mls_rosters: dict = {}

    # Weather for MLS home clubs -- removed alongside the fetch above (same
    # retirement); mls_schedule is now always empty so this loop would never
    # have run anyway, but skipping the log()/fetch_soccer_weather() calls
    # entirely avoids a misleading "Fetching MLS weather…" log line every run.
    soccer_weather: dict = {}
    if mls_stats.get("teams"):
        # soccer_fbref["mls"] keeps the slim xg/npxg/xag/poss schema the
        # frontend's _socXGFromFBref() already reads for every league, so
        # nothing downstream breaks; the full 30+ field mlssoccer.com payload
        # goes to its own docs/mls_stats.json for the Monte Carlo layer to
        # pull richer signal from without the frontend needing to change.
        # Preserve matchLog/recentForm/homeSplit/awaySplit from whatever
        # fetch_soccer_team_stats_all() already put in soccer_fbref["mls"]
        # (via fetch_espn_soccer_league("mls")) before this overwrites the
        # rest of each team's entry -- MLS's real mlssoccer.com stats don't
        # include match-by-match history, only season aggregates, so this
        # is the only place that data comes from for MLS.
        _mls_prior = (soccer_fbref.get("mls") or {}).get("teams") or {}
        normalized = {}
        for name, t in mls_stats["teams"].items():
            gp = t.get("mp") or 1
            # Season TOTALS, not per-game — the frontend's _socXGFromFBref()
            # divides every field by mp itself (same convention FBref/ESPN
            # use), so storing already-per-game values here would silently
            # halve/shrink everything a second time.
            normalized[name] = {
                "mp": gp, "poss": (t.get("possession_ratio") or 0) * 100 if (t.get("possession_ratio") or 0) <= 1 else t.get("possession_ratio"),
                "gf": t.get("goals", 0), "xg": t.get("xG", 0), "npxg": t.get("xG", 0),
                "xag": t.get("assists", 0),
                "ga": t.get("goals_conceded", 0), "xga": t.get("goals_conceded", 0),  # no true xGA field from this API; goals-conceded proxy
                "shots_pg": round((t.get("shots", 0) or 0) / gp, 2),
                "sot_pg": round((t.get("shots_on_target", 0) or 0) / gp, 2),
                "src": "mlssoccer",
            }
            # mlssoccer.com and ESPN name clubs differently ("Los Angeles
            # Football Club" vs "lafc", "New York City Football Club" vs
            # "New York City FC") -- an exact dict-key lookup here silently
            # dropped matchLog/recentForm/homeSplit/awaySplit for 5 of 30
            # clubs (verified live: Atlanta United, LAFC, NYCFC, Orlando
            # City, Vancouver Whitecaps all failed to match). _fuzzy_
            # mls_name_lookup resolves the same 5 via suffix-stripping
            # ("football club"/"soccer club"/"fc"/"sc"/etc, both sides)
            # plus an acronym fallback for the LAFC case neither substring
            # nor suffix-stripping alone reaches.
            prior = _fuzzy_mls_name_lookup(name, _mls_prior)
            if prior:
                for f in ("matchLog", "recentForm", "homeSplit", "awaySplit"):
                    if f in prior:
                        normalized[name][f] = prior[f]
        soccer_fbref["mls"] = {"league": "MLS", "fetchedAt": TODAY_ISO, "teams": normalized}
        (ROOT / "docs" / "mls_stats.json").write_text(json.dumps(mls_stats, indent=2))
        (DATA / "mls_stats.json").write_text(json.dumps(mls_stats, indent=2))
        note("mls_stats.json written (full mlssoccer.com club stats)")
    if soccer_fbref:
        (ROOT / "docs" / "soccer_fbref.json").write_text(json.dumps(soccer_fbref, indent=2))
        (DATA / "soccer_fbref.json").write_text(json.dumps(soccer_fbref, indent=2))
        note("soccer_fbref.json written")
    # Soccer standings snapshot (docs/soccer_standings.json) is written by
    # its own dedicated daily script (scripts/scrape_soccer_standings.py /
    # daily-schedules-refresh.yml), not here -- standings move slowly enough
    # that bundling it into this 3x/day pipeline would just be redundant
    # API load for no real freshness gain.
    mls_bundle = {"fetchedAt": TODAY_ISO, "standings": mls_standings, "schedule": mls_schedule, "weather": soccer_weather, "rosters": mls_rosters}
    if mls_standings or mls_schedule:
        (ROOT / "docs" / "mls_schedule.json").write_text(json.dumps(mls_bundle, indent=2))
        (DATA / "mls_schedule.json").write_text(json.dumps(mls_bundle, indent=2))
        note("mls_schedule.json written")

    # Week schedules
    mlb_week_schedule: list = []  # MLB retired 2026-09-08
    # limit 15, not 8: a full NBA regular-season slate is up to 15 games and the
    # old cap of 8 silently truncated busy nights (preseason slates are small,
    # so this only bites from Oct 21).
    nba_week_schedule = fetch_week_schedule("basketball/nba","nba",15)     if S in ("nba","all") else []
    nhl_week_schedule = fetch_week_schedule("hockey/nhl","nhl",8)          if S in ("nhl","all") else []

    # News + injuries + transactions — all now cover every tracked league,
    # not just the original MLB/NBA/NHL(/WNBA) subset.
    sports_news  = fetch_sports_news()
    injuries     = fetch_injuries_all()
    transactions = fetch_transactions_all()

    # Best bets + auto-settle
    # Best odds per sport (ESPN odds only -- The Odds API removed 2026-10-03)
    mlb_best_odds: dict = {}  # MLB retired 2026-09-08
    nba_best_odds = fetch_best_odds("nba", nba_today) if S in ("nba","all") else {}
    nhl_best_odds = fetch_best_odds("nhl", nhl_today) if S in ("nhl","all") else {}
    # WNBA/NFL/CFB weren't covered server-side before — the frontend was
    # instead making its own live client-side Odds API calls for these (plus
    # soccer), using a key hardcoded in the shipped page source. That both
    # leaked a real secret publicly and multiplied quota usage across every
    # visitor's browser against the same 500 req/month free-tier budget,
    # which is what caused the 401s on exactly these sports. Fetching them
    # here instead means one shared call per scheduled run, not one per
    # visitor per page load. Soccer leagues aren't covered yet — their team
    # names need their own name->abbr map, tracked as a follow-up.
    wnba_best_odds: dict = {}  # WNBA retired 2026-09-08
    # nfl/cfb/pl/liga were Odds-API-only (game_list=[] => always {} on the ESPN path) -- retired 2026-10-03 with the API; keys stay present-but-empty.
    nfl_best_odds: dict  = {}
    cfb_best_odds: dict  = {}
    # Soccer leagues — the piece explicitly deferred in the last odds pass.
    # Club leagues (PL/La Liga/Bundesliga/MLS) key by normalized full club
    # name (_soccer_club_key) to match how soccer_fbref.json already keys
    # its team data; World Cup keys by the same 3-letter country codes
    # WC26_SCHEDULE already uses (_wc_name_to_abbr).
    pl_best_odds: dict   = {}
    liga_best_odds: dict = {}
    # bl_best_odds/mls_best_odds hardcoded empty 2026-10-01 -- real bug found
    # in this retired-league audit, same class as mls_stats/mls_standings/
    # mls_schedule/mls_rosters above: both Bundesliga (2026-09-23) and MLS
    # (2026-09-27) are fully retired, but this kept calling fetch_best_odds()
    # live against the Odds API for both every "soccer"/"all" run. Neither
    # was ever actually consumed downstream either way -- _backfill_odds()
    # a few lines below only ever applies to mlb/nba/nhl/wnba, never to any
    # soccer league, so this was a live fetch for a retired league feeding a
    # bestOddsExt.bl/bestOddsExt.mls key nothing in docs/app.html reads.
    bl_best_odds: dict = {}  # Bundesliga retired 2026-09-23
    mls_best_odds: dict = {}  # MLS retired 2026-09-27
    wc_best_odds: dict = {}  # World Cup retired 2026-09-08

    # Backfill real book odds into game objects so the app displays them
    def _backfill_odds(game_list: list, odds_map: dict) -> None:
        for g in game_list:
            key = f"{g.get('home','')}:{g.get('away','')}"
            bk  = odds_map.get(key, {})
            if bk.get("homeML") is not None:
                g["homeML"] = bk["homeML"]
            if bk.get("awayML") is not None:
                g["awayML"] = bk["awayML"]
            if bk.get("ou") is not None:
                g["ou"] = bk["ou"]
            if bk.get("book"):
                g["oddsBook"] = bk["book"]
    _backfill_odds(mlb_today, mlb_best_odds)
    _backfill_odds(nba_today, nba_best_odds)
    _backfill_odds(nhl_today, nhl_best_odds)
    _backfill_odds(wnba.get("today", []), wnba_best_odds)

    best_bets = calculate_best_bets(
        nba_today, mlb_today, nhl_today, weather, mp, nhl_edge,
        nhl_props=lm_props.get("nhl", []),
        nhl_trends=lm_trends.get("nhl", []),
        mlb_sabre=mlb_sabre,
        best_odds={
            **mlb_best_odds, **nba_best_odds, **nhl_best_odds,
            "_wnba_today":  wnba.get("today", []),
            "_ncaa_today":  ncaa_baseball.get("today", []),
        },
        nba_adv=nba_adv,
        mlb_standings=mlb_standings,
    )
    # Merge F1 race bets
    if f1_comprehensive.get("raceBets"):
        best_bets = best_bets + [b for b in f1_comprehensive["raceBets"] if b.get("ev", 0) > 0]
        log(f"F1 race bets merged: +{len(f1_comprehensive['raceBets'])} → {len(best_bets)} total")
    settled   = auto_settle(
        nba_today + nba_tom,
        mlb_today + mlb_tom,
        nhl_today + nhl_tom,
    )
    # Real bet ledger lives in Supabase now (mirrored from the browser) —
    # run the same settle/stale-audit logic against it so this actually
    # happens on schedule instead of only while a browser tab is open.
    supabase_history: list[dict] = []
    try:
        sb_result = run_supabase_automation(
            nba_today + nba_tom,
            mlb_today + mlb_tom,
            nhl_today + nhl_tom,
        )
        supabase_history = supabase_bets_to_history(sb_result.get("bets", []))
    except Exception as exc:
        log(f"Supabase automation: {exc}", "WARN")

    # Bet history — merges the local (legacy, effectively empty) path with
    # the real Supabase-mirrored ledger, deduped by id, so overallStats and
    # calibration reflect the actual 346-bet history rather than nothing.
    history = merge_settled_to_history(settled)
    if supabase_history:
        existing_ids = {b.get("id", "") for b in history}
        new_from_supabase = [b for b in supabase_history if b.get("id") not in existing_ids]
        history = history + new_from_supabase
        log(f"History: +{len(new_from_supabase)} from Supabase ledger ({len(history)} total)")
    # Drop synthetic/placeholder seed rows (e.g. a stray "test-0"/"test-10"
    # row with a sport tag but no actual bet content) before anything gets
    # exported or aggregated — this is what let a phantom FOOTBALL row show
    # up on the Sport Performance card during the NFL off-season. A row
    # only ever counts as junk if BOTH its id matches the test-N pattern
    # AND it has no real bet content, so a legitimately-named real bet can
    # never be swept up here.
    _junk_n = 0
    _clean_history = []
    for _h in history:
        _hid = str(_h.get("id") or "")
        if re.match(r"^test-\d+$", _hid, re.I) and not _h.get("betOn") and not _h.get("game") and not _h.get("pick"):
            _junk_n += 1
            continue
        _clean_history.append(_h)
    if _junk_n:
        log(f"History: dropped {_junk_n} synthetic/placeholder seed row(s)", "WARN")
    history = _clean_history
    export_bet_history_csv(history)
    overall_stats = build_overall_stats(history)

    # ── bundle ───────────────────────────────────────────────────────────────
    bundle: dict = {
        "generated":    NOW.isoformat(),
        "generatedMT":  TS_DISPLAY,
        "version":      "7.0",
        "mlb": {
            "today":        mlb_today,
            "tomorrow":     mlb_tom,
            "standings":    mlb_standings,
            "weekSchedule": mlb_week,
            "weekSchedule7": mlb_week_schedule,
            "nrfi":         mlb_nrfi,
            "sabre":        mlb_sabre,
            "fielding":     mlb_fielding,
            "reference":    mlb_ref,
            "bullpen":      mlb_bullpen,
            "batters":      mlb_batters,
            "statcast":     mlb_statcast,
        },
        "nba": {
            "today":        nba_today,
            "tomorrow":     nba_tom,
            "standings":    nba_standings,
            "players":      nba_players,
            "bracket":      nba_bracket,
            "reference":    nba_ref,
            "teamAdv":      nba_adv,
            "fourFactors":  nba_four_factors,
            "weekSchedule": nba_week_schedule,
            "roster":       nba_roster,
            # Season rollover (2026-10-03). season = ESPN/BBRef season-end year.
            # teamRatings: all-30-team prior/current record+margin+Elo; eloSeed: flat
            # {ESPN_abbr: elo} the app should load into NBA_ELO. See build_nba_team_ratings().
            "season":       _nba_sd.get("season"),
            "teamRatings":  nba_team_ratings,
            "eloSeed":      nba_elo_seed,
            # ESPN-sourced (2026-10-03): playerProps = per-player season avgs + last-5 form + stdev + injury status (the
            # inputs the app's model-generated NBA prop lines need); sources = per-field provenance; health = run health.
            "playerProps":  _nba_sd.get("playerProps", {}),
            "sources":      _nba_sd.get("sources", {}),
            "health":       _nba_sd.get("health", {}),
        },
        "nhl": {
            "today":        nhl_today,
            "tomorrow":     nhl_tom,
            "standings":    nhl_standings,
            "roster":       nhl_roster,
            "bracket":      nhl_bracket,
            "edge":         nhl_edge,
            "skaterValue":  nhl_skater_value,   # per-skater points/game for the app's NHL injury adjustment (nhlMC)
            "hockeyviz":    hockeyviz,          # retired 2026-10-03 -- kept present-but-empty (see fetch_nhl_skater_value's note)
            "hockeyRef":    hockey_ref,
            "hockeyRefTeams": hockey_ref_teams,
            "props":        lm_props.get("nhl", []),
            "trends":       lm_trends.get("nhl", []),
            "form":         lm_form.get("nhl", []),
            "weekSchedule": nhl_week_schedule,
        },
        "ncaaBaseball": ncaa_baseball,
        "wnba":         wnba,
        "pwhl":         pwhl,
        "mp":      mp,
        "weather": weather,
        "futures":   futures_odds,
        "f1": {
            **f1_data,
            "analytics":    f1_analytics,
            "tracing":      f1_tracing,
            "calendar":     f1_calendar,
            "comprehensive": f1_comprehensive,
            "unchained":    f1_unchained,
        },
        "linemate": {
            "props":  lm_props,
            "trends": lm_trends,
            "form":   lm_form,
            "wnba_props":  lm_props.get("wnba", []),
            "wnba_trends": lm_trends.get("wnba", []),
            # No timestamp existed anywhere on this block before -- the
            # frontend had zero way to tell how fresh a Linemate scrape
            # actually was, unlike the WNBA_PROPS_DATA fallback (which
            # already shows a real "stale, refresh" banner off its own
            # date). Real props showing a wrong/old matchup with no
            # staleness disclosure at all was exactly the gap this closes.
            "generatedAt": TS_DISPLAY,
        },
        "bestBets":      best_bets,
        "heroPicksForDay": surface_best_bets_for_day(best_bets),
        "bestOdds":      {**mlb_best_odds, **nba_best_odds, **nhl_best_odds},
        "bestOddsExt":   {
            "wnba": wnba_best_odds, "nfl": nfl_best_odds, "cfb": cfb_best_odds,
            "pl": pl_best_odds, "liga": liga_best_odds, "bl": bl_best_odds,
            "mls": mls_best_odds, "wc": wc_best_odds,
        },
        "settled":       settled,
        "betHistory":    history[-200:],  # last 200 for frontend
        "overallStats":  overall_stats,
        "seededBets":    SEEDED_BETS,
        "news":          sports_news,
        "injuries":      injuries,
        "transactions":  transactions,
    }

    (DATA / "bundle.json").write_text(json.dumps(bundle, indent=2))

    if args.dry_run:
        log("Dry run — skipping writes and push"); return

    write_data_json(bundle)
    patch_html_timestamp()

    # Social content + card
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from content_generator import generate_content, write_social_json, detect_slot
        from generate_card import generate_card
        slot   = detect_slot()
        social = generate_content(bundle, slot=slot, verbose=_verbose)
        if social:
            write_social_json(social)
            note("social_copy.json written")
            img = generate_card(bundle, social)
            img.save(str(ROOT/"docs"/"card.png"), format="PNG", optimize=True)
            note("card.png written")
            top_pick = bundle.get("bestBets", [{}])[0]
            pick_summary = f"{top_pick.get('pick','—')}  EV {top_pick.get('ev','?')}%" if top_pick else "No picks today"
            _notify("Content Delivered", f"card.png + social copy ready · {pick_summary}")
    except Exception as exc:
        log(f"Content generation skipped: {exc}", "WARN")

    # Always push
    if args.push:
        summary = (
            f"MLB: {len(mlb_today)} games | NBA: {len(nba_today)} games | "
            f"NHL: {len(nhl_today)} games\n"
            f"Best bets: {len(best_bets)} | Settled: {len(settled)} | "
            f"History: {len(history)} total"
        )
        pushed = git_push(summary)
        if pushed:
            n_bets = len(best_bets)
            top_grade = best_bets[0].get("evGrade","—") if best_bets else "—"
            _notify("Refresh Complete", f"Push done · {n_bets} picks · top grade {top_grade} · {TS_DISPLAY}")
            verify_deployment()
    else:
        _notify("Refresh Complete", f"Data updated (no push) · {len(best_bets)} picks · {TS_DISPLAY}")

    log("=" * 60)
    log(f"Done. {len(_changes)} changes.")
    for c in _changes: log(f"  • {c}")
    log("=" * 60)


if __name__ == "__main__":
    main()
