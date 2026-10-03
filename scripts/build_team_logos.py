#!/usr/bin/env python3
"""Build docs/team_logos.json -- a URL-ONLY mapping of team keys -> logo image URLs for the engine's in-app game cards.

IMPORTANT (public repo, GitHub Pages): this file stores URL STRINGS that point at ESPN's own CDN.  No image bytes, binaries or
data: URIs are ever fetched, stored or committed, so no trademarked artwork is redistributed from this repo -- the browser loads
each logo from ESPN at display time (docs/app.html _teamLogoHTML).  The URLs are taken verbatim from ESPN's own team payloads
(team.logos[0].href), never hand-built.

Keys (what each card already has in hand):
  nhl / nba / nfl : ESPN team abbreviation        (e.g. "BOS")
  cfb             : ESPN team displayName         (e.g. "Ohio State Buckeyes") -- abbreviations are ambiguous in college
                    football (OSU = Ohio State AND Ohio State Newark), full names are unique
  soccer          : ESPN team displayName         (e.g. "Arsenal"), merged across PL / La Liga / Serie A / Bundesliga / MLS / UCL
The four European hockey leagues are NOT here: their logo URLs are scraped from Flashscore into
docs/{liiga,shl,nla,extraliga}_schedule.json (teams[id].logo) by scripts/_flashscore_logos.py.

    python3 scripts/build_team_logos.py            # rewrite docs/team_logos.json
    python3 scripts/build_team_logos.py --check    # fetch + print coverage, write nothing

Fail-open: a league that fails to fetch keeps its previous entries (the app also uses the event payload's own team.logo when it
has the event object, so this file is a fallback layer).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "team_logos.json"
BASE = "https://site.api.espn.com/apis/site/v2/sports/"
SOCCER = ["eng.1", "esp.1", "ita.1", "ger.1", "usa.1", "UEFA.champions"]
# Only ever store a URL on ESPN's own image CDN, so the app can never be handed an arbitrary host through this file.
URL_PREFIX = "https://a.espncdn.com/i/teamlogos/"


def log(msg: str) -> None:
    print(f"[build_team_logos] {msg}", file=sys.stderr)


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (clairvoyance logo-map builder)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def teams_of(path: str, paged: bool = False) -> list[dict]:
    """All team objects ESPN lists for a league (paged when the league has more than one page, e.g. college football)."""
    out, seen, page = [], set(), 1
    while True:
        d = get_json(f"{BASE}{path}/teams?limit=200&page={page}" if paged else f"{BASE}{path}/teams?limit=500")
        ts = [t["team"] for t in d["sports"][0]["leagues"][0].get("teams", [])]
        fresh = [t for t in ts if t.get("id") not in seen]
        for t in fresh:
            seen.add(t.get("id"))
        out += fresh
        if not paged or not fresh:
            return out
        page += 1


# docs/nhl_schedule.json (the Flashscore/NHL-style schedule the Upcoming/Tonight cards read) spells five franchises differently from
# ESPN's abbreviations; both spellings resolve to the same logo.
NHL_ALIASES = {"LAK": "LA", "NJD": "NJ", "SJS": "SJ", "TBL": "TB", "UTA": "UTAH"}


def scoreboard_teams(lg: str) -> list[dict]:
    """Teams seen in a soccer league's season scoreboard (covers clubs the /teams list no longer carries: relegated, UCL
    visitors from other leagues).  Each competitor's team object carries its own `logo` string."""
    out = []
    yr = datetime.now(timezone.utc).year
    for y in (yr - 1, yr, yr + 1):
        try:
            d = get_json(f"{BASE}soccer/{lg}/scoreboard?dates={y}&limit=1000")
        except Exception:
            continue
        for ev in d.get("events", []):
            for c in (ev.get("competitions") or [{}])[0].get("competitors", []):
                t = c.get("team") or {}
                if t.get("displayName") and (t.get("logo") or "").startswith(URL_PREFIX):
                    out.append({"displayName": t["displayName"], "logos": [{"href": t["logo"], "rel": ["default"]}]})
    return out


def logo_of(team: dict) -> str | None:
    for lg in team.get("logos") or []:
        href = lg.get("href") or ""
        rel = lg.get("rel") or []
        if href.startswith(URL_PREFIX) and "default" in rel and "dark" not in rel:
            return href
    for lg in team.get("logos") or []:
        if (lg.get("href") or "").startswith(URL_PREFIX):
            return lg["href"]
    return None


def build(prev: dict) -> dict:
    out = {"_about": "URL strings only, pointing at ESPN's CDN (no images are stored in this repo). Built by scripts/build_team_logos.py; "
                     "European hockey logo URLs live in docs/*_schedule.json teams[id].logo instead.",
           "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    plan = [("nhl", "hockey/nhl", "abbreviation", False), ("nba", "basketball/nba", "abbreviation", False),
            ("nfl", "football/nfl", "abbreviation", False), ("cfb", "football/college-football", "displayName", True)]
    for key, path, field, paged in plan:
        try:
            m = {}
            for t in teams_of(path, paged):
                u = logo_of(t)
                if u and t.get(field):
                    m.setdefault(t[field], u)
            if not m:
                raise RuntimeError("0 logos")
            if key == "nhl":
                for alias, real in NHL_ALIASES.items():
                    if real in m:
                        m[alias] = m[real]
            out[key] = dict(sorted(m.items()))
            log(f"{key}: {len(m)} entries")
        except Exception as exc:
            log(f"{key}: FAILED ({exc}) -- keeping previous entries")
            out[key] = prev.get(key, {})
    soc, ok = {}, False
    for lg in SOCCER:
        try:
            n = 0
            for t in teams_of("soccer/" + lg) + scoreboard_teams(lg):
                u = logo_of(t)
                if u and t.get("displayName"):
                    soc.setdefault(t["displayName"], u)
                    n += 1
            ok = ok or n > 0
            log(f"soccer/{lg}: {n} team entries (repeats included)")
        except Exception as exc:
            log(f"soccer/{lg}: FAILED ({exc})")
    out["soccer"] = dict(sorted(soc.items())) if ok else prev.get("soccer", {})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    try:
        prev = json.loads(OUT.read_text())
    except Exception:
        prev = {}
    doc = build(prev)
    if a.check:
        return 0
    # Keep the file byte-stable when nothing but the timestamp changed (bots commit constantly -- avoid noise diffs).
    strip = lambda d: {k: v for k, v in d.items() if k != "generated_at"}
    if prev and strip(prev) == strip(doc):
        log("no change -- file untouched")
        return 0
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=False) + "\n")
    log(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
