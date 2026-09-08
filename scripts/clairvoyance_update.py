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
  python3 scripts/clairvoyance_update.py --mode props     # Linemate only
  python3 scripts/clairvoyance_update.py --sport nhl      # single sport
  python3 scripts/clairvoyance_update.py --no-linemate    # skip Playwright
  python3 scripts/clairvoyance_update.py --no-reference   # skip Baseball/Basketball/Hockey Ref
  python3 scripts/clairvoyance_update.py --verbose

Cron (MT times — TZ=America/Denver):
  0 8,12,16,20,0 * * *  full refresh + push
  0 17           * * *  live-window mode (self-terminates 23:00 MT)

Data sources (v6.0):
  ESPN APIs, NHL API, MoneyPuck, HockeyViz, TennisAbstract Elo, Ergast F1,
  ESPN F1 scoreboard/standings, TennisAbstract Roland Garros, Sports-Reference
  (Baseball/Basketball/Hockey-Reference), FBref (Champions League/Premier
  League/La Liga/Bundesliga/MLS), Open-Meteo weather, Linemate Playwright

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
YEAR      = 2026

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

def fetch_json(url: str, timeout: int = 15, retries: int = 2, params: dict | None = None) -> dict | list | None:
    for attempt in range(retries + 1):
        try:
            r = _session.get(url, timeout=timeout, params=params)
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
    }
    g.update(_espn_odds(comp, sport=espn_sport, league=espn_league, event_id=event_id))
    for note_obj in comp.get("notes") or []:
        h = note_obj.get("headline", "")
        if "Game" in h or "Series" in h:
            g["seriesNote"] = h; break
    return g

def fetch_espn_injuries(sport_path: str, sport_key: str) -> list[dict]:
    """Fetch ESPN injury report for a sport (e.g. 'baseball/mlb')."""
    log(f"ESPN injuries {sport_key}…")
    url  = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/injuries"
    data = fetch_json(url)
    items: list[dict] = []
    for team in (data or {}).get("injuries") or []:
        abbr = (team.get("team") or {}).get("abbreviation", "")
        for inj in team.get("injuries") or []:
            ath = inj.get("athlete") or {}
            items.append({
                "team":   abbr,
                "name":   ath.get("displayName", ""),
                "pos":    (ath.get("position") or {}).get("abbreviation", ""),
                "status": inj.get("status", ""),
                "detail": inj.get("details", {}).get("detail", ""),
                "return": inj.get("details", {}).get("returnDate", ""),
                "sport":  sport_key,
            })
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

