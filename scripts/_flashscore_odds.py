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

REAL PRICES (added 2026-10-02, explicit request -- these four leagues'
game cards used to price every bet with NO market data at all: ML = the
model's own probability turned into odds, O/U a flat 1.91, puck line a flat
1.87, so every logged ROI figure was simulated against assumed prices).
fetch_match_odds() now also scrapes, per match, from the same ODDS tab
(confirmed live, 2026-10-02, on a finished LIIGA match -- completed matches
KEEP their closing odds on the page, which is what the backfill relies on):
  - Moneyline:   .../odds/home-away/ft-including-ot/?mid={id}
                 rows = one per bookmaker, 2 cells [home, away]
  - Over/Under:  .../odds/over-under/ft-including-ot/?mid={id}
                 rows = one per bookmaker per line, [total] + 2 cells [over, under]
  - Puck line:   .../odds/asian-handicap/ft-including-ot/?mid={id}
                 rows = one per bookmaker per handicap, [handicap, from the
                 HOME side's point of view] + 2 cells [home price, away
                 price]. Only the +/-1.5 rows (the real puck line) are kept.
  (Flashscore resolves the match from ?mid= alone -- the team slugs in the
  path can be anything and the site redirects to the canonical URL.)
Bookmaker set depends on the visitor's region (a US session sees
Fanduel/bet365.us/DraftKings/Fanatics...); a bookmaker whose row shows "-"
for both cells has no price and is skipped.

Price format: Flashscore renders decimal by default, American (-145/+200) or
fractional if the visitor's odds-format setting says so. parse_price()
accepts all three and converts to decimal -- decimal is the canonical stored
form (the app converts to American for display). The formats actually seen
are logged (FORMATS_SEEN) so a headless-vs-owner-browser mismatch is visible.

Consensus across bookmakers = MEDIAN of each side's price (robust to one
stale/outlier book; mean would be dragged by it). The best (highest) price
quoted by any book is also stored as *Best for line-shopping sensitivity.
Pairs whose implied-probability sum is outside [0.98, 1.30] are dropped as
malformed/placeholder.
"""
from __future__ import annotations
import json
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone

BASE = "https://www.flashscore.com"

# Price formats actually seen this process ("decimal"/"american"/"fractional")
# -- logged so a headless-session vs owner-browser format mismatch is visible.
FORMATS_SEEN: Counter = Counter()

_AMERICAN_RE = re.compile(r"^[+\-−]\d{3,4}$")
_DECIMAL_RE = re.compile(r"^\d{1,3}\.\d{1,3}$")
_FRACTION_RE = re.compile(r"^(\d{1,3})/(\d{1,3})$")
MAX_DECIMAL = 200.0  # anything above is a placeholder, not a real price


def log(msg: str) -> None:
    print(f"[flashscore_odds] {msg}", file=sys.stderr)


def parse_price(txt) -> tuple[float | None, str | None]:
    """One displayed odds string -> (decimal, format) or (None, None).

    Handles decimal (1.65), American (-145 / +200, also the unicode minus
    Flashscore-style widgets sometimes use) and fractional (5/2). Anything
    nonsensical -- "-" placeholders, empty, a decimal <= 1.0, American
    magnitude < 100, absurdly large prices -- is rejected."""
    if txt is None:
        return None, None
    t = str(txt).strip().replace("−", "-")
    if not t or t in ("-", "--", "–"):
        return None, None
    if _AMERICAN_RE.match(t):
        a = int(t)
        if abs(a) < 100:
            return None, None
        dec = 1 + a / 100 if a > 0 else 1 + 100 / abs(a)
        fmt = "american"
    elif _DECIMAL_RE.match(t):
        dec = float(t)
        fmt = "decimal"
    elif _FRACTION_RE.match(t):
        n, d = (int(x) for x in _FRACTION_RE.match(t).groups())
        if d == 0:
            return None, None
        dec = 1 + n / d
        fmt = "fractional"
    else:
        return None, None
    if not (1.0 < dec <= MAX_DECIMAL):
        return None, None
    return round(dec, 4), fmt


def _pair_ok(a: float, b: float) -> bool:
    """Two-way market sanity: implied probs should sum to ~1.0-1.15 (vig);
    allow a little slack either side, reject placeholder/mismatched pairs."""
    return 0.98 <= (1 / a + 1 / b) <= 1.30


def _odds_url(home_slug: str, home_id: str, away_slug: str, away_id: str, match_id: str,
              market: str = "over-under") -> str:
    return (f"{BASE}/match/hockey/{home_slug}-{home_id}/{away_slug}-{away_id}"
            f"/odds/{market}/ft-including-ot/?mid={match_id}")


_ROW_JS = """rows => rows.map(r => ({
    total: r.querySelector('[data-testid="wcl-oddsValue"]')?.textContent.trim() ?? null,
    odds: [...r.querySelectorAll('a.oddsCell__odd')].map(a => a.textContent.trim()),
    book: r.querySelector('.oddsCell__bookmakerPart img')?.getAttribute('alt') ?? null,
}))"""


def _load_rows(page, url: str):
    """Rows of one odds sub-page -> list of {total, odds[], book}, or None
    when the page has no priced rows (no market posted yet / page didn't
    load in time -- both fail open)."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_selector(".ui-table__row .oddsCell__odd", timeout=10000)
    except Exception:
        return None
    try:
        return page.eval_on_selector_all(".ui-table__row", _ROW_JS)
    except Exception:
        return None


