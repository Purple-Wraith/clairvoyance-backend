"""QuantHockey team stats scraper -- Liiga and SHL.

New source, explicit request 2026-09-22 (user supplied both real URLs
directly: quanthockey.com/liiga/en/seasons/2026-27/ and .../shl/en/...).
Fills a real, verified gap Flashscore (fetch_liiga.py/fetch_shl.py)
does not cover for either league: shots (SOG/shots-per-game/shooting%),
team-level SAVE PERCENTAGE, and full special-teams data (power-play
opportunities/goals/%, penalty-kill opportunities/goals-allowed/%).
Confirmed live by reading the real rendered table for both leagues --
identical 29-column structure, just a different URL slug/team set.

Does NOT replace Flashscore -- this has no schedule, no fixtures, no
GF/GA, no standings points. It is purely an additive team-stats source,
consumed alongside fetch_liiga.py/fetch_shl.py's own output, not instead
of it.

QuantHockey's team-stats page is a JS-rendered SPA (confirmed live: a
plain requests.get() returns 0 bytes of real team-name markup), same
reason fetch_liiga.py/fetch_shl.py already need Playwright rather than
requests.

Table structure (docs/quanthockey.com structure, confirmed live via a
real browser session against both leagues' 2026-27 pages):
  - <table id="statistics"> with 2 header rows (group headers, then the
    real per-column abbreviations) followed by one <tr> per team.
  - Column order (both leagues, identical):
    Rk, Team, Regular-Season-link, Playoffs, ATT., AA, WAA, GP, G, A, P,
    PIM, G/GP, A/GP, A/G, P/GP, PIM/GP, SOG, S/GP, SH%, SV, SV/GP, SV%,
    PPO, PPG, PP%, PKO, PPG-A, PK%.
  - G/A/P here are TEAM totals (goals for/team assists/combined) despite
    sitting under a "Player Stats" group header -- confirmed by cross-
    checking G/GP against G/GP (e.g. G=11, GP=2 -> G/GP=5.500, matches
    the table's own per-game column exactly) -- this is QuantHockey
    reusing a player-stats table template for team rows, not a real
    player-level breakdown; there is no player-specific data ingested
    here at all, consistent with this whole expansion's team-level-only
    scope.
  - Team name's own <a href="../../teams/<slug>-players-...html"> gives
    a stable QuantHockey slug (e.g. "brynas-if") -- stored alongside the
    display name for later identity resolution against Flashscore's own
    team keys (a separate, not-yet-built reconciliation step -- this
    script only captures QuantHockey's own identity, it does not
    attempt to resolve it against _LIIGA_DATA/_SHL_DATA's keys).

Usage:
  python3 scripts/fetch_quanthockey.py --league liiga [--push]
  python3 scripts/fetch_quanthockey.py --league shl [--push]
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

ROOT = Path(__file__).resolve().parent.parent

SEASON = "2026-27"
LEAGUE_URLS = {
    "liiga": f"https://www.quanthockey.com/liiga/en/seasons/{SEASON}/",
    "shl": f"https://www.quanthockey.com/shl/en/seasons/{SEASON}/",
}

# Exact column order confirmed live against both leagues' real rendered
# tables -- see module docstring. Index into each data row's cell list.
COLUMNS = [
    "rk", "team", "_regular_season_link", "_playoffs", "_att",
    "age_avg", "age_weighted_avg", "gp", "g", "a", "p", "pim",
    "g_per_gp", "a_per_gp", "a_per_g", "p_per_gp", "pim_per_gp",
    "sog", "shots_per_gp", "sh_pct",
    "sv", "sv_per_gp", "sv_pct",
    "ppo", "ppg", "pp_pct",
    "pko", "ppg_allowed", "pk_pct",
]
TEAM_SLUG_RE = re.compile(r"/teams/([a-z0-9-]+)-players-")


def log(msg: str) -> None:
    print(f"[fetch_quanthockey] {msg}", file=sys.stderr)


def _num(s: str) -> float | int | None:
    """Parses a cell's text into a real number, stripping a trailing %
    and any thousands separators. Empty/non-numeric cells (Playoffs,
    ATT. on a team with no listed attendance, etc.) become None -- never
    a fabricated 0, matching this whole expansion's anti-fabrication
    convention."""
    s = (s or "").strip().replace(",", "").rstrip("%")
    if not s or s == "-":
        return None
    try:
        return int(s) if re.fullmatch(r"-?\d+", s) else float(s)
    except ValueError:
        return None


def fetch_league(page, league: str) -> dict:
    url = LEAGUE_URLS[league]
    log(f"loading {url}")
    page.goto(url, wait_until="load", timeout=45000)
    page.wait_for_selector("#statistics tr", timeout=20000)

    rows = page.eval_on_selector_all(
        "#statistics tr",
        """rows => rows.map(r => ({
            cls: r.className,
            cells: [...r.querySelectorAll('th,td')].map(c => c.textContent.trim()),
            teamHref: r.querySelector('a[href*="teams/"]')?.getAttribute('href') || null,
        }))""",
    )
    # Data rows are every <tr> after the two header rows (group header +
    # column-abbreviation header) -- confirmed live: exactly len(teams)
    # rows follow, classed alternately odd/even/even_lr, never green/orange
    # (the two header rows' own classes).
    teams: dict[str, dict] = {}
    for row in rows:
        if row["cls"] in ("green", "orange") or not row["teamHref"]:
            continue
        cells = row["cells"]
        if len(cells) != len(COLUMNS):
            log(f"  skipping row with {len(cells)} cells (expected {len(COLUMNS)}): {cells[:2]}")
            continue
        rec = dict(zip(COLUMNS, cells))
        m = TEAM_SLUG_RE.search(row["teamHref"])
        slug = m.group(1) if m else rec["team"].lower().replace(" ", "-")
        teams[slug] = {
            "name": rec["team"],
            "slug": slug,
            "gp": _num(rec["gp"]),
            "g": _num(rec["g"]), "a": _num(rec["a"]), "p": _num(rec["p"]),
            "pim": _num(rec["pim"]),
            "gPerGp": _num(rec["g_per_gp"]), "aPerGp": _num(rec["a_per_gp"]),
            "pimPerGp": _num(rec["pim_per_gp"]),
            "sog": _num(rec["sog"]), "shotsPerGp": _num(rec["shots_per_gp"]),
            "shPct": _num(rec["sh_pct"]),
            "sv": _num(rec["sv"]), "svPerGp": _num(rec["sv_per_gp"]),
            "svPct": _num(rec["sv_pct"]),
            "ppo": _num(rec["ppo"]), "ppg": _num(rec["ppg"]), "ppPct": _num(rec["pp_pct"]),
            "pko": _num(rec["pko"]), "ppgAllowed": _num(rec["ppg_allowed"]),
            "pkPct": _num(rec["pk_pct"]),
        }

    if not teams:
        raise RuntimeError(f"parsed 0 teams for {league} -- QuantHockey markup may have changed")

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "league": league,
        "season": SEASON,
        "source": "quanthockey.com",
        "source_url": url,
        "teams": teams,
    }


def run(league: str) -> dict:
    out_path = ROOT / "docs" / f"{league}_quanthockey.json"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(timezone_id="UTC")
        page = context.new_page()
        data = fetch_league(page, league)
        browser.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    log(f"wrote {out_path} -- {len(data['teams'])} teams")
    return data


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
    ap.add_argument("--league", choices=sorted(LEAGUE_URLS.keys()), required=True)
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    result = run(args.league)

    if args.push:
        git_push([f"docs/{args.league}_quanthockey.json"],
                  f"chore: refresh {args.league.upper()} QuantHockey team stats")
