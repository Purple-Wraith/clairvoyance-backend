"""StatMuse insights scraper.

StatMuse (statmuse.com) has no public API — every page is a client-side
rendered Astro app with hashed/utility CSS classes (no stable "card" class
names to hook a DOM scraper on), so this reads the page's rendered
*visible text* via Playwright and splits it into blurb blocks on StatMuse's
own consistent blank-line formatting, rather than depending on markup that
will break on their next redesign. This means results are more like the
existing injury/news "headline + summary" cards than a clean structured
stat table — StatMuse's own content is narrative fun-facts, not a raw
numbers feed. Treat the blurbs themselves as an insights/context panel,
not model input.

A minority of blurbs do contain a usable numeric rate-stat (e.g. "23.5
PPG · 5.8 APG"), which extract_player_stats() pulls out separately into
a player-name-keyed lookup. docs/app.html uses that (only that — never
the raw blurb text) as a small, clamped secondary cross-check against
the MC sims' own player averages, never a primary input.

Output: docs/statmuse_data.json — "sections" (one key per page, each a
list of {headline, detail} blurbs) plus "player_stats" (lowercased player
name -> {STAT_ABBR: value}).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "statmuse_data.json"

SECTIONS = {
    "nfl": "https://www.statmuse.com/nfl",
    "cfb": "https://www.statmuse.com/cfb",
    "nhl": "https://www.statmuse.com/nhl",
    "nba": "https://www.statmuse.com/nba",
    "soccer": "https://www.statmuse.com/fc",
    "news": "https://www.statmuse.com/news",
    "scores": "https://www.statmuse.com/scores",
}
# mlb/wnba sections removed 2026-09-08 -- both sports retired from the
# engine entirely, and no docs/app.html call site has passed either
# section name to _statmuseGameInsightsHTML() since (grep confirmed
# zero remaining callers), so scraping them was pure wasted effort.

# Nav/chrome lines that show up on every page (top nav, footer) — stripped
# out so only real blurb content survives. Matched as exact, case-sensitive
# lines after strip().
CHROME_LINES = {
    "Sign in", "Home", "CFB", "MLB", "WNBA", "FC", "NBA", "NHL", "NFL",
    "PGA", "Money", "Scores", "News", "Trending", "Trending Sports",
    "Trending Money", "Trending Live", "Examples", "Data & Glossary",
    "Gallery", "About", "Blog", "Shop", "Toggle Theme", "No games today",
    "More Scores",
}


def _log(msg: str) -> None:
    print(f"[statmuse] {msg}", flush=True)


import re

_STAT_LINE = re.compile(r"^[\d.,%+-]")  # "67.8 CMP%", "3,667 YDS", "-- Leo Messi"


def _is_headline(line: str) -> bool:
    """A new blurb starts at a real sentence — StatMuse's own card
    boundaries don't survive as blank lines in Playwright's flattened
    innerText (each card is a separate DOM node with no rendered gap), so
    this infers block starts from content shape instead: a headline reads
    as prose (ends in '.' or ':' , reasonably long, doesn't start with a
    number/stat token) while everything else is a stat fragment that
    belongs to the previous headline."""
    if len(line) < 20:
        return False
    if _STAT_LINE.match(line):
        return False
    return line.endswith((".", ":", "?", ")"))


def _blocks_from_text(text: str) -> list[dict]:
    """Split rendered body text into blurb blocks, dropping nav chrome.
    Each block's first line is the headline, subsequent stat-fragment
    lines are joined into the detail."""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip() and ln.strip() not in CHROME_LINES]

    # Each sport page opens with a live-scores ticker widget (team codes,
    # moneylines, times) before the first real narrative blurb — those
    # lines don't match _is_headline, so they'd otherwise get silently
    # swept into a garbage leading "blurb". Drop everything until the
    # first real headline shows up.
    blocks: list[list[str]] = []
    current: list[str] = []
    started = False
    for ln in lines:
        if _is_headline(ln):
            started = True
            if current:
                blocks.append(current)
            current = [ln]
        elif started:
            current.append(ln)
    if current:
        blocks.append(current)

    out = []
    for b in blocks:
        if not b:
            continue
        headline = b[0]
        detail = " · ".join(b[1:]).strip(" ·")
        if len(headline) < 15 and not detail:
            continue
        out.append({"headline": headline, "detail": detail})
    return out


_DATE_LINE = re.compile(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}$")  # "Aug 3, 2026"
_LEAGUE_HDR = {"CFB", "MLB", "WNBA", "FC", "NBA", "NHL", "NFL", "PGA", "Today", "All"}
_ML_LINE = re.compile(r"^[+-]\d{2,4}$")
_TIME_LINE = re.compile(r"^\d{1,2}:\d{2} [AP]M$")


def _parse_news(text: str) -> list[dict]:
    """News headlines are followed one line later by a bare date ("Aug 3,
    2026") rather than ending in sentence punctuation, so this pairs each
    headline with its date instead of reusing the prose heuristic."""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip() and ln.strip() not in CHROME_LINES]
    out = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln in _LEAGUE_HDR or _DATE_LINE.match(ln):
            i += 1
            continue
        date = lines[i + 1] if i + 1 < len(lines) and _DATE_LINE.match(lines[i + 1]) else ""
        if len(ln) >= 15:
            out.append({"headline": ln, "detail": date})
        i += 2 if date else 1
    return out


def _parse_scores(text: str) -> list[dict]:
    """Scoreboard rows render as repeating 5-line groups (away, home,
    away ML, home ML, time) under a league header line."""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip() and ln.strip() not in CHROME_LINES]
    out = []
    league = ""
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln in _LEAGUE_HDR:
            if ln not in ("Today", "All"):
                league = ln
            i += 1
            continue
        if (i + 4 < len(lines) and _ML_LINE.match(lines[i + 2]) and _ML_LINE.match(lines[i + 3])
                and _TIME_LINE.match(lines[i + 4])):
            away, home, aml, hml, tm = lines[i:i + 5]
            out.append({"headline": f"{league}: {away} @ {home}".strip(": "),
                        "detail": f"{away} {aml} · {home} {hml} · {tm}"})
            i += 5
        else:
            i += 1
    return out


def fetch_section(page, name: str, url: str) -> list[dict]:
    _log(f"loading {name} ({url}) …")
    try:
        page.goto(url, wait_until="load", timeout=45000)
        page.wait_for_timeout(3000)
        text = page.inner_text("body")
    except Exception as e:
        _log(f"  FAILED: {e}")
        return []
    if name == "news":
        blocks = _parse_news(text)
    elif name == "scores":
        blocks = _parse_scores(text)
    else:
        blocks = _blocks_from_text(text)
    _log(f"  {len(blocks)} blurbs")
    return blocks


_PLAYER_NAME = re.compile(r"^[A-Z][a-zA-Z'.\-]+ [A-Z][a-zA-Z'.\-]+(?: Jr\.?| Sr\.?| III| II)?")
# Recognized rate-stat tokens — deliberately narrow (season/recent-form
# per-game or per-career rate numbers only), not every number StatMuse
# prints (win counts, draft position, jersey numbers, etc. would be noise
# or actively misleading if picked up here).
_STAT_TOKEN = re.compile(
    r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(PPG|RPG|APG|3PM|BPG|SPG|YPG|CMP%|PPG|"
    r"PTS|REB|AST|YDS|TD|SO|K|REC|RBI|HR|AVG|ERA|K/9|MPG)\b",
    re.IGNORECASE,
)


def extract_player_stats(sections: dict[str, list[dict]]) -> dict[str, dict[str, float]]:
    """Pull numeric rate-stats out of blurbs where they exist, keyed by
    lowercased player name. Most blurbs are pure trivia with no usable
    number for a specific stat ("8th QB in NFL history to win OROY") — this
    only captures the minority that have one, and is meant as a light
    secondary cross-check against the MC sims' own player averages
    (docs/app.html's _statmuseFormNudge), not a data source of its own."""
    out: dict[str, dict[str, float]] = {}
    for blurbs in sections.values():
        for b in blurbs:
            headline = b.get("headline", "")
            m = _PLAYER_NAME.match(headline)
            if not m:
                continue
            name = m.group(0).strip().lower()
            detail = b.get("detail", "")
            for val, unit in _STAT_TOKEN.findall(detail):
                try:
                    num = float(val.replace(",", ""))
                except ValueError:
                    continue
                out.setdefault(name, {})[unit.upper()] = num
    return out


def run() -> dict:
    from playwright.sync_api import sync_playwright

    result: dict[str, list[dict]] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        for name, url in SECTIONS.items():
            result[name] = fetch_section(page, name, url)
            time.sleep(1)
        browser.close()

    player_stats = extract_player_stats(result)
    _log(f"extracted numeric stats for {len(player_stats)} players")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "sections": result,
        "player_stats": player_stats,
    }, indent=2))
    _log(f"wrote {OUT_PATH}")
    return result


if __name__ == "__main__":
    run()
