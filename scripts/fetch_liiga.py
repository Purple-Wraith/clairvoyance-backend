"""Finnish Liiga schedule/standings/results scraper.

New league, explicit request 2026-09-16. Liiga has no ESPN coverage (the
same API every other sport in this repo relies on returns a plain 400
for hockey/fin.1 -- confirmed live) so this uses Flashscore instead, the
sources the user supplied directly:
  - fixtures:            /hockey/finland/liiga/fixtures/
  - results:             /hockey/finland/liiga/results/
  - standings (current): /hockey/finland/liiga/standings/{id}/standings/overall/
  - standings (25-26):   /hockey/finland/liiga-2025-2026/standings/

Flashscore is a JS-rendered SPA -- a plain request gets an empty shell,
confirmed live (0 bytes of real match markup in a raw fetch). Needs a
real browser, same reason fetch_cfb.py's rankings fetch already runs
through Playwright rather than requests.

Team identity: Flashscore assigns every team a short stable ID baked
into every URL (e.g. Jukurit = vmuCbWr3, in both /team/jukurit/vmuCbWr3/
and every match URL that team appears in). Using that ID as this
script's own team key -- not a hand-typed abbreviation -- means team
identity can never silently drift the way a hand-maintained mapping
table could (the exact bug class multiple sports in this app have hit
before). Display names are carried alongside for anything user-facing.

No per-game market odds yet: Flashscore only exposes those on each
individual match's own /odds/ sub-page (confirmed live: the league-wide
/odds/ page the user linked is actually season-long championship-winner
futures, not per-game lines) -- fetching real per-game odds for the full
multi-week schedule already listed would mean one extra page load per
future fixture. Deferred; the model computes its own price the same way
CFB already does when no market line has posted yet.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "liiga_schedule.json"

BASE = "https://www.flashscore.com/hockey/finland"
CURRENT_STANDINGS_URL = f"{BASE}/liiga/standings/C8KZXayI/standings/overall/"
PRIOR_STANDINGS_URL = f"{BASE}/liiga-2025-2026/standings/"
FIXTURES_URL = f"{BASE}/liiga/fixtures/"
RESULTS_URL = f"{BASE}/liiga/results/"
# G/M (goals per match a team is involved in, both sides combined) is the
# same real number on every Over/Under threshold page for a given season
# -- only the O/U hit-COUNTS at that specific line change -- so one page
# per season is enough to pull it. Current-season page uses the 6.5 line
# the user supplied directly; prior-season uses 5.5 (the middle of the
# three prior-season links given, and hockey's own most standard total).
CURRENT_OU_URL = f"{BASE}/liiga/standings/C8KZXayI/over_under/overall/6.5/"
PRIOR_OU_URL = f"{BASE}/liiga-2025-2026/standings/SCI7qRwB/over_under/overall/5.5/"

TEAM_ID_RE = re.compile(r"/team/([a-z0-9-]+)/([A-Za-z0-9]+)/?")
MATCH_HREF_RE = re.compile(
    r"/match/hockey/([a-z0-9-]+)-([A-Za-z0-9]{6,})/([a-z0-9-]+)-([A-Za-z0-9]{6,})/\?mid=([A-Za-z0-9]+)"
)


def log(msg: str) -> None:
    print(f"[fetch_liiga] {msg}", file=sys.stderr)


def _parse_standings_table(page) -> dict:
    """Returns {teamId: {name, gp, w, wo, lo, l, gf, ga, pts}}."""
    out: dict = {}
    rows = page.query_selector_all("[class*='ui-table__row']")
    for row in rows:
        link = row.query_selector("a.tableCellParticipant__name") or row.query_selector(
            ".table__cell--participant a"
        )
        if not link:
            continue
        href = link.get_attribute("href") or ""
        m = TEAM_ID_RE.search(href)
        if not m:
            continue
        team_id = m.group(2)
        name = (link.inner_text() or "").strip()
        values = [v.inner_text().strip() for v in row.query_selector_all(".table__cell--value")]
        # values order: MP, W, WO(win-OT), LO(loss-OT), L, then score "gf:ga", then pts
        # table__cell--value also matches the score/points spans (they share
        # the base class with extra modifiers) -- score/points parsed
        # separately below by their own distinct modifier classes instead
        # of trusting a fixed index into `values`.
        plain_values = [
            v.inner_text().strip()
            for v in row.query_selector_all(
                ".table__cell--value:not(.table__cell--score):not(.table__cell--points)"
            )
        ]
        score_el = row.query_selector(".table__cell--score")
        pts_el = row.query_selector(".table__cell--points")
        try:
            gp, w, wo, lo, l = (int(x) for x in plain_values[:5])
        except (ValueError, IndexError):
            continue
        gf, ga = 0, 0
        if score_el:
            score_txt = score_el.inner_text().strip()
            gm = re.match(r"(\d+)\s*:\s*(\d+)", score_txt)
            if gm:
                gf, ga = int(gm.group(1)), int(gm.group(2))
        pts = int(pts_el.inner_text().strip()) if pts_el and pts_el.inner_text().strip().isdigit() else 0
        out[team_id] = {
            "name": name, "gp": gp, "w": w, "wo": wo, "lo": lo, "l": l,
            "gf": gf, "ga": ga, "pts": pts,
        }
    return out


def fetch_gm_rates(page, url: str) -> dict:
    """Returns {teamId: goalsPerMatch} from an Over/Under standings page --
    same row layout as the main standings table (team link -> team ID),
    but G/M (average combined goals in a team's own games) sits in the
    last plain .table__cell--value cell, after the over/under counts and
    the raw goals-for:against score. Real per-team scoring-environment
    signal, not derivable from GF/GA alone (a team can have modest GF/GA
    rates individually but still play in unusually high- or low-scoring
    games depending on its opponents' own scoring)."""
    log(f"Over/Under (G/M source): {url}")
    page.goto(url, wait_until="networkidle", timeout=30000)
    try:
        page.wait_for_selector(".table__cell--value", timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(1000)
    out: dict = {}
    rows = page.query_selector_all("[class*='ui-table__row']")
    for row in rows:
        link = row.query_selector(".table__cell--participant a")
        if not link:
            continue
        m = TEAM_ID_RE.search(link.get_attribute("href") or "")
        if not m:
            continue
        team_id = m.group(2)
        # MP, O, U are all `.table__cell--value` too (with their own extra
        # modifier classes) -- excluding those plus the score cell leaves
        # exactly [MP, G/M] in DOM order.
        plain = row.query_selector_all(
            ".table__cell--value:not(.table__cell--over):not(.table__cell--under):not(.table__cell--score)"
        )
        if len(plain) < 2:
            continue
        try:
            out[team_id] = float(plain[1].inner_text().strip())
        except ValueError:
            continue
    log(f"  {len(out)} teams' G/M parsed")
    return out


def fetch_standings(page, url: str) -> dict:
    log(f"Standings: {url}")
    page.goto(url, wait_until="networkidle", timeout=30000)
    # Real readiness condition (the table's own cells actually existing)
    # instead of a guessed fixed delay -- confirmed live that a flat 1500ms
    # sometimes wasn't enough for this specific page's client-side hydration
    # even after 'networkidle' fired (0 teams parsed on an otherwise-
    # identical page/selector combo that worked fine with a longer wait).
    try:
        page.wait_for_selector(".table__cell--value", timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(1000)
    teams = _parse_standings_table(page)
    log(f"  {len(teams)} teams parsed")
    return teams


def _extract_match_row(row) -> dict | None:
    """CRITICAL FIX, 2026-09-22: Flashscore's match URL slug order does
    NOT reliably encode home-first/away-second -- confirmed live on a
    real SHL result where the href was
    '/match/hockey/brynas-MZgKvenk/linkoping-8njZmM61/' but Linkoping was
    the real home team (VENUE: Saab Arena, Linkoping's own rink; page
    title 'Linkoping v Brynas') and Brynas the away team. The prior
    version of this function trusted URL slug order for home/away
    identity (a real desync bug had already been found and fixed for
    display NAMES specifically, but the home/away IDENTITY itself was
    never questioned) -- and since settlement grades a locked pick by
    comparing its team name against homeScore/awayScore
    (_autoSettleFlashscoreHockey in docs/app.html), a swapped home/away
    identity silently flips which score belongs to which team, which can
    grade a real win as a loss or vice versa. Fixed by reading the row's
    own explicit, unambiguous DOM markers instead of ever trusting URL or
    positional order: event__homeParticipant/event__awayParticipant for
    team identity, event__score--home/event__score--away for score.
    Confirmed live these markers exist on both fixture and result rows.
    """
    link = row.query_selector("a.eventRowLink")
    if not link:
        return None
    href = link.get_attribute("href") or ""
    m = MATCH_HREF_RE.search(href)
    if not m:
        return None
    slug_a, id_a, slug_b, id_b, match_id = m.groups()
    home_name_el = row.query_selector(".event__homeParticipant .wcl-name_jjfMf") or row.query_selector(".event__homeParticipant")
    away_name_el = row.query_selector(".event__awayParticipant .wcl-name_jjfMf") or row.query_selector(".event__awayParticipant")
    home_name_txt = (home_name_el.inner_text() or "").strip() if home_name_el else ""

    def _norm_key(s: str) -> str:
        s = unicodedata.normalize("NFKD", s or "")
        s = "".join(c for c in s if not unicodedata.combining(c))
        return re.sub(r"[^a-z0-9]", "", s.lower())

    def _matches(slug: str, name_txt: str) -> bool:
        ns, nn = _norm_key(slug), _norm_key(name_txt)
        return bool(ns and nn and (ns == nn or ns in nn or nn in ns))

    if home_name_txt and _matches(slug_a, home_name_txt):
        home_slug, home_id, away_slug, away_id = slug_a, id_a, slug_b, id_b
    elif home_name_txt and _matches(slug_b, home_name_txt):
        home_slug, home_id, away_slug, away_id = slug_b, id_b, slug_a, id_a
    else:
        # Couldn't confidently match the DOM's home-team text against
        # either URL slug (unexpected markup change) -- fall back to the
        # old URL-order assumption rather than dropping the game. This is
        # a degraded path that should never normally trigger.
        home_slug, home_id, away_slug, away_id = slug_a, id_a, slug_b, id_b
    date_el = row.query_selector(".wcl-dateContent_eEChT") or row.query_selector(
        "[class*='event__stageTime']"
    )
    date_txt = date_el.inner_text().strip() if date_el else ""
    home_score_el = row.query_selector(".event__score--home")
    away_score_el = row.query_selector(".event__score--away")
    scores: list[str] = []
    if home_score_el and away_score_el:
        hs, as_ = home_score_el.inner_text().strip(), away_score_el.inner_text().strip()
        if hs.isdigit() and as_.isdigit():
            scores = [hs, as_]
    return {
        "id": match_id, "home": home_id, "home_slug": home_slug,
        "away": away_id, "away_slug": away_slug, "dateTxt": date_txt,
        "scores": scores,
    }


def _slug_to_name(slug: str) -> str:
    """Fallback display name (team not found in the standings lookup --
    shouldn't happen for a team actually playing this season, but never
    silently drop a game over a cosmetic name gap)."""
    return " ".join(w.upper() if w in ("tps", "ifk", "hifk") else w.capitalize() for w in slug.split("-"))


def _date_txt_to_iso(date_txt: str, season_start_year: int) -> str | None:
    """Flashscore's fixture/result rows show 'DD.MM.' or 'DD.MM. HH:MM' with
    no year -- the browser context is pinned to UTC (see fetch_liiga's own
    context creation) so this reads as a real UTC instant once the year is
    inferred. Liiga's season spans two calendar years (Sep-Apr) -- July-Dec
    dates belong to season_start_year, Jan-Jun to season_start_year+1."""
    m = re.match(r"(\d{2})\.(\d{2})\.\s*(?:(\d{2}):(\d{2}))?", date_txt)
    if not m:
        return None
    day, mon, hh, mm = m.groups()
    year = season_start_year if int(mon) >= 7 else season_start_year + 1
    hh, mm = hh or "00", mm or "00"
    try:
        dt = datetime(year, int(mon), int(day), int(hh), int(mm), tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def fetch_fixtures(page, season_start_year: int, team_names: dict) -> list[dict]:
    log(f"Fixtures: {FIXTURES_URL}")
    page.goto(FIXTURES_URL, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1500)
    rows = page.query_selector_all("[class*='event__match']")
    games = []
    for row in rows:
        parsed = _extract_match_row(row)
        if not parsed:
            continue
        iso = _date_txt_to_iso(parsed["dateTxt"], season_start_year)
        if not iso:
            continue
        games.append({
            "id": parsed["id"], "date": iso,
            "home": parsed["home"], "homeName": team_names.get(parsed["home"], _slug_to_name(parsed["home_slug"])),
            "away": parsed["away"], "awayName": team_names.get(parsed["away"], _slug_to_name(parsed["away_slug"])),
            "state": "pre", "homeScore": None, "awayScore": None,
        })
    log(f"  {len(games)} upcoming fixtures parsed")
    return games


def fetch_results(page, season_start_year: int, team_names: dict) -> list[dict]:
    log(f"Results: {RESULTS_URL}")
    page.goto(RESULTS_URL, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1500)
    rows = page.query_selector_all("[class*='event__match']")
    games = []
    for row in rows:
        parsed = _extract_match_row(row)
        if not parsed or len(parsed["scores"]) < 2:
            continue
        iso = _date_txt_to_iso(parsed["dateTxt"], season_start_year)
        if not iso:
            continue
        games.append({
            "id": parsed["id"], "date": iso,
            "home": parsed["home"], "homeName": team_names.get(parsed["home"], _slug_to_name(parsed["home_slug"])),
            "away": parsed["away"], "awayName": team_names.get(parsed["away"], _slug_to_name(parsed["away_slug"])),
            "state": "post",
            "homeScore": int(parsed["scores"][0]), "awayScore": int(parsed["scores"][1]),
        })
    log(f"  {len(games)} final results parsed")
    return games


def run() -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        # UTC context so fixture/result date-times (no year, no explicit
        # timezone on the page) always resolve the same way regardless of
        # which machine/CI runner this executes on -- app.html's own
        # today()/lockedAt logic converts UTC into America/Denver for
        # display exactly like every other sport's ISO game date already does.
        context = browser.new_context(timezone_id="UTC")
        page = context.new_page()

        current_teams = fetch_standings(page, CURRENT_STANDINGS_URL)
        prior_teams = fetch_standings(page, PRIOR_STANDINGS_URL)
        current_gm = fetch_gm_rates(page, CURRENT_OU_URL)
        prior_gm = fetch_gm_rates(page, PRIOR_OU_URL)

        # Liiga's season starts in September -- season_start_year is simply
        # today's year if we're already past July, else last year (covers
        # running this script in the Jan-Jun back half of a season).
        now = datetime.now(timezone.utc)
        season_start_year = now.year if now.month >= 7 else now.year - 1

        team_names = {tid: t["name"] for tid, t in current_teams.items()}
        fixtures = fetch_fixtures(page, season_start_year, team_names)
        results = fetch_results(page, season_start_year, team_names)

        browser.close()

    teams = {}
    for team_id, cur in current_teams.items():
        prev = prior_teams.get(team_id)
        if prev is not None and team_id in prior_gm:
            prev = {**prev, "gm": prior_gm[team_id]}
        teams[team_id] = {**cur, "gm": current_gm.get(team_id), "prevSeason": prev}
    # Prior-season-only teams (relegated/renamed) aren't playable this
    # season -- intentionally excluded from `teams`, matching MoneyPuck's
    # own "blend degrades to prior season until the current one has real
    # rows" policy for a team that DOES exist this season, not for one
    # that no longer does.

    # Merge fixtures + results into one list, deduped by match id (a game
    # can't be in both at once, but keep the loop simple/defensive).
    by_id = {g["id"]: g for g in fixtures}
    for g in results:
        by_id[g["id"]] = g
    games = sorted(by_id.values(), key=lambda g: g["date"])

    out = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "season": f"{season_start_year}-{season_start_year+1}",
        "teams": teams,
        "games": games,
    }
    # Real incident, 2026-09-23: a transient scrape failure (Flashscore
    # slow to hydrate the fixtures page, or a similar one-off network
    # hiccup) returned 0 upcoming fixtures for both Liiga and SHL during
    # a manual test run -- with no guard, that got written straight over
    # a healthy previous file and committed, silently breaking the app's
    # day-filter dropdown ("NO SCHEDULE DATA") in production for hours
    # before a user caught it. A real end-of-regular-season gap would
    # also hit 0 fixtures, but it's far safer to keep serving yesterday's
    # real data (and fail the workflow loudly, which is what raising here
    # does -- git_push in __main__ never runs) than to silently replace a
    # working schedule with a broken one on every transient flake.
    fixtures_count = sum(1 for g in games if g.get("state") == "pre")
    if fixtures_count == 0 and OUT.exists():
        try:
            prev_games = json.loads(OUT.read_text()).get("games") or []
            prev_fixtures = sum(1 for g in prev_games if g.get("state") == "pre")
        except Exception:
            prev_fixtures = 0
        if prev_fixtures > 0:
            raise RuntimeError(
                f"Refusing to overwrite {OUT}: this run found 0 upcoming fixtures but the "
                f"existing file has {prev_fixtures} -- almost certainly a transient scrape "
                f"failure, not a real schedule gap. Keeping the existing file; will retry on "
                f"the next scheduled run."
            )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2))
    log(f"Wrote {OUT} -- {len(teams)} teams, {len(games)} games")
    return out


def git_push(paths: list[str], message: str) -> None:
    """Same push-first-then-rebase-retry pattern every other fetch script
    in this repo already uses (fetch_nhl.py/fetch_cfb.py) -- main gets
    pushed to constantly by concurrent scheduled jobs, so a bare push
    failing once is a normal race, not a real error."""
    subprocess.run(["git", "add", *paths], cwd=ROOT, check=True)
    r = subprocess.run(["git", "commit", "-m", message], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  nothing to commit ({r.stdout.strip()[:120]})")
        return
    for attempt in range(5):
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=ROOT, capture_output=True)
        push = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT, capture_output=True, text=True)
        if push.returncode == 0:
            log("  pushed")
            return
        log(f"  push attempt {attempt + 1}/5 failed, retrying: {push.stderr.strip()[:160]}")
        time.sleep(3 + attempt * 2)
    raise RuntimeError("git push failed after 5 retries")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    run()

    if args.push:
        git_push(["docs/liiga_schedule.json"], "chore: refresh Liiga schedule/standings")