# ═══════════════════════════════════════════════════════════════════════════════
# MLB
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_mlb_scoreboard(date: str = TODAY_ET) -> tuple[list, list]:
    """Fetch MLB scoreboard. Uses Eastern Time date since MLB schedules games in ET.
    Deduplicates by event ID and filters to only include today's games (ET date)."""
    log(f"MLB scoreboard {date} (ET)…")
    data = fetch_json(f"{ESPN_BASE}/baseball/mlb/scoreboard?dates={date}&limit=30")
    if not data: return [], []
    seen_ids: set = set()
    games = []
    for e in (data.get("events") or []):
        eid = e.get("id", "")
        # Only include events whose date matches today (ET) — event date is YYYYMMDD-prefixed in UTC
        event_date_raw = e.get("date", "")  # ISO string e.g. "2026-05-23T18:05Z"
        try:
            event_date_et = datetime.fromisoformat(event_date_raw.replace("Z", "+00:00")).astimezone(
                _ET if "zoneinfo" in sys.modules else timezone(timedelta(hours=-4))
            ).strftime("%Y%m%d")
        except Exception:
            event_date_et = date  # default to requested date if parse fails
        if event_date_et != date:
            vlog(f"  MLB skip stale/future event {eid} dated {event_date_et}")
            continue
        if eid in seen_ids:
            vlog(f"  MLB skip duplicate event {eid}")
            continue
        seen_ids.add(eid)
        games.append(_espn_game(e, "MLB"))
    tom   = (datetime.strptime(date, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
    data2 = fetch_json(f"{ESPN_BASE}/baseball/mlb/scoreboard?dates={tom}&limit=30")
    seen_tom: set = set()
    tomorrow = []
    for e in ((data2 or {}).get("events") or []):
        eid = e.get("id", "")
        if eid in seen_tom: continue
        seen_tom.add(eid)
        tomorrow.append(_espn_game(e, "MLB"))
    vlog(f"  MLB: {len(games)} today, {len(tomorrow)} tomorrow")
    return games, tomorrow

def fetch_mlb_standings() -> dict:
    log("MLB standings…")
    data = fetch_json(
        "https://site.web.api.espn.com/apis/v2/sports/baseball/mlb/standings"
        "?region=us&lang=en&season=2026&type=2"
    )
    if not data: return {}
    out: dict = {}
    for division in data.get("children") or []:
        for entry in (division.get("standings") or {}).get("entries") or []:
            team  = entry.get("team") or {}
            abbr  = team.get("abbreviation", "")
            stats = {s["name"]: s.get("displayValue", s.get("value", ""))
                     for s in (entry.get("stats") or [])}
            out[abbr] = {
                "w": stats.get("wins","0"), "l": stats.get("losses","0"),
                "pct": stats.get("winPercent",".000"), "gb": stats.get("gamesBehind","—"),
                "streak": stats.get("streak",""), "rs": stats.get("pointsFor","0"),
                "ra": stats.get("pointsAgainst","0"),
                "div": (division.get("name") or team.get("shortDisplayName","")),
            }
    vlog(f"  MLB standings: {len(out)} teams")
    return out

def fetch_mlb_schedule_week() -> list[dict]:
    """Fetch MLB schedule for next 7 days (using Eastern Time base)."""
    log("MLB week schedule…")
    games: list[dict] = []
    for offset in range(7):
        d = (NOW_ET + timedelta(days=offset)).strftime("%Y%m%d")
        data = fetch_json(f"{ESPN_BASE}/baseball/mlb/scoreboard?dates={d}&limit=30")
        for e in (data or {}).get("events") or []:
            g = _espn_game(e, "MLB")
            g["schedDate"] = d
            games.append(g)
    vlog(f"  MLB week: {len(games)} games")
    return games

def fetch_baseball_reference() -> dict:
    """Scrape MLB batting & pitching leaders from Baseball Reference."""
    log("Baseball Reference stats…")
    result: dict = {"batting": [], "pitching": [], "fetchedAt": TODAY_ISO}
    pairs = [
        ("batting",  "https://www.baseball-reference.com/leagues/majors/2026-standard-batting.shtml",   "players_standard_batting"),
        ("pitching", "https://www.baseball-reference.com/leagues/majors/2026-standard-pitching.shtml",  "players_standard_pitching"),
    ]
    for key, url, tbl_id in pairs:
        # Real gap, found via a live MLB-accuracy audit: the pitching table
        # was capped at limit=50 (league-wide, sorted by IP) -- fine for a
        # "top-50 leaders" display, nowhere near enough to see a real
        # bullpen. Confirmed live: the real 2026 players_standard_pitching
        # table has 1079 rows; a reliever with modest IP sits well past
        # row 50, sorted behind every team's starters. Raised to 1500 --
        # comfortably above the real current total with room for the rest
        # of the season's roster churn, and this table naturally self-caps
        # at "however many pitchers have actually appeared in a real MLB
        # game this year," not an ever-growing number. batting keeps its
        # original 50-row cap -- this fix is specifically about pitching/
        # bullpen coverage, not batting leaders, which nothing here needs
        # beyond the existing top-50 display use.
        limit = 1500 if key == "pitching" else 50
        try:
            time.sleep(2)    # rate-limit SR
            soup = fetch_html(url, timeout=25, ref=True)
            if not soup: continue
            rows = _table_to_rows(soup, tbl_id, limit=limit)
            if not rows:   # fallback: first big table
                for tbl in soup.find_all("table"):
                    r = _table_to_rows(soup, tbl.get("id",""), limit=limit) if tbl.get("id") else []
                    if len(r) > 10: rows = r; break
            result[key] = rows[:limit]
            vlog(f"  Baseball Ref {key}: {len(rows)} rows")
        except Exception as exc:
            log(f"Baseball Ref {key}: {exc}", "WARN")
    return result


# Baseball-Reference's team_name_abbr occasionally differs from this app's
# own canonical MLB team keys (see the MLB const in docs/app.html) --
# confirmed via a live scrape of players_standard_pitching (2026-09-02).
_BREF_TEAM_ABBR_MAP = {
    "ATH": "OAK", "CHW": "CWS", "KCR": "KC", "SDP": "SD", "SFG": "SF", "TBR": "TB",
}


def fetch_mlb_bullpen_stats(pitching_rows: list[dict]) -> dict:
    """
    Isolates real bullpen-only pitching quality per team from Baseball-
    Reference's players_standard_pitching rows (see fetch_baseball_reference,
    now fetched at a high enough limit to cover the whole league's real
    usage, not just the top-50 overall leaders) -- the MLB win-probability
    model (adjLam/mlbMC in docs/app.html) currently has real signal for a
    team's OFFENSE (bat.RG) and today's probable STARTER (PIT[abbr], from
    ESPN's live probable-pitcher feed) but nothing at all for the bullpen
    that actually pitches innings 6-9 of a real game -- a team with a great
    rotation and a terrible bullpen currently prices identically to one
    with a great rotation AND a great bullpen.

    A pitcher is classified as a reliever when GS/G < 0.5 (mostly relief
    appearances, the standard sabermetric convention) -- everything else
    (a real starter, or a spot-starter who's mostly started) is excluded.
    Real per-team, per-stint rows only: Baseball-Reference's own combined
    "2TM"/"3TM"/etc rows for a traded player are excluded (confirmed live:
    a real traded pitcher's 2TM row's IP exactly equals the sum of his 2
    separate real-team stint rows -- keeping both would double-count that
    pitcher's innings onto both his own team AND the league-wide total).

    Filters below a minimum-innings threshold so a one-batter emergency/
    position-player appearance (real ERA of 27.00 off 2 batters faced,
    common in real blowouts) can't skew a team's actual bullpen quality
    off a tiny, noisy sample -- and drops any team with fewer than 3 real
    relievers on record rather than reporting a number from 1-2 pitchers.

    Returns {team_abbr: {era, fip, ip, n}} -- era/fip are innings-weighted
    averages across that team's real relief corps (not a simple mean --
    a reliever with 60 IP should count far more than one with 5), ip is
    total relief innings (a rough bullpen workload/depth signal), n is the
    real reliever count that sample was built from.
    """
    MIN_IP = 3.0
    MIN_RELIEVERS = 3
    agg: dict[str, dict] = {}
    for row in pitching_rows:
        tm_raw = (row.get("team_name_abbr") or "").strip()
        if not tm_raw or tm_raw in ("", "Tm", "--") or re.match(r"^\d+TM$", tm_raw):
            continue  # blank/header row or a combined multi-team summary row
        tm = _BREF_TEAM_ABBR_MAP.get(tm_raw, tm_raw)
        try:
            g  = float(row.get("p_g") or 0)
            gs = float(row.get("p_gs") or 0)
            ip = float(row.get("p_ip") or 0)
        except (ValueError, TypeError):
            continue
        if g <= 0 or ip < MIN_IP:
            continue
        if gs / g >= 0.5:
            continue  # a real starter (or mostly-starter), not bullpen
        try:
            era = float(row.get("p_earned_run_avg") or 0)
        except (ValueError, TypeError):
            era = 0.0
        try:
            fip = float(row.get("p_fip") or 0)
        except (ValueError, TypeError):
            fip = 0.0
        a = agg.setdefault(tm, {"era_ip": 0.0, "fip_ip": 0.0, "era_wsum": 0.0, "fip_wsum": 0.0, "ip": 0.0, "n": 0})
        a["ip"] += ip
        a["n"]  += 1
        if era > 0:
            a["era_wsum"] += era * ip
            a["era_ip"]   += ip
        if fip > 0:
            a["fip_wsum"] += fip * ip
            a["fip_ip"]   += ip
    result: dict = {}
    for tm, a in agg.items():
        if a["n"] < MIN_RELIEVERS:
            continue
        result[tm] = {
            "era": round(a["era_wsum"] / a["era_ip"], 3) if a["era_ip"] > 0 else None,
            "fip": round(a["fip_wsum"] / a["fip_ip"], 3) if a["fip_ip"] > 0 else None,
            "ip":  round(a["ip"], 1),
            "n":   a["n"],
        }
    log(f"MLB bullpen: {len(result)} teams (from {len(pitching_rows)} pitching rows)")
    return result

def fetch_mlb_team_sabermetrics() -> dict:
    """
    Fetch team-level sabermetrics from Baseball Reference 2026 team batting/pitching.
    Returns dict keyed by team abbreviation with wOBA, ISO, FIP, ERA-.
    """
    log("MLB team sabermetrics…")
    result: dict = {}
    try:
        # Team batting — OPS+, ISO, wOBA proxy
        time.sleep(2)
        soup = fetch_html("https://www.baseball-reference.com/leagues/majors/2026-standard-batting.shtml",
                          timeout=25, ref=True)
        if soup:
            tbl = soup.find("table", {"id": "teams_standard_batting"})
            if tbl:
                for tr in tbl.find_all("tr")[1:]:
                    cells = tr.find_all(["th","td"])
                    if len(cells) < 18: continue
                    tm = cells[0].get_text(strip=True)
                    if tm in ("","Tm","LgAvg","--"): continue
                    try:
                        ops_plus = float(cells[15].get_text(strip=True) or 100)
                    except: ops_plus = 100.0
                    try:
                        iso = float(cells[17].get_text(strip=True) or 0.15)
                    except: iso = 0.15
                    result[tm] = result.get(tm, {})
                    result[tm].update({"ops_plus": ops_plus, "iso": iso})
    except Exception as exc:
        log(f"MLB team batting sabermetrics: {exc}", "WARN")
    try:
        # Team pitching — FIP, ERA-
        time.sleep(2)
        soup = fetch_html("https://www.baseball-reference.com/leagues/majors/2026-standard-pitching.shtml",
                          timeout=25, ref=True)
        if soup:
            tbl = soup.find("table", {"id": "teams_standard_pitching"})
            if tbl:
                for tr in tbl.find_all("tr")[1:]:
                    cells = tr.find_all(["th","td"])
                    if len(cells) < 20: continue
                    tm = cells[0].get_text(strip=True)
                    if tm in ("","Tm","LgAvg","--"): continue
                    try:
                        fip = float(cells[18].get_text(strip=True) or 4.20)
                    except: fip = 4.20
                    try:
                        era_minus = float(cells[19].get_text(strip=True) or 100)
                    except: era_minus = 100.0
                    result[tm] = result.get(tm, {})
                    result[tm].update({"fip": fip, "era_minus": era_minus})
    except Exception as exc:
        log(f"MLB team pitching sabermetrics: {exc}", "WARN")
    log(f"MLB team sabermetrics: {len(result)} teams")
    return result

def fetch_mlb_team_fielding() -> dict:
    """
    Additive: team-level defensive efficiency from Baseball Reference's
    teams_standard_fielding table (not covered by fetch_mlb_team_sabermetrics,
    which only reads batting/pitching). Uses _table_to_rows for resilience
    instead of positional cell-index parsing.
    """
    log("MLB team fielding…")
    result: dict = {}
    try:
        time.sleep(2)
        soup = fetch_html("https://www.baseball-reference.com/leagues/majors/2026-standard-fielding.shtml",
                          timeout=25, ref=True)
        if not soup:
            return result
        rows = _table_to_rows(soup, "teams_standard_fielding", limit=40)
        for row in rows:
            tm = (row.get("team_name") or row.get("team") or "").strip()
            if not tm or tm in ("", "Tm", "LgAvg", "--"):
                continue
            try:
                fld_pct = float(row.get("fielding_perc") or 0.982)
            except (ValueError, TypeError):
                fld_pct = 0.982
            try:
                dp = float(row.get("double_plays") or 0)
            except (ValueError, TypeError):
                dp = 0.0
            try:
                rtot = float(row.get("total_zone_runs_total") or row.get("range_factor_per_game") or 0)
            except (ValueError, TypeError):
                rtot = 0.0
            result[tm] = {"fld_pct": fld_pct, "dp": dp, "def_runs": rtot}
        log(f"MLB team fielding: {len(result)} teams")
    except Exception as exc:
        log(f"MLB team fielding: {exc}", "WARN")
    return result

def fetch_mlb_batter_rosters() -> dict:
    """
    Closes the injury-integration gap documented in app.html above
    computeInjuryImpact(): the frontend's MLB injury penalty only ever
    matched starting pitchers (window.PIT), because no batter roster
    existed anywhere — baseW()'s position weights for C/SS/CF/3B/etc were
    dead code. This doesn't need a separate "rating" system the way NBA
    does; baseW() already weights purely by POSITION (C/SS/CF/3B highest,
    corner spots lower), and ESPN's team roster endpoint returns exactly
    that — name, team, position — for every player on all 30 teams in one
    request per team, no per-player stat calls needed.

    Returns {"lastname firstname": {"team": "SD", "pos": "SS"}, ...} keyed
    the same way the frontend's window.PIT/window.NBA_PLAYERS rosters are,
    for direct use building window._injRoster in buildInjuryRoster().
    """
    log("MLB batter rosters (ESPN)…")
    result: dict = {}
    try:
        teams_data = fetch_json("https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/teams?limit=40")
        teams = ((teams_data or {}).get("sports") or [{}])[0].get("leagues", [{}])[0].get("teams", [])
        for t in teams:
            tm = t.get("team", {})
            team_id, abbr = tm.get("id"), tm.get("abbreviation", "")
            if not team_id or not abbr:
                continue
            try:
                time.sleep(0.2)
                roster = fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/teams/{team_id}/roster")
                for grp in (roster or {}).get("athletes", []):
                    for p in grp.get("items", []):
                        pos = (p.get("position") or {}).get("abbreviation", "")
                        if pos in ("SP", "RP"):  # pitchers already covered by window.PIT (today's starters)
                            continue
                        name = p.get("fullName", "")
                        if name:
                            result[name.lower()] = {"team": abbr, "pos": pos}
            except Exception as exc:
                log(f"MLB roster {abbr}: {exc}", "WARN")
        log(f"  MLB batter rosters: {len(result)} players across {len(teams)} teams")
    except Exception as exc:
        log(f"MLB batter rosters: {exc}", "WARN")
    return result

def fetch_nba_roster() -> dict:
    """
    Real, current NBA roster for all 30 teams -- replaces the app's
    hand-embedded NBA_PLAYERS table (which only ever covered the
    handful of players relevant to whatever series/Finals matchup was
    current when it was last hand-edited) with a scraped source that
    reflects real offseason trades/signings automatically instead of
    needing another manual edit every time a player changes teams.

    Same fetch_mlb_batter_rosters() pattern: one ESPN team-list call,
    then one roster call per team. Returns {"player name": {"team":
    "ABBR", "pos": "PG"}, ...}, keyed lowercase to match this
    codebase's existing name-lookup convention (see fetch_mlb_batter_rosters).
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
    ESPN team-list -> per-team-roster pattern as fetch_nba_roster()/
    fetch_mlb_batter_rosters(). Returns {"player name": {"team": "ABBR",
    "pos": "C"}, ...}, keyed lowercase.
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
                for p in (roster or {}).get("athletes", []):
                    pos = (p.get("position") or {}).get("abbreviation", "")
                    name = p.get("fullName", "")
                    if name:
                        result[name.lower()] = {"team": abbr, "pos": pos}
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

def fetch_mlb_statcast_team(batter_rosters: dict) -> dict:
    """
    Real Statcast quality-of-contact metrics — xwOBA, barrel rate, hard-hit%,
    xSLG — a tier beyond the traditional sabermetrics already fetched
    (fetch_mlb_team_sabermetrics: OPS+/ISO/FIP/ERA-, all Baseball-Reference).
    Baseball Savant's leaderboard only exports at the individual-player
    level (no team-aggregate endpoint), so this aggregates qualified
    batters up to team level itself, using the ESPN roster name->team
    lookup already built by fetch_mlb_batter_rosters() rather than a second
    roster fetch. Savant names are "Last, First"; ESPN's are "First Last" —
    reformatted to match the same lowercase key convention.
    """
    log("MLB Statcast quality-of-contact (Baseball Savant)…")
    result: dict = {}
    try:
        r = _session.get(
            "https://baseballsavant.mlb.com/leaderboard/custom",
            params={"year": date.today().year, "type": "batter", "min": "1", "chart": "false", "csv": "true",
                    "selections": "xwoba,barrel_batted_rate,hard_hit_percent,xslg,xba"},
            timeout=20,
        )
        r.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿"))))
        buckets: dict[str, dict] = {}
        for row in rows:
            raw_name = row.get("last_name, first_name", "")
            if "," not in raw_name:
                continue
            last, first = [s.strip() for s in raw_name.split(",", 1)]
            key = f"{first} {last}".lower()
            entry = batter_rosters.get(key)
            if not entry:
                continue
            team = entry["team"]
            b = buckets.setdefault(team, {"n": 0, "xwoba": 0.0, "barrel": 0.0, "hardhit": 0.0, "xslg": 0.0})
            try:
                b["n"] += 1
                b["xwoba"]   += float(row.get("xwoba") or 0)
                b["barrel"]  += float(row.get("barrel_batted_rate") or 0)
                b["hardhit"] += float(row.get("hard_hit_percent") or 0)
                b["xslg"]    += float(row.get("xslg") or 0)
            except (ValueError, TypeError):
                b["n"] -= 1
        for team, b in buckets.items():
            if b["n"] == 0:
                continue
            result[team] = {
                "xwoba":   round(b["xwoba"] / b["n"], 3),
                "barrel_pct": round(b["barrel"] / b["n"], 1),
                "hardhit_pct": round(b["hardhit"] / b["n"], 1),
                "xslg":    round(b["xslg"] / b["n"], 3),
                "n_batters": b["n"],
            }
        log(f"  MLB Statcast: {len(result)} teams from {len(rows)} qualified batters")
    except Exception as exc:
        log(f"MLB Statcast: {exc}", "WARN")
    return result


def fetch_nba_team_advanced() -> dict:
    """
    Fetch team-level NBA advanced stats from Basketball Reference.
    Returns dict keyed by team abbreviation with ortg, drtg, pace, efg_pct, ts_pct.
    Used for probability adjustment in calculate_best_bets.

    Two-phase, like fetch_wnba_team_stats(): the regular-season leagues
    page's advanced-team table covers all 30 teams (BBRef's real full-
    season numbers), scraped first as the base. The playoffs page's
    misc_stats table -- previously the ONLY source here, hardcoded to an
    18-team ABBR_MAP of "common playoff teams" -- is then overlaid on
    top for whichever teams it has, since in-progress playoff performance
    is a fresher signal than full-season averages for those specific
    teams. Any of the other ~12 teams simply keep their real regular-
    season numbers instead of having no live data at all.
    """
    log("NBA team advanced stats…")
    result: dict = {}
    # BBRef abbreviation → ESPN abbreviation mapping (differs for a few teams)
    ABBR_MAP = {
        "NYK":"NY","GSW":"GS","PHX":"PHX","SAS":"SA",
    }
    try:
        time.sleep(2)
        soup0 = fetch_html(
            "https://www.basketball-reference.com/leagues/NBA_2026.html",
            timeout=25, ref=True
        )
        if soup0:
            tbl0 = soup0.find("table", {"id": "advanced-team"})
            if not tbl0:
                from bs4 import Comment
                for cmt in soup0.find_all(string=lambda t: isinstance(t, Comment)):
                    if "advanced-team" in cmt:
                        frag = BeautifulSoup(cmt, "lxml")
                        tbl0 = frag.find("table", {"id": "advanced-team"})
                        if tbl0: break
            if tbl0:
                for tr in tbl0.find_all("tr"):
                    cells = {td.get("data-stat",""): td.get_text(strip=True)
                             for td in tr.find_all(["td","th"])}
                    tm = (cells.get("team_id") or cells.get("team_name") or "").strip().upper()
                    if not tm or tm in ("TEAM","",):
                        continue
                    espn_abbr = ABBR_MAP.get(tm, tm)
                    try:
                        ortg = float(cells.get("off_rtg","") or 0)
                        drtg = float(cells.get("def_rtg","") or 0)
                        pace = float(cells.get("pace","") or 0)
                        efg  = float(cells.get("efg_pct","") or 0)
                        ts   = float(cells.get("ts_pct","") or 0)
                        if ortg > 0:
                            result[espn_abbr] = {
                                "ortg": ortg, "drtg": drtg, "pace": pace,
                                "efg_pct": efg, "ts_pct": ts,
                                "net_rtg": ortg - drtg,
                            }
                    except (ValueError, TypeError):
                        continue
        log(f"  NBA advanced (full season): {len(result)} teams")
    except Exception as exc:
        log(f"NBA team advanced (full season): {exc}", "WARN")

    try:
        time.sleep(2)
        soup = fetch_html(
            "https://www.basketball-reference.com/playoffs/NBA_2026.html",
            timeout=25, ref=True
        )
        if not soup:
            return result
        # Team misc stats table: team_misc
        tbl = soup.find("table", {"id": "misc_stats"})
        if not tbl:
            # Sometimes embedded in HTML comments
            from bs4 import Comment
            for cmt in soup.find_all(string=lambda t: isinstance(t, Comment)):
                if "misc_stats" in cmt:
                    frag = BeautifulSoup(cmt, "lxml")
                    tbl = frag.find("table", {"id": "misc_stats"})
                    if tbl: break
        if tbl:
            headers = [th.get("data-stat","") for th in tbl.find_all("th") if th.get("data-stat")]
            for tr in tbl.find_all("tr"):
                cells = {td.get("data-stat",""): td.get_text(strip=True)
                         for td in tr.find_all(["td","th"])}
                tm = cells.get("team_id","").upper()
                if not tm or tm in ("TEAM","",): continue
                espn_abbr = ABBR_MAP.get(tm, tm)
                try:
                    ortg = float(cells.get("off_rtg","") or 0)
                    drtg = float(cells.get("def_rtg","") or 0)
                    pace = float(cells.get("pace","") or 0)
                    efg  = float(cells.get("efg_pct","") or 0)
                    ts   = float(cells.get("ts_pct","") or 0)
                    if ortg > 0:
                        result[espn_abbr] = {
                            "ortg": ortg, "drtg": drtg, "pace": pace,
                            "efg_pct": efg, "ts_pct": ts,
                            "net_rtg": ortg - drtg,
                        }
                except (ValueError, TypeError):
                    continue
    except Exception as exc:
        log(f"NBA team advanced: {exc}", "WARN")
    log(f"NBA team advanced: {len(result)} teams")
    return result

def fetch_nba_four_factors() -> dict:
    """
    Additive: Dean Oliver's "Four Factors" (eFG%, TOV%, ORB%, FT/FGA) for
    both offense and defense, from Basketball Reference's four_factors
    table — complements fetch_nba_team_advanced()'s misc_stats data with
    the specific factors most predictive of pace-adjusted win probability.
    Works for both NBA and WNBA by passing the appropriate Sports-Reference
    season URL.
    """
    log("NBA/WNBA four factors…")
    result: dict = {}
    ABBR_MAP = {
        "NYK":"NY","GSW":"GS","PHX":"PHX","SAS":"SA",
    }
    try:
        time.sleep(2)
        soup = fetch_html("https://www.basketball-reference.com/leagues/NBA_2026.html", timeout=25, ref=True)
        if not soup:
            return result
        rows = _table_to_rows(soup, "four_factors", limit=40)
        for row in rows:
            tm = (row.get("team_name") or row.get("team") or "").strip().upper()
            if not tm or tm in ("", "TEAM", "LEAGUE AVERAGE"):
                continue
            espn_abbr = ABBR_MAP.get(tm, tm)
            try:
                result[espn_abbr] = {
                    "efg_pct":  float(row.get("efg_pct") or 0),
                    "tov_pct":  float(row.get("tov_pct") or 0),
                    "orb_pct":  float(row.get("orb_pct") or 0),
                    "ft_rate":  float(row.get("ft_rate") or 0),
                    "opp_efg_pct": float(row.get("opp_efg_pct") or 0),
                    "opp_tov_pct": float(row.get("opp_tov_pct") or 0),
                    "drb_pct":  float(row.get("drb_pct") or 0),
                    "opp_ft_rate": float(row.get("opp_ft_rate") or 0),
                }
            except (ValueError, TypeError):
                continue
        log(f"NBA four factors: {len(result)} teams")
    except Exception as exc:
        log(f"NBA four factors: {exc}", "WARN")
    return result


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

def _name_to_abbr(name: str) -> str:
    """Convert Odds API team name → ESPN abbreviation. Falls back to first 3 chars."""
    return _TEAM_NAME_TO_ABBR.get(name, name[:3].upper())

def fetch_best_odds(sport: str, game_list: list, name_resolver=None) -> dict:
    """
    Fetch best available moneyline + O/U odds from The Odds API (free tier).
    Falls back to ESPN odds already in game_list if no API key.
    Returns dict keyed by 'home_key:away_key' → {homeML, awayML, ou, book}.

    name_resolver: full team/country name -> the key this sport actually
    identifies teams by elsewhere in the engine. Defaults to _name_to_abbr
    (ESPN 3-letter abbreviations, used by mlb/nba/nhl/wnba/nfl/cfb). Soccer
    callers pass _wc_name_to_abbr (World Cup 3-letter country codes) or
    _soccer_club_key (club leagues, which key by normalized full name, not
    an abbreviation — see _soccer_club_key's docstring for why).
    """
    resolver = name_resolver or _name_to_abbr
    api_key = os.environ.get("ODDS_API_KEY", "")
    best: dict = {}
    if not api_key:
        for g in game_list:
            key = f"{g.get('home','')}:{g.get('away','')}"
            best[key] = {"homeML": g.get("homeML"), "awayML": g.get("awayML"),
                         "ou": g.get("ou"), "book": "ESPN"}
        return best

    sport_key = {"mlb": "baseball_mlb", "nba": "basketball_nba",
                 "nhl": "icehockey_nhl", "wnba": "basketball_wnba",
                 "nfl": "americanfootball_nfl", "cfb": "americanfootball_ncaaf",
                 "pl": "soccer_epl", "liga": "soccer_spain_la_liga",
                 "bl": "soccer_germany_bundesliga", "mls": "soccer_usa_mls",
                 "wc": "soccer_fifa_world_cup_2026"}.get(sport, "")
    if not sport_key:
        return best

    try:
        url  = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
        params = {
            "apiKey": api_key, "regions": "us",
            "markets": "h2h,totals",       # moneyline + over/under
            "oddsFormat": "american", "dateFormat": "iso",
        }
        # Soccer markets settle draws too — h2h alone still returns 3-way
        # (home/draw/away) prices from The Odds API for these sport keys,
        # no separate market needed.
        data = fetch_json(url, params=params) or []

        remaining = None
        log(f"Odds API {sport}: {len(data)} events")

        for event in data:
            home_name = event.get("home_team", "")
            away_name = event.get("away_team", "")
            home_abbr = resolver(home_name)
            away_abbr = resolver(away_name)
            key = f"{home_abbr}:{away_abbr}"

            best_home_ml: int | None = None
            best_away_ml: int | None = None
            best_draw_ml: int | None = None
            best_home_book = ""
            best_away_book = ""
            best_ou: float | None  = None

            for bk in (event.get("bookmakers") or []):
                bk_title = bk.get("title", "")
                for market in (bk.get("markets") or []):
                    mkey = market.get("key", "")
                    for outcome in (market.get("outcomes") or []):
                        p   = outcome.get("price")
                        nm  = outcome.get("name", "")
                        pt  = outcome.get("point")          # for totals
                        if p is None: continue

                        if mkey == "h2h":
                            if nm == home_name:
                                if best_home_ml is None or int(p) > best_home_ml:
                                    best_home_ml   = int(p)
                                    best_home_book = bk_title
                            elif nm == away_name:
                                if best_away_ml is None or int(p) > best_away_ml:
                                    best_away_ml   = int(p)
                                    best_away_book = bk_title
                            elif nm == "Draw":
                                if best_draw_ml is None or int(p) > best_draw_ml:
                                    best_draw_ml = int(p)
                        elif mkey == "totals" and nm == "Over" and pt is not None:
                            # Take the highest (most favorable) total line
                            if best_ou is None or float(pt) > best_ou:
                                best_ou = float(pt)

            if best_home_ml or best_away_ml:
                book_str = best_home_book or best_away_book or "Odds API"
                best[key] = {
                    "homeML":   best_home_ml,
                    "awayML":   best_away_ml,
                    "drawML":   best_draw_ml,
                    "ou":       best_ou,
                    "book":     book_str,
                    "homeBook": best_home_book,
                    "awayBook": best_away_book,
                }
                vlog(f"  {key}: home {best_home_ml} ({best_home_book}) / "
                     f"away {best_away_ml} ({best_away_book}) draw {best_draw_ml} O/U {best_ou}")

    except Exception as exc:
        log(f"Odds API fetch ({sport}): {exc}", "WARN")
    return best


def fetch_mlb_nrfi_data(mlb_today: list) -> list[dict]:
    """Build NRFI entries from today's MLB game list + any weather data."""
    return [
        {"game": f"{g['away']} @ {g['home']}", "home": g["home"], "away": g["away"],
         "ou": g.get("ou"), "homeML": g.get("homeML"), "awayML": g.get("awayML"),
         "state": g.get("state","pre"), "venue": g.get("venue","")}
        for g in mlb_today
    ]

# ═══════════════════════════════════════════════════════════════════════════════
# NBA
# ═══════════════════════════════════════════════════════════════════════════════
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

def fetch_nba_standings() -> dict:
    log("NBA standings…")
    data = fetch_json(
        "https://site.web.api.espn.com/apis/v2/sports/basketball/nba/standings"
        "?region=us&lang=en&season=2026&type=2"
    )
    if not data: return {}
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
            }
    vlog(f"  NBA standings: {len(out)} teams")
    return out

def fetch_nba_playoff_bracket() -> dict:
    """Fetch NBA playoff bracket from ESPN."""
    log("NBA playoff bracket…")
    data = fetch_json(f"{ESPN_BASE}/basketball/nba/playoffs?season=2026")
    if not data: return {}
    return {"raw": data, "fetchedAt": TODAY_ISO}

def fetch_nba_player_stats() -> list[dict]:
    log("NBA player stats…")
    players: dict[str, dict] = {}
    data = fetch_json(
        f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
        f"/leaders?season=2026&seasontype=3&limit=20"
    )
    if not data: return []
    for cat in data.get("categories") or []:
        for leader in cat.get("leaders") or []:
            ath  = leader.get("athlete") or {}
            name = ath.get("displayName", "")
            team = (ath.get("team") or {}).get("abbreviation", "")
            if name not in players:
                players[name] = {"name": name, "team": team}
            players[name][(cat.get("name","")).upper()[:3]] = leader.get("displayValue","—")
    return list(players.values())

def fetch_basketball_reference() -> dict:
    """Scrape NBA playoff stats: per-game, per-100 possessions, advanced, shooting."""
    log("Basketball Reference playoff stats…")
    result: dict = {"perGame": [], "per100": [], "advanced": [], "shooting": [], "opponentPerGame": [], "fetchedAt": TODAY_ISO}
    base = "https://www.basketball-reference.com/playoffs/NBA_2026.html"
    table_map = [
        ("perGame",        "playoffs_per_game"),
        ("per100",         "playoffs_per_poss"),
        ("advanced",       "playoffs_advanced"),
        ("shooting",       "playoffs_shooting"),
        ("opponentPerGame","playoffs_opponent_per_game"),
    ]
    try:
        time.sleep(2)
        soup = fetch_html(base, ref=True)
        if soup:
            for key, tbl_id in table_map:
                rows = _table_to_rows(soup, tbl_id, limit=60)
                result[key] = rows
                vlog(f"  BBRef {key}: {len(rows)} rows")
    except Exception as exc:
        log(f"Basketball Reference: {exc}", "WARN")

    # Series-level stats
    series_urls = [
        ("east_finals", "https://www.basketball-reference.com/playoffs/2026-nba-eastern-conference-finals-cavaliers-vs-knicks.html"),
        ("west_finals", "https://www.basketball-reference.com/playoffs/2026-nba-western-conference-finals-spurs-vs-thunder.html"),
    ]
    result["series"] = {}
    for label, url in series_urls:
        try:
            time.sleep(2)
            soup2 = fetch_html(url, ref=True)
            if soup2:
                series_data: dict = {}
                for key, tbl_id in [("perGame","per_game"),("advanced","advanced")]:
                    rows = _table_to_rows(soup2, tbl_id, limit=20)
                    if rows: series_data[key] = rows
                result["series"][label] = series_data
                vlog(f"  BBRef series {label}: {len(series_data)} tables")
        except Exception as exc:
            log(f"Basketball Reference series {label}: {exc}", "WARN")

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

def fetch_nhl_playoff_bracket() -> dict:
    """Fetch NHL playoff bracket from ESPN."""
    log("NHL playoff bracket…")
    data = fetch_json(f"{ESPN_BASE}/hockey/nhl/playoffs?season=2026")
    if not data: return {}
    return {"raw": data, "fetchedAt": TODAY_ISO}


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
    per goalie per situation (all/5v5/4v5/...), same as teams.csv."""
    rows = fetch_csv_rows(f"{MP_BASE}/{year}/regular/goalies.csv")
    out: dict = {}
    for row in rows:
        name = row.get("name","")
        situation = row.get("situation","all")
        if not name: continue
        try:
            out[(name, situation)] = {
                "team":      row.get("team",""),
                "gp":        int(row.get("games_played") or 0),
                "gsaa":      float(row.get("goalsAboveAverage") or 0),
                "savePct":   float(row.get("savePct") or 0),
                "xSavePct":  float(row.get("xSavePct") or 0),
                "hdSavePct": float(row.get("highDangerSavePct") or 0),
                "mdSavePct": float(row.get("mediumDangerSavePct") or 0),
                "ldSavePct": float(row.get("lowDangerSavePct") or 0),
                "shots":     int(row.get("shotsOnGoalAgainst") or 0),
                "ga":        float(row.get("goalsAgainst") or 0),
                "xga":       float(row.get("xGoalsAgainst") or 0),
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

def fetch_hockeyviz() -> dict:
    """Scrape HockeyViz team-level shot rate and zone data."""
    log("HockeyViz stats…")
    result: dict = {"teams": {}, "fetchedAt": TODAY_ISO}
    try:
        soup = fetch_html("https://hockeyviz.com/txt/shotRatesByScore4", timeout=20)
        if soup:
            for tbl in soup.find_all("table")[:3]:
                headers_row = tbl.find("tr")
                col_names = [th.get_text(strip=True) for th in headers_row.find_all(["th","td"])] if headers_row else []
                for tr in tbl.find_all("tr")[1:35]:
                    cells = tr.find_all(["td","th"])
                    if not cells: continue
                    row = {col_names[i] if i < len(col_names) else f"c{i}": cells[i].get_text(strip=True)
                           for i in range(len(cells))}
                    team = row.get("Team","") or row.get("team","") or row.get(col_names[0] if col_names else "","")
                    if team:
                        result["teams"][team] = row
    except Exception as exc:
        log(f"HockeyViz: {exc}", "WARN")

    # Try individual stat endpoints
    for endpoint, label in [
        ("/txt/teamStats4", "teamStats"),
    ]:
        try:
            soup2 = fetch_html(f"https://hockeyviz.com{endpoint}", timeout=20)
            if soup2:
                rows: list[dict] = []
                for tbl in soup2.find_all("table")[:2]:
                    hdrs = [th.get_text(strip=True) for th in (tbl.find("tr") or BeautifulSoup("","lxml")).find_all(["th","td"])]
                    for tr in tbl.find_all("tr")[1:35]:
                        cells = tr.find_all(["td","th"])
                        if not cells: continue
                        rows.append({hdrs[i] if i < len(hdrs) else f"c{i}": cells[i].get_text(strip=True)
                                     for i in range(len(cells))})
                if rows: result[label] = rows
        except Exception as exc:
            log(f"HockeyViz {label}: {exc}", "WARN")

    vlog(f"  HockeyViz: {len(result['teams'])} teams")
    return result

def fetch_hockey_reference() -> dict:
    """Scrape Hockey Reference conference finals series stats."""
    log("Hockey Reference series stats…")
    result: dict = {"series": {}, "fetchedAt": TODAY_ISO}
    series_urls = {
        "east_finals": "https://www.hockey-reference.com/playoffs/2026-carolina-hurricanes-vs-montreal-canadiens-eastern-conference-finals.html",
        "west_finals": "https://www.hockey-reference.com/playoffs/2026-colorado-avalanche-vs-vegas-golden-knights-western-conference-finals.html",
    }
    for label, url in series_urls.items():
        try:
            time.sleep(2)
            soup = fetch_html(url, ref=True)
            if not soup: continue
            tables_data: dict = {}
            for tbl in soup.find_all("table")[:6]:
                tbl_id = tbl.get("id","")
                rows = _table_to_rows(soup, tbl_id, limit=30) if tbl_id else []
                if rows: tables_data[tbl_id or f"tbl{len(tables_data)}"] = rows
            result["series"][label] = tables_data
            vlog(f"  Hockey Ref {label}: {len(tables_data)} tables")
        except Exception as exc:
            log(f"Hockey Reference {label}: {exc}", "WARN")
    return result

def fetch_hockey_reference_team_stats() -> dict:
    """
    Full-league (not just 2 playoff series) team stats from Hockey-Reference's
    season page — PP%, PK%, shooting%, save%, and the two components needed to
    derive PDO (SH% + SV%) for every team, not just whoever made a given
    year's finals. This is what the existing fetch_hockey_reference() above
    is missing: it only ever covered two hardcoded playoff-series URLs, which
    go stale the moment that year's finalists change.
    """
    log("Hockey Reference full team stats…")
    result: dict = {"fetchedAt": TODAY_ISO, "teams": {}}
    try:
        time.sleep(2)
        soup = fetch_html("https://www.hockey-reference.com/leagues/NHL_2026.html", timeout=25, ref=True)
        if not soup:
            return result
        rows = _table_to_rows(soup, "stats", limit=40)
        for row in rows:
            team = (row.get("team_name") or row.get("team") or "").strip()
            if not team or team in ("League Average", ""):
                continue
            try:
                pp_pct = float(row.get("power_play_pct") or 0)
                pk_pct = float(row.get("penalty_kill_pct") or 0)
                sh_pct = float(row.get("shooting_pct") or 0)
                sv_pct = float(row.get("save_pct") or 0)
                result["teams"][team] = {
                    "pp_pct": pp_pct, "pk_pct": pk_pct,
                    "sh_pct": sh_pct, "sv_pct": sv_pct,
                    # PDO = SH% + SV%, expressed per-100 (~100 is "sustainable" —
                    # far above/below signals regression is likely coming,
                    # useful as a Monte Carlo confidence-interval widener).
                    "pdo": round(sh_pct + sv_pct * 100, 1) if sv_pct < 2 else round(sh_pct + sv_pct, 1),
                }
            except (ValueError, TypeError):
                continue
        log(f"  Hockey Ref team stats: {len(result['teams'])} teams")
    except Exception as exc:
        log(f"Hockey Reference team stats: {exc}", "WARN")
    return result


def fetch_futures_odds() -> dict:
    """
    Fetch championship futures odds from The Odds API.
    Covers: MLB WS, NBA Title, NHL Cup, golf majors.
    Returns {mlb, nba, nhl, golf} — each a list of {team/player, ml, book}.
    """
    api_key = os.environ.get("ODDS_API_KEY", "")
    result: dict = {"mlb": [], "nba": [], "nhl": [], "golf": [], "source": "none"}
    if not api_key:
        return result
    markets = [
        ("baseball_mlb_world_series_winner", "mlb", "World Series"),
        ("basketball_nba_championship_winner", "nba", "NBA Championship"),
        ("icehockey_nhl_championship_winner", "nhl", "Stanley Cup"),
        ("golf_us_open_winner", "golf", "US Open"),
    ]
    any_found = False
    for sport_key, sport_cat, label in markets:
        try:
            resp = fetch_json(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
                params={"apiKey": api_key, "regions": "us", "markets": "outrights",
                        "oddsFormat": "american", "dateFormat": "iso"},
            )
            if not isinstance(resp, list) or not resp:
                continue
            # Take first event (the futures market)
            ev = resp[0]
            picks: dict[str, dict] = {}  # name → {ml, book}
            for bk in (ev.get("bookmakers") or []):
                for mkt in (bk.get("markets") or []):
                    if mkt.get("key") != "outrights": continue
                    for o in (mkt.get("outcomes") or []):
                        nm, p = o.get("name",""), o.get("price")
                        if not nm or p is None: continue
                        ml = int(p)
                        if nm not in picks or ml > picks[nm]["ml"]:
                            picks[nm] = {"ml": ml, "book": bk.get("title",""), "label": label}
            # Sort by best odds (ascending ml = biggest favorite first)
            sorted_picks = sorted(picks.items(), key=lambda x: x[1]["ml"])
            result[sport_cat] = [{"name": k, **v} for k, v in sorted_picks[:20]]
            log(f"Futures {label}: {len(sorted_picks)} picks")
            any_found = True
        except Exception as exc:
            log(f"Futures odds {sport_key}: {exc}", "WARN")
    if any_found:
        result["source"] = "The Odds API"
    return result



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
# Linemate props / trends / cheatsheets  (Playwright)
# ═══════════════════════════════════════════════════════════════════════════════
def _linemate_playwright(url: str, selectors: list[str], limit: int = 100) -> list[str]:
    """Generic Playwright scraper — returns list of raw inner_text strings."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("Playwright not installed — skipping", "WARN"); return []
    items: list[str] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx     = browser.new_context(user_agent=HEADERS["User-Agent"],
                                          viewport={"width":1280,"height":900})
            page    = ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=45_000)
            page.wait_for_timeout(5_000)
            for sel in selectors:
                rows = page.query_selector_all(sel)
                if len(rows) > 3:
                    for row in rows[:limit]:
                        txt = row.inner_text().strip()
                        if len(txt) > 8: items.append(txt)
                    break
            # Fallback: grab large content sections from main/body
            if not items:
                for container in ['main', '[class*="content"]', 'body']:
                    try:
                        txt = page.inner_text(container)
                        if txt and len(txt) > 200:
                            # split into blocks separated by blank lines
                            blocks = [b.strip() for b in re.split(r'\n{2,}', txt) if len(b.strip()) > 20]
                            items = blocks[:limit]
                            if items: break
                    except: pass
            browser.close()
    except Exception as exc:
        log(f"Playwright {url}: {exc}", "WARN")
    return items

