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
import re
import subprocess
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `from _flashscore_odds import ...` works however this is launched

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

# ── Real Flashscore bookmaker prices (ML + O/U + puck line), added 2026-10-03 ────────────────────────────────
# ESPN gives ONE book's ML/O-U and no puck-line price at all. Flashscore's NHL match pages carry the same three
# ODDS tabs the European leagues already use (home-away / over-under / asian-handicap, "FT including OT" -- see
# _flashscore_odds.py), quoted by ~4-5 US books (FanDuel/bet365.us/BetMGM.us/DraftKings/Fanatics). Confirmed live
# 2026-10-02: the NHL puck line is the Asian-handicap tab's +/-1.5 rows (the US books also quote +/-2.5..5.5 alt
# lines, never +/-0.5); prices agree with ESPN's DraftKings-sourced numbers (e.g. CAR -142 vs Flashscore 1.68-1.71).
# NHL markets only post ~24-36h before puck drop (a game 2+ days out has no priced rows yet), so a pass is only
# useful for the next ~day and a half -- the workflow runs it right before each lock window (see
# daily-schedules-refresh.yml). Everything below is additive + fail-open: any Flashscore/Playwright problem leaves
# the ESPN-only schedule exactly as it was.
FLASHSCORE_NHL_FIXTURES_URL = "https://www.flashscore.com/hockey/usa/nhl/fixtures/"
# Games starting further out than this get no Flashscore page loads (nothing is posted that early; each wasted
# game costs ~11s of page timeouts). 54h comfortably covers every lock window plus the thin 2-book leading edge.
NHL_ODDS_LOOKAHEAD_HOURS = 54

# Flashscore team name -> this app's NHL abbreviation (docs/app.html NHL const keys). Static + explicit: a team
# whose name is not in this table is SKIPPED (and reported in odds_meta.unmapped), never guessed. Aliases cover
# Utah's rebrand (Utah Hockey Club -> Utah Mammoth, 2025) and the common short forms.
NHL_NAME_TO_ABBR = {
    "anaheim ducks": "ANA", "boston bruins": "BOS", "buffalo sabres": "BUF", "calgary flames": "CGY",
    "carolina hurricanes": "CAR", "chicago blackhawks": "CHI", "colorado avalanche": "COL",
    "columbus blue jackets": "CBJ", "dallas stars": "DAL", "detroit red wings": "DET", "edmonton oilers": "EDM",
    "florida panthers": "FLA", "los angeles kings": "LAK", "minnesota wild": "MIN", "montreal canadiens": "MTL",
    "nashville predators": "NSH", "new jersey devils": "NJD", "new york islanders": "NYI",
    "new york rangers": "NYR", "ottawa senators": "OTT", "philadelphia flyers": "PHI",
    "pittsburgh penguins": "PIT", "san jose sharks": "SJS", "seattle kraken": "SEA", "st louis blues": "STL",
    "saint louis blues": "STL", "tampa bay lightning": "TBL", "toronto maple leafs": "TOR",
    "utah mammoth": "UTA", "utah hockey club": "UTA", "utah": "UTA", "vancouver canucks": "VAN",
    "vegas golden knights": "VGK", "washington capitals": "WSH", "winnipeg jets": "WPG",
}
_FS_MATCH_HREF_RE = re.compile(
    r"/match/hockey/([a-z0-9-]+)-([A-Za-z0-9]{6,})/([a-z0-9-]+)-([A-Za-z0-9]{6,})/\?mid=([A-Za-z0-9]+)")


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


def _fs_norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("-", " ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", s)).strip()


def _fs_abbr(name: str) -> str | None:
    return NHL_NAME_TO_ABBR.get(_fs_norm(name))


