"""NBA rolling schedule -- real matchups, dates, live states/scores and market lines for yesterday + the next ~10 days, one file
(docs/nba_schedule.json), same architecture as fetch_nhl.py / fetch_cfb.py / fetch_nfl.py.

WHY: until this existed the NBA had NO schedule file.  `nba.today` / `nba.tomorrow` / `nba.weekSchedule` come only from the 3x/day
scoreboard pull inside docs/data.json (scheduled-refresh.yml, landing 3-6 h late), so the NBA cards and the lock pipeline read dates,
states and market lines that could be hours old around a lock.  This file is refreshed by daily-schedules-refresh.yml (twice a day)
and, right before every lock pass, by scripts/lock_prep.py (job `nba`) -- exactly like docs/nhl_schedule.json.

SOURCE: ESPN's public scoreboard, one call per Eastern-time calendar day (ESPN indexes `dates=YYYYMMDD` by the ET day, so a 10 PM ET
tip-off is on that ET day even though its UTC date is the next one):
    https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD&limit=50
A request carrying a custom User-Agent is 403'd by site.api.espn.com (documented in fetch_nhl.py / fetch_nfl.py), so none is sent.

ODDS SHAPE (verified live 2026-10-03 on the NHL + NFL scoreboards, which share ESPN's current DraftKings odds object; NBA preseason
games carry no odds object at all, so regular-season NBA is expected to look the same):
    competitions[0].odds[0] = {provider:{name}, details:"IND -4.5", overUnder:46.5, spread:4.5,   <- spread = HOME team's line (signed)
        homeTeamOdds/awayTeamOdds: {favorite, underdog, team}   <- NO moneyLine key any more
        moneyline:  {home:{close:{odds:"+170"}, open:..}, away:{close:{odds:"-205"}}}
        pointSpread:{home:{close:{line:"+4.5", odds:"-115"}}, away:{close:{line:"-4.5", odds:"-105"}}}
        total:      {over:{close:{line:"o46.5", odds:"-115"}}, under:{close:{line:"u46.5", odds:"-105"}}}}
parse_odds() reads those nested fields first and falls back to the OLD flat shape (homeTeamOdds.moneyLine, top-level overOdds/
underOdds -- still what the ESPN *core* odds endpoint returns), so either shape produces the same output.  The top-level numbers are
kept raw (American odds as ints, lines as floats).

OUTPUT (generated_at is deliberately the FIRST key: the app's freshness line reads the first ~1 KB of the file):
    {"generated_at": "YYYY-MM-DD HH:MM UTC", "season": "2026-27", "seasonYear": 2027, "source": ..., "window": {...}, "meta": {...},
     "games": [{id, date, day, home, away, homeName, awayName, state, homeScore, awayScore, period, displayClock, statusDetail,
                seasonType, seasonSlug, preseason, neutralSite, venue, network, note, homeRecord, awayRecord,
                homeML, awayML, spread, spreadFav, spreadHomeOdds, spreadAwayOdds, overUnder, ou, ouOver, ouUnder, details,
                provider, oddsAt}, ...]}

FAIL-OPEN: a failed day never blanks the file -- that day's games from the previous file are carried over (marked "carried": true,
"lastSeen"); if EVERY day fails (or ESPN suddenly returns nothing while the previous file had upcoming games) nothing is written at
all and the process exits 1 (the workflows run it continue-on-error).  Writes are atomic (temp file + os.replace).  A pre-game line
that vanishes for one run is carried for ODDS_CARRY_MAX_H hours (marked "oddsCarried": true; oddsAt keeps the real age visible).

    python3 scripts/fetch_nba.py --out /tmp/nba_schedule.json --dry-run     # fetch + summarise, write nothing
    python3 scripts/fetch_nba.py --out /tmp/nba_schedule.json --days 10
    python3 scripts/fetch_nba.py --push                                    # the workflow form: write docs/ + commit + push
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - tzdata missing: fall back to a fixed EST offset (only shifts the day boundary by an hour in summer)
    ET = timezone(timedelta(hours=-5))

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_OUT = ROOT / "docs" / "nba_schedule.json"
HEALTH_LEAGUE = "NBA"

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_CORE_ODDS = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{eid}/competitions/{eid}/odds"
SOURCE = "ESPN scoreboard (site.api.espn.com), one call per Eastern-time day"
# No custom headers: site.api.espn.com 403s a custom User-Agent (see fetch_nhl.py HEADERS).
HEADERS: dict = {}

DEFAULT_DAYS = 10            # days AFTER today (ET) to fetch; yesterday is always fetched too (finals / late settle)
PAUSE_S = 0.25               # polite pacing between ESPN calls
HTTP_TIMEOUT_S = 15
HTTP_RETRIES = 3
ODDS_CARRY_MAX_H = 36        # a vanished pre-game line is carried at most this long (its real age stays in oddsAt)
CORE_ODDS_MAX = 30           # cap on per-game core-odds fallback calls per run
CORE_ODDS_LOOKAHEAD_H = 36   # only games this close to tip-off get the fallback (lines post ~1-2 days out)

GENERATED_FMT = "%Y-%m-%d %H:%M UTC"
ISO_FMT = "%Y-%m-%dT%H:%MZ"
# Per-game fields that come from the odds object (what carry-forward copies / what "has a line" means).
ODDS_FIELDS = ("homeML", "awayML", "spread", "spreadFav", "spreadHomeOdds", "spreadAwayOdds", "overUnder", "ou", "ouOver",
               "ouUnder", "details", "provider")
ODDS_VALUE_FIELDS = ("homeML", "awayML", "spread", "overUnder")   # any of these non-null == the game has a usable line


def _log(msg: str) -> None:
    print(f"[nba] {msg}", flush=True)


# ── tiny parsing helpers ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _num(x):
    """'+4.5' / '-1.5' / 46.5 / 'PK' -> float (PK / EVEN-style pick'em lines -> 0.0); None when unusable."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if not s:
        return None
    if s.upper() in ("PK", "PICK", "PICKEM", "PICK'EM"):
        return 0.0
    try:
        return float(s.lstrip("+"))
    except ValueError:
        return None


def _american(x):
    """American odds ('-230', '+190', -230.0, 'EVEN') -> int, else None."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return int(round(x)) if x == x else None
    s = str(x).strip().upper()
    if s in ("EVEN", "EV", "PK"):
        return 100
    try:
        return int(float(s.lstrip("+")))
    except ValueError:
        return None


def _line(x):
    """Total line text 'o46.5' / 'u46.5' / 46.5 -> 46.5."""
    if isinstance(x, str):
        x = re.sub(r"^[ou]", "", x.strip(), flags=re.I)
    return _num(x)


def _side(odds: dict, market: str, side: str, which: str = "close") -> dict:
    return (((odds.get(market) or {}).get(side) or {}).get(which)) or {}


def parse_odds(odds, home_abbr: str | None = None, away_abbr: str | None = None) -> dict:
    """One ESPN odds object (scoreboard `competitions[0].odds[0]` or a core-API odds item) -> the flat per-game odds fields
    (ODDS_FIELDS), or {} when `odds` carries no usable number.  `spread` is the HOME team's line (negative = home favoured), the same
    convention as ESPN's own top-level `spread` and as `hL` in docs/app.html's NBA_TONIGHT; `spreadFav` is the favourite's abbreviation."""
    if not isinstance(odds, dict) or not odds:
        return {}
    home_t, away_t = odds.get("homeTeamOdds") or {}, odds.get("awayTeamOdds") or {}
    home_ml = _american(_side(odds, "moneyline", "home").get("odds"))
    away_ml = _american(_side(odds, "moneyline", "away").get("odds"))
    if home_ml is None:
        home_ml = _american(home_t.get("moneyLine"))      # OLD flat shape / core API
    if away_ml is None:
        away_ml = _american(away_t.get("moneyLine"))
    spread_c, spread_a = _side(odds, "pointSpread", "home"), _side(odds, "pointSpread", "away")
    spread = _num(spread_c.get("line"))
    if spread is None:
        spread = _num(odds.get("spread"))                  # ESPN's top-level number is the home line too
    total = _num(odds.get("overUnder"))
    if total is None:
        total = _line(_side(odds, "total", "over").get("line")) or _line(_side(odds, "total", "under").get("line"))
    ou_over = _american(_side(odds, "total", "over").get("odds"))
    ou_under = _american(_side(odds, "total", "under").get("odds"))
    if ou_over is None:
        ou_over = _american(odds.get("overOdds"))
    if ou_under is None:
        ou_under = _american(odds.get("underOdds"))
    fav = None
    if spread is not None and spread != 0:
        fav = home_abbr if spread < 0 else away_abbr
    out = {
        "homeML": home_ml, "awayML": away_ml,
        "spread": spread, "spreadFav": fav,
        "spreadHomeOdds": _american(spread_c.get("odds")), "spreadAwayOdds": _american(spread_a.get("odds")),
        "overUnder": total, "ou": total, "ouOver": ou_over, "ouUnder": ou_under,
        "details": odds.get("details") or None,
        "provider": (odds.get("provider") or {}).get("name") or None,
    }
    if all(out[k] is None for k in ODDS_VALUE_FIELDS):
        return {}
    return out


def empty_odds() -> dict:
    d = {k: None for k in ODDS_FIELDS}
    d["oddsAt"] = None
    return d


def et_day(dt: datetime) -> str:
    """aware datetime -> 'YYYYMMDD' of the Eastern-time calendar day (ESPN's `dates=` index)."""
    return dt.astimezone(ET).strftime("%Y%m%d")


def iso_to_dt(s):
    try:
        return datetime.strptime(str(s), ISO_FMT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None


def parse_event(ev: dict, now: datetime) -> dict | None:
    """One scoreboard event -> a schedule game (without odds oddsAt stamping beyond `now`); None when it has no id / both teams."""
    comp = (ev.get("competitions") or [{}])[0]
    comps = comp.get("competitors") or []
    home = next((c for c in comps if c.get("homeAway") == "home"), {})
    away = next((c for c in comps if c.get("homeAway") == "away"), {})
    h_team, a_team = home.get("team") or {}, away.get("team") or {}
    if not ev.get("id") or not h_team.get("abbreviation") or not a_team.get("abbreviation"):
        return None
    status = comp.get("status") or ev.get("status") or {}
    stype = status.get("type") or {}
    state = stype.get("state") or "pre"
    sname = str(stype.get("name") or "")
    postponed = any(w in sname.upper() for w in ("POSTPONED", "CANCELED", "CANCELLED", "SUSPENDED", "DELAYED_TO"))
    if postponed:
        state = "pre"       # never let a postponed/cancelled game look final to the settle logic
    season = ev.get("season") or {}
    season_type = season.get("type")

    def score(c):
        if state == "pre":
            return None
        try:
            return int(c.get("score"))
        except (TypeError, ValueError):
            return None

    def record(c):
        for r in c.get("records") or []:
            if r.get("type") == "total":
                return r.get("summary")
        return None

    date = ev.get("date") or comp.get("date")
    dt = iso_to_dt(date)
    notes = [n.get("headline") for n in (comp.get("notes") or []) if n.get("headline")]
    g = {
        "id": str(ev["id"]),
        "date": date,
        "day": dt.astimezone(ET).strftime("%Y-%m-%d") if dt else None,      # Eastern-time calendar day (what ESPN indexes by)
        "home": h_team["abbreviation"],
        "away": a_team["abbreviation"],
        "homeName": h_team.get("displayName"),
        "awayName": a_team.get("displayName"),
        "state": state,
        "homeScore": score(home),
        "awayScore": score(away),
        "period": status.get("period", 0),
        "displayClock": status.get("displayClock", ""),
        "statusDetail": stype.get("shortDetail") or stype.get("detail"),
        "seasonType": season_type,                    # 1 preseason, 2 regular, 3 post, 5 play-in
        "seasonSlug": season.get("slug"),
        "preseason": season_type == 1,                # the lock pipeline never locks these (auto_lock_settle.py: seasonType === 1)
        "neutralSite": bool(comp.get("neutralSite")),
        "venue": (comp.get("venue") or {}).get("fullName"),
        "network": ((comp.get("broadcasts") or [{}])[0].get("names") or [""])[0] or None,
        "note": notes[0] if notes else None,
        "homeRecord": record(home),
        "awayRecord": record(away),
    }
    if postponed:
        g["postponed"] = True
        g["statusName"] = sname
    odds = parse_odds((comp.get("odds") or [None])[0], g["home"], g["away"])
    g.update(empty_odds())
    if odds:
        g.update(odds)
        g["oddsAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return g


# ── HTTP ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def get_json(url: str, params: dict | None = None, *, retries: int = HTTP_RETRIES, timeout: float = HTTP_TIMEOUT_S,
             sleep=time.sleep, session=None):
    """GET + JSON with retries (network errors, 429, 5xx).  Raises the last error when every attempt fails."""
    getter = (session or requests).get
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            r = getter(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = RuntimeError(f"HTTP {r.status_code}")
            if r.status_code not in (429, 500, 502, 503, 504):
                break                                     # 403/404: retrying will not change it
        except Exception as exc:                          # network / JSON decode
            last = exc
        if attempt + 1 < retries:
            sleep(1.0 * (2 ** attempt))
    raise last if last else RuntimeError("request failed")


def fetch_day(day: str, get=None, now: datetime | None = None) -> tuple[list[dict], dict]:
    """One ET day -> (games, league_season_info).  Raises on a failed fetch (the caller carries that day forward)."""
    now = now or datetime.now(timezone.utc)
    get = get or get_json
    resp = get(f"{ESPN_BASE}/scoreboard", params={"dates": day, "limit": 50})
    if not isinstance(resp, dict) or "events" not in resp:
        raise RuntimeError("unexpected scoreboard payload (no 'events')")
    games, seen = [], set()
    for ev in resp.get("events") or []:
        try:
            g = parse_event(ev, now)
        except Exception as exc:                          # one malformed event must never drop the day
            _log(f"  {day}: skipped malformed event {ev.get('id')}: {exc}")
            continue
        if g and g["id"] not in seen:
            seen.add(g["id"])
            games.append(g)
    league0 = (resp.get("leagues") or [{}])[0]
    season = dict(league0.get("season") or {})
    cal = league0.get("calendar")
    if isinstance(cal, list):
        season["_calendar"] = [str(c)[:10] for c in cal if isinstance(c, str) and len(c) >= 10]   # every ET day of the season that has games (preseason + regular + playoffs)
    return games, season


# ── carry-forward ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def load_previous(path: Path) -> dict:
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _prev_day(g: dict) -> str | None:
    d = g.get("day")
    if d:
        return str(d).replace("-", "")
    dt = iso_to_dt(g.get("date"))
    return et_day(dt) if dt else None


def carry_failed_days(games: list[dict], prev_doc: dict, failed_days: set, prev_seen: str | None) -> list[str]:
    """Append the previous file's games of every day whose fetch FAILED (unless already present); -> ids carried."""
    carried: list[str] = []
    present = {g["id"] for g in games}
    for pg in (prev_doc or {}).get("games") or []:
        if not isinstance(pg, dict) or not pg.get("id") or pg["id"] in present:
            continue
        if _prev_day(pg) in failed_days:
            g = dict(pg)
            g["carried"] = True
            g["lastSeen"] = pg.get("lastSeen") or prev_seen
            games.append(g)
            present.add(g["id"])
            carried.append(g["id"])
    return carried


def carry_odds(games: list[dict], prev_doc: dict, now: datetime, max_hours: float = ODDS_CARRY_MAX_H) -> list[str]:
    """A still-upcoming game whose line is missing this run but was present in the previous file keeps that line (<= max_hours old by
    its own oddsAt).  Marked oddsCarried; oddsAt is NOT refreshed.  -> ids that kept a carried line."""
    prev = {g.get("id"): g for g in (prev_doc or {}).get("games") or [] if isinstance(g, dict)}
    out: list[str] = []
    for g in games:
        if g.get("state") != "pre" or any(g.get(k) is not None for k in ODDS_VALUE_FIELDS):
            continue
        pg = prev.get(g["id"])
        if not pg or pg.get("state") != "pre" or all(pg.get(k) is None for k in ODDS_VALUE_FIELDS):
            continue
        at = iso_to_dt(pg.get("oddsAt"))
        if at is None or (now - at).total_seconds() > max_hours * 3600:
            continue
        for k in ODDS_FIELDS:
            g[k] = pg.get(k)
        g["oddsAt"] = pg.get("oddsAt")
        g["oddsCarried"] = True
        out.append(g["id"])
    return out


def core_odds_fallback(games: list[dict], now: datetime, get=None, sleep=time.sleep,
                       max_calls: int = CORE_ODDS_MAX, lookahead_h: float = CORE_ODDS_LOOKAHEAD_H) -> int:
    """For near-term non-preseason pre-games the scoreboard gave no line for, try ESPN's core odds endpoint (same fallback
    clairvoyance_update._espn_odds uses).  Fail-open, capped.  -> number of games that gained a line."""
    get = get or get_json
    gained = calls = 0
    for g in games:
        if calls >= max_calls:
            break
        if g.get("state") != "pre" or g.get("preseason") or g.get("carried") or any(g.get(k) is not None for k in ODDS_VALUE_FIELDS):
            continue
        dt = iso_to_dt(g.get("date"))
        if dt is None or not (0 <= (dt - now).total_seconds() / 3600 <= lookahead_h):
            continue
        calls += 1
        try:
            url = ESPN_CORE_ODDS.format(eid=g["id"])
            idx = get(url, params=None, retries=2, timeout=10)
            items = (idx or {}).get("items") or []
            item = items[0] if items else None
            if item and "$ref" in item and not item.get("details") and item.get("overUnder") is None:
                item = get(item["$ref"].replace("http://", "https://"), params=None, retries=2, timeout=10)
            odds = parse_odds(item, g["home"], g["away"])
            if odds:
                g.update(odds)
                g["oddsAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                gained += 1
        except Exception as exc:
            _log(f"  core odds fallback failed for {g['id']}: {exc}")
        sleep(PAUSE_S)
    return gained


ESPN_CORE_SEASON_TYPE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/seasons/{year}/types/2"


def regular_season_days(season_info: dict, season_year: int, get=None, prev_doc: dict | None = None) -> tuple[dict | None, list[str] | None]:
    """-> ({"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, [every ET day with a REGULAR-season game]) -- the NBA tab's date dropdown (preseason, play-in and playoffs excluded).
    Days come from ESPN's season calendar (carried inside the scoreboard response), bounded by the regular-season type's own start/end dates.  Any failure keeps the previous
    file's values, so a flaky call never empties the dropdown."""
    get = get or get_json
    prev_rng, prev_days = (prev_doc or {}).get("regularSeason"), (prev_doc or {}).get("regularSeasonDays")
    try:
        cal = (season_info or {}).get("_calendar")
        if not cal:
            raise RuntimeError("no calendar in the scoreboard response")
        t = get(ESPN_CORE_SEASON_TYPE.format(year=season_year))
        start, end = str(t.get("startDate") or "")[:10], str(t.get("endDate") or "")[:10]
        if len(start) != 10 or len(end) != 10:
            raise RuntimeError("regular-season dates missing")
        days = sorted({d for d in cal if start <= d <= end})
        if not days:
            raise RuntimeError("no regular-season days in the calendar")
        return {"start": start, "end": end}, days
    except Exception as exc:
        _log(f"  regular-season days: kept previous ({exc})")
        return (prev_rng if isinstance(prev_rng, dict) else None), (prev_days if isinstance(prev_days, list) else None)


# ── build ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def build_schedule(prev_doc: dict, days: int = DEFAULT_DAYS, now: datetime | None = None, get=None, sleep=time.sleep,
                   core_odds: bool = True) -> dict | None:
    """Fetch yesterday..today+days (ET) and merge with the previous file.  -> the payload WITHOUT generated_at, or None when nothing
    trustworthy could be fetched (every day failed, or ESPN returned nothing while the previous file had upcoming games)."""
    now = now or datetime.now(timezone.utc)
    get = get or get_json
    today_et = now.astimezone(ET).date()
    wanted = [(today_et + timedelta(days=o)).strftime("%Y%m%d") for o in range(-1, days + 1)]
    games: list[dict] = []
    failed: set = set()
    season_info: dict = {}
    n_events = 0
    for i, day in enumerate(wanted):
        try:
            day_games, season = fetch_day(day, get=get, now=now)
            have = {x["id"] for x in games}
            games.extend(g for g in day_games if g["id"] not in have)
            n_events += len(day_games)
            season_info = season or season_info
        except Exception as exc:
            failed.add(day)
            _log(f"  {day}: FAILED ({exc})")
        if i + 1 < len(wanted):
            sleep(PAUSE_S)
    prev_games = (prev_doc or {}).get("games") or []

    def prev_upcoming_in_window() -> bool:
        for pg in prev_games:
            if isinstance(pg, dict) and pg.get("state") == "pre" and _prev_day(pg) in wanted:
                dt = iso_to_dt(pg.get("date"))
                if dt and dt > now:
                    return True
        return False

    if len(failed) == len(wanted):
        _log("every day failed -- nothing written, previous file kept")
        return None
    if n_events == 0 and not failed and prev_upcoming_in_window():
        _log("ESPN returned zero games for the whole window but the previous file has upcoming games -- treating as an outage, previous file kept")
        return None
    prev_seen = (prev_doc or {}).get("generated_at")
    prev_seen_iso = None
    if prev_seen:
        try:
            prev_seen_iso = datetime.strptime(prev_seen, GENERATED_FMT).strftime(ISO_FMT)
        except ValueError:
            prev_seen_iso = None
    carried = carry_failed_days(games, prev_doc, failed, prev_seen_iso)
    odds_carried = carry_odds(games, prev_doc, now)
    core_gained = core_odds_fallback(games, now, get=get, sleep=sleep) if core_odds else 0
    games.sort(key=lambda g: (g.get("date") or "", g.get("id")))
    sy = season_info.get("year") if isinstance(season_info.get("year"), int) else None
    if sy is None:                                         # ESPN names the season by the year it ENDS (2026-27 -> 2027)
        sy = today_et.year + 1 if today_et.month >= 9 else today_et.year
    label = season_info.get("displayName") or f"{sy - 1}-{str(sy)[2:]}"
    with_line = sum(1 for g in games if any(g.get(k) is not None for k in ODDS_VALUE_FIELDS))
    _log(f"{len(games)} games ({sum(1 for g in games if g['preseason'])} preseason, {with_line} with a line, "
         f"{len(carried)} carried, {len(odds_carried)} lines carried, {core_gained} core-odds fills); "
         f"{len(wanted) - len(failed)}/{len(wanted)} days ok")
    reg_range, reg_days = regular_season_days(season_info, sy, get=get, prev_doc=prev_doc)
    out_extra = {}
    if reg_days:
        out_extra = {"regularSeason": reg_range, "regularSeasonDays": reg_days}
    return {
        "season": label,
        "seasonYear": sy,
        "source": SOURCE,
        "window": {"from": wanted[0], "to": wanted[-1], "tz": "America/New_York"},
        **out_extra,
        "meta": {"days_ok": len(wanted) - len(failed), "days_failed": sorted(failed), "events": n_events, "games": len(games),
                 "with_line": with_line, "carried": carried, "odds_carried": odds_carried, "core_odds_fills": core_gained},
        "games": games,
    }


# ── writing ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def write_schedule(path: Path, payload: dict, now: datetime | None = None) -> None:
    """Atomic write: temp file in the same directory, then os.replace.  generated_at is the FIRST key."""
    now = now or datetime.now(timezone.utc)
    doc = {"generated_at": now.strftime(GENERATED_FMT), **{k: v for k, v in payload.items() if k != "generated_at"}}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(doc, indent=2))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    _log(f"wrote {path}")


def git_push(paths: list[str], message: str) -> None:
    subprocess.run(["git", "add", *paths], cwd=ROOT, check=True)
    r = subprocess.run(["git", "commit", "-m", message], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        _log(f"  nothing to commit ({r.stdout.strip()[:120]})")
        return
    from _scraper_health import park_health
    restore = park_health()      # a dirty docs/scraper_health.json would make every `git pull --rebase` below refuse to run
    try:
        for attempt in range(5):
            subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], cwd=ROOT, capture_output=True)
            push = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT, capture_output=True, text=True)
            if push.returncode == 0:
                _log("  pushed")
                return
            _log(f"  push attempt {attempt + 1}/5 failed, retrying: {push.stderr.strip()[:160]}")
            time.sleep(3 + attempt * 2)
        raise RuntimeError("git push failed after 5 retries")
    finally:
        restore()