def fetch_linemate_props(sport: str) -> list[dict]:
    log(f"Linemate props {sport.upper()}…")
    raw = _linemate_playwright(
        f"https://linemate.io/{sport}",
        ["[class*='PlayerPropCard']","[class*='player-prop-card']","[class*='PropCard']",
         "[class*='prop-card']","[class*='PlayerRow']","[class*='player-row']",
         "[data-testid*='prop']","[data-testid*='player']","article","li[class*='prop']"],
    )
    props = []
    STAT_KWDS = [("strikeout","Ks"),("saves","Saves"),("goal","Goals"),
                 ("point","PTS"),("rebound","REB"),("assist","AST"),
                 ("hit","Hits"),("rbi","RBIs"),("home run","HR"),
                 ("total base","TB"),("shot","Shots"),("three","3PM"),("block","BLK"),("steal","STL"),
                 ("passing yard","Pass Yds"),("rushing yard","Rush Yds"),("receiving yard","Rec Yds"),
                 ("reception","Rec"),("passing touchdown","Pass TD"),("rushing touchdown","Rush TD"),
                 ("receiving touchdown","Rec TD"),("interception","INT"),("completion","Comp")]
    for t in raw:
        if not t or len(t) < 8: continue
        lines = [l.strip() for l in t.split("\n") if l.strip()]
        txt_lower = t.lower()
        # Identify player name: first Title Case line that's not a pure stat/number line
        player_name = ""
        for ln in lines[:6]:
            # Skip pure stat lines like "5+ Points", numbers, team tags, Over/Under
            is_stat_line = bool(re.match(r'^\d+[+\-.]', ln)) or ln.lower() in ('over','under','home','away')
            is_team_tag = bool(re.match(r'^[A-Z]{2,4}$', ln))
            is_pct = bool(re.search(r'\d+%', ln))
            is_fraction = bool(re.search(r'\d+/\d+', ln))
            is_name = bool(re.match(r'^[A-Z][a-zA-Z\'.\-]+ [A-Z][a-zA-Z\'.\-]+', ln)) and not is_stat_line
            if is_name and not is_team_tag and not is_pct and not is_fraction:
                player_name = ln; break
        # Extract over/under and line value
        over_match  = re.search(r'(over|under)\s*([\d.]+)', txt_lower)
        # Also match patterns like "15+ Points" → line=15, over=True
        plus_match  = re.search(r'(\d+(?:\.\d+)?)\+\s+\w', t) if not over_match else None
        conf_match  = re.search(r'(\d{2,3})%', t)
        team_match  = re.search(r'\b([A-Z]{2,4})\b', t)
        hit_match   = re.search(r'(\d+)/(\d+)', t)
        stat_cat = next((c for kw,c in STAT_KWDS if kw in txt_lower), "")
        # No fallback to lines[0] here on purpose — when a sport's Linemate
        # page has no real prop cards (e.g. NBA/NFL off-season), the
        # selector fallback in _linemate_playwright grabs generic page
        # content instead ("Daily picks", "GET ACCESS TO ADVANCED PLAYS…"),
        # and treating that first line as a "player name" injected page
        # marketing chrome into the live props feed as if it were a real
        # card. Require an actual name-shaped match; skip the block
        # entirely otherwise rather than inventing a fake player.
        if not player_name:
            continue
        over_val = None; line_val = None
        if over_match:
            over_val = over_match.group(1).lower() == "over"
            line_val = float(over_match.group(2))
        elif plus_match:
            over_val = True
            line_val = float(plus_match.group(1))
        # Also require a real line/over-under value — a name-shaped line
        # with no actual prop number attached isn't a usable card either.
        if line_val is None:
            continue
        hit_rate = f"{hit_match.group(1)}/{hit_match.group(2)}" if hit_match else ""
        props.append({
            "raw":      t[:300],
            "sport":    sport.upper(),
            "src":      "Linemate",
            "player":   player_name,
            "team":     team_match.group(1) if team_match else "",
            "over":     over_val,
            "line":     line_val,
            "conf":     int(conf_match.group(1)) if conf_match else 55,
            "stat":     stat_cat,
            "hitRate":  hit_rate,
        })
    log(f"  Linemate {sport.upper()}: {len(props)} cards")
    return props

