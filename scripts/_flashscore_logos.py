"""Flashscore team-logo URL scraping (URL STRINGS ONLY) for the four European hockey leagues.

The engine's game cards show each team's real logo, hotlinked at display time from the host that serves it (Flashscore's own
static CDN here, ESPN's for the ESPN-covered leagues).  This repo is PUBLIC and docs/ is served by GitHub Pages, so no image
bytes are ever fetched, stored or committed -- only the URL string per team, as teams[<flashscoreTeamId>].logo in
docs/{liiga,shl,nla,extraliga}_schedule.json.

Where the URL lives: every standings row is
    <a class="tableCellParticipant__image" href="/team/<slug>/<id>/"><img class="participant__image" src="https://static.flashscore.com/res/image/data/<hash>.png"></a>
(confirmed live 2026-10-02 on the Liiga standings page; the same participant cell markup the existing scrapers already read the
team id / name from).

FAIL-OPEN by construction: every function here swallows its own errors and returns "nothing found", so a logo-scrape problem can
never break (or even slow) the schedule scrape that calls it.  The app falls back to a letter badge for any team without a logo.
"""
from __future__ import annotations

import json
import re
import sys

TEAM_ID_RE = re.compile(r"/team/([a-z0-9-]+)/([A-Za-z0-9]+)/?")
# Only ever accept an https image on Flashscore's own static host -- anything else (data: URIs, tracking pixels, placeholders)
# is ignored, so the committed JSON can never carry an arbitrary URL that the app would later put in an <img src>.
LOGO_URL_RE = re.compile(r"^https://static\.flashscore\.com/res/image/data/[A-Za-z0-9_-]+\.(?:png|jpg|jpeg|webp|svg)$")


def log(msg: str) -> None:
    print(f"[flashscore_logos] {msg}", file=sys.stderr)


def valid_logo_url(url) -> str | None:
    if isinstance(url, str) and LOGO_URL_RE.match(url.strip()):
        return url.strip()
    return None


def row_logo_url(row) -> str | None:
    """The logo URL string in one standings row (Playwright ElementHandle), or None."""
    try:
        img = row.query_selector("img.participant__image") or row.query_selector(".tableCellParticipant__image img")
        if not img:
            return None
        # `src` is populated once the row has hydrated; `data-src` covers a lazy-loaded row that has not.
        for attr in ("src", "data-src"):
            u = valid_logo_url(img.get_attribute(attr))
            if u:
                return u
    except Exception:
        pass
    return None


def attach_logos(page, teams: dict) -> int:
    """Sets teams[id]["logo"] for every standings row on the currently loaded page whose team id is already in `teams`.
    Returns how many teams got a logo.  Never raises."""
    n = 0
    try:
        for row in page.query_selector_all("[class*='ui-table__row']"):
            try:
                link = row.query_selector("a.tableCellParticipant__name") or row.query_selector(".table__cell--participant a")
                m = TEAM_ID_RE.search((link.get_attribute("href") if link else "") or "")
                if not m or m.group(2) not in teams:
                    continue
                u = row_logo_url(row)
                if u:
                    teams[m.group(2)]["logo"] = u
                    n += 1
            except Exception:
                continue
    except Exception as exc:
        log(f"logo scrape skipped ({exc})")
    return n


def carry_over_logos(teams: dict, prev_path) -> int:
    """Keep a team's previously scraped logo URL when this run found none (transient hydration miss). Never raises."""
    try:
        prev = json.loads(prev_path.read_text()).get("teams") or {}
    except Exception:
        return 0
    n = 0
    try:
        for tid, t in teams.items():
            if not t.get("logo"):
                u = valid_logo_url((prev.get(tid) or {}).get("logo"))
                if u:
                    t["logo"] = u
                    n += 1
    except Exception:
        pass
    return n
