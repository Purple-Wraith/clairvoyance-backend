"""Swiss National League (NLA) schedule/standings/results scraper.

New league, explicit request 2026-09-22 (same session as the Czech
Extraliga build-out, immediately following the LIIGA/SHL + QuantHockey
work). This is a direct port of fetch_liiga.py/fetch_shl.py -- confirmed
live that Flashscore's Switzerland "National League" pages use the
identical table markup (same .table__cell--value/--over/--under/--score/
--points classes, same /team/{slug}/{id}/ href convention, 14 real teams
parsed on the first check) -- see fetch_liiga.py's own docstring for the
full design rationale (team-ID-as-key instead of a hand-typed
abbreviation table, no per-game odds yet, G/M as a real per-team
scoring-environment signal separate from GF/GA).

URLs confirmed live via the interactive browser (Flashscore's own site
search, not user-supplied this time):
  - fixtures:            /hockey/switzerland/national-league/fixtures/
  - results:             /hockey/switzerland/national-league/results/
  - standings (current): /hockey/switzerland/national-league/standings/UmJLocZR/standings/overall/
  - standings (25-26):   /hockey/switzerland/national-league-2025-2026/standings/YwJVrFRr/standings/overall/
  - over/under (both seasons, 5.5 line -- Flashscore's own default)

Kept as its own file rather than parameterizing fetch_liiga/fetch_shl --
same reasoning as fetch_shl.py's docstring: these are genuinely separate
leagues with their own schedules/team sets/output files, and a shared
"hockey scraper" abstraction would be premature generalization.
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
OUT = ROOT / "docs" / "nla_schedule.json"

BASE = "https://www.flashscore.com/hockey/switzerland"
CURRENT_STANDINGS_URL = f"{BASE}/national-league/standings/UmJLocZR/standings/overall/"
PRIOR_STANDINGS_URL = f"{BASE}/national-league-2025-2026/standings/YwJVrFRr/standings/overall/"
FIXTURES_URL = f"{BASE}/national-league/fixtures/"
RESULTS_URL = f"{BASE}/national-league/results/"
CURRENT_OU_URL = f"{BASE}/national-league/standings/UmJLocZR/over_under/overall/5.5/"
PRIOR_OU_URL = f"{BASE}/national-league-2025-2026/standings/YwJVrFRr/over_under/overall/5.5/"

TEAM_ID_RE = re.compile(r"/team/([a-z0-9-]+)/([A-Za-z0-9]+)/?")
MATCH_HREF_RE = re.compile(
    r"/match/hockey/([a-z0-9-]+)-([A-Za-z0-9]{6,})/([a-z0-9-]+)-([A-Za-z0-9]{6,})/\?mid=([A-Za-z0-9]+)"
)


def log(msg: str) -> None:
    print(f"[fetch_nla] {msg}", file=sys.stderr)


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
    """See fetch_liiga.py's identical function for the full rationale."""
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
    return " ".join(w.upper() if w in ("hc",) else w.capitalize() for w in slug.split("-"))


def _date_txt_to_iso(date_txt: str, season_start_year: int) -> str | None:
    """NLA's season also spans two calendar years (Sep-Apr, same as
    Liiga/SHL) -- July-Dec dates belong to season_start_year, Jan-Jun to
    season_start_year+1."""
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
        context = browser.new_context(timezone_id="UTC")
        page = context.new_page()

        current_teams = fetch_standings(page, CURRENT_STANDINGS_URL)
        prior_teams = fetch_standings(page, PRIOR_STANDINGS_URL)
        current_gm = fetch_gm_rates(page, CURRENT_OU_URL)
        prior_gm = fetch_gm_rates(page, PRIOR_OU_URL)

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
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2))
    log(f"Wrote {OUT} -- {len(teams)} teams, {len(games)} games")
    return out


def git_push(paths: list[str], message: str) -> None:
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
        git_push(["docs/nla_schedule.json"], "chore: refresh NLA schedule/standings")