def validate_props_against_schedule(props: list[dict], todays_games: list[dict]) -> list[dict]:
    """Drop any prop whose parsed team abbreviation doesn't belong to a team
    actually playing today per the real ESPN schedule for that sport — guards
    against Linemate's regex-based team-tag extraction (fetch_linemate_props'
    team_match) silently mis-assigning a prop to the wrong matchup, which is
    exactly the bug that produced wrong-matchup prop cards this session.
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

def fetch_linemate_trends(sport: str) -> list[dict]:
    log(f"Linemate trends {sport.upper()}…")
    raw = _linemate_playwright(
        f"https://linemate.io/{sport}/trends",
        ["[class*='TrendRow']","[class*='trend-row']","[class*='PlayerRow']",
         "[class*='player-row']","[class*='prop-row']","table tr","article"],
    )
    trends: list[dict] = []
    for txt in raw:
        if not txt or len(txt) < 5: continue
        parts     = [p.strip() for p in txt.split("\n") if p.strip()]
        txt_lower = txt.lower()
        # Skip pure header/table rows
        if parts and (parts[0].lower() in ("player","name","trend","gp","h","r","tb","ab","timeframe") or
                      parts[0][0].isdigit() or "	" in parts[0]):
            continue
        # Player name is usually the first non-numeric, non-header line
        player_name = next((p for p in parts if len(p) > 2 and not p[0].isdigit() and
                           p.lower() not in ("over","under","home","away","season")), parts[0] if parts else "")
        direction = ("hot" if any(k in txt_lower for k in ["hot","fire","streak","on fire"]) else
                     "cold" if any(k in txt_lower for k in ["cold","slump","cold streak"]) else
                     "up"   if any(k in txt_lower for k in ["up","↑","trending up"]) else
                     "down" if any(k in txt_lower for k in ["down","↓","trending down"]) else "neutral")
        # Extract L5/L10 hit rates like "4/5" or "8/10"
        nums = re.findall(r'(\d+)/(\d+)', txt)
        # Detect stat category from content
        stat_category = ""
        for kw, cat in [("strikeout","Ks"),("saves","Saves"),("goal","Goals"),
                        ("point","PTS"),("rebound","REB"),("assist","AST"),
                        ("hit","Hits"),("rbi","RBIs"),("home run","HR"),
                        ("total base","TB"),("shot","Shots")]:
            if kw in txt_lower:
                stat_category = cat
                break
        if not player_name or len(player_name) < 3:
            continue
        trends.append({
            "player":    player_name,
            "category":  stat_category or (parts[1] if len(parts)>1 else ""),
            "direction": direction,
            "l5":        f"{nums[0][0]}/{nums[0][1]}" if nums else "",
            "l10":       f"{nums[1][0]}/{nums[1][1]}" if len(nums)>1 else "",
            "lineMove":  "up" if "line up" in txt_lower else ("down" if "line down" in txt_lower else ""),
            "raw":       txt[:250], "sport": sport.upper(), "src": "Linemate/trends",
        })
    log(f"  Linemate trends {sport.upper()}: {len(trends)} entries")
    return trends

def fetch_linemate_cheatsheet(sport: str) -> list[dict]:
    log(f"Linemate cheatsheet {sport.upper()}…")
    raw = _linemate_playwright(
        f"https://linemate.io/{sport}/cheatsheets/recent-form",
        ["[class*='Row']","[class*='row']","table tr","li","article"],
    )
    return [{"raw":t, "sport":sport.upper(), "src":"Linemate/form"} for t in raw]

# ═══════════════════════════════════════════════════════════════════════════════
# Sports news + injuries
# ═══════════════════════════════════════════════════════════════════════════════
_INJURY_KEYWORDS = [
    "injured","out","doubtful","questionable","day-to-day","IR","scratch",
    "suspended","illness","flu","knee","ankle","shoulder","back","concussion",
    "unavailable","game-time","sidelined","hamstring","wrist","hand","elbow",
]

# ═══════════════════════════════════════════════════════════════════════════════
# Soccer (Champions League / Premier League / La Liga / Bundesliga / MLS)
# ═══════════════════════════════════════════════════════════════════════════════
# FBref scraping (fbref.com, part of the Sports-Reference family) was retired
# 2026-08-22: its Cloudflare bot-check 403s every request from both
# residential and GitHub Actions-runner IPs, with no header/UA workaround, and
# had done so for the whole time this pipeline existed. It was never a real
# fallback source in practice, just a guaranteed-403 request plus a 2-second
# rate-limit sleep on every run before falling through to fetch_espn_soccer_
# league() anyway. See that function for the actual live data path.
_SOCCER_LEAGUES: tuple[str, ...] = ("cl", "pl", "liga", "bl", "mls", "ita")

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
        f"https://sports.core.api.espn.com/v2/sports/soccer/leagues/{espn_league}/seasons/{season_year}/types/1/teams/{tid}/statistics"
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
    Fetch all 6 tracked soccer leagues' team stats via fetch_espn_soccer_
    league() -- see that function's docstring for the season-blend logic and
    why FBref (formerly tried first here) was retired entirely rather than
    kept as a dead-weight first attempt.
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
    it yet; revisit once those leagues resume in August). Same pattern as
    fetch_mlb_batter_rosters(): ESPN's team-roster endpoint gives name/team/
    position for all 30 MLS clubs in one request per team — no separate
    stat-based rating needed, computeInjuryImpact()'s soccer branch will key
    off position (GK highest impact, then DEF/MID/FWD) same as MLB keys off
    the injury record's own position field.
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
    YEAR = 2026
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
    YEAR = 2026
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

def fetch_sports_news() -> dict:
    """Fetch latest news articles for all sports/leagues from ESPN."""
    news: dict = {}
    sport_map = dict(ESPN_LEAGUE_PATHS)
    sport_map["football"] = sport_map.pop("nfl")  # keep the pre-existing "football" key the frontend already reads
    for sport_key, espn_path in sport_map.items():
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
    for row in bets:
        if row.get("outcome") not in ("win", "loss", "push"):
            continue
        raw = row.get("raw") or {}
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
    for g in nba_today:
        if g.get("state") != "pre": continue
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
    # Also mirror to frontend/ for local dev
    fe_mirror = ROOT / "frontend" / "data.json"
    fe_mirror.write_text(payload)
    note(f"data.json written ({len(payload)//1024} KB) → docs/ (github.io) + frontend/ (local)")
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
            # local frontend mirror (not pushed to Pages but kept in sync)
            "frontend/data.json",
            "frontend/index.html",
            "frontend/live_data.json",
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
            games_total = (len(d.get("mlb", {}).get("today", [])) + len(d.get("nba", {}).get("today", []))
                           + len(d.get("nhl", {}).get("today", [])) + len(d.get("wnba", {}).get("today", []))
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
            mlb_t, _  = fetch_mlb_scoreboard()
            nba_t, _  = fetch_nba_scoreboard()
            nhl_t, _  = fetch_nhl_today()
            live_bundle = {
                "generatedMT": now_mt.isoformat(),
                "ts":          now_mt.strftime("%H:%M MT"),
                "mlbLive":     [g for g in mlb_t  if g.get("state") == "in"],
                "nbaLive":     [g for g in nba_t  if g.get("state") == "in"],
                "nhlLive":     [g for g in nhl_t  if g.get("state") in ("LIVE","CRIT","IN")],
                "mlbAll":      mlb_t,
                "nbaAll":      nba_t,
                "nhlAll":      nhl_t,
            }
            live_probs = {"mlb":[], "nba":[], "nhl":[]}
            for g in live_bundle["mlbLive"]:
                live_probs["mlb"].append(compute_live_win_prob(g, "mlb"))
            for g in live_bundle["nbaLive"]:
                live_probs["nba"].append(compute_live_win_prob(g, "nba"))
            for g in live_bundle["nhlLive"]:
                live_probs["nhl"].append(compute_live_win_prob(g, "nhl"))
            live_bundle["liveProbs"] = live_probs
            live_bundle["autoSettled"] = auto_settle(live_bundle["mlbAll"], live_bundle["nbaAll"], live_bundle["nhlAll"])
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
def main() -> None:
    global _verbose

    parser = argparse.ArgumentParser(description="Clairvoyance v6.0 data refresh")
    parser.add_argument("--push",          action="store_true", help="Commit + push to GitHub")
    parser.add_argument("--dry-run",       action="store_true", help="Fetch only, no writes")
    parser.add_argument("--no-linemate",   action="store_true", help="Skip Playwright/Linemate")
    parser.add_argument("--no-reference",  action="store_true", help="Skip Baseball/Basketball/Hockey Reference")
    parser.add_argument("--mode",          choices=["full","live","props"], default="full")
    parser.add_argument("--sport",         choices=["nba","mlb","nhl","nfl","soccer","all"], default="all")
    parser.add_argument("--verbose","-v",  action="store_true")
    args    = parser.parse_args()
    _verbose = args.verbose

    # ── live-window short-circuit ────────────────────────────────────────────
    if args.mode == "live":
        run_live_window(push=args.push)
        return

    log("=" * 60)
    log(f"Clairvoyance v6.0 — {TS_DISPLAY}")
    log(f"Mode: {args.mode} | Sport: {args.sport}")
    log("=" * 60)

    S = args.sport  # shorthand

    # ── props-only mode ──────────────────────────────────────────────────────
    # Was writing to data/linemate.json, which nothing else in this codebase
    # ever reads — the live app only reads docs/data.json's own
    # bundle["linemate"] key (written by the full run below), so this mode
    # ran real scrapes whose output silently went nowhere. Now reads the
    # current docs/data.json, merges in just this run's props/trends/form
    # for the requested sport(s), and writes it back through the same
    # write_data_json()/git_push() path the full run uses — everything else
    # in the bundle (standings, odds, injuries, etc, last written by the
    # most recent full 3x/day run) passes through untouched.
    if args.mode == "props":
        if not FE_DATA.exists():
            log("props-only mode: docs/data.json doesn't exist yet — run a full sync first", "ERROR")
            return
        bundle = json.loads(FE_DATA.read_text())
        bundle.setdefault("linemate", {}).setdefault("props", {})
        bundle["linemate"].setdefault("trends", {})
        bundle["linemate"].setdefault("form", {})
        for sport in ["mlb","nba","nhl","nfl"]:
            if S in (sport,"all"):
                props = fetch_linemate_props(sport)
                # This fast-path never applied validate_props_against_schedule()
                # at all -- the real fix for Linemate's regex-based team-tag
                # extraction silently mis-assigning a prop to the wrong
                # matchup only ever ran in the full sync path. Since NBA/NFL/
                # NHL's actual daily props workflows call --mode props (this
                # exact code path), every real daily refresh for those three
                # sports was missing this protection. NFL's schedule spans a
                # week rather than "today" like the others, so it needs its
                # own fresh weekly fetch; MLB/NBA/NHL validate against
                # whatever real schedule the last full sync already cached
                # in this same bundle, avoiding an extra fetch here.
                if sport == "nfl":
                    props = validate_props_against_schedule(props, fetch_week_schedule("football/nfl","nfl"))
                else:
                    props = validate_props_against_schedule(props, (bundle.get(sport) or {}).get("today") or [])
                bundle["linemate"]["props"][sport]  = props
                bundle["linemate"]["trends"][sport] = fetch_linemate_trends(sport)
                bundle["linemate"]["form"][sport]   = fetch_linemate_cheatsheet(sport)
                time.sleep(1)
        bundle["linemate"]["generatedAt"] = TS_DISPLAY
        write_data_json(bundle)
        if args.push: git_push("props-only refresh")
        return

    # ── full fetch phase ─────────────────────────────────────────────────────
    # Schedule accuracy: log the exact dates used per sport to confirm alignment
    log(f"Schedule dates → MLB/NBA: {TODAY_ET} (ET) · NHL/F1: {TODAY_ISO} (MT ISO)")
    mlb_today, mlb_tom   = fetch_mlb_scoreboard(TODAY_ET)  if S in ("mlb","all") else ([],[])
    mlb_standings        = fetch_mlb_standings()          if S in ("mlb","all") else {}
    mlb_week             = fetch_mlb_schedule_week()      if S in ("mlb","all") else []
    mlb_ref              = (fetch_baseball_reference()    if not args.no_reference else {}) if S in ("mlb","all") else {}
    mlb_bullpen          = (fetch_mlb_bullpen_stats(mlb_ref.get("pitching", [])) if not args.no_reference else {}) if S in ("mlb","all") else {}
    mlb_sabre            = (fetch_mlb_team_sabermetrics() if not args.no_reference else {}) if S in ("mlb","all") else {}
    mlb_fielding         = (fetch_mlb_team_fielding()     if not args.no_reference else {}) if S in ("mlb","all") else {}
    mlb_batters          = fetch_mlb_batter_rosters()     if S in ("mlb","all") else {}
    mlb_statcast         = fetch_mlb_statcast_team(mlb_batters) if S in ("mlb","all") else {}
    mlb_nrfi             = fetch_mlb_nrfi_data(mlb_today) if S in ("mlb","all") else []
    if S in ("mlb","all"):
        _check_source_health("MLB batter rosters (ESPN)", len(mlb_batters))
        _check_source_health("MLB Statcast (Baseball Savant)", len(mlb_statcast))

    nba_today, nba_tom   = fetch_nba_scoreboard()         if S in ("nba","all") else ([],[])
    nba_standings        = fetch_nba_standings()          if S in ("nba","all") else {}
    nba_players          = fetch_nba_player_stats()       if S in ("nba","all") else []
    nba_bracket          = fetch_nba_playoff_bracket()    if S in ("nba","all") else {}
    nba_ref              = (fetch_basketball_reference()  if not args.no_reference else {}) if S in ("nba","all") else {}
    nba_adv              = (fetch_nba_team_advanced()     if not args.no_reference else {}) if S in ("nba","all") else {}
    nba_four_factors     = (fetch_nba_four_factors()      if not args.no_reference else {}) if S in ("nba","all") else {}
    nba_roster           = fetch_nba_roster()             if S in ("nba","all") else {}

    nhl_today, nhl_tom   = fetch_nhl_today()               if S in ("nhl","all") else ([],[])
    nhl_standings        = fetch_nhl_standings()          if S in ("nhl","all") else {}
    nhl_roster           = fetch_nhl_roster()             if S in ("nhl","all") else {}
    nhl_bracket          = fetch_nhl_playoff_bracket()    if S in ("nhl","all") else {}
    nhl_edge             = fetch_nhl_edge()               if S in ("nhl","all") else {}
    mp                   = fetch_moneypuck()              if S in ("nhl","all") else {}
    hockeyviz            = fetch_hockeyviz()              if S in ("nhl","all") else {}
    hockey_ref           = (fetch_hockey_reference()      if not args.no_reference else {}) if S in ("nhl","all") else {}
    hockey_ref_teams     = (fetch_hockey_reference_team_stats() if not args.no_reference else {}) if S in ("nhl","all") else {}

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

    # Weather for MLB home teams
    weather: dict = {}
    if S in ("mlb","all"):
        log("Fetching MLB weather…")
        for g in mlb_today:
            home = g.get("home","")
            if home and home not in weather:
                w = fetch_weather(home)
                if w: weather[home] = w
                time.sleep(0.3)

    # Linemate
    lm_props:  dict = {"nba":[],"mlb":[],"nhl":[],"wnba":[],"nfl":[]}
    lm_trends: dict = {"nba":[],"mlb":[],"nhl":[],"wnba":[],"nfl":[]}
    lm_form:   dict = {"nba":[],"mlb":[],"nhl":[],"wnba":[],"nfl":[]}
    _lm_schedule = {"nba": nba_today, "mlb": mlb_today, "nhl": nhl_today, "wnba": []}
    if not args.no_linemate:
        for sport in ["nba","mlb","nhl","wnba","nfl"]:
            if S in (sport,"all") or (sport=="wnba" and S=="nba"):
                lm_props[sport]  = fetch_linemate_props(sport);     time.sleep(1)
                lm_trends[sport] = fetch_linemate_trends(sport);    time.sleep(1)
                lm_form[sport]   = fetch_linemate_cheatsheet(sport); time.sleep(1)
                if sport == "nfl":
                    # Weekly, not daily, schedule — NFL games span the
                    # whole week rather than clustering on "today" the way
                    # the other sports' schedules do.
                    lm_props[sport] = validate_props_against_schedule(lm_props[sport], fetch_week_schedule("football/nfl","nfl"))
                elif sport != "wnba":  # WNBA's today-schedule isn't fetched yet — validated below
                    lm_props[sport] = validate_props_against_schedule(lm_props[sport], _lm_schedule[sport])

    # NCAA Baseball + WNBA + PWHL
    # NCAA baseball is no longer tracked in the engine — purged from the
    # daily fetch. Bundle key kept (empty) below for the same reason as F1.
    ncaa_baseball: dict = {}
    wnba          = fetch_wnba()          if S in ("nba","all") else {}
    wnba_roster   = fetch_wnba_roster()   if S in ("nba","all") else {}
    if wnba: wnba["roster"] = wnba_roster
    if lm_props.get("wnba"):
        lm_props["wnba"] = validate_props_against_schedule(lm_props["wnba"], wnba.get("today", []))
    pwhl          = fetch_pwhl()          if S in ("nhl","all") else {}

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
    if S in ("soccer","all"):
        for _lkey, _lcfg in ESPN_SOCCER_LEAGUES.items():
            if _lkey == "mls":
                continue  # MLS rosters already fetched separately (fetch_mls_rosters)
            _rosters = fetch_espn_soccer_rosters(_lcfg["espn"], _lcfg["name"])
            if _rosters:
                soccer_fbref.setdefault(_lkey, {"league": _lcfg["name"], "fetchedAt": TODAY_ISO, "teams": {}})
                soccer_fbref[_lkey]["rosters"] = _rosters
            time.sleep(0.3)
    # MLS gets its own first-party feed straight from mlssoccer.com's stats
    # API — real xG per club (not the goals-per-game proxy the ESPN fallback
    # uses for the other leagues), so it takes priority over whatever
    # fetch_soccer_team_stats_all() put in soccer_fbref["mls"] above.
    mls_stats     = fetch_mls_team_stats() if S in ("soccer","all") else {}
    mls_standings = fetch_mls_standings()  if S in ("soccer","all") else []
    mls_schedule  = fetch_mls_schedule()   if S in ("soccer","all") else []
    mls_rosters   = fetch_mls_rosters()    if S in ("soccer","all") else {}
    if S in ("soccer","all"):
        # MLS always has 30 clubs in-season — unlike a daily schedule, this
        # should never legitimately be 0, so it's safe to alert on.
        _check_source_health("MLS club stats (mlssoccer.com)", len(mls_stats.get("teams", {})))
        _check_source_health("MLS standings (mlssoccer.com)", len(mls_standings))
        _check_source_health("MLS rosters (ESPN)", len(mls_rosters))

    # Weather for MLS home clubs — same rationale as MLB: open-air stadiums,
    # wind/rain measurably suppress O/U goal totals. Keyed by lowercase club
    # name (matching mls_schedule.json's "home" field) rather than abbreviation.
    soccer_weather: dict = {}
    if S in ("soccer","all"):
        log("Fetching MLS weather…")
        for m in mls_schedule:
            home = (m.get("home") or "").strip()
            key = home.lower()
            if key and key not in soccer_weather:
                w = fetch_soccer_weather(home)
                if w: soccer_weather[key] = w
                time.sleep(0.3)
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
    mlb_week_schedule = fetch_week_schedule("baseball/mlb","mlb",10)      if S in ("mlb","all") else []
    nba_week_schedule = fetch_week_schedule("basketball/nba","nba",8)      if S in ("nba","all") else []
    nhl_week_schedule = fetch_week_schedule("hockey/nhl","nhl",8)          if S in ("nhl","all") else []

    # News + injuries + transactions — all now cover every tracked league,
    # not just the original MLB/NBA/NHL(/WNBA) subset.
    sports_news  = fetch_sports_news()
    injuries     = fetch_injuries_all()
    transactions = fetch_transactions_all()

    # Best bets + auto-settle
    # Best odds per sport (Odds API if key set, ESPN fallback)
    mlb_best_odds = fetch_best_odds("mlb", mlb_today) if S in ("mlb","all") else {}
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
    wnba_best_odds = fetch_best_odds("wnba", wnba.get("today", [])) if S in ("nba","all") else {}
    nfl_best_odds  = fetch_best_odds("nfl", []) if S in ("all",) else {}
    cfb_best_odds  = fetch_best_odds("cfb", []) if S in ("all",) else {}
    # Soccer leagues — the piece explicitly deferred in the last odds pass.
    # Club leagues (PL/La Liga/Bundesliga/MLS) key by normalized full club
    # name (_soccer_club_key) to match how soccer_fbref.json already keys
    # its team data; World Cup keys by the same 3-letter country codes
    # WC26_SCHEDULE already uses (_wc_name_to_abbr).
    pl_best_odds   = fetch_best_odds("pl",   [], name_resolver=_soccer_club_key) if S in ("soccer","all") else {}
    liga_best_odds = fetch_best_odds("liga", [], name_resolver=_soccer_club_key) if S in ("soccer","all") else {}
    bl_best_odds   = fetch_best_odds("bl",   [], name_resolver=_soccer_club_key) if S in ("soccer","all") else {}
    mls_best_odds = fetch_best_odds("mls", [], name_resolver=_soccer_club_key) if S in ("soccer","all") else {}
    wc_best_odds   = fetch_best_odds("wc",   [], name_resolver=_wc_name_to_abbr) if S in ("soccer","all") else {}

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
        },
        "nhl": {
            "today":        nhl_today,
            "tomorrow":     nhl_tom,
            "standings":    nhl_standings,
            "roster":       nhl_roster,
            "bracket":      nhl_bracket,
            "edge":         nhl_edge,
            "hockeyviz":    hockeyviz,
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
            for p in (ROOT/"frontend"/"card.png", ROOT/"docs"/"card.png"):
                img.save(str(p), format="PNG", optimize=True)
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