def _fs_date_to_iso(date_txt: str, season_start_year: int) -> str | None:
    """"03.10. 23:00" (Flashscore, browser context pinned to UTC) -> "2026-10-03T23:00Z" (ESPN's own date format).
    Jul-Dec belong to the season's start year, Jan-Jun to the next (same rule as fetch_shl.py)."""
    m = re.match(r"(\d{2})\.(\d{2})\.\s*(\d{2}):(\d{2})", (date_txt or "").strip())
    if not m:
        return None  # live / finished / postponed rows show a status text instead of a kickoff time
    day, mon, hh, mm = (int(x) for x in m.groups())
    year = season_start_year if mon >= 7 else season_start_year + 1
    try:
        return datetime(year, mon, day, hh, mm, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    except ValueError:
        return None


def _fs_parse_fixtures(page, season_start_year: int) -> tuple[list[dict], set[str]]:
    """Upcoming NHL fixtures from Flashscore -> ([{mid, home, away, iso, slugs/ids}], unmapped team names).
    Home/away identity comes from the row's own .event__homeParticipant/.event__awayParticipant markers, never
    from URL slug order (Flashscore's URL order is NOT reliably home-first -- see fetch_shl._extract_match_row)."""
    page.goto(FLASHSCORE_NHL_FIXTURES_URL, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(1500)
    out, unmapped = [], set()
    for row in page.query_selector_all("[class*='event__match']"):
        link = row.query_selector("a.eventRowLink")
        m = _FS_MATCH_HREF_RE.search((link.get_attribute("href") if link else "") or "")
        if not m:
            continue
        slug_a, id_a, slug_b, id_b, mid = m.groups()
        h_el, a_el = row.query_selector(".event__homeParticipant"), row.query_selector(".event__awayParticipant")
        h_name = (h_el.inner_text() or "").strip() if h_el else ""
        a_name = (a_el.inner_text() or "").strip() if a_el else ""
        t_el = row.query_selector("[class*='event__time'], .wcl-dateContent_eEChT")
        iso = _fs_date_to_iso((t_el.inner_text() or "") if t_el else "", season_start_year)
        if not (h_name and a_name and iso):
            continue
        h_abbr, a_abbr = _fs_abbr(h_name), _fs_abbr(a_name)
        if not h_abbr or not a_abbr:
            unmapped.update(n for n, ab in ((h_name, h_abbr), (a_name, a_abbr)) if not ab)
            continue
        # Pair each slug/id with the right side by name (fetch_match_odds only needs a resolvable URL, but keep it honest)
        if _fs_norm(slug_a) == _fs_norm(h_name) or _fs_norm(slug_b) == _fs_norm(a_name):
            hs, hi, as_, ai = slug_a, id_a, slug_b, id_b
        else:
            hs, hi, as_, ai = slug_b, id_b, slug_a, id_a
        out.append({"mid": mid, "home": h_abbr, "away": a_abbr, "iso": iso,
                    "hs": hs, "hi": hi, "as": as_, "ai": ai})
    return out, unmapped


def attach_flashscore_odds(games: list[dict], now: datetime | None = None,
                           lookahead_hours: float = NHL_ODDS_LOOKAHEAD_HOURS) -> dict:
    """Scrape real bookmaker prices for upcoming NHL games and write the SAME `odds` object the European leagues use
    ({ml:{home,away,books,..}, ou:[{line,over,under,books}], pl:{"-1.5":{home,away},"+1.5":{..}}, fmt, bk, at},
    decimal) onto each matching game in `games` (matched by home+away abbreviation and start time). Reuses
    _flashscore_odds.fetch_match_odds. Fail-open end to end: returns a coverage dict (also stored as
    odds_meta in the JSON, read by Engine Health) and never raises."""
    now = now or datetime.now(timezone.utc)
    meta = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "lookahead_h": lookahead_hours, "fixtures": 0,
            "in_window": 0, "matched": 0, "priced": 0, "no_market": 0, "unmatched": 0, "unmapped": [], "error": None}
    try:
        from playwright.sync_api import sync_playwright
        from _flashscore_odds import fetch_match_odds
    except Exception as exc:  # Playwright not installed -> ESPN-only schedule, exactly as before
        meta["error"] = f"playwright unavailable: {exc}"[:200]
        _log(f"  Flashscore odds skipped: {meta['error']}")
        return meta
    try:
        season_start_year = now.year if now.month >= 7 else now.year - 1
        by_pair: dict[tuple, list[dict]] = {}
        for g in games:
            if g.get("state") == "pre" and g.get("home") and g.get("away"):
                by_pair.setdefault((g["home"], g["away"]), []).append(g)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_context(timezone_id="UTC").new_page()
                fixtures, unmapped = _fs_parse_fixtures(page, season_start_year)
                meta["fixtures"] = len(fixtures)
                meta["unmapped"] = sorted(unmapped)
                if unmapped:
                    _log(f"  WARNING unmapped Flashscore team names (skipped): {sorted(unmapped)}")
                for fx in fixtures:
                    start = datetime.strptime(fx["iso"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
                    hours_out = (start - now).total_seconds() / 3600
                    if not (0 <= hours_out <= lookahead_hours):
                        continue
                    meta["in_window"] += 1
                    cands = []
                    for g in by_pair.get((fx["home"], fx["away"]), []):
                        try:
                            gdt = datetime.strptime(g["date"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
                        except Exception:
                            continue
                        if abs((gdt - start).total_seconds()) <= 3 * 3600:
                            cands.append(g)
                    if len(cands) != 1:
                        meta["unmatched"] += 1
                        _log(f"  no unique ESPN game for Flashscore {fx['away']}@{fx['home']} {fx['iso']} (cands={len(cands)})")
                        continue
                    meta["matched"] += 1
                    try:
                        res = fetch_match_odds(page, fx["hs"], fx["hi"], fx["as"], fx["ai"], fx["mid"])
                    except Exception as exc:
                        _log(f"  odds fetch failed for {fx['mid']}: {exc}")
                        res = {"odds": None}
                    if res.get("odds"):
                        cands[0]["odds"] = res["odds"]
                        cands[0]["fsId"] = fx["mid"]
                        # Flashscore's consensus MAIN total (most bookmaker rows, then most symmetric prices) -- same
                        # `ou` field the European schedules carry. ESPN's own overUnder stays untouched as the fallback.
                        cands[0]["ou"] = res.get("ou")
                        meta["priced"] += 1
                    else:
                        meta["no_market"] += 1
            finally:
                browser.close()
    except Exception as exc:
        meta["error"] = str(exc)[:200]
        _log(f"  Flashscore odds pass aborted (fail-open, ESPN data unaffected): {exc}")
    _log(f"  Flashscore odds: {meta}")
    return meta


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
    ap.add_argument("--flashscore-odds", action="store_true",
                    help="also scrape real Flashscore ML/O-U/puck-line prices for the next ~2 days (needs Playwright)")
    args = ap.parse_args()

    sched = fetch_full_schedule()
    try:
        prev = json.loads(SCHEDULE_OUT.read_text()) if SCHEDULE_OUT.exists() else {}
    except Exception:
        prev = {}
    # Keep a game's previously scraped real prices when this run has none for it (ESPN ids are stable): the
    # per-odds `at` stamp keeps staleness visible to the app. Fail-open, additive.
    try:
        from _flashscore_odds import carry_over_odds
        _log(f"  odds carried over from previous file for {carry_over_odds(sched['games'], SCHEDULE_OUT)} games")
    except Exception as exc:
        _log(f"  odds carry-over skipped: {exc}")
    if args.flashscore_odds:
        sched["odds_meta"] = attach_flashscore_odds(sched["games"])
    elif prev.get("odds_meta"):
        sched["odds_meta"] = prev["odds_meta"]
    _write(SCHEDULE_OUT, sched)

    if args.push:
        git_push(["docs/nhl_schedule.json"], "chore: refresh NHL full-season schedule")