def run(out: Path, days: int, dry_run: bool = False, health: bool = True, core_odds: bool = True, now: datetime | None = None,
        get=None, sleep=time.sleep) -> int:
    """-> process exit code: 0 written (or dry run), 1 nothing trustworthy fetched (previous file untouched)."""
    now = now or datetime.now(timezone.utc)
    t0 = time.time()
    prev = load_previous(out)
    payload = build_schedule(prev, days=days, now=now, get=get, sleep=sleep, core_odds=core_odds)
    if payload is None:
        return 1
    if dry_run:
        _log(f"dry run -- not writing {out} ({time.time() - t0:.1f}s)")
        print(json.dumps({"generated_at": now.strftime(GENERATED_FMT), **{k: v for k, v in payload.items() if k != "games"},
                          "games": payload["games"][:3], "games_total": len(payload["games"])}, indent=2))
        return 0
    write_schedule(out, payload, now=now)
    if health:                                            # Engine Health scrape log (docs/scraper_health.json); best effort
        try:
            import _scraper_health
            gs = payload["games"]
            _scraper_health.log_scrape(HEALTH_LEAGUE, sum(1 for g in gs if g["state"] != "post"), sum(1 for g in gs if g["state"] == "post"))
        except Exception as exc:
            _log(f"  scraper-health log skipped: {exc}")
    _log(f"done in {time.time() - t0:.1f}s")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(SCHEDULE_OUT), help="output file (default docs/nba_schedule.json)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="days after today (ET) to fetch; yesterday is always included")
    ap.add_argument("--dry-run", action="store_true", help="fetch and print a summary, write nothing")
    ap.add_argument("--push", action="store_true", help="commit + push docs/nba_schedule.json (and the scraper-health log) to main")
    ap.add_argument("--no-health", action="store_true", help="do not append to docs/scraper_health.json")
    ap.add_argument("--no-core-odds", action="store_true", help="skip the per-game ESPN core-odds fallback")
    a = ap.parse_args(argv)
    out = Path(a.out)
    default_out = out.resolve() == SCHEDULE_OUT.resolve()
    rc = run(out, a.days, dry_run=a.dry_run, health=default_out and not a.no_health, core_odds=not a.no_core_odds)
    if rc == 0 and a.push and not a.dry_run:
        if not default_out:
            _log("--push ignored: --out is not docs/nba_schedule.json")
            return rc
        git_push(["docs/nba_schedule.json"], "chore: refresh NBA schedule/lines")
        try:
            from _scraper_health import commit_and_push as commit_and_push_health
            commit_and_push_health("chore: scraper health log (NBA)")
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
