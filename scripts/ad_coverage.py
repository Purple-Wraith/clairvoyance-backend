"""
ad_coverage.py -- ONE source of truth for what every Clairvoyance advertisement says.

Stdlib only (no PIL / requests) so the ad generators, the social caption builders and the
tests can all import it cheaply.  Anything customer-facing that lists leagues, sports,
pricing or what a subscription includes must take it from here (or from a generator that
imports this) -- never hard-code a league list in a card script, video template or caption.

scripts/test_ad_coverage.py pins this module to the real engine:
  * LEAGUE_TAGS / LEAGUES_BY_PRODUCT  == auto_lock_settle.PRODUCT_SPORTS (what is actually locked + emailed)
  * PRICING_BY_COUNT                  == _subscribers.PRICING_BY_COUNT
  * no retired league / market appears in any ad string or ad template
  * (when ~/clairvoyance-landing exists) the landing page's coverage + pricing agree

HOW TO UPDATE when a league is added / retired: change PRODUCT_SPORTS in auto_lock_settle.py first,
then edit LEAGUES_BY_PRODUCT / LEAGUE_ROWS / CORE_COPY below, re-run scripts/test_ad_coverage.py,
then regenerate the cards (scripts/gen_pinned_card.py, gen_reddit_ad_v1.py, gen_subscription_card.py).
"""
from __future__ import annotations

# ── Paid products -> display names of the leagues each one bundles ─────────────────────────────
# (mirror of auto_lock_settle.PRODUCT_SPORTS; the test enforces it)
LEAGUES_BY_PRODUCT: dict[str, tuple[str, ...]] = {
    "nfl":    ("NFL",),
    "cfb":    ("CFB",),
    "nba":    ("NBA",),
    "hockey": ("NHL", "Liiga", "SHL", "NLA", "Extraliga"),
    "soccer": ("Premier League", "La Liga", "Serie A", "Champions League"),
}
# Display name -> the engine's sport tag used in PRODUCT_SPORTS.
LEAGUE_TAGS: dict[str, str] = {
    "NFL": "NFL", "CFB": "CFB", "NBA": "NBA", "NHL": "NHL",
    "Liiga": "LIIGA", "SHL": "SHL", "NLA": "NLA", "Extraliga": "EXTRALIGA",
    "Premier League": "SOC_PL", "La Liga": "SOC_LIGA", "Serie A": "SOC_ITA", "Champions League": "SOC_CL",
}
ALL_LEAGUES: tuple[str, ...] = tuple(l for ls in LEAGUES_BY_PRODUCT.values() for l in ls)
LEAGUE_COUNT = len(ALL_LEAGUES)                      # 12
SPORT_GROUPS = ("football", "basketball", "hockey", "soccer")   # the landing page's "12 leagues across 4 sports"
PRODUCT_COUNT = len(LEAGUES_BY_PRODUCT)              # 5 purchasable products ("1-5 sports" in the pricing ladder)

# Rows used by every league-list card (3 rows, similar visual weight per row -- same grouping the
# pinned / Reddit / subscription cards have used since 2026-09-09).
LEAGUE_ROWS: tuple[tuple[str, ...], ...] = (
    ("NFL", "CFB", "NBA", "NHL"),
    ("Liiga", "SHL", "NLA", "Extraliga"),
    ("Premier League", "La Liga", "Serie A", "Champions League"),
)

# Two-row variant for the older, narrower-text pinned-card images in docs/ (see refresh_pinned_card_images.py).
LEAGUE_ROWS_WIDE: tuple[tuple[str, ...], ...] = (
    ("NFL", "CFB", "NBA", "NHL", "Liiga", "SHL", "NLA", "Extraliga"),
    ("Premier League", "La Liga", "Serie A", "Champions League"),
)