def _row_pair(row) -> tuple[float, float] | None:
    odds = row.get("odds") or []
    if len(odds) < 2:
        return None
    a, fa = parse_price(odds[0])
    b, fb = parse_price(odds[1])
    if a is None or b is None:
        return None
    FORMATS_SEEN[fa] += 1
    FORMATS_SEEN[fb] += 1
    return a, b


def _main_line(rows) -> float | None:
    """The existing consensus-line selection (unchanged logic, 2026-09-29):
    the half-point total with the most bookmaker rows, ties broken by the
    most symmetric over/under prices."""
    by_total: dict[float, list[tuple[float, float]]] = {}
    for row in rows:
        total_txt = row.get("total")
        if not total_txt:
            continue
        try:
            total = float(total_txt)
        except ValueError:
            continue
        if total % 1 != 0.5:
            continue  # whole-number rows are side/alternate totals, never a book's real main line
        pair = _row_pair(row)
        if pair is None:
            continue  # malformed/placeholder price
        by_total.setdefault(total, []).append(pair)
    if not by_total:
        return None

    def _vig_skew(pairs: list[tuple[float, float]]) -> float:
        over_p = sum(1 / p[0] for p in pairs) / len(pairs)
        under_p = sum(1 / p[1] for p in pairs) / len(pairs)
        return abs(over_p - under_p)

    best_total, _ = max(by_total.items(), key=lambda kv: (len(kv[1]), -_vig_skew(kv[1])))
    return best_total


def _r(x: float) -> float:
    return round(x, 3)


def _consensus(pairs: list[tuple[float, float]]) -> dict:
    a = [p[0] for p in pairs]
    b = [p[1] for p in pairs]
    return {"a": _r(statistics.median(a)), "b": _r(statistics.median(b)),
            "aBest": _r(max(a)), "bBest": _r(max(b)), "books": len(pairs)}


def build_ml(rows) -> dict | None:
    pairs = [p for p in (_row_pair(r) for r in rows) if p and _pair_ok(*p)]
    if not pairs:
        return None
    c = _consensus(pairs)
    return {"home": c["a"], "away": c["b"], "homeBest": c["aBest"], "awayBest": c["bBest"],
            "books": c["books"]}


def build_ou(rows) -> list[dict]:
    by_line: dict[float, list[tuple[float, float]]] = {}
    for r in rows:
        try:
            line = float(r.get("total"))
        except (TypeError, ValueError):
            continue
        if line % 1 != 0.5:
            continue
        p = _row_pair(r)
        if p and _pair_ok(*p):
            by_line.setdefault(line, []).append(p)
    out = []
    for line in sorted(by_line):
        c = _consensus(by_line[line])
        out.append({"line": line, "over": c["a"], "under": c["b"],
                    "overBest": c["aBest"], "underBest": c["bBest"], "books": c["books"]})
    return out


def build_pl(rows) -> dict | None:
    """Puck line from the Asian-handicap tab: keyed by the HOME side's
    handicap ("-1.5" = home gives 1.5, "+1.5" = home gets 1.5); each entry
    has the home price (home covers at that handicap) and away price (away
    covers the opposite handicap)."""
    by_h: dict[str, list[tuple[float, float]]] = {}
    for r in rows:
        try:
            h = float(r.get("total"))
        except (TypeError, ValueError):
            continue
        if abs(h) != 1.5:
            continue
        p = _row_pair(r)
        if p and _pair_ok(*p):
            by_h.setdefault("+1.5" if h > 0 else "-1.5", []).append(p)
    if not by_h:
        return None
    out = {}
    for k in sorted(by_h):
        c = _consensus(by_h[k])
        out[k] = {"home": c["a"], "away": c["b"], "homeBest": c["aBest"], "awayBest": c["bBest"],
                  "books": c["books"]}
    return out


