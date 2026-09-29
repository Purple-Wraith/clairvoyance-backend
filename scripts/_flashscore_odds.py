"""Shared Flashscore per-match Over/Under odds scraper.

Real per-game market O/U lines exist on Flashscore's own match pages under
the ODDS tab -> Over/Under sub-tab -> "FT including OT" sub-tab (confirmed
live, 2026-09-29, via a real LIIGA match -- Karpat v SaiPa -- and a real NLA
match -- Lausanne v Servette -- same tab structure both leagues): e.g.
https://www.flashscore.com/match/hockey/{homeSlug}-{homeId}/{awaySlug}-{awayId}/odds/over-under/ft-including-ot/?mid={matchId}

"FT including OT" (not the plain "Full Time" tab, which is a separate,
thinner, regulation-only market) is used because every one of this app's
own hockey totals -- the model's own totLam, and the G/M fallback line
itself, sourced from Flashscore's own standings pages -- represents a
game's final recorded score, which in hockey always includes the OT/SO-
winning goal.

Shared across fetch_liiga.py/fetch_shl.py/fetch_nla.py/fetch_extraliga.py
instead of duplicated 4x -- same site, same page structure, same line-
selection rule for all 4 leagues (confirmed live on 2 of the 4).

Line selection: Flashscore surfaces every side/alternate total a
bookmaker quotes (half-points AND whole numbers, e.g. 3.5, 4, 4.5, 5,
5.5...), not just the one line a book actually stands behind as its main
number. Real sportsbook hockey totals are always posted at a half-point
(guarantees a winner, never a push) -- confirmed live: a same-day LIIGA
match had 4 separate bookmaker rows all independently quoting the SAME
5.5 total, while every other half-point total on that match had at most
1 row. The half-point total with the most independent bookmaker quotes
is therefore the real consensus line; ties are broken by picking whichever
candidate's average over/under prices are closest to symmetric (least vig
skew from a true 50/50 game -- the standard definition of a book's "main"
number).
"""
from __future__ import annotations

import sys

BASE = "https://www.flashscore.com"


def log(msg: str) -> None:
    print(f"[flashscore_odds] {msg}", file=sys.stderr)


def _odds_url(home_slug: str, home_id: str, away_slug: str, away_id: str, match_id: str) -> str:
    return (f"{BASE}/match/hockey/{home_slug}-{home_id}/{away_slug}-{away_id}"
            f"/odds/over-under/ft-including-ot/?mid={match_id}")


def fetch_match_ou_line(page, home_slug: str, home_id: str, away_slug: str, away_id: str,
                         match_id: str) -> float | None:
    """Real market Over/Under total for one match, or None if no market has
    posted yet (fails open -- every caller falls back to its own G/M-
    derived line on None, same `real value || fallback` convention
    nhlMC's own `TONIGHT.find(...)?.ou||5.5` already uses client-side)."""
    url = _odds_url(home_slug, home_id, away_slug, away_id, match_id)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_selector(".ui-table__row", timeout=6000)
    except Exception:
        return None  # no market posted yet, or page didn't load in time
    try:
        rows = page.eval_on_selector_all(
            ".ui-table__row",
            """rows => rows.map(r => ({
                total: r.querySelector('[data-testid="wcl-oddsValue"]')?.textContent.trim() ?? null,
                odds: [...r.querySelectorAll('a.oddsCell__odd')].map(a => a.textContent.trim()),
            }))""",
        )
    except Exception:
        return None

    # {total: [(overDec, underDec), ...]} -- one entry per bookmaker row
    # quoting that total.
    by_total: dict[float, list[tuple[float, float]]] = {}
    for row in rows:
        total_txt = row.get("total")
        odds = row.get("odds") or []
        if not total_txt or len(odds) < 2:
            continue
        try:
            total = float(total_txt)
        except ValueError:
            continue
        if total % 1 != 0.5:
            continue  # whole-number rows are side/alternate totals, never a book's real main line
        try:
            over_d, under_d = float(odds[0]), float(odds[1])
        except ValueError:
            continue
        if over_d <= 1 or under_d <= 1:
            continue  # malformed/placeholder price
        by_total.setdefault(total, []).append((over_d, under_d))

    if not by_total:
        return None

    def _vig_skew(pairs: list[tuple[float, float]]) -> float:
        over_p = sum(1 / p[0] for p in pairs) / len(pairs)
        under_p = sum(1 / p[1] for p in pairs) / len(pairs)
        return abs(over_p - under_p)

    best_total, _ = max(by_total.items(), key=lambda kv: (len(kv[1]), -_vig_skew(kv[1])))
    return best_total