# ── What a subscription contains ────────────────────────────────────────────────────────────────
# Game lines (ML / Spread / O-U) for every league.  Player props: NBA ONLY (NFL props removed
# 2026-09-23, NHL props removed 2026-10-09; WNBA retired).  Hockey's European leagues carry the
# HIGH PROB lane + adjusted lines.
# NBA player props are PAUSED in all ad copy (owner 2026-10-25 landing / 2026-10-27 cards): off until ~20 NBA games are settled.
# To bring them back, add the wording to CORE_COPY / INCLUDES_LINE / BODY_LINES below and flip the props assertions in test_ad_coverage.py.
CORE_COPY = "graded picks and game lines"
INCLUDES_LINE = "Graded picks + game lines on every league"
EURO_HOCKEY_NOTE ="European hockey: HIGH PROB lane + adjusted lines"

# ── Pricing (30-day access, 1-5 products) ────────────────────────────────────────────────────────
PRICING_BY_COUNT: dict[int, int] = {1: 20, 2: 30, 3: 45, 4: 60, 5: 75}
ACCESS_DAYS = 30
PRICE_FROM, PRICE_ALL = PRICING_BY_COUNT[1], PRICING_BY_COUNT[PRODUCT_COUNT]

# ── Card copy shared by the image generators ────────────────────────────────────────────────────
TAGLINE = "ADVANCED SPORTS INTELLIGENCE ENGINE"
BODY_LINES: tuple[str, ...] = (
    "An advanced sports intelligence engine built around",
    "mathematical precision, multi-layered ensemble models,",
    "and adaptive intelligence — delivering graded picks",
    "and game lines.",
)
PRICING_LINE_PARTS = (f"Starting at ${PRICE_FROM}", f"Full Access ${PRICE_ALL}", f"{ACCESS_DAYS}-Day Access")
HANDLES = ("X @ClairvoyanceEng", "clairvoyanceengine.info", "IG @clairvoyanceengine")


# ── Fingerprints: committed ad PNGs carry a hash of the copy they were rendered from (PNG tEXt chunk), so a test can ──
# ── tell "this image is stale vs the source of truth" without re-rendering (rendering differs across Pillow versions). ──
FP_KEY = "cv-ad-fingerprint"


def _fp(*parts) -> str:
    import hashlib
    import json
    return hashlib.sha1(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def pinned_card_fingerprint() -> str:
    """Copy of the pinned / covers card (gen_pinned_card.py -> scripts/assets/covers_card.png)."""
    return _fp("pinned", TAGLINE, BODY_LINES, LEAGUE_ROWS, PRICING_LINE_PARTS)


def pinned_docs_fingerprint() -> str:
    """Copy of the repainted block in docs/pinned_card_*.png (refresh_pinned_card_images.py)."""
    return _fp("pinned-docs", LEAGUE_ROWS_WIDE)


def read_png_text(path) -> dict[str, str]:
    """tEXt chunks of a PNG, stdlib only (Pillow is not installed in CI)."""
    import struct
    import zlib
    data = open(path, "rb").read()
    out, pos = {}, 8
    while pos < len(data):
        n, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if typ == b"tEXt":
            k, _, v = body.partition(b"\x00")
            out[k.decode("latin-1")] = v.decode("latin-1")
        elif typ == b"iTXt":
            k, _, rest = body.partition(b"\x00")
            flag, rest = rest[0], rest[2:]
            _lang, _, rest = rest.partition(b"\x00")
            _tr, _, txt = rest.partition(b"\x00")
            out[k.decode("latin-1")] = (zlib.decompress(txt) if flag else txt).decode("utf-8")
        pos += 12 + n
    return out


def coverage_blurb() -> str:
    """'12 leagues across football, basketball, hockey and soccer' -- matches the landing page."""
    sports = ", ".join(SPORT_GROUPS[:-1]) + " and " + SPORT_GROUPS[-1]
    return f"{LEAGUE_COUNT} leagues across {sports}"


# ── Retired -- must never appear as covered in any ad ────────────────────────────────────────────
# (case-insensitive substring / word checks done by scripts/test_ad_coverage.py)
RETIRED_TERMS: tuple[str, ...] = (
    "MLS", "Bundesliga", "BUND", "MLB", "baseball", "WNBA", "NCAAH", "college hockey", "tennis", "ATP", "WTA",
    "World Cup", "parlay", "KHL", "F1", "Day Pass", "Weekend Pass", "INSIDER", "$25/MO", "$50/MO", "$80/MO",
)