def _books_seen(*row_sets) -> list[str]:
    names = {r.get("book") for rows in row_sets if rows for r in rows
             if r.get("book") and len(r.get("odds") or []) >= 2 and parse_price((r["odds"] or [None])[0])[0]}
    return sorted(n for n in names if n)


def fetch_match_ou_line(page, home_slug: str, home_id: str, away_slug: str, away_id: str,
                         match_id: str) -> float | None:
    """Real market Over/Under total for one match, or None if no market has
    posted yet (fails open -- every caller falls back to its own G/M-
    derived line on None, same `real value || fallback` convention
    nhlMC's own `TONIGHT.find(...)?.ou||5.5` already uses client-side)."""
    rows = _load_rows(page, _odds_url(home_slug, home_id, away_slug, away_id, match_id, "over-under"))
    if not rows:
        return None
    return _main_line(rows)


def fetch_match_odds(page, home_slug: str, home_id: str, away_slug: str, away_id: str,
                     match_id: str) -> dict:
    """Everything priced for one match: {"ou": main-line float|None (exactly
    what fetch_match_ou_line returns), "odds": {ml, ou[], pl, fmt, bk, at}|None}.
    Never raises (fail open). 3 page loads (~5s each); the ML and puck-line
    pages are skipped when the O/U page shows no market at all (nothing posted
    yet -- saves ~10s per not-yet-priced fixture)."""
    result = {"ou": None, "odds": None}
    fmt_before = Counter(FORMATS_SEEN)
    try:
        # Image/font/media aren't needed for text scraping; aborting them
        # makes each odds page load noticeably faster.
        handler = lambda route: (route.abort() if route.request.resource_type in ("image", "font", "media")
                                 else route.continue_())
        page.route("**/*", handler)
        try:
            args = (home_slug, home_id, away_slug, away_id, match_id)
            ou_rows = _load_rows(page, _odds_url(*args, market="over-under"))
            if not ou_rows:
                return result
            result["ou"] = _main_line(ou_rows)
            ml_rows = _load_rows(page, _odds_url(*args, market="home-away"))
            pl_rows = _load_rows(page, _odds_url(*args, market="asian-handicap"))
        finally:
            try:
                page.unroute("**/*", handler)
            except Exception:
                pass
        ml = build_ml(ml_rows) if ml_rows else None
        ou = build_ou(ou_rows)
        pl = build_pl(pl_rows) if pl_rows else None
        if ml or ou or pl:
            result["odds"] = {
                "ml": ml, "ou": ou, "pl": pl,
                "fmt": "/".join(sorted((FORMATS_SEEN - fmt_before).keys())) or None,
                "bk": _books_seen(ou_rows, ml_rows, pl_rows),
                "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        log(f"odds {match_id}: ml={'%s/%s (%s bk)' % (ml['home'], ml['away'], ml['books']) if ml else None} "
            f"ou_lines={len(ou)} main={result['ou']} pl={sorted(pl) if pl else None} formats={dict(FORMATS_SEEN - fmt_before)}")
    except Exception as e:  # fail open -- never break the schedule scrape
        log(f"odds fetch error for {match_id}: {e}")
    return result


def carry_over_odds(games: list[dict], prev_path) -> int:
    """Re-attach a previously scraped `odds` object to any game that lacks one
    this run (a fixture beyond the lookahead window or whose odds fetch
    failed transiently; above all a game that just flipped from 'pre' to
    'post' -- the results page row carries no odds, and the pre-game price is
    the one the pick would actually have been locked at). Keyed by match id;
    additive and fail-open. Returns how many games got odds carried over."""
    try:
        prev = json.loads(prev_path.read_text()).get("games") or []
    except Exception:
        return 0
    prev_odds = {g["id"]: g["odds"] for g in prev if g.get("odds")}
    n = 0
    for g in games:
        if not g.get("odds") and g.get("id") in prev_odds:
            g["odds"] = prev_odds[g["id"]]
            n += 1
    return n
