"""
Server-side auto-lock and auto-settle, driving the real live app headlessly
via Playwright so both reuse the EXACT same engine/grading/settlement logic
the UI itself uses -- single source of truth, zero duplicated logic here.

Background: server-side settlement previously depended on data/locked_props.json,
a file nothing in this pipeline ever actually wrote (the only writer was a
manual browser export button, never used) -- so auto_settle() in
clairvoyance_update.py silently no-op'd on every run. This script replaces
that dead path: it loads the REAL bet ledger straight from Supabase (same
proven pattern generate_social_cards.py already uses), settles it via the
real client-side autoSettle*() functions, and locks new PREMIUM/OPTIMAL
picks via the real lockPick()/lockProp()/lockNHLProp() functions -- then
flushes everything back to Supabase via the app's own syncBetsToSupabase().

Auto-lock scope: ML, spread, and O/U for every sport/league with a real
proprietary model (NBA, NHL, NFL, CFB, and 4 soccer leagues -- Bundesliga
retired 2026-09-23, MLS retired 2026-09-27).
NCAAH only has a market-read-back model (no proprietary edge to
grade), matching how the rest of the app already treats it -- ML only
there via _epGatherESPNCacheLegs, not extended here. Player props covered
for NBA/NHL (both with real live prop engines built this session).
NFL player props REMOVED from this pipeline 2026-09-23, explicit
request following a real settled-bet audit: TD props specifically were
badly overconfident (49.1% actual win vs 79.5% avg predicted, -21.7u on
57 bets, the single largest drag on NFL's entire season-to-date ROI --
receiving/passing props were fine, so this wasn't a prop-engine-wide
problem), and the user's stated direction going forward is NFL game
lines only -- "more dependable," not per-market tuning. NFL's real live
prop engine (_nflModelPropsForGame, docs/app.html) is untouched and
still browsable in-app for personal reference; it's just no longer fed
into gather_legs()'s propLegs, so nothing it produces can ever qualify
or lock automatically. Tennis engine retired 2026-09-08, and MLB/WNBA/
CBB/World Cup retired 2026-09-08 (removed from app.html and this
pipeline entirely, not merely kept off paid products) -- none are
fetched, locked, or settled anywhere in this pipeline anymore.

Grade capture for game markets: docs/app.html has a small _autoLockCapture()
hook wired into every sport's real game-card render function, right after
each one computes its own real _evalMkts() result -- this script never
re-derives spread/O-U probabilities itself, it only reads what the UI
already computed for that exact game, guaranteeing zero drift.

Known pre-existing app characteristic (not introduced here): lockPick()'s
single `type` parameter can encode EITHER the sport tag OR a bet-type
keyword ('OU'/'SPREAD'/'RL'/'PL'), not always both -- passing the explicit
sport tag (required for correct classification on non-abbreviation-
guessable sports) means the stored betType field defaults to 'ML' even for
a real spread/O-U pick. This already happens for real manually-locked bets
today (confirmed: NHL's own spread-lock button passes type='NHL', not
'PL'). This script matches that exact existing behavior rather than
inventing a new convention -- the betOn text itself (e.g. "PHI -1.5") is
always correct regardless.

Safety: defaults to dry-run (logs exactly what it WOULD lock/settle,
writes nothing anywhere). Pass --live to actually write to Supabase.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).parent))
from _gmail_email import send_email as _send_gmail  # noqa: E402
from _gmail_email import EMAIL_WRAP_OPEN as _EMAIL_WRAP_OPEN, EMAIL_WRAP_CLOSE as _EMAIL_WRAP_CLOSE  # noqa: E402
from _gmail_email import EMAIL_WRAP_CLOSE_DISCLOSED as _LOCKS_EMAIL_CLOSE  # noqa: E402
from _subscribers import recipients_for, OWNER_EMAIL, EMAIL_BANNER_URL  # noqa: E402
# Reused (not reimplemented) for the landing-perf JSON snapshot this
# script now also writes on every run -- see the call site in main() for
# why this replaced a second, fully independent browser+Supabase pull.
from generate_social_cards import write_landing_json as _write_landing_json  # noqa: E402
import lock_timing  # noqa: E402  -- shared known-late classifier (public figures + digests are pre-start locks only)

# _LOCKS_EMAIL_CLOSE (the disclaimer-bearing close, defined in
# _gmail_email.py so _subscribers.py's receipt email can share the exact
# same copy without a circular import) is used at BOTH return points in
# build_locks_email_html() below, including the "no legs qualified
# today" early return -- structurally impossible to add a new return
# path that skips it.

ROOT = Path(__file__).resolve().parent.parent
APP_URL = "https://purple-wraith.github.io/clairvoyance-backend/app.html"
# Every headless page this pipeline (and the social-card generators) opens carries ?nologo=1: docs/app.html's _teamLogoHTML() then
# renders NO team-logo markup and never fetches docs/team_logos.json, so the card HTML is byte-identical to the pre-logo app and
# the passes add zero image requests (logos are an interactive-UI feature only; navigator.webdriver is a second, independent guard).
NOLOGO_QS = "nologo=1"


def with_nologo(url: str) -> str:
    return url if NOLOGO_QS in url else url + ("&" if "?" in url else "?") + NOLOGO_QS
# Separate from SOCIAL_CARD_EMAIL_TO on purpose -- this is a personal daily
# betting reference, not public social content, so it can (and probably
# should) go to a different inbox. Falls back to the social recipient only
# so this doesn't silently go nowhere if the dedicated secret isn't set yet.
LOCKS_EMAIL_TO = os.environ.get("LOCKS_EMAIL_TO", "") or os.environ.get("SOCIAL_CARD_EMAIL_TO", "")

# Human-readable section headers for the email, keyed by the same sport
# tags SPORT_TO_LOCKPICK_TYPE/_autoLockCapture use.
# "SOC_BL": "Bundesliga" entry removed 2026-09-23, explicit follow-up
# request after Bundesliga's retirement -- can never appear in a future
# locks email anyway (SOC_BL can no longer qualify), removed so it can't
# resurface anywhere, including a legacy email label lookup.
# "SOC_MLS": "MLS" entry removed 2026-09-27, same treatment following
# MLS's own retirement -- can never appear in a future locks email either
# (SOC_MLS can no longer qualify once PRODUCT_SPORTS["soccer"] stops
# including it -- see that constant's own comment).
SPORT_DISPLAY_NAME = {
    "NBA": "NBA", "NHL": "NHL", "NFL": "NFL", "CFB": "CFB", "LIIGA": "Liiga", "SHL": "SHL",
    "NLA": "NLA", "EXTRALIGA": "Extraliga",
    "SOC_LIGA": "La Liga", "SOC_PL": "Premier League",
    "SOC_ITA": "Serie A", "SOC_CL": "Champions League",
}

# Real bug, found + fixed 2026-09-30 auditing the settlement email: this
# email used to group by the raw `sport` field with no normalization at
# all, so a real settled bet tagged sport:"FOOTBALL" (a legacy/broad tag
# some lock paths still write) formed its own separate "FOOTBALL" section
# in the email, distinct from every other real NFL bet tagged sport:"NFL"
# -- both are the same league, just split across two headers. Confirmed
# live in the real ledger: FOOTBALL/ML, FOOTBALL/OU, FOOTBALL/SPREAD,
# FOOTBALL/PROP all exist as real settled rows alongside NFL/* rows for
# the same sport. Direct Python port of docs/app.html's own _normSport(p)
# -- the canonical classifier every other display in this app already
# uses -- covering just the explicit-tag branches (no team-abbreviation
# guessing: a settled bet always has a real tag already, so that fallback
# path in _normSport never applies here).
_BROAD_AMBIGUOUS_SPORTS = {"FOOTBALL", "BASKETBALL", "HOCKEY", "SOCCER", "BASEBALL"}


def norm_sport_for_email(sport: str | None, league: str | None) -> str:
    sport_up = (sport or "").strip().upper()
    league_up = (league or "").strip().upper()
    raw = league_up if (sport_up in _BROAD_AMBIGUOUS_SPORTS and league_up) else (sport_up or league_up)
    if raw in ("MLB", "BASEBALL"):
        return "MLB"
    if raw in ("NHL", "HOCKEY", "ICE HOCKEY"):
        return "NHL"
    if raw == "WNBA":
        return "WNBA"
    if raw in ("NBA", "BASKETBALL"):
        return "NBA"
    if raw in ("CBB", "NCAAB", "COLLEGE BASKETBALL"):
        return "CBB"
    if raw in ("NFL", "FOOTBALL"):
        return "NFL"
    if raw in ("CFB", "COLLEGE FOOTBALL", "NCAAF"):
        return "CFB"
    if raw in ("KHL", "SHL", "LIIGA", "NLA", "EXTRALIGA"):
        return raw
    if raw in ("NCAAH", "COLLEGE HOCKEY"):
        return "NCAAH"
    if raw in ("WC", "WORLDCUP", "WORLD_CUP", "WORLD CUP", "SOC"):
        return "WC"
    if raw in ("PL", "PREMIER LEAGUE"):
        return "PL"
    if raw in ("LIGA", "LA LIGA"):
        return "LIGA"
    if raw in ("BUND", "BL", "BUNDESLIGA"):
        return "BUND"
    if raw == "MLS":
        return "MLS"
    if raw in ("SERIEA", "SERIE A"):
        return "SERIEA"
    if raw in ("CL", "CH", "CHAMPIONS LEAGUE"):
        return "CL"
    # Real _normSport(p)'s own fallthrough: any tag it doesn't explicitly
    # recognize (ATP, WTA, TEN, etc.) is returned as-is rather than
    # guessed at -- matching that exactly instead of inventing new cases.
    return raw or "MISC"

# Display names keyed by the LEDGER's own `league` field (lockPick()'s
# _leagueMap in docs/app.html -- BUND/LIGA/PL/SERIEA/CL/MLS, no "SOC_"
# prefix) -- a DIFFERENT tag scheme from SPORT_DISPLAY_NAME above, which
# is keyed by _autoLockCapture's sport tag instead. The top-picks digest
# groups by the stored `league` field (what's actually on a locked bet
# row), so it needs this mapping, not SPORT_DISPLAY_NAME.
# "BUND": "Bundesliga" entry removed 2026-09-23, same explicit follow-up
# request -- zero pending Bundesliga bets existed at retirement time, so
# this can't affect a real future digest; .get(lg, lg) below falls back
# to the raw code if it's ever somehow hit.
# "MLS": "MLS" entry removed 2026-09-27, same treatment following MLS's
# own retirement -- same .get(lg, lg) fallback applies if it's ever hit.
LEDGER_LEAGUE_DISPLAY_NAME = {
    "NBA": "NBA", "NHL": "NHL", "NFL": "NFL", "CFB": "CFB",
    "KHL": "KHL", "SHL": "SHL", "LIIGA": "Liiga", "NLA": "NLA", "EXTRALIGA": "Extraliga",
    "NCAAH": "College Hockey",
    "CL": "Champions League", "PL": "Premier League", "LIGA": "La Liga",
    "SERIEA": "Serie A",
}

# Maps the sport tag docs/app.html's _autoLockCapture() attaches to each
# game leg (e.g. 'SOC_PL' for Premier League, to keep 'PL' unambiguous --
# lockPick's own type='PL' means NHL puck line) to the exact `type` string
# lockPick() itself expects to resolve the correct sportTag.
# Real bug, found + fixed 2026-09-23 auditing NLA/Extraliga's real
# production status: NLA and EXTRALIGA were never added here when they
# were promoted into EARLY_HOCKEY_SPORTS/PRODUCT_SPORTS the same day --
# confirmed live in that same day's actual hockey-lock-evening.yml run
# log: gather_hockey_evening_legs_for_date() correctly found and
# qualified real PREMIUM/OPTIMAL NLA/Extraliga picks for 2026-09-24
# (EHC Kloten +1.5, Liberec +1.5, UNDER 5.5), but lock_game_leg() then
# failed all 3 with "no lockPick type mapping for sport NLA"/
# "...EXTRALIGA" -- every real qualifying NLA/Extraliga pick had been
# silently dropped since promotion, 0 ever actually locked, while
# LIIGA/SHL (present here since their own 2026-09-16 promotion) worked
# the whole time. docs/app.html's own manual lock buttons already pass
# 'NLA'/'EXTRALIGA' as lockPick()'s literal `type` argument (see
# _nlaMatchCard/_extraligaMatchCard), so -- like LIIGA/SHL -- they map
# to themselves, no translation needed.
SPORT_TO_LOCKPICK_TYPE = {
    "NBA": "NBA", "NHL": "NHL", "NFL": "NFL", "CFB": "CFB", "LIIGA": "LIIGA", "SHL": "SHL",
    "NLA": "NLA", "EXTRALIGA": "EXTRALIGA",
    "SOC_LIGA": "LIGA", "SOC_PL": "PL_SOC",
    "SOC_ITA": "SERIEA", "SOC_CL": "CL",
}
# The European leagues. Originally kept separate from SOC_MLS as its own
# constant purely so build_locks_email_html could split the combined
# soccer email into an "EUROPEAN" section and a "NORTH AMERICA (MLS)"
# section; that split is now moot since MLS's 2026-09-27 retirement --
# PRODUCT_SPORTS["soccer"] (below) no longer unions in SOC_MLS, so no
# qualifying leg can ever land in a "NORTH AMERICA" section again. Left
# as its own named constant regardless (still used directly by the
# evening-prior lock and elsewhere).
# SOC_BL (Bundesliga) removed 2026-09-23, explicit request following a
# real settled-bet audit: the only negative-ROI soccer league (-2.6%
# all-time, worsening to -21.8% ROI over its last 20 bets), concentrated
# in a genuinely broken O/U signal (47.6% win / -1.9u) that soccer's
# shared model has no per-league calibration lever to fix. Retired the
# same way MLB/WNBA/tennis/World Cup were -- removed from the active
# pipeline and product, real settled-bet history untouched.
EURO_SOCCER_SPORTS = frozenset({"SOC_CL", "SOC_PL", "SOC_LIGA", "SOC_ITA"})
# The 4 PAID-PRODUCT hockey leagues with a real early-kickoff problem --
# SHL games as early as 7:15 AM MT, Liiga around 9:30 AM MT (confirmed
# real schedule data, 2026-09-17); NLA/Extraliga on the same Central
# European evening schedule, same real early-MT-morning kickoffs. NHL
# deliberately excluded: it never plays this early, so it keeps its own
# slot in the main run instead of joining this set. Folded into the same
# early-morning pass as the 5 European soccer leagues (run_euro_early_lock)
# rather than getting its own separate workflow -- one browser session,
# one gather_legs() pull for both products, each still gets its own
# separately-addressed email.
# IMPORTANT: this set feeds recipients_for("hockey") -- the real paid
# subscriber list -- in both run_euro_early_lock and
# run_hockey_evening_lock. Do not add a new league here without a
# deliberate product decision to start selling it.
#
# NLA (Swiss National League) and Extraliga (Czech) promoted into this
# set 2026-09-23, explicit request -- "integrate nla and extra liga into
# the paid subscriber section for hockey and make sure they part of
# early lock email for hockey like shl and liiga." They'd briefly sat in
# a separate EARLY_HOCKEY_SPORTS_PERSONAL/OTHER_ALLOWED_SPORTS scope
# (personal-use-only, following the precedent SHL/Liiga/NCAAH themselves
# started on) for less than a day before this explicit promotion --
# see PRODUCT_SPORTS' own comment for the full history.
EARLY_HOCKEY_SPORTS = frozenset({"SHL", "LIIGA", "NLA", "EXTRALIGA"})

# The 5 paid products (confirmed structure, see _subscribers.py) -- each
# maps to the exact sport tags gather_legs()/_autoLockCapture use. Soccer
# bundled all 6 leagues (5 European + MLS) as one purchase until
# Bundesliga's 2026-09-23 retirement (5 leagues); MLS retired 2026-09-27,
# explicit request following the same pattern -- SOC_MLS dropped from the
# union below, so the soccer product is now just EURO_SOCCER_SPORTS' own
# 4 European leagues, real settled-bet history untouched everywhere it
# already displays.
#
# Hockey product promoted from NHL-only to NHL+Liiga+SHL, 2026-09-16 --
# explicit request, now that both Liiga and SHL have real engines
# (Flashscore-sourced schedule/standings/G-per-match, an exact-Poisson MC
# model, and full ML/spread/O-U game cards -- see fetch_liiga.py/
# fetch_shl.py and docs/app.html's liigaMC/shlMC + their Ens/MatchCard
# equivalents). Promoted again 2026-09-23 to add NLA/Extraliga the same
# way, explicit request -- same real engines (fetch_nla.py/
# fetch_extraliga.py, nlaMC/extraligaMC), same day they were built.
# NCAAH (College Hockey) joins the same way once it gets its own real
# engine -- the user's stated direction is "hockey" as a product should
# mean NHL+Liiga+SHL+NLA+Extraliga+NCAAH going forward, not NHL alone;
# add each sport tag here as it's actually built, not before (KHL is
# deliberately never included -- see below).
#
# Explicit decision 2026-09-03 (superseded above for Liiga/SHL/NLA/
# Extraliga specifically): KHL, SHL, LIIGA, and College Hockey (NCAAH)
# were real, live engine features kept running for PERSONAL USE ONLY --
# never sold, routed to the owner-only "other" pass instead of any paid
# product, back when none of them had real game cards wired up yet. KHL
# stays excluded permanently (its own manual lock/settle buttons in the
# app UI still work for personal use) -- there's no stated plan to build
# it out or sell it, unlike SHL/LIIGA/NLA/Extraliga/NCAAH.
# Explicit decision 2026-09-08: tennis, MLB, WNBA, CBB, and World Cup
# were all retired from the engine entirely -- removed from app.html and
# this pipeline, not merely kept off paid products. "mlb" was dropped
# from PRODUCT_SPORTS/PRODUCT_LABEL, leaving 5 paid products instead of
# 6 (WNBA had already never been a paid product).
PRODUCT_SPORTS: dict[str, frozenset[str]] = {
    "nfl": frozenset({"NFL"}),
    "cfb": frozenset({"CFB"}),
    "nba": frozenset({"NBA"}),
    "hockey": frozenset({"NHL", "LIIGA", "SHL", "NLA", "EXTRALIGA"}),
    # SOC_MLS dropped 2026-09-27 -- MLS retired, explicit request. Soccer's
    # paid product is now exactly EURO_SOCCER_SPORTS (CL/PL/La Liga/Serie A).
    "soccer": EURO_SOCCER_SPORTS,
}
# NCAAH (College Hockey) retired 2026-10-02, explicit decision -- this
# set used to carry it as "owner-only/personal-use until it has a real
# engine built" (SHL/Liiga/NLA/Extraliga all passed through that same
# stage before being promoted into PRODUCT_SPORTS; NCAAH never got a real
# engine and is being fully retired instead of promoted). No sport should
# ever sit in both PRODUCT_SPORTS and here. autoSettleNCAAH stays wired
# in docs/app.html's settle loop below (never remove a settle path for a
# retired sport -- it may still have real pending bets to grade; see the
# same convention already established for MLB/WNBA/CBB/World Cup), but
# this set controls LOCKING eligibility, and NCAAH no longer qualifies
# for any new lock. KHL is permanently owner-only -- see PRODUCT_SPORTS'
# own comment above (KHL was never in this set to begin with).
OTHER_ALLOWED_SPORTS: frozenset[str] = frozenset()
PRODUCT_LABEL: dict[str, str] = {
    "nfl": "NFL", "cfb": "CFB", "nba": "NBA",
    "hockey": "HOCKEY", "soccer": "SOCCER",
}
# OPTIMAL=2, PREMIUM=3 in _evalMkts()'s own tierN scale.
QUALIFYING_TIERS = {2, 3}
TIER_LABEL = {0: "SKIP", 1: "LEAN", 2: "OPTIMAL", 3: "PREMIUM"}

# ── HOCKEY QUALIFICATION RULES (NHL + SHL/LIIGA/NLA/EXTRALIGA) ──────────────────────────────────────────────────────────
# The numbers live in ONE place: the "HOCKEY QUALIFICATION CUTOFFS" block in docs/app.html (HOCKEY_TIER_EV,
# HOCKEY_LANE_*, HOCKEY_REQUIRE_REAL_PRICE ...), which grades every hockey leg (tierN) and flags high-probability-lane
# legs (hkLane) inside _evalMkts. build_qualifying() below just consumes those verdicts (QUALIFYING_TIERS {2,3} or hkLane).
# The constants here only DESCRIBE the rules in the subscriber email legend; scripts/verify_hockey_rules.py checks they equal
# the app.html block so the legend can never drift from what actually qualifies. Backtest: scripts/backtest_hockey_models.py tiers.
HOCKEY_SPORTS = frozenset({"NHL", "LIIGA", "SHL", "NLA", "EXTRALIGA"})
HOCKEY_REQUIRE_REAL_PRICE = True
HOCKEY_TIER_EV = {"LEAN": 0.01, "OPTIMAL": 0.03, "PREMIUM": 0.05}      # EV floors on the REAL price (tier probability floors stay .55/.62/.67)
HOCKEY_TIER_PROB = {"LEAN": 0.55, "OPTIMAL": 0.62, "PREMIUM": 0.67}
HOCKEY_LANE_ML_P = 0.65            # high-probability lane: moneyline win probability floor
HOCKEY_LANE_PLDOG_P = 0.65         # high-probability lane: +1.5 puck-line (underdog side) cover probability floor
HOCKEY_LANE_EV_MIN = -0.07         # high-probability lane: EV at the real price may not be worse than this
HOCKEY_ODDS_MAX_AGE_H = 18         # a posted price older than this is not trusted (mirrors HOCKEY_ODDS_MAX_AGE_H in app.html)
HOCKEY_LANE_LABEL = "HIGH PROB"

# ── PRE-START LOCK GUARD (2026-10-02) ────────────────────────────────────────────────────────────────────────────────────
# The automated passes must never lock a game that has started. gather_legs() and the evening-prior gatherers capture every
# schedule-file game whose `state` isn't 'post', but those files refresh only 2-3x/day and GitHub Actions lock passes land
# hours after their nominal time, so `state` can lag real kickoff. build_qualifying()/lock_game_leg() now refuse any leg whose
# scheduled start (startMs, epoch ms, threaded from docs/app.html's _autoLockCapture) is within LOCK_START_MARGIN_MIN of now
# or past. (The ledger's picks locked after start are mostly the owner's deliberate MANUAL locks -- those stay allowed; see
# lockPick's lockTiming stamp. This guard only governs the automated passes.)
# Mirrors LOCK_START_MARGIN_MIN in docs/app.html (checked by scripts/verify_hockey_rules.py).
LOCK_START_MARGIN_MIN = 10
# Sports where an UNKNOWN/unparseable start fails CLOSED (leg refused). Every other sport fails OPEN when startMs is
# missing (all current card call sites do pass g.date, so that only happens on a malformed schedule row) and is reported
# in the guard summary line as "unguarded".
START_GUARD_FAIL_CLOSED_SPORTS = HOCKEY_SPORTS
# Per-process running totals so the lock-status detail text can say how many legs a pass skipped. Reset never needed:
# each workflow run is its own short-lived process.
START_GUARD_TOTALS = {"skipped": 0, "unguarded": 0}


def guard_note() -> str:
    """Suffix for the lock-status detail text: how many legs this process skipped because the game had already started.
    Empty when none. A skip is the guard WORKING -- it must never flip a status to ok=False (see write_automation_status
    callers: ok depends only on locked-vs-verified counts) or be read as a failed lock by verify_lock_workflows.py."""
    n = START_GUARD_TOTALS["skipped"]
    return f"; {n} leg(s) skipped: game already started" if n else ""


def _now_ms(now=None) -> float:
    """now: None (real clock), a datetime, or epoch milliseconds (int/float) -- injectable for tests."""
    if now is None:
        return datetime.now(timezone.utc).timestamp() * 1000.0
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.timestamp() * 1000.0
    return float(now)


def parse_start_ms(v):
    """startMs as the card captured it (epoch ms) -> float epoch ms, or None if missing/unparseable. Also tolerates an
    ISO-8601 string. Values that look like epoch SECONDS (< 1e11) or are absurd are rejected rather than guessed at."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.strip().replace("Z", "+00:00")).timestamp() * 1000.0
        except ValueError:
            return None
    try:
        t = float(v)
    except (TypeError, ValueError):
        return None
    if t != t or not (946684800000 <= t <= 4102444800000):  # NaN, or outside 2000-01-01 .. 2100-01-01 in ms
        return None
    return t


def start_guard(sport, start_ms, now=None, margin_min: float = LOCK_START_MARGIN_MIN):
    """-> (ok, reason, minutes_until_start). ok=False means the leg must NOT be locked. reason is a short human string.
    Unknown start: refused for START_GUARD_FAIL_CLOSED_SPORTS, allowed (reason 'no start time') otherwise."""
    t = parse_start_ms(start_ms)
    if t is None:
        if (sport or "") in START_GUARD_FAIL_CLOSED_SPORTS:
            return False, "no usable start time (fail closed)", None
        return True, "no start time (unguarded)", None
    mins = (t - _now_ms(now)) / 60000.0
    if mins < margin_min:
        if mins <= 0:
            return False, f"game already started {-mins:.0f}m ago", mins
        return False, f"game starts in {mins:.0f}m (< {margin_min:g}m margin)", mins
    return True, "ok", mins


MT = ZoneInfo("America/Denver")


def mt_date_of_ms(ms) -> str | None:
    """America/Denver calendar date (YYYY-MM-DD) of an epoch-ms start, or None if unusable."""
    t = parse_start_ms(ms)
    return None if t is None else datetime.fromtimestamp(t / 1000.0, MT).strftime("%Y-%m-%d")


def horizon_dates(now=None, days: int = 2) -> list[str]:
    """The Mountain-time calendar dates a ROLLING-HORIZON pass covers: today and tomorrow, relative to the moment the pass
    ACTUALLY runs. The evening-prior passes used to lock "tomorrow" only. GitHub delays scheduled runs by 5-7 hours, so a pass
    scheduled for 10 PM MT really runs at ~4 AM MT -- AFTER midnight -- when "tomorrow" is the day AFTER the games that are about
    to start (SHL kicks off at 7:15 AM MT that same morning). Covering [today, tomorrow] makes the pass correct whether it lands
    before or after midnight; games that already started are refused by the pre-start guard, so the extra date costs nothing."""
    t = datetime.fromtimestamp(_now_ms(now) / 1000.0, MT)
    return [(t + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


# One report per lock pass run in this process (appended by the run_* functions, read by main() for the result file, the
# automation status and the owner alert). Plain dicts so they serialise straight to JSON.
PASS_REPORTS: list[dict] = []


def analyze_games(result: dict, only_sports=None, now=None) -> dict:
    """Facts about the games a pass gathered (independent of what qualified): how many, how many already started, when the
    next one kicks off, and which UPCOMING hockey games have no real posted price (so cannot qualify YET -- a later pass,
    after the odds post, may lock them: the pass is not 'complete')."""
    now_ms = _now_ms(now)
    games = [g for g in (result.get("gameLegs") or []) if only_sports is None or g.get("sport") in only_sports]
    upcoming: list[float] = []
    started = 0
    no_price: list[str] = []
    for g in games:
        st = parse_start_ms(g.get("startMs"))
        if st is None:
            continue
        if st <= now_ms:
            started += 1
            continue
        upcoming.append(st)
        if g.get("sport") in HOCKEY_SPORTS and not any(m.get("priceSource") == "market" for m in (g.get("markets") or [])):
            no_price.append(f"{g.get('sport')} {g.get('awA')} @ {g.get('hA')}")
    return {
        "games": len(games), "started": started, "upcoming": len(upcoming),
        "nextStartMs": min(upcoming) if upcoming else None,
        "firstStartMs": min([parse_start_ms(g.get("startMs")) for g in games if parse_start_ms(g.get("startMs"))] or [None]) if games else None,
        "noPrice": no_price,
    }


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


_GIT_IDENTITY = ["-c", "user.name=clairvoyance-bot", "-c", "user.email=bot@clairvoyance.local"]


def _commit_and_push(paths: list[str], message: str) -> None:
    """Shared by every marker file this module writes (automation_status.json,
    last_lock_date.txt, last_lock_email_date.txt, ...) -- add + commit +
    push with one fetch+rebase retry on a rejected push, same pattern
    already proven in clairvoyance_update.py's git_push()/run_live_window
    for the same reason: this repo gets pushed to from multiple places
    concurrently, so a plain push failing once is a normal race, not a
    real error, and deserves one retry before giving up.

    Real bug, found via audit: neither the commit nor the rebase below
    carried a git identity, and the workflow that calls this
    (auto-lock-settle.yml) never sets one globally either -- so the
    commit itself failed ("empty ident name... not allowed") on every
    single real CI invocation, silently (the returncode was never
    checked). write_automation_status()'s docs/automation_status.json
    updates only ever reached production when some OTHER, correctly-
    identified step happened to run a commit later in the same job and
    coincidentally swept up this function's still-staged-but-uncommitted
    change along with its own. Confirmed live: exactly one real commit to
    that file in this repo's entire history actually came from this
    function running standalone; every other one rode along on a
    different step's commit. _GIT_IDENTITY here matches the identity
    every workflow's own inline marker-file commits already use."""
    subprocess.run(["git", "-C", str(ROOT), "add", *paths], capture_output=True)
    diff = subprocess.run(["git", "-C", str(ROOT), "diff", "--cached", "--quiet"], capture_output=True)
    if diff.returncode == 0:
        return
    commit_res = subprocess.run(["git", "-C", str(ROOT), *_GIT_IDENTITY, "commit", "-m", message], capture_output=True, text=True)
    if commit_res.returncode != 0:
        log(f"_commit_and_push: commit failed: {commit_res.stderr.strip()[:300]}")
        return
    push_res = subprocess.run(["git", "-C", str(ROOT), "push", "origin", "main"], capture_output=True, text=True)
    if push_res.returncode != 0:
        subprocess.run(["git", "-C", str(ROOT), "fetch", "origin", "main"], capture_output=True)
        if subprocess.run(["git", "-C", str(ROOT), *_GIT_IDENTITY, "rebase", "--autostash", "origin/main"], capture_output=True).returncode == 0:
            subprocess.run(["git", "-C", str(ROOT), "push", "origin", "main"], capture_output=True)
        else:
            subprocess.run(["git", "-C", str(ROOT), "rebase", "--abort"], capture_output=True)
            log(f"_commit_and_push: push failed and rebase also failed: {push_res.stderr}")


LOCK_PASS_HISTORY_MAX = 60  # how many recent lock passes docs/automation_status.json keeps under "lockPasses" (Engine Health)


def read_marker_from_origin(rel_path: str) -> str:
    """Contents of a data/ marker file as of origin/main (fetching first), falling back to the checked-out copy. A scheduled run's
    checkout is the commit that existed when it was TRIGGERED, so a run queued behind another slot of the same workflow (they share a
    concurrency group) does not see the marker that earlier slot pushed -- which would let both email subscribers."""
    try:
        subprocess.run(["git", "-C", str(ROOT), "fetch", "--quiet", "--depth=1", "origin", "main"], capture_output=True, timeout=60)
        r = subprocess.run(["git", "-C", str(ROOT), "show", f"origin/main:{rel_path}"], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    try:
        return (ROOT / rel_path).read_text().strip()
    except Exception:
        return ""


def write_automation_status(kind: str, ok: bool, detail: str, extra: dict | None = None,
                            passes: list[dict] | None = None, owner_alert_hash: str | None = None) -> None:
    """kind: 'lastLock' or 'lastSettle'. Drives the header's LAST LOCK/LAST
    SETTLE indicators (docs/app.html reads this same file, same-origin, no
    CORS concerns). Written only for real --live runs -- a dry-run
    represents nothing that actually happened, so surfacing one here would
    misrepresent real automation health to anyone reading the indicator.
    Built directly in response to today's real incident: a scheduled lock
    trigger silently never fired and nothing on the page gave any hint
    anything was wrong -- this makes that failure mode visible without
    needing to go dig through GitHub Actions run history to notice it."""
    path = ROOT / "docs" / "automation_status.json"
    try:
        status = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        status = {}
    now_utc = datetime.now(timezone.utc)
    now_mt = now_utc.astimezone(ZoneInfo("America/Denver"))
    status[kind] = {
        "tsUTC": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tsMT": now_mt.strftime("%Y-%m-%d %I:%M %p MT"),
        "ok": ok,
        "detail": detail,
        **(extra or {}),
    }
    # Engine Health (2026-10-02): every lock pass appends one entry -- when it LANDED vs the next kickoff and whether it refused
    # legs because games had already started -- so a late pass is visible instead of silently succeeding. Newest last.
    if passes:
        hist = status.get("lockPasses") if isinstance(status.get("lockPasses"), list) else []
        hist.extend(passes)
        status["lockPasses"] = hist[-LOCK_PASS_HISTORY_MAX:]
    if owner_alert_hash:
        al = status.get("ownerAlerts") if isinstance(status.get("ownerAlerts"), dict) else {}
        al[owner_alert_hash] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        # keep only the last ~3 days of hashes
        cutoff = (now_utc - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        status["ownerAlerts"] = {k: v for k, v in al.items() if v >= cutoff}
    try:
        path.write_text(json.dumps(status, indent=2))
        _commit_and_push(["docs/automation_status.json"], f"chore: {kind} status ({'ok' if ok else 'FAILED'})")
    except Exception as exc:
        log(f"write_automation_status failed: {exc}")


def pass_entries(kind_key: str, reports: list[dict]) -> list[dict]:
    """PASS_REPORTS -> compact lockPasses entries for docs/automation_status.json (what Engine Health shows)."""
    now_utc = datetime.now(timezone.utc)
    now_ms = now_utc.timestamp() * 1000.0
    out = []
    for r in reports:
        nxt = r.get("nextStartMs")
        out.append({
            "key": kind_key, "kind": r.get("kind"), "label": r.get("label"),
            "tsUTC": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "tsMT": now_utc.astimezone(MT).strftime("%Y-%m-%d %I:%M %p MT"),
            "dates": r.get("dates"), "live": bool(r.get("live")), "complete": bool(r.get("complete")),
            "qualifying": r.get("qualifying", 0), "new": r.get("new", 0), "already": r.get("already", 0),
            "failed": r.get("failed", 0), "skippedStarted": r.get("skipped", 0),
            "games": r.get("games", 0), "started": r.get("started", 0), "upcoming": r.get("upcoming", 0),
            "nextStartMs": nxt, "nextStartMT": (datetime.fromtimestamp(nxt / 1000.0, MT).strftime("%a %m-%d %I:%M %p MT") if nxt else None),
            "marginMin": (round((nxt - now_ms) / 60000.0) if nxt else None),
            "noPriceGames": len(r.get("noPrice") or []),
            # Red flag: this pass refused qualifying legs because their game had already started / was about to.
            "late": bool(r.get("skipped")),
        })
    return out


def owner_alert_for(reports: list[dict]) -> tuple[str, str, str] | None:
    """-> (hash, subject, html) for the OWNER-ONLY pre-kickoff alert, or None. Raised when a LIVE pass found qualifying legs it
    could NOT lock before kickoff (refused by the start guard: the game already started / starts within LOCK_START_MARGIN_MIN) or
    legs that failed to lock. Those are exactly the picks that would otherwise need a manual lock -- listed so the owner knows
    whether manual action is needed. Never goes to subscribers. Deduped by the hash of the leg set (see main())."""
    items: list[dict] = []
    fails: list[str] = []
    for r in reports:
        if not r.get("live"):
            continue
        items += [dict(d, pass_label=r.get("label")) for d in (r.get("skippedDetail") or [])]
        fails += [f"{r.get('label')}: {x}" for x in (r.get("failedLabels") or [])]
    if not items and not fails:
        return None
    import hashlib
    key = "|".join(sorted(f"{i.get('sport')}:{i.get('game')}:{i.get('leg')}" for i in items) + sorted(fails))
    h = hashlib.sha1(key.encode()).hexdigest()[:12]
    now_mt = datetime.now(MT).strftime("%a %m-%d %I:%M %p MT")
    rows = []
    for i in sorted(items, key=lambda d: d.get("startMs") or 0):
        st = i.get("startMs")
        st_txt = datetime.fromtimestamp(st / 1000.0, MT).strftime("%a %I:%M %p MT") if st else "start unknown"
        rows.append(f"<li><strong>{_esc(i.get('sport'))}</strong> {_esc(i.get('game'))} — {_esc(i.get('leg'))} "
                    f"({_esc(i.get('tier'))}, {(i.get('prob') or 0) * 100:.0f}%) — kickoff {st_txt} — <em>{_esc(i.get('why'))}</em></li>")
    for f in fails:
        rows.append(f"<li><strong>LOCK FAILED</strong> {_esc(f)}</li>")
    n = len(items) + len(fails)
    html = (f"{_EMAIL_WRAP_OPEN}"
            f'<div style="font-size:16px;color:#ff9090;font-weight:700">⚠ {n} qualifying pick(s) NOT locked before kickoff</div>'
            f'<div style="margin-top:10px;font-size:14px;color:#ccc">A lock pass landed at {now_mt} and found these legs qualifying, '
            f"but could not lock them (game already started or within {LOCK_START_MARGIN_MIN} min of start, or the lock failed). "
            f"No subscriber email was sent for them. If you still want any of these, lock them manually in the app -- they are "
            f"stamped 'late-manual' and excluded from every published figure and from model learning."
            f'<ul style="margin:8px 0 0;padding-left:18px">{"".join(rows)}</ul></div>{_EMAIL_WRAP_CLOSE}')
    return h, f"Clairvoyance — ACTION: {n} qualifying pick(s) not locked before kickoff", html


def send_owner_alert(subject: str, html: str) -> bool:
    """OWNER-ONLY email (OWNER_EMAIL / LOCKS_EMAIL_TO / SOCIAL_CARD_EMAIL_TO) -- never any subscriber list."""
    to = OWNER_EMAIL or LOCKS_EMAIL_TO
    if not to:
        log("owner alert: no OWNER_EMAIL/LOCKS_EMAIL_TO configured -- not sent")
        return False
    ok, msg = _send_gmail(subject, to, html)
    log("Owner alert sent" if ok else f"Owner alert send failed: {msg}")
    return bool(ok)


def finish_live_pass(status_key: str, ok: bool, detail: str, reports: list[dict], result_file: str | None) -> None:
    """Everything a LIVE lock pass does when it is over: writes the pass result file (the workflow reads `complete` from it to
    decide whether to record the lock marker), appends the Engine Health pass entries, writes the lastX status, and sends the
    owner-only pre-kickoff alert (deduped). Dry runs never reach here (nothing real happened)."""
    complete = bool(reports) and all(r.get("complete") for r in reports) and ok
    if result_file:
        try:
            Path(result_file).write_text(json.dumps({"complete": complete, "ok": ok, "reports": reports}, default=str))
        except Exception as exc:
            log(f"could not write result file {result_file}: {exc}")
    alert = owner_alert_for(reports)
    sent_hash = None
    if alert:
        h, subject, html = alert
        try:
            seen = json.loads((ROOT / "docs" / "automation_status.json").read_text()).get("ownerAlerts", {})
        except Exception:
            seen = {}
        if h in seen:
            log(f"owner alert {h} already sent ({seen[h]}) -- not repeating")
        elif send_owner_alert(subject, html):
            sent_hash = h
    write_automation_status(status_key, ok, detail, passes=pass_entries(status_key, reports), owner_alert_hash=sent_hash)


def run_adaptive_recalibration(page, live: bool) -> None:
    """Runs the real adaptiveTick() calibration against the full real
    settled-bet history and persists the resulting ensemble weights to
    docs/adaptive_weights.json (git-committed). Durable fix for a real
    gap: adaptiveTick() only ever wrote its learned weights to THIS
    browser's own localStorage -- meaningless for the automated lock
    pipeline, which runs a fresh headless session (no persisted
    localStorage) on every single invocation, so none of that learning
    ever reached the picks actually locked and sold. docs/app.html's own
    boot sequence now fetches this same file before falling back to the
    hardcoded literal defaults, so every fresh session -- including the
    automated one -- starts from real learned state, nudged further each
    time this recalibration re-runs against the latest settled results."""
    log("=== ADAPTIVE RECALIBRATION ===")
    result = page.evaluate(
        """
        async () => {
          if (typeof adaptiveTick !== 'function') return null;
          // 2026-10-02: calibration/learning inputs exclude picks locked after the game started (late manual locks -- see
          // docs/app.html _calEligible). That needs the schedule-derived lock-timing index, which loads asynchronously;
          // renderOverall()/adaptiveTick() are synchronous, so load it first. If it can't load, the 4 European hockey
          // leagues are withheld (unknown timing) rather than learned from blindly.
          try { if (typeof _tbLoad === 'function') await _tbLoad(); } catch (e) {}
          const state = adaptiveTick();
          // renderOverall() is what actually computes+sets
          // window.__CV_CAL_ADJ_BY_SPORT (the per-sport probability-band
          // calibration -- see docs/app.html's own comment on this, added
          // 2026-09-02) -- this headless session never visits the Overall
          // tab under normal operation (everything here drives specific
          // functions directly, not real UI navigation), so nothing would
          // ever compute it otherwise. Safe to call directly: it only
          // needs #ovr-dashboard to exist in the DOM, which it always
          // does regardless of which tab is visually active.
          try{ if(typeof renderOverall==='function') renderOverall(); }catch(e){}
          return {
            state,
            // Raw ensemble objects, not state.weights' flattened sub-objects --
            // NHL_ENS carries an extra `hv` field state.weights.NHL doesn't
            // capture, and overwriting NHL_ENS with an incomplete object at
            // boot would silently drop it.
            raw: {ens: ENS, nhl_ens: NHL_ENS, nba_ens: NBA_ENS, wnba_ens: WNBA_ENS},
            calAdjBySport: window.__CV_CAL_ADJ_BY_SPORT || null,
            calExcl: window.__CV_CAL_EXCL || null,
          };
        }
        """
    )
    if not result:
        log("adaptiveTick() unavailable on this page -- skipping")
        return
    state = result.get("state") or {}
    raw = result.get("raw") or {}
    cal_adj_by_sport = result.get("calAdjBySport")
    cal_excl = result.get("calExcl")
    if cal_excl:
        log(f"  calibration inputs: {cal_excl.get('used')} of {cal_excl.get('settled')} settled picks used, "
            f"{cal_excl.get('excluded')} withheld (locked after game start / unknown-timing euro hockey): "
            f"{cal_excl.get('byLeague')}; unknown-timing kept: {cal_excl.get('unknownKept')}; "
            f"schedule index loaded: {cal_excl.get('idxLoaded')}")
    if state.get("status") == "INSUFFICIENT_DATA":
        log(f"Skipping: {state.get('msg')}")
        return

    log(f"Recalibrated on {state.get('betsAnalyzed')} settled bets (of {state.get('totalBets')} total) -- "
        f"overall {state.get('overallAcc', 0)*100:.1f}%, recent-10 {state.get('recentAcc', 0)*100:.1f}%")
    for sport, perf in sorted((state.get("sportPerf") or {}).items()):
        log(f"  {sport}: {perf.get('acc', 0)*100:.1f}% ({perf.get('wins')}W-{perf.get('losses')}L)")
    for rec in state.get("recommendations") or []:
        log(f"  [{rec.get('priority')}] {rec.get('action')}: {rec.get('detail')}")
    if cal_adj_by_sport:
        for sport, bands in sorted(cal_adj_by_sport.items()):
            if bands:
                log(f"  {sport} per-band calibration: " +
                    ", ".join(f"{float(k)*100:.0f}%+={v*100:+.1f}pp" for k, v in bands.items()))

    if not live:
        log("[DRY RUN] Would write docs/adaptive_weights.json (pass --live to write)")
        return

    snapshot = {
        "ens": raw.get("ens"),
        "nhl_ens": raw.get("nhl_ens"),
        "nba_ens": raw.get("nba_ens"),
        "wnba_ens": raw.get("wnba_ens"),
        "cal_adj_by_sport": cal_adj_by_sport,
        # What the calibration learned from: picks locked after the game started are withheld (docs/app.html _calEligible).
        "cal_inputs": cal_excl,
        "evt": state.get("evThreshold"),
        "betsAnalyzed": state.get("betsAnalyzed"),
        "overallAcc": state.get("overallAcc"),
        "generatedUTC": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = ROOT / "docs" / "adaptive_weights.json"
    try:
        path.write_text(json.dumps(snapshot, indent=2))
        _commit_and_push(["docs/adaptive_weights.json"], "chore: adaptive recalibration")
    except Exception as exc:
        log(f"run_adaptive_recalibration failed to write/push: {exc}")


# Real bug, found and fixed: every autoSettle*() function fetches its own
# scores directly from the browser via fetch() -- but site.api.espn.com and
# api-web.nhle.com both reject requests from this exact headless Chromium
# session outright (confirmed directly: 3 different endpoints across 2
# hosts, 100% "Failed to fetch" at the network level, while a neutral host
# like api.github.com succeeds fine from the same page in the same run --
# this isn't a sandbox network restriction, it's these two hosts
# fingerprinting and rejecting the automated browser itself, matching the
# identical, already-documented block on the LOCK side -- see gather_legs()'s
# own comment on this same issue for ESPN). That means every autoSettle*()
# call in this pipeline has likely never had real data to work with, no
# matter what date-handling or betType logic sits downstream of it -- almost
# certainly the dominant reason bets have been piling up unsettled.
#
# requests (plain Python HTTP, no browser fingerprint at all) hits these
# same hosts successfully -- already proven throughout this codebase (see
# fetch_cfb.py, fetch_nfl.py). Rather than rewrite 9 separate JS fetch call
# sites, this transparently relays each blocked in-page fetch() through a
# real Python requests.get() to the exact same URL and hands the JS side
# back a normal-looking response -- every autoSettle*() function keeps
# working completely unchanged.
_ESPN_RELAY_HOSTS = ("site.api.espn.com", "api-web.nhle.com")


def _route_relay_espn(route) -> None:
    url = route.request.url
    if not any(h in url for h in _ESPN_RELAY_HOSTS):
        route.continue_()
        return
    try:
        r = requests.get(url, timeout=15)
        route.fulfill(status=r.status_code, content_type="application/json", body=r.text)
    except Exception as e:
        log(f"  [relay] {url} -> failed: {e}")
        route.fulfill(status=502, content_type="application/json", body="{}")


def install_espn_relay(page) -> None:
    """Route every in-page fetch to ESPN/NHL through a real Python request
    instead, since the browser's own network path to those two hosts is
    blocked (see _route_relay_espn's comment)."""
    page.route("**/*", _route_relay_espn)


# ── Ledger source + backup (rewritten 2026-10-03: Supabase free-tier egress hit 96% of its cycle) ──────────────────────────────────────────────
# One place decides where a CI session's ledger comes from and how the committed backup (docs/picks_backup.json + docs/picks_backup_meta.json) is kept
# current, consistent and usable when Supabase is unavailable:
#   FULL      complete Supabase pull (~3,900 rows, ~2-3 MB). Needed rarely: at most once per ~20h (meta.last_full_supabase_sync), after any degraded
#             period (reconcile), or when the backup is missing/unreadable.
#   HYBRID    committed backup as the base + only the last HYBRID_WINDOW_DAYS days and every pending pick from Supabase (a few hundred KB). Used by every
#             other run, so egress per run drops ~85-90%.
#   DEGRADED  Supabase could not be read at all (quota restriction, outage): the backup alone. Nothing reaches Supabase; at the end of the run the changes are
#             merged into the backup and committed, and meta.needs_reconcile is set so the next healthy run pushes them back (reconcile_from_backup).
# Every live run that changes the ledger writes the backup through a THREE-WAY merge (what this run loaded / what it ended with / newest on origin), so two
# overlapping workflows can never erase each other's locks, and a validation gate refuses to commit a backup that lost rows.
BACKUP_PATH = lambda: ROOT / "docs" / "picks_backup.json"      # noqa: E731  (lambdas: tests re-point ROOT)
META_PATH = lambda: ROOT / "docs" / "picks_backup_meta.json"   # noqa: E731
FULL_SYNC_MAX_AGE_H = 20
HYBRID_WINDOW_DAYS = 14
BACKUP_MIN_KEEP_RATIO = 0.97   # a new backup smaller than this share of origin's is refused (a bad pull must not shrink the safety net)

LEDGER_DEGRADED = False
LEDGER_MODE = "none"                    # none | full | hybrid | degraded
_FULL_PULL_THIS_RUN = False
_RECONCILED_THIS_RUN = False
_LEDGER_LOADED_AT: datetime | None = None
_DEGRADED_INITIAL: dict[str, str] = {}  # id -> canonical JSON of every pick as this run LOADED it (all modes; name kept for the tests)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _canon(p: dict) -> str:
    return json.dumps(p, sort_keys=True)


def _ids(preds) -> dict[str, dict]:
    return {p["id"]: p for p in preds if isinstance(p, dict) and p.get("id")}


def _read_meta(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or META_PATH()).read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _read_backup_file() -> list | None:
    try:
        d = json.loads(BACKUP_PATH().read_text())
        return d if isinstance(d, list) and d else None
    except Exception:
        return None


def _parse_iso(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _snapshot_initial(page) -> None:
    """Remember every pick exactly as this run loaded it, so the end-of-run backup write can tell what THIS run changed."""
    global _DEGRADED_INITIAL, _LEDGER_LOADED_AT
    preds = page.evaluate("() => getP()")
    _DEGRADED_INITIAL = {p["id"]: _canon(p) for p in (preds or []) if isinstance(p, dict) and p.get("id")}
    _LEDGER_LOADED_AT = _now_utc()


def _load_ledger_from_backup(page) -> int:
    """Degraded mode: the ledger is the committed backup (Supabase unreadable)."""
    global LEDGER_DEGRADED, LEDGER_MODE
    preds = _read_backup_file()
    if preds is None:
        raise RuntimeError("Supabase ledger unavailable AND docs/picks_backup.json missing/empty/unreadable")
    page.evaluate("(preds) => { saveP(preds); }", preds)
    LEDGER_DEGRADED = True
    LEDGER_MODE = "degraded"
    _snapshot_initial(page)
    log(f"WARNING: DEGRADED MODE -- Supabase ledger unavailable; loaded {len(preds)} bets from docs/picks_backup.json instead. "
        "Locks/settles this run are persisted by committing the merged backup, not to Supabase.")
    try:
        merge_manual_locks(page)
    except Exception as exc:
        log(f"WARNING: manual-lock relay merge failed: {exc}")
    return page.evaluate("() => getP().length")


_JS_FULL_PULL = """
async () => {
  const rows = []; let offset = 0; const page_size = 1000;
  while (true) {
    const r = await fetch(SUPABASE_URL + '/rest/v1/bets?select=raw&order=date.desc&outcome=neq._removed', {
      headers: { apikey: SUPABASE_KEY, Authorization: 'Bearer ' + SUPABASE_KEY, Range: offset + '-' + (offset + page_size - 1) } });
    if (!r.ok) return -1;
    const batch = await r.json(); rows.push(...batch);
    if (batch.length < page_size) break;
    offset += page_size;
  }
  const preds = rows.map(x => x.raw).filter(Boolean);
  saveP(preds);
  return preds.length;
}
"""

_JS_WINDOW_PULL = """
async (cutoff) => {
  const rows = []; let offset = 0; const page_size = 1000;
  while (true) {
    const r = await fetch(SUPABASE_URL + '/rest/v1/bets?select=id,outcome,raw&or=(date.gte.' + cutoff + ',outcome.eq.pending)&order=id', {
      headers: { apikey: SUPABASE_KEY, Authorization: 'Bearer ' + SUPABASE_KEY, Range: offset + '-' + (offset + page_size - 1) } });
    if (!r.ok) return null;
    const batch = await r.json(); rows.push(...batch);
    if (batch.length < page_size) break;
    offset += page_size;
  }
  return rows;
}
"""


def _hybrid_allowed(meta: dict) -> bool:
    last = _parse_iso(meta.get("last_full_supabase_sync"))
    if last is None or (_now_utc() - last) > timedelta(hours=FULL_SYNC_MAX_AGE_H):
        return False
    if meta.get("needs_reconcile"):
        return False
    return _read_backup_file() is not None


def _load_hybrid(page) -> int | None:
    """Backup as the base, Supabase only for the recent window + every pending pick (tombstones drop their id). None if Supabase failed."""
    base = _read_backup_file()
    cutoff = (datetime.now(ZoneInfo("America/Denver")) - timedelta(days=HYBRID_WINDOW_DAYS)).strftime("%Y-%m-%d")
    try:
        rows = page.evaluate(_JS_WINDOW_PULL, cutoff)
    except Exception as exc:
        log(f"Supabase window pull raised: {str(exc)[:200]}")
        rows = None
    if rows is None or base is None:
        return None
    merged = _ids(base)
    for r in rows:
        if r.get("outcome") == "_removed":
            merged.pop(r.get("id"), None)
        elif isinstance(r.get("raw"), dict) and r["raw"].get("id"):
            merged[r["raw"]["id"]] = r["raw"]
    page.evaluate("(preds) => { saveP(preds); }", list(merged.values()))
    return len(merged)


def load_bet_ledger(page, force_full: bool = False) -> int:
    """Put the real ledger into the page's getP()/saveP() store so every client function (settlement, dedup checks, sync) operates on it.
    See the block comment above for FULL / HYBRID / DEGRADED. Uses docs/app.html's own SUPABASE_URL/SUPABASE_KEY consts (the anon key, safe client-side by
    RLS design). Once degraded, later calls keep the in-page state rather than reloading the (older) backup over this run's changes."""
    global LEDGER_MODE, _FULL_PULL_THIS_RUN
    if LEDGER_DEGRADED:
        return page.evaluate("() => getP().length")
    meta = _read_meta()
    if not force_full and _hybrid_allowed(meta):
        n = _load_hybrid(page)
        if n is not None:
            LEDGER_MODE = "hybrid"
            _snapshot_initial(page)
            return n
        log("Hybrid (window) pull failed -- trying a full pull")
    try:
        count = page.evaluate(_JS_FULL_PULL)
    except Exception as exc:
        log(f"Supabase ledger pull raised: {str(exc)[:200]}")
        count = None
    if count is None or count < 0:
        return _load_ledger_from_backup(page)
    LEDGER_MODE = "full"
    _FULL_PULL_THIS_RUN = True
    _snapshot_initial(page)
    if meta.get("needs_reconcile"):
        try:
            reconcile_from_backup(page)
        except Exception as exc:
            log(f"WARNING: reconcile from backup failed (will retry next run): {exc}")
    return page.evaluate("() => getP().length")


def reconcile_from_backup(page) -> int:
    """Supabase is readable again after a degraded period: push what only the backup knows. A pick is pushed when it is missing from Supabase, or the
    backup has it settled while Supabase still has it pending. The page ledger is updated first (so the run continues on the merged truth), then ONLY
    those rows are upserted (id-keyed, return=minimal). Clears meta.needs_reconcile through the next backup write."""
    global _RECONCILED_THIS_RUN
    backup = _read_backup_file() or []
    # what Supabase itself holds (taken BEFORE anything is merged in) -- the comparison base for "missing" / "settled in the backup only"
    cur = {p["id"]: p for p in (page.evaluate("() => getP()") or []) if isinstance(p, dict) and p.get("id")}
    try:
        merge_manual_locks(page)           # manual locks made during the outage join the ledger and are pushed below
    except Exception as exc:
        log(f"WARNING: manual-lock relay merge failed: {exc}")
    # backup picks plus the relay's, so both get pushed
    try:
        backup = list(backup) + [p for p in ((json.loads((ROOT / "docs" / "manual_locks.json").read_text()) or {}).get("picks") or [])
                                 if isinstance(p, dict) and p.get("id") and p["id"] not in {b.get("id") for b in backup}]
    except Exception:
        pass
    push = []
    for p in backup:
        if not (isinstance(p, dict) and p.get("id")):
            continue
        c = cur.get(p["id"])
        if c is None or (c.get("outcome") == "pending" and p.get("outcome") not in (None, "pending")):
            push.append(p)
    if push:
        page.evaluate("(ps) => { const m = new Map(getP().map(x => [x.id, x])); ps.forEach(x => m.set(x.id, x)); saveP(Array.from(m.values())); }", push)
        ok = page.evaluate(
            """
            async (ids) => {
              const rows = getP().filter(p => ids.includes(p.id)).map(_supabaseBetRow);
              for (let i = 0; i < rows.length; i += 200) {
                const r = await fetch(SUPABASE_URL + '/rest/v1/bets', { method: 'POST',
                  headers: { apikey: SUPABASE_KEY, Authorization: 'Bearer ' + SUPABASE_KEY, 'Content-Type': 'application/json',
                             Prefer: 'resolution=merge-duplicates,return=minimal' },
                  body: JSON.stringify(rows.slice(i, i + 200)) });
                if (!r.ok) return false;
              }
              return true;
            }
            """,
            [p["id"] for p in push],
        )
        if not ok:
            raise RuntimeError("upsert of backup-only picks to Supabase failed")
    _RECONCILED_THIS_RUN = True
    log(f"RECONCILE: Supabase is back -- pushed {len(push)} pick(s) that only the backup had")
    return len(push)


def merge_manual_locks(page) -> int:
    """docs/manual_locks.json is written by the owner's browser (manual-lock relay in app.html) while Supabase is unreachable. Used ONLY in degraded mode and
    during the reconcile after one (in healthy mode the app pushes manual locks to Supabase itself, and a pick the owner later removed there must not be
    resurrected from this file). Adds picks the ledger lacks, and takes the relay's settled result over a pending ledger copy."""
    try:
        picks = (json.loads((ROOT / "docs" / "manual_locks.json").read_text()) or {}).get("picks") or []
    except Exception:
        return 0
    cur = {p["id"]: p for p in (page.evaluate("() => getP()") or []) if isinstance(p, dict) and p.get("id")}
    add = []
    for p in picks:
        if not (isinstance(p, dict) and p.get("id")):
            continue
        c = cur.get(p["id"])
        if c is None or (c.get("outcome") == "pending" and p.get("outcome") not in (None, "pending")):
            add.append(p)
    if add:
        page.evaluate("(ps) => { const m = new Map(getP().map(x => [x.id, x])); ps.forEach(x => m.set(x.id, x)); saveP(Array.from(m.values())); }", add)
        log(f"Manual-lock relay: merged {len(add)} manual lock(s) from docs/manual_locks.json into the ledger")
    return len(add)


def _read_origin_state() -> tuple[list | None, dict]:
    """Newest committed backup + meta on origin/main (another workflow may have refreshed them since this run started)."""
    try:
        subprocess.run(["git", "-C", str(ROOT), "fetch", "origin", "main"], capture_output=True, timeout=60)
        b = subprocess.run(["git", "-C", str(ROOT), "show", "origin/main:docs/picks_backup.json"], capture_output=True, text=True, timeout=60)
        m = subprocess.run(["git", "-C", str(ROOT), "show", "origin/main:docs/picks_backup_meta.json"], capture_output=True, text=True, timeout=60)
        preds = json.loads(b.stdout) if b.returncode == 0 else None
        meta = json.loads(m.stdout) if m.returncode == 0 else {}
        return (preds if isinstance(preds, list) and preds else None), (meta if isinstance(meta, dict) else {})
    except Exception:
        return None, {}


def _three_way_merge(final: list, origin: list | None, origin_newer: bool) -> list:
    """final = this run's ledger, _DEGRADED_INITIAL = what it loaded, origin = newest committed backup.
    A pick this run added or changed always takes this run's version. A pick it did NOT touch takes origin's version when origin was written after this run
    loaded its ledger (someone else's newer change), otherwise this run's (Supabase truth, which can be newer than a stale backup).
    A pick that only origin has is kept -- unless this run did a COMPLETE Supabase pull and origin is not newer than that pull: then Supabase no longer has
    it (the owner removed it), so it is dropped; otherwise a removed pick would live in the backup forever."""
    f = _ids(final)
    if origin is None:
        return list(f.values())
    o = _ids(origin)
    authoritative = _FULL_PULL_THIS_RUN and not LEDGER_DEGRADED
    out: dict[str, dict] = {}
    for pid in list(o) + [k for k in f if k not in o]:
        mine = f.get(pid)
        if mine is None:
            if not (authoritative and not origin_newer):
                out[pid] = o[pid]
            continue
        touched = _DEGRADED_INITIAL.get(pid) != _canon(mine)
        if touched:
            out[pid] = mine
        elif origin_newer and pid in o:
            out[pid] = o[pid]
        else:
            out[pid] = mine
    return list(out.values())


def _validate_backup(preds: list, origin: list | None) -> str | None:
    """None if the backup is safe to commit, else the reason it is not."""
    if not preds:
        return "empty"
    ids = [p.get("id") if isinstance(p, dict) else None for p in preds]
    if any(i is None for i in ids):
        return "a pick without an id"
    if len(set(ids)) != len(ids):
        return "duplicate ids"
    if any(not isinstance(p.get("date"), str) or not p.get("outcome") for p in preds):
        return "a pick missing date/outcome"
    if origin and len(preds) < BACKUP_MIN_KEEP_RATIO * len(origin):
        return f"would shrink the backup from {len(origin)} to {len(preds)} picks"
    return None


def write_ledger_backup(page) -> int:
    """docs/picks_backup.json (+ docs/picks_backup_meta.json): a real, current backup of the ledger, independent of Supabase, read by app.html's
    loadPicksFromBackupJSON() when Supabase is unreachable and used as the base of every HYBRID CI load. Written through a three-way merge with origin's
    newest copy (see the block comment above), validated before it replaces anything, sorted by id so diffs stay small, and the meta file records where
    the ledger came from (full / hybrid / degraded), when the last COMPLETE Supabase pull happened, and whether a degraded period still has to be pushed
    back to Supabase (needs_reconcile). Committed by the workflow right after this call (main run) or by persist_ledger_backup (lock-only passes).
    Returns the pick count now on disk."""
    final = page.evaluate("() => getP()")
    origin, ometa = _read_origin_state()
    old_meta = _read_meta()
    origin_gen = _parse_iso(ometa.get("generated_at"))
    origin_newer = bool(origin_gen and _LEDGER_LOADED_AT and origin_gen > _LEDGER_LOADED_AT)
    merged = _three_way_merge(final, origin, origin_newer)
    reason = _validate_backup(merged, origin)
    if reason:
        log(f"ERROR: ledger backup NOT written ({reason}) -- keeping the committed copy")
        return len(origin or [])
    merged.sort(key=lambda p: str(p.get("id")))
    body = json.dumps(merged, indent=2)
    path = BACKUP_PATH()
    changed = (not path.exists()) or path.read_text() != body
    if changed:
        path.write_text(body)
    base_meta = ometa or old_meta
    meta = {
        "count": len(merged),
        "source": LEDGER_MODE,
        "last_full_supabase_sync": _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ") if _FULL_PULL_THIS_RUN else base_meta.get("last_full_supabase_sync"),
        "needs_reconcile": True if LEDGER_DEGRADED else (False if _RECONCILED_THIS_RUN else bool(base_meta.get("needs_reconcile", False))),
        "last_degraded_at": _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ") if LEDGER_DEGRADED else base_meta.get("last_degraded_at"),
        "last_reconcile_at": _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ") if _RECONCILED_THIS_RUN else base_meta.get("last_reconcile_at"),
    }
    strip = lambda d: {k: v for k, v in d.items() if k != "generated_at"}  # noqa: E731
    if changed or strip(meta) != strip(old_meta):
        meta["generated_at"] = _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        META_PATH().write_text(json.dumps(meta, indent=2) + "\n")
    return len(merged)


def _ledger_changed(page) -> bool:
    cur = {p["id"]: _canon(p) for p in (page.evaluate("() => getP()") or []) if isinstance(p, dict) and p.get("id")}
    return cur != _DEGRADED_INITIAL


def persist_ledger_backup(page, commit: bool) -> bool:
    """End-of-run: write the backup if this run changed the ledger, did a complete pull, ran degraded or reconciled; commit it when `commit` (every pass
    except the main workflow, whose own step commits the files). Keeps the backup current after EVERY lock pass, not only the main workflow's."""
    if not (LEDGER_DEGRADED or _FULL_PULL_THIS_RUN or _RECONCILED_THIS_RUN or _ledger_changed(page)):
        return False
    n = write_ledger_backup(page)
    log(f"Ledger backup updated ({n} bets, source={LEDGER_MODE})")
    if commit:
        _commit_and_push(["docs/picks_backup.json", "docs/picks_backup_meta.json"],
                         f"chore: ledger backup ({LEDGER_MODE}, {n} bets)")
    return True


def ledger_fingerprint(page) -> str:
    """Hash of the page's whole in-memory ledger (getP()), to detect whether a pass changed it. No network."""
    import hashlib
    raw = page.evaluate("() => JSON.stringify(getP())")
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def write_landing_performance(page) -> None:
    """Same 3 JSON files landing-performance-refresh.yml used to produce
    via its OWN fully separate browser launch + Supabase pull -- folded
    in here (2026-09-15) to close a real self-inflicted regression: when
    that refresh was first folded into this workflow as an extra step
    (2026-09-12), it kept its own independent generate_social_cards.py
    --json-only invocation, which doubled this workflow's real Supabase
    egress (two full-ledger pulls per run instead of one) at exactly the
    frequency that contributed to blowing through the account's egress
    quota. Calling the same get_engine_performance*/get_sport_performance
    functions directly against THIS run's already-loaded page removes
    the second pull entirely -- one Supabase read per invocation, not
    two, for the exact same 3 output files."""
    # 2026-10-02: the 3 JSONs are PRE-START LOCKS ONLY (known-late manual locks excluded, with a machine-readable basis in
    # each file) -- written by the one shared writer in generate_social_cards.py so run() there and this call cannot diverge.
    _write_landing_json(page, datetime.now(ZoneInfo("America/Denver")))


def flush_to_supabase(page) -> None:
    """Awaits _syncBetsToSupabaseNow() -- the same pull-then-upload logic
    the app's own debounced syncBetsToSupabase() uses, but bypassing the
    debounce and actually awaited here instead of guessed at with a fixed
    timeout. Real bug, found and fixed: this used to fire the DEBOUNCED
    syncBetsToSupabase() (arms a 2.5s setTimeout and returns immediately)
    then blind-wait 4000ms -- but the sync's own "pull latest first" step
    is a paginated read of the whole bets table (2500+ rows = 3 round
    trips) before the upload even starts, which routinely took longer
    than 4s total. Confirmed live 2026-08-29: a CFB lock run logged
    "Locked 16/18 legs" + "Flushed locks to Supabase" and still showed 0
    of them in an immediate verify pull -- a race, not a write failure.
    Awaiting the real completion here closes that gap."""
    page.evaluate("async () => { await _syncBetsToSupabaseNow(); }")


# ─────────────────────────────────────────────────────────────────────────
# SETTLE
# ─────────────────────────────────────────────────────────────────────────
def run_settle(page, live: bool, only_dates: list[str] | None = None) -> list[dict]:
    """only_dates: settle exactly these date(s) instead of every distinct
    pending date -- used for the once-daily settlement digest's final,
    targeted check on yesterday specifically (see
    send_daily_settlement_digest). None (the default) keeps the normal
    intraday behavior: backfill every distinct pending date found."""
    log("=== AUTO-SETTLE ===" if only_dates is None else f"=== AUTO-SETTLE (targeted: {only_dates}) ===")
    before = page.evaluate("() => getP().filter(p => p.outcome === 'pending').length")
    log(f"Pending bets before settle: {before}")

    # Real bug, found and fixed: every autoSettle*() function used to be
    # hardcoded to "today" only -- either an explicit `p.date !== today()`
    # filter, or (WNBA/soccer/tennis/props) no date check at all, relying
    # on ESPN's scoreboard defaulting to today when no dates= param is
    # given. Either way, a bet that missed settlement on its own calendar
    # day (a late West Coast night game, a missed run, an API hiccup)
    # became PERMANENTLY unsettleable -- confirmed via a real backlog of
    # pending bets up to ~2 months old. Every autoSettle*() function now
    # takes an optional targetDate and actually uses it in its fetch URL
    # (dates=, confirmed directly against ESPN's real API to return real
    # historical events for a past date). This loop drives that: instead
    # of one pass for "today", it runs one full settlement pass per
    # DISTINCT date that actually has a pending bet -- self-limiting by
    # design, since once the backlog clears there are normally only 1-2
    # recent dates to check per run, not an ever-growing blind lookback
    # window.
    result = page.evaluate(
        """
        async (onlyDates) => {
          const results = {perDate: {}};
          const pendingDates = onlyDates || [...new Set(
            getP().filter(p => p.outcome === 'pending').map(p => p.date).filter(Boolean)
          )].sort();
          results.datesChecked = pendingDates.length;
          for (const targetDate of pendingDates) {
            const dEspn = targetDate.replace(/-/g, '');
            const r = {};
            try { if (typeof autoSettleNBA === 'function') await autoSettleNBA(targetDate); r.nba = 'ok'; } catch (e) { r.nba = 'err:' + e.message; }
            try { if (typeof autoSettleNHL === 'function') await autoSettleNHL(targetDate); r.nhl = 'ok'; } catch (e) { r.nhl = 'err:' + e.message; }
            try { if (typeof autoSettleCFB === 'function') await autoSettleCFB(targetDate); r.cfb = 'ok'; } catch (e) { r.cfb = 'err:' + e.message; }
            try { if (typeof autoSettleSoccer === 'function') await autoSettleSoccer(targetDate); r.soccer = 'ok'; } catch (e) { r.soccer = 'err:' + e.message; }
            try { if (typeof autoSettleNFL2 === 'function') await autoSettleNFL2(targetDate); r.nfl = 'ok'; } catch (e) { r.nfl = 'err:' + e.message; }
            try { if (typeof autoSettlePropsESPN === 'function') await autoSettlePropsESPN(targetDate); r.props = 'ok'; } catch (e) { r.props = 'err:' + e.message; }
            // Real gap, found and fixed 2026-09-16: LIIGA/SHL had no
            // settlement path at all (this loop never called them),
            // meaning a locked pick for either league sat pending forever
            // unless someone manually hit WIN/LOSS. autoSettleLiiga/
            // autoSettleShl read the same docs/liiga_schedule.json/
            // shl_schedule.json Flashscore results the schedule browser
            // already uses (same-origin, no ESPN coverage exists for
            // these leagues at all). autoSettleNCAAH settles the same
            // live-ESPN-scoreboard way autoSettleCFB does -- NCAAH's own
            // schedule already comes from ESPN, not Flashscore -- though
            // note its game card (_genericGameCard) has no lock button
            // yet, so there is nothing for this to actually grade until a
            // real NCAAH pick-locking path exists; wired in now so
            // settlement is ready the moment that changes.
            try { if (typeof autoSettleLiiga === 'function') await autoSettleLiiga(targetDate); r.liiga = 'ok'; } catch (e) { r.liiga = 'err:' + e.message; }
            try { if (typeof autoSettleShl === 'function') await autoSettleShl(targetDate); r.shl = 'ok'; } catch (e) { r.shl = 'err:' + e.message; }
            // NLA/Extraliga, 2026-09-23 -- same settlement path as Liiga/
            // SHL (Flashscore results, see autoSettleNla/autoSettleExtraliga
            // in docs/app.html); personal-use-only for locking/emailing
            // purposes (EARLY_HOCKEY_SPORTS_PERSONAL) but settlement itself
            // is safe to run unconditionally -- it only resolves win/loss/
            // push on whatever's already locked, no billing implication.
            try { if (typeof autoSettleNla === 'function') await autoSettleNla(targetDate); r.nla = 'ok'; } catch (e) { r.nla = 'err:' + e.message; }
            try { if (typeof autoSettleExtraliga === 'function') await autoSettleExtraliga(targetDate); r.extraliga = 'ok'; } catch (e) { r.extraliga = 'err:' + e.message; }
            try { if (typeof autoSettleNCAAH === 'function') await autoSettleNCAAH(targetDate); r.ncaah = 'ok'; } catch (e) { r.ncaah = 'err:' + e.message; }
            results.perDate[targetDate] = r;
          }
          return results;
        }
        """,
        only_dates,
    )
    log(f"Settlement dates checked: {result.get('datesChecked')} -> {sorted(result.get('perDate', {}).keys())}")

    after = page.evaluate("() => getP().filter(p => p.outcome === 'pending').length")
    settled_count = before - after
    log(f"Pending bets after settle: {after} ({settled_count} newly settled)")

    newly: list[dict] = []
    if settled_count > 0:
        newly = page.evaluate(
            """
            () => getP().filter(p => p.outcome !== 'pending' && p.settledAt && (Date.now() - p.settledAt) < 180000)
              .map(p => ({
                betOn: p.betOn, sport: p.sport, league: p.league, outcome: p.outcome, date: p.date,
                hA: p.hA, awA: p.awA, hScore: p.hScore, aScore: p.aScore,
                playerResult: p.playerResult,
              }))
            """
        )
        for b in newly:
            log(f"  SETTLED: [{b.get('sport')}] {b.get('betOn')} -> {(b.get('outcome') or '').upper()}")

    if settled_count > 0 and live:
        flush_to_supabase(page)
        log("Flushed settlement results to Supabase")
    elif settled_count > 0:
        log("[DRY RUN] Would sync these settlements to Supabase (pass --live to write)")

    return newly


def _esc(s) -> str:
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# _EMAIL_WRAP_OPEN/_EMAIL_WRAP_CLOSE imported from _gmail_email above --
# shared across every script that sends mail (see that module for why),
# not just the two locks/settlement emails built here.
_OUTCOME_COLOR = {"win": "#00e676", "loss": "#ff3b5c", "push": "#ffb300"}


def _settle_result_html(b: dict) -> str:
    """One result row per settled bet: what happened (score for game bets,
    actual stat value for props) alongside the win/loss/push outcome, so
    the email doubles as a same-day reference for picks that have now
    completed."""
    outcome = (b.get("outcome") or "").lower()
    color = _OUTCOME_COLOR.get(outcome, "#999")
    if b.get("hScore") is not None and b.get("aScore") is not None:
        result = f"{_esc(b.get('hA'))} {b['hScore']} – {_esc(b.get('awA'))} {b['aScore']}"
    elif b.get("playerResult") is not None:
        result = f"actual: {_esc(b['playerResult'])}"
    else:
        result = "no score/result captured"
    # Real bug, found and fixed: this never showed which calendar date a
    # result actually happened on. Harmless back when a settle run only
    # ever processed "today" -- became actively misleading once run_settle
    # started backfilling every distinct pending date in one pass (needed
    # to actually clear a real backlog), since a single run's email can
    # now legitimately contain results from many different real dates
    # while its own subject line still named just one. Confirmed live:
    # a settlement email dated 2026-08-28 included games that never
    # happened on 8/28 at all -- this label is the fix.
    date_label = f' <span style="color:#666;font-size:12px">[{_esc(b.get("date") or "?")}]</span>' if b.get("date") else ""
    return (f'<div style="padding:5px 0;font-size:14px;color:#eee">'
            f'<span style="background:{color};color:#000;font-weight:700;font-size:11px;padding:1px 7px;'
            f'border-radius:3px;margin-right:8px;text-transform:uppercase">{_esc(outcome)}</span>'
            f'{_esc(b.get("betOn"))} <span style="color:#bbb">({result})</span>{date_label}</div>')


def build_settlement_email_html(settled: list[dict], unresolved: list[dict] | None = None, late_excluded: int = 0) -> str:
    by_sport: dict[str, list[dict]] = {}
    for b in settled:
        by_sport.setdefault(norm_sport_for_email(b.get("sport"), b.get("league")), []).append(b)

    wins = sum(1 for b in settled if b.get("outcome") == "win")
    losses = sum(1 for b in settled if b.get("outcome") == "loss")
    pushes = sum(1 for b in settled if b.get("outcome") not in ("win", "loss"))
    record = f"{wins}W-{losses}L" + (f"-{pushes}P" if pushes else "")
    parts = [_EMAIL_WRAP_OPEN,
              f'<div style="font-size:12px;letter-spacing:1px;color:#555;text-transform:uppercase">'
              f'{len(settled)} bets settled — {record}</div>']
    # Surfacing unresolved bets directly in the email (rather than just
    # silently omitting them) is the whole point of running a final,
    # targeted settle pass right before building this -- if something is
    # STILL pending after that, that's a real gap worth knowing about the
    # same morning, not discovering days later that a pick silently never
    # got graded.
    if unresolved:
        parts.append(f'<div style="margin-top:10px;padding:10px 14px;background:#3a0000;border:1px solid #ff3b5c;'
                      f'border-radius:6px;color:#ff9090;font-size:13px">'
                      f'⚠ {len(unresolved)} pick(s) from this date could not be confirmed settled yet -- '
                      f'real score/result not found. Will keep retrying automatically.<ul style="margin:6px 0 0;padding-left:18px">'
                      + "".join(f'<li>{_esc(SPORT_DISPLAY_NAME.get(norm_sport_for_email(b.get("sport"), b.get("league")), norm_sport_for_email(b.get("sport"), b.get("league"))))}: {_esc(b.get("betOn"))}</li>' for b in unresolved)
                      + '</ul></div>')
    # Pre-start-locks-only basis (2026-10-02): picks KNOWN to have been locked after their game started (manual late locks) are
    # not predictions and were never sent to subscribers pre-game -- they are left out of the lists and the totals above, and
    # only COUNTED here (no names), so the record in this email matches every published figure.
    late_note = (f'<div style="margin-top:14px;font-size:12px;color:#777">Pre-start locks only: {late_excluded} pick(s) locked '
                 f'after their game started are not counted or listed.</div>') if late_excluded else ""
    if not settled:
        parts.append('<div style="padding:20px 0;color:#555;font-size:14px">No bets settled this run.</div>')
        parts.append(late_note)
        parts.append(_EMAIL_WRAP_CLOSE)
        return "".join(parts)

    for sport in sorted(by_sport, key=lambda s: SPORT_DISPLAY_NAME.get(s, s)):
        bets = by_sport[sport]
        sw = sum(1 for b in bets if b.get("outcome") == "win")
        sl = sum(1 for b in bets if b.get("outcome") == "loss")
        parts.append(f'<div style="font-size:13px;letter-spacing:2px;color:#0090a8;text-transform:uppercase;'
                      f'margin:22px 0 10px;border-bottom:1px solid rgba(0,144,168,.3);padding-bottom:4px">'
                      f'{_esc(SPORT_DISPLAY_NAME.get(sport, sport))} ({sw}W-{sl}L)</div>')
        parts.append('<div style="background:#14001f;border-radius:6px;padding:10px 14px">')
        parts.append("".join(_settle_result_html(b) for b in bets))
        parts.append('</div>')
    parts.append(late_note)
    parts.append(_EMAIL_WRAP_CLOSE)
    return "".join(parts)


def send_settlement_email(settled: list[dict], live: bool, forced_date: str | None = None,
                           unresolved: list[dict] | None = None, late_excluded: int = 0) -> None:
    """forced_date: used by the once-daily digest (see
    send_daily_settlement_digest) to assert exactly which date this email
    covers, rather than inferring it from whatever settled this run --
    the digest is always scoped to exactly one date (yesterday, MT) by
    design, so this should always be set from that caller. Left optional
    only so any other/future caller without a fixed date still gets a
    sane subject line instead of a crash."""
    if not LOCKS_EMAIL_TO:
        log("LOCKS_EMAIL_TO not set — skipping settlement email")
        return
    if not settled and not unresolved:
        log("Nothing settled this run — skipping settlement email")
        return
    body_html = build_settlement_email_html(settled, unresolved=unresolved, late_excluded=late_excluded)
    if forced_date:
        date_str = forced_date
    else:
        # Real bug, found and fixed: this always used TODAY's date
        # regardless of which real date(s) the settled bets actually came
        # from -- always correct back when a settle run only ever
        # processed "today", wrong as soon as run_settle started
        # backfilling a real multi-date backlog in one pass. Reflect the
        # actual distinct dates covered instead of asserting a single
        # date that may not match any of the games listed.
        bet_dates = sorted({b["date"] for b in settled if b.get("date")})
        if len(bet_dates) <= 1:
            date_str = bet_dates[0] if bet_dates else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        elif len(bet_dates) == 2:
            date_str = f"{bet_dates[0]} & {bet_dates[1]}"
        else:
            date_str = f"{bet_dates[0]}..{bet_dates[-1]} ({len(bet_dates)} dates)"
    wins = sum(1 for b in settled if b.get("outcome") == "win")
    losses = sum(1 for b in settled if b.get("outcome") == "loss")
    prefix = "" if live else "[DRY RUN] "
    unresolved_note = f" · {len(unresolved)} unresolved" if unresolved else ""
    subject = f"Clairvoyance — {prefix}Settled {date_str}: {wins}W-{losses}L ({len(settled)}){unresolved_note}"
    ok, msg = _send_gmail(subject, LOCKS_EMAIL_TO, body_html)
    log(f"Settlement email sent to {LOCKS_EMAIL_TO}" if ok else f"Settlement email send failed: {msg}")


def send_daily_settlement_digest(page, live: bool) -> None:
    """Once-daily digest covering EXACTLY yesterday's (America/Denver)
    locked picks -- decoupled from the frequent intraday settle passes,
    which must keep running often to actually mark bets win/loss as soon
    as real results are available, but must never each fire their own
    email (a backlog-clearing pass can span many dates at once, which
    would read as a confusing multi-date dump rather than a clean daily
    report). Runs one final, targeted settle pass for yesterday's date
    specifically first -- the "multiple checks" pass -- so this report
    reflects the most complete, correct picture available: anything the
    day's earlier intraday passes might have missed gets one more real
    chance to resolve before the email goes out, and anything that STILL
    can't be confirmed gets called out explicitly in the email itself
    (see build_settlement_email_html's unresolved section) instead of
    just silently vanishing from the report."""
    yesterday = (datetime.now(ZoneInfo("America/Denver")) - timedelta(days=1)).strftime("%Y-%m-%d")
    log(f"=== DAILY SETTLEMENT DIGEST for {yesterday} ===")
    run_settle(page, live, only_dates=[yesterday])

    rows = page.evaluate(
        """
        (targetDate) => getP().filter(p => p.date === targetDate)
          .map(p => ({
            id: p.id, betOn: p.betOn, sport: p.sport, league: p.league, outcome: p.outcome, date: p.date,
            hA: p.hA, awA: p.awA, hScore: p.hScore, aScore: p.aScore,
            playerResult: p.playerResult,
            lockedAt: p.lockedAt, startMs: p.startMs, lockTiming: p.lockTiming,
          }))
        """,
        yesterday,
    )
    # Pre-start-locks-only (2026-10-02): drop picks KNOWN to be locked after their game started (shared classifier). They stay
    # in the ledger; they are just not listed or counted in this email, so its record matches every published figure.
    rows, late_rows = lock_timing.split_picks(rows, lock_timing.load_index())
    if late_rows:
        log(f"Digest for {yesterday}: {len(late_rows)} known-late (locked after game start) pick(s) left out of the email")
    settled = [r for r in rows if r.get("outcome") != "pending"]
    unresolved = [r for r in rows if r.get("outcome") == "pending"]
    if unresolved:
        log(f"WARNING: {len(unresolved)} bet(s) from {yesterday} still unresolved after final digest check: "
            + ", ".join(r.get("betOn") or "?" for r in unresolved))
    log(f"Digest for {yesterday}: {len(settled)} settled, {len(unresolved)} unresolved")
    send_settlement_email(settled, live, forced_date=yesterday, unresolved=unresolved, late_excluded=len(late_rows))


# ─────────────────────────────────────────────────────────────────────────
# LOCK
# ─────────────────────────────────────────────────────────────────────────
def gather_legs(page) -> dict:
    return page.evaluate(
        """
        async () => {
          window._autoLockLegs = [];
          // ESPN's site.api.espn.com blocks fetch() calls made from an
          // automated/headless browser context (confirmed: identical
          // failure in a real GitHub Actions run with unrestricted
          // network, and even with a real installed Chrome channel --
          // this isn't a sandbox artifact, it's ESPN detecting the
          // automation fingerprint itself, e.g. navigator.webdriver).
          // loadGames()/renderGenericWeek()/etc. all depend entirely on
          // that live cross-origin fetch with no same-origin fallback, so
          // they silently produce zero games here. Where real game data
          // is already bundled server-side (window.__CV_DATA, written by
          // the Python pipeline's own successful requests-based ESPN
          // calls, a completely different, unblocked code path) this
          // pre-step feeds it into the same card-render function a real
          // page load would use, so _autoLockCapture still fires with
          // real data instead of nothing.
          // cfb_schedule.json / nfl_schedule.json are same-origin static
          // files the Python pipeline already publishes -- same unblocked
          // fetch as window.__CV_DATA, just a separate file rather than
          // bundled into data.json.
          try {
            if (typeof _cfbGameCard === 'function') {
              const r = await fetch('cfb_schedule.json');
              if (r.ok) {
                const sched = await r.json();
                const todayIso = typeof today === 'function' ? today() : new Date().toISOString().slice(0, 10);
                Object.values(sched.weeks || {}).forEach(week => (week || []).forEach(g => {
                  const d = new Date(g.date);
                  const localIso = isNaN(d) ? (g.date || '').slice(0, 10) : d.toLocaleDateString('sv-SE', { timeZone: 'America/Denver' });
                  if (localIso !== todayIso || g.state === 'post') return;
                  try { _cfbGameCard(g); } catch (e) {}
                }));
              }
            }
          } catch (e) {}
          // Real gap, found via audit 2026-09-09: NFL's own warmup below
          // (renderGenericWeek('nfl-week-list', ...)) depends entirely on
          // the same blocked live cross-origin ESPN fetch as CFB's did --
          // confirmed live in a fresh headless Playwright session that the
          // cross-origin call fails ('Failed to fetch') while the
          // same-origin nfl_schedule.json (25 weeks, 318 games) loads
          // fine. Unlike CFB, this never got a same-origin fallback added,
          // meaning the morning auto-lock pass has likely been silently
          // capturing zero NFL game legs (ML/spread/O-U) for its entire
          // lifetime -- found and fixed just before the 2026 NFL season
          // opener. Mirrors the CFB block above exactly, using
          // _nflGameCard2 (confirmed to call _autoLockCapture('NFL', ...)
          // same as _cfbGameCard does for CFB).
          try {
            if (typeof _nflGameCard2 === 'function') {
              const r = await fetch('nfl_schedule.json');
              if (r.ok) {
                const sched = await r.json();
                const todayIso = typeof today === 'function' ? today() : new Date().toISOString().slice(0, 10);
                Object.values(sched.weeks || {}).forEach(week => (week || []).forEach(g => {
                  const d = new Date(g.date);
                  const localIso = isNaN(d) ? (g.date || '').slice(0, 10) : d.toLocaleDateString('sv-SE', { timeZone: 'America/Denver' });
                  // seasonType 1 = preseason (nfl_schedule.json's own "Preseason
                  // Week N" entries) -- real bug, found 2026-09-15: this had no
                  // exclusion at all, unlike _nflPopulateModelPropsGameFilter's
                  // manual-UI dropdown (g.seasonType!==1) elsewhere in app.html.
                  // A preseason game landing on todayIso got auto-locked exactly
                  // like a real regular-season one, contaminating win-rate/record
                  // stats with a game the model was never meant to grade.
                  if (localIso !== todayIso || g.state === 'post' || g.seasonType === 1) return;
                  try { _nflGameCard2(g); } catch (e) {}
                }));
              }
            }
          } catch (e) {}
          // Liiga -- added 2026-09-16 alongside the hockey product's
          // promotion to include it (see auto_lock_settle.py's own
          // PRODUCT_SPORTS comment). _liigaMatchCard reads docs/
          // liiga_schedule.json (via loadLiigaScheduleData(), same
          // fetch-if-not-cached pattern _nhlUpcomingCard's schedule
          // already uses) and calls _autoLockCapture('LIIGA', ...)
          // itself -- mirrors the NFL/CFB game-leg warmup immediately
          // above exactly: today's local-date games only, skip anything
          // already final.
          try {
            if (typeof _liigaMatchCard === 'function' && typeof loadLiigaScheduleData === 'function') {
              const liigaData = await loadLiigaScheduleData();
              const todayIso = typeof today === 'function' ? today() : new Date().toISOString().slice(0, 10);
              (liigaData.games || []).forEach(g => {
                if (!g.date) return;
                const d = new Date(g.date);
                const localIso = isNaN(d) ? g.date.slice(0, 10) : d.toLocaleDateString('sv-SE', { timeZone: 'America/Denver' });
                if (localIso !== todayIso || g.state === 'post') return;
                try { _liigaMatchCard(g); } catch (e) {}
              });
            }
          } catch (e) {}
          // SHL -- same treatment as Liiga immediately above (added the
          // same day, same hockey-product promotion), reading docs/
          // shl_schedule.json via loadShlScheduleData() and calling
          // _autoLockCapture('SHL', ...) itself from inside _shlMatchCard.
          try {
            if (typeof _shlMatchCard === 'function' && typeof loadShlScheduleData === 'function') {
              const shlData = await loadShlScheduleData();
              const todayIso = typeof today === 'function' ? today() : new Date().toISOString().slice(0, 10);
              (shlData.games || []).forEach(g => {
                if (!g.date) return;
                const d = new Date(g.date);
                const localIso = isNaN(d) ? g.date.slice(0, 10) : d.toLocaleDateString('sv-SE', { timeZone: 'America/Denver' });
                if (localIso !== todayIso || g.state === 'post') return;
                try { _shlMatchCard(g); } catch (e) {}
              });
            }
          } catch (e) {}
          // NHL -- real gap, found + fixed 2026-09-29 auditing the NHL
          // engine ahead of the season opener: the warmup below
          // (renderNHLGames()) depends entirely on a LIVE cross-origin
          // fetch to ESPN's scoreboard API (fetchESPNNHL), the exact same
          // "blocked for automation, no same-origin fallback" gap already
          // found and fixed for CFB/NFL above -- confirmed live in a real
          // dry-run: "Gathered 0 games' worth of markets" and
          // nhl={'games': 0, ...} despite 5 real games existing that day.
          // renderNHLGames()'s own last-resort fallback (renderNHLGamesOffline,
          // the hardcoded TONIGHT array) can't help either -- TONIGHT is
          // empty by design now (see docs/app.html's own comment on why).
          // Fixed the same way as CFB/NFL/Liiga/SHL immediately above:
          // read the same-origin docs/nhl_schedule.json the Python
          // pipeline already publishes (real per-game ML/spread/O-U from
          // fetch_nhl.py) via the existing loadNHLScheduleData() loader,
          // and call _nhlUpcomingCard(g, 0) directly -- confirmed by
          // reading it that it calls _autoLockCapture('NHL', ...) itself,
          // same as every other sport's card function here.
          try {
            if (typeof _nhlUpcomingCard === 'function' && typeof loadNHLScheduleData === 'function') {
              const nhlData = await loadNHLScheduleData();
              const todayIso = typeof today === 'function' ? today() : new Date().toISOString().slice(0, 10);
              (nhlData.games || []).forEach(g => {
                if (!g.date) return;
                const d = new Date(g.date);
                const localIso = isNaN(d) ? g.date.slice(0, 10) : d.toLocaleDateString('sv-SE', { timeZone: 'America/Denver' });
                if (localIso !== todayIso || g.state === 'post') return;
                try { _nhlUpcomingCard(g, 0); } catch (e) {}
              });
            }
          } catch (e) {}
          // NBA -- same real gap as NHL just above, found the same day
          // auditing the other sports: renderNBAGames() (the warmup a few
          // lines below) does its own live cross-origin ESPN fetch with
          // no same-origin fallback, so it gathers nothing in this headless
          // context and _nbaGameCard() -- the only function that calls
          // _autoLockCapture('NBA', ...) -- never runs. Confirmed
          // separately against the real ledger: NBA's own settled ML bets
          // show 0% real score capture across 130 real rows, the same
          // signature NHL had before its fix. Unlike NHL/Liiga/SHL, there
          // is no dedicated nba_schedule.json -- NBA's real same-origin
          // data is docs/data.json's own nba.today (fetch_nba_scoreboard()
          // in clairvoyance_update.py, real ML/spread/O-U already
          // included). Fetched directly here (not assumed already loaded
          // into window.__CV_DATA, whose own load timing in this headless
          // context isn't guaranteed) and adapted into the minimal ESPN-
          // event shape _nbaGameCard() itself expects (competitions[0].
          // competitors/status/odds) -- reusing that real, already-correct
          // card function instead of duplicating its model/market logic.
          try {
            const dataResp = await fetch('data.json', { cache: 'no-store' });
            const cvData = dataResp.ok ? await dataResp.json() : null;
            const nbaToday = cvData?.nba?.today || [];
            const todayIso = typeof today === 'function' ? today() : new Date().toISOString().slice(0, 10);
            nbaToday.forEach(g => {
              if (!g.date) return;
              const d = new Date(g.date);
              const localIso = isNaN(d) ? g.date.slice(0, 10) : d.toLocaleDateString('sv-SE', { timeZone: 'America/Denver' });
              if (localIso !== todayIso || g.state === 'post') return;
              const espnEv = {
                id: g.id, date: g.date,
                competitions: [{
                  competitors: [
                    { homeAway: 'home', team: { abbreviation: g.home, displayName: g.home }, score: String(g.homeScore ?? 0) },
                    { homeAway: 'away', team: { abbreviation: g.away, displayName: g.away }, score: String(g.awayScore ?? 0) },
                  ],
                  status: { type: { state: g.state }, period: g.period, displayClock: g.displayClock },
                  venue: { fullName: g.venue || '' },
                  odds: (g.homeML != null || g.awayML != null || g.ou != null)
                    ? [{ homeTeamOdds: { moneyLine: g.homeML }, awayTeamOdds: { moneyLine: g.awayML }, overUnder: g.ou, details: g.details || '' }]
                    : [],
                }],
              };
              if (typeof _nbaGameCard === 'function') { try { _nbaGameCard(espnEv); } catch (e) {} }
            });
          } catch (e) {}
          const warmups = [];
          if (typeof renderNBAGames === 'function') { try { renderNBAGames(); } catch (e) {} }
          if (typeof renderNHLGames === 'function') warmups.push(renderNHLGames().catch(() => {}));
          if (typeof renderGenericWeek === 'function') {
            warmups.push(renderGenericWeek('cfb-week-list', 'football/college-football', 'CFB').catch(() => {}));
            warmups.push(renderGenericWeek('nfl-week-list', 'football/nfl', 'NFL').catch(() => {}));
          }
          // 6 soccer leagues -- explicit request, 2026-09-16: their MATCHES
          // tab got the same full-season day-picker every other sport's
          // schedule browser already has (renderLeagueMatches now fetches
          // the whole season and defaults its dropdown to today, or the
          // EARLIEST UPCOMING date if today has no games -- see
          // _socPopulateDayFilter's own comment). Calling
          // renderLeagueMatches(k) directly here would auto-lock-capture
          // whatever day the dropdown happens to default to, which is only
          // "today" some of the time now. Mirrors the Liiga/SHL blocks
          // immediately above instead: fetch the full schedule, filter to
          // today's local date ourselves, and call _renderSocMatchCard(g,
          // key) directly per game -- the same card function
          // renderLeagueMatches uses internally, which already calls
          // _autoLockCapture('SOC_'+key.toUpperCase(), ...) on its own, so
          // this needs no extra capture wiring, just the right game list.
          // Real gap, found auditing this exact block, 2026-09-16:
          // _fetchLeagueScoreboard's own live call goes straight to
          // site.api.espn.com -- the exact cross-origin host this whole
          // function's own top comment already documents ESPN blocking
          // fetch() from an automated/headless context for (confirmed in a
          // real GitHub Actions run). That means the European leagues
          // below likely got ZERO real games here on every single
          // automated run, silently, for as long as this warmup has
          // existed -- there's no visible symptom, gather_legs() just
          // quietly returns no soccer legs for those leagues. Awaiting
          // loadSoccerScheduleSnapshot() first guarantees docs/app.html's
          // own emergency fallback (window.__CV_SOC_SCHED_SNAPSHOT, loaded
          // from the same-origin, always-reachable soccer_schedule.json --
          // see that function's own comment) is populated before
          // _fetchLeagueScoreboard runs, so when its live ESPN call
          // predictably fails here, it falls back to that snapshot's real
          // today-only games instead of silently returning [].
          try {
            if (typeof _renderSocMatchCard === 'function' && typeof _fetchLeagueScoreboard === 'function') {
              if (typeof loadSoccerScheduleSnapshot === 'function') { try { await loadSoccerScheduleSnapshot(); } catch (e) {} }
              const todayIso = typeof today === 'function' ? today() : new Date().toISOString().slice(0, 10);
              // 'bl' (Bundesliga) removed 2026-09-23 -- retired, see
              // EURO_SOCCER_SPORTS' own comment for the full rationale.
              // 'mls' removed 2026-09-27 -- retired the same way, see
              // PRODUCT_SPORTS' own comment for the full rationale. This is
              // the actual automated-locking entry point (calls
              // _renderSocMatchCard(g, 'mls') directly, bypassing the UI nav
              // entirely), so removing it here is what structurally
              // guarantees no new MLS pick can ever be auto-locked again.
              const soccerKeys = ['liga', 'pl', 'ita', 'cl'];
              for (const k of soccerKeys) {
                try {
                  const games = await _fetchLeagueScoreboard(k);
                  (games || []).forEach(g => {
                    if (!g.home || !g.away || g.status === 'post') return;
                    const d = new Date(g.date);
                    const localIso = isNaN(d) ? (g.date || '').slice(0, 10) : d.toLocaleDateString('sv-SE', { timeZone: 'America/Denver' });
                    if (localIso !== todayIso) return;
                    try { _renderSocMatchCard(g, k); } catch (e) {}
                  });
                } catch (e) {}
              }
            }
          } catch (e) {}
          await Promise.allSettled(warmups);
          // Card renders above are synchronous once their data warmup
          // resolves, but give any trailing async chip/radar work a moment
          // before reading back what _autoLockCapture collected.
          await new Promise(r => setTimeout(r, 1500));

          const gameLegs = window._autoLockLegs || [];

          // Real gap, found via audit: every one of these catches was a
          // bare `catch (e) {}` -- if a prop generator ever threw (a live
          // ESPN fetch blocked/erroring in this headless context, a shape
          // mismatch in the stats payload, anything), it failed completely
          // silently. Confirmed via the real ledger: 0 settled PROP-type
          // bets in 21+ days despite game legs actively locking on the
          // same days from the same gather_legs() pass, which is exactly
          // the shape a silent props-path failure would produce and
          // exactly what these bare catches made impossible to diagnose
          // from the CI logs alone. propDiag doesn't fix whatever's wrong
          // -- it makes the next real failure visible instead of
          // indistinguishable from "nothing qualified today."
          const propLegs = [];
          const propDiag = {};
          try {
            if (typeof _generateNBAProps === 'function' && typeof _fetchNBAPlayerStats === 'function') {
              const stats = await _fetchNBAPlayerStats();
              const games = window._nbaTodayGames || (typeof NBA_TONIGHT !== 'undefined' ? NBA_TONIGHT : []) || [];
              // Single call, not two -- _generateNBAProps runs a Monte Carlo
              // sim internally (same as its WNBA/NHL siblings), so calling
              // it twice (once for a count, once to push) would double the
              // compute AND risk the count silently disagreeing with what
              // actually got pushed on two independent random draws.
              const generated = stats ? _generateNBAProps(games, stats) : [];
              generated.forEach(p => propLegs.push({ ...p, sportTag: 'NBA' }));
              propDiag.nba = { games: games.length, stats: stats ? Object.keys(stats).length : 0, generated: generated.length };
            } else propDiag.nba = { skipped: 'fn missing' };
          } catch (e) { propDiag.nba = { error: e.message }; }
          try {
            if (typeof _generateNHLPropsLive === 'function' && typeof _fetchNHLPlayerStats === 'function') {
              const stats = await _fetchNHLPlayerStats();
              const games = window._nhlTodayGames || [];
              const generated = stats ? _generateNHLPropsLive(games, stats) : [];
              generated.forEach(p => propLegs.push({ ...p, sportTag: 'NHL' }));
              propDiag.nhl = { games: games.length, stats: stats ? Object.keys(stats).length : 0, generated: generated.length };
            } else propDiag.nhl = { skipped: 'fn missing' };
          } catch (e) { propDiag.nhl = { error: e.message }; }
          // NFL player props REMOVED from generation entirely, 2026-09-23,
          // explicit request after a real settled-bet audit found TD props
          // specifically badly overconfident (49.1% actual vs 79.5% avg
          // predicted, -21.7u on 57 bets -- the single largest drag on
          // NFL's whole season-to-date ROI, while receiving/passing props
          // were fine) -- the user's stated direction is NFL game lines
          // only going forward, "more dependable" than per-market prop
          // tuning. This used to call _nflModelPropsForGame(g) per today's
          // game here (see git history for that block, including the
          // real 2026-09-03 window._NFL_DATA scoping fix that first made
          // NFL props actually generate) and push results into propLegs;
          // now it's a no-op so nothing NFL-tagged can ever reach
          // `qualifying` below. _nflModelPropsForGame itself and its
          // in-app PROPS tab (docs/app.html) are untouched -- still
          // real and browsable for personal reference, just never fed
          // into this automated pipeline anymore.
          propDiag.nfl = { skipped: 'NFL player props removed from auto-lock 2026-09-23, explicit request' };

          return { gameLegs, propLegs, propDiag };
        }
        """
    )


# Evening-prior-lock leagues: the European leagues named for this feature
# (CL/PL/La Liga/Serie A -- Bundesliga retired 2026-09-23, see
# EURO_SOCCER_SPORTS' own comment) -- MLS deliberately excluded, it kicks
# off at normal US evening times and never had the early-kickoff problem
# this exists for. Local key -> _autoLockCapture's sport tag (SOC_<KEY>,
# matching docs/app.html's leagueKey.toUpperCase() convention).
EURO_LEAGUE_KEY_TO_SPORT = {"cl": "SOC_CL", "pl": "SOC_PL", "liga": "SOC_LIGA", "ita": "SOC_ITA"}


def gather_soccer_legs_for_date(page, target_date_iso: str) -> dict:
    """Single-date wrapper kept for tests/back-compat -- see gather_soccer_legs_for_dates."""
    return gather_soccer_legs_for_dates(page, [target_date_iso])


def gather_soccer_legs_for_dates(page, target_dates: list[str]) -> dict:
    """Evening-prior-lock version of gather_legs(), narrowed to just the
    4 European soccer leagues and a set of target dates instead of "today".

    Unlike the main gather_legs() (which drives the live page's own render
    functions -- renderNBAGames(), renderLeagueMatches(), etc. -- all of
    which are hardcoded to "today" by design, see _fetchLeagueScoreboard's
    own comment on why), this calls docs/app.html's _renderSocMatchCard(g,
    leagueKey) directly, one game object at a time, sourced from the
    published snapshots instead of any live/today-scoped fetch.
    _renderSocMatchCard itself has no "today" dependency -- it computes
    xG/Monte-Carlo/EV purely from the game object it's given and fires the
    same _autoLockCapture() hook every other sport's card renderer uses, so
    this reuses the exact same evaluation logic as the live site with zero
    duplicated grading code.

    ROLLING HORIZON (2026-10-02): target_dates is a list of Mountain dates
    (see horizon_dates), and BOTH snapshots are read: soccer_schedule_tomorrow.json
    (written by scrape_soccer_schedule.py --tomorrow) and soccer_schedule.json
    (today's games, daily-schedules-refresh). A snapshot is used only when its
    own `date` field (ESPN YYYYMMDD) is one of the target dates -- the same "reject a
    stale snapshot rather than silently grading the wrong day's games" guard
    loadSoccerScheduleSnapshot() applies -- so a pass that runs after midnight
    MT (GitHub delays these workflows 5-7h) still covers the games about to
    start that morning. Games already final are skipped; the pre-start guard
    in build_qualifying handles started ones."""
    target_espn = [d.replace("-", "") for d in target_dates]
    return page.evaluate(
        """
        async ({ targetEspn, leagueKeys }) => {
          window._autoLockLegs = [];
          for (const file of ['soccer_schedule_tomorrow.json', 'soccer_schedule.json']) {
            try {
              const r = await fetch(file, { cache: 'no-store' });
              if (!r.ok) { console.warn('[CV evening-lock] ' + file + ' fetch failed:', r.status); continue; }
              const d = await r.json();
              if (d && targetEspn.includes(d.date) && d.leagues) {
                leagueKeys.forEach(key => {
                  (d.leagues[key] || []).forEach(g => {
                    if (!g.home || !g.away || g.status === 'post') return;
                    try { _renderSocMatchCard(g, key); } catch (e) {}
                  });
                });
              } else if (d) {
                console.warn('[CV evening-lock] ' + file + ' date ' + d.date + ' not in target dates ' + targetEspn.join(',') + ', skipping');
              }
            } catch (e) { console.warn('[CV evening-lock] snapshot load error (' + file + '):', e.message); }
          }
          await new Promise(r => setTimeout(r, 300));
          return { gameLegs: window._autoLockLegs || [], propLegs: [] };
        }
        """,
        {"targetEspn": target_espn, "leagueKeys": list(EURO_LEAGUE_KEY_TO_SPORT.keys())},
    )


def gather_cfb_legs_for_date(page, target_date_iso: str) -> dict:
    """Single-date wrapper kept for tests/back-compat -- see gather_cfb_legs_for_dates."""
    return gather_cfb_legs_for_dates(page, [target_date_iso])


def gather_cfb_legs_for_dates(page, target_dates: list[str]) -> dict:
    """Rolling-horizon (list of Mountain dates -- see horizon_dates) evening-prior-lock version for CFB, same rationale as
    gather_soccer_legs_for_date above -- but simpler, since CFB needs no
    new data-feed workflow at all: docs/cfb_schedule.json already covers
    the FULL SEASON in one file (confirmed live: dates spanning Aug 2026
    through Jan 2027), refreshed twice daily by the existing
    daily-schedules-refresh.yml, so this just targets tomorrow's date against
    data that's already there.

    _cfbGameCard(g) has no "today" dependency of its own -- confirmed by
    reading it directly: it computes its model purely from the game
    object's own g.spread/g.overUnder market fields (real market data,
    confirmed live 6+ days out for every game), deriving its own display
    moneyline from the model's win probability rather than needing a
    posted one (which ESPN's site API doesn't carry for CFB regardless,
    at any lead time -- confirmed live, 0 of 68 games 6 days out had one,
    same as every closer date checked). Fires the same _autoLockCapture()
    hook every sport's card renderer uses, so this reuses the exact same
    grading logic as the live site with zero duplicated code.

    Filters out g.state === 'post' defensively (shouldn't ever match for
    a genuinely future date, but costs nothing to guard)."""
    return page.evaluate(
        """
        async (targetDates) => {
          window._autoLockLegs = [];
          try {
            const r = await fetch('cfb_schedule.json', { cache: 'no-store' });
            if (r.ok) {
              const sched = await r.json();
              Object.values(sched.weeks || {}).forEach(week => (week || []).forEach(g => {
                const d = new Date(g.date);
                const localIso = isNaN(d) ? (g.date || '').slice(0, 10) : d.toLocaleDateString('sv-SE', { timeZone: 'America/Denver' });
                if (!targetDates.includes(localIso) || g.state === 'post') return;
                try { _cfbGameCard(g); } catch (e) {}
              }));
            } else {
              console.warn('[CV evening-lock CFB] cfb_schedule.json fetch failed:', r.status);
            }
          } catch (e) { console.warn('[CV evening-lock CFB] error:', e.message); }
          await new Promise(r => setTimeout(r, 300));
          return { gameLegs: window._autoLockLegs || [], propLegs: [] };
        }
        """,
        list(target_dates),
    )


def gather_hockey_evening_legs_for_date(page, target_date_iso: str) -> dict:
    """Single-date wrapper kept for tests/back-compat -- see gather_hockey_legs_for_dates."""
    return gather_hockey_legs_for_dates(page, [target_date_iso])


def gather_hockey_legs_for_dates(page, target_dates: list[str]) -> dict:
    """Evening-prior-lock version for SHL/Liiga/NLA/Extraliga combined,
    same rationale as gather_cfb_legs_for_date above -- no new data-feed
    workflow needed: docs/liiga_schedule.json/shl_schedule.json/
    nla_schedule.json/extraliga_schedule.json each already cover several
    weeks ahead (refreshed twice daily, liiga-schedule-refresh.yml/
    shl-schedule-refresh.yml/nla-schedule-refresh.yml/
    extraliga-schedule-refresh.yml), so this just targets tomorrow's date
    against data that's already there.

    _liigaMatchCard(g)/_shlMatchCard(g)/_nlaMatchCard(g)/
    _extraligaMatchCard(g) have no "today" dependency of their own --
    confirmed by reading them directly: their date stamp comes from
    g.date itself (a today()-fallback only fires if g.date fails to
    parse, which never happens for a real game). Fires the same
    _autoLockCapture() hook every sport's card renderer uses. Unlike
    CFB's always-resident static table, _LIIGA_DATA/_SHL_DATA/_NLA_DATA/
    _EXTRALIGA_DATA are loaded lazily -- the loader functions are
    awaited here first since every match-card function reads team rates
    from them.

    NLA (Swiss National League) and Extraliga (Czech) added 2026-09-23,
    same session as the leagues themselves -- both play on the same
    Central European evening schedule as Liiga/SHL (real kickoffs land
    in the early-MT-morning window from a US perspective), so they
    belong in the same evening-prior-lock pass rather than the standard
    same-day morning lock.

    ROLLING HORIZON (2026-10-02): takes a LIST of Mountain dates (see horizon_dates) instead of one. GitHub runs these
    "evening" workflows 5-7h late, i.e. after midnight MT, when a single "tomorrow" date would skip the games about to start
    that morning; every game on any listed date that is not final is captured, and build_qualifying's pre-start guard drops
    the ones that already started."""
    return page.evaluate(
        """
        async (targetDates) => {
          window._autoLockLegs = [];
          const leagues = [
            { load: loadLiigaScheduleData, card: (typeof _liigaMatchCard === 'function') ? _liigaMatchCard : null, label: 'Liiga' },
            { load: loadShlScheduleData, card: (typeof _shlMatchCard === 'function') ? _shlMatchCard : null, label: 'SHL' },
            { load: loadNlaScheduleData, card: (typeof _nlaMatchCard === 'function') ? _nlaMatchCard : null, label: 'NLA' },
            { load: loadExtraligaScheduleData, card: (typeof _extraligaMatchCard === 'function') ? _extraligaMatchCard : null, label: 'Extraliga' },
          ];
          for (const lg of leagues) {
            if (!lg.card) continue;
            try {
              const data = await lg.load();
              (data && data.games || []).forEach(g => {
                const d = new Date(g.date);
                const localIso = isNaN(d) ? (g.date || '').slice(0, 10) : d.toLocaleDateString('sv-SE', { timeZone: 'America/Denver' });
                if (!targetDates.includes(localIso) || g.state === 'post') return;
                try { lg.card(g); } catch (e) {}
              });
            } catch (e) { console.warn('[CV evening-lock ' + lg.label + '] error:', e.message); }
          }
          await new Promise(r => setTimeout(r, 300));
          return { gameLegs: window._autoLockLegs || [], propLegs: [] };
        }
        """,
        list(target_dates),
    )


def _dedupe_opposite_sides(game_qualifying: list[dict]) -> list[dict]:
    """Drops the weaker leg of any pair that's really just the two opposite
    sides of ONE market on the same game -- real bug, found via a live
    ledger audit: a single tennis match (2026-08-22, Herbert vs Miyoshi)
    had BOTH players' ML, BOTH sides of its sets O/U, and BOTH sides of
    its games O/U all qualify and lock simultaneously -- 6 legs, 3
    self-cancelling pairs where one side was mathematically guaranteed to
    lose no matter what happened on court. That's not diversification,
    it's the same coin counted twice: it inflates the pick count while
    silently dragging the sport's real accuracy down by a fixed amount
    regardless of model quality. Tennis surfaced it first (its sets/games
    O/U legs use fixed self-generated prices on both sides -- see
    _usoMatchCards' own comment on why -- which makes near-50/50 matches
    the likeliest place for both sides to independently clear the EV bar),
    but the same shape is possible for any sport's ML/spread ties too, so
    this runs for every sport, ahead of the narrower same-team
    ML+spread cap below (which handles a different, less severe kind of
    correlation -- two DIFFERENT markets on the same team, not two sides
    of the same one).
    ML/SPREAD: only ever 2-3 sides possible for one game's market, so 2+
    qualifying at once is never real diversification -- keep the single
    strongest. OU: a game can have more than one distinct O/U market
    (tennis games O/U AND sets O/U), so sides are grouped by the line
    itself (the label with its leading OVER/UNDER stripped, e.g. "25.5
    games") rather than by type alone, and only trimmed within a group.
    """
    by_type: dict[str, list[dict]] = {}
    for leg in game_qualifying:
        by_type.setdefault(_market_type(leg.get("side")), []).append(leg)
    kept: list[dict] = []
    for mkt_type, legs in by_type.items():
        if mkt_type in ("ML", "SPREAD") and len(legs) > 1:
            legs.sort(key=lambda q: (q.get("tierN") or 0, q.get("evVal") or 0), reverse=True)
            kept.append(legs[0])
        elif mkt_type == "OU":
            families: dict[str, list[dict]] = {}
            for leg in legs:
                fam = re.sub(r"^(OVER|UNDER)\s+", "", (leg.get("label") or ""), flags=re.I).strip().lower()
                families.setdefault(fam, []).append(leg)
            for fam_legs in families.values():
                fam_legs.sort(key=lambda q: (q.get("tierN") or 0, q.get("evVal") or 0), reverse=True)
                kept.append(fam_legs[0])
        else:
            kept.extend(legs)
    return kept


# ── CFB per-game selection (2026-10-03) ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# A Saturday CFB slate locked 46-113 picks (89 across 44 games on Oct 3): ~2 per game, 70% of them spread / O-U. The settled ledger (359 CFB picks) says why that is
# wasteful: moneyline favourites hit 88.9% but spreads only 54.7% and O/Us 56.4%, and for spreads/O-Us the stated probability was ANTI-informative above ~75%
# (spread 70-85% stated -> 38-40% won, O/U 75-85% -> 45%). So per game:
#   RULE B  one pick: the moneyline if it is a heavy favourite (p >= CFB_ML_MIN_P), otherwise the single best spread / O-U whose probability sits in the band where
#           the history held up (CFB_NONML_BAND).
#   RULE A  two picks (ML + the O/U) only when the moneyline is very strong (p >= CFB_PAIR_ML_MIN_P) AND an O/U qualifies in the band. ML + spread is never paired
#           (same directional read priced twice; the generic cap below already excludes it).
# In-sample on the ledger: keeps ~47% of picks and lifts the win rate 65.7% -> 74.6%; on Oct 3's slate it would have been 37 picks over 35 games instead of 89 over 44.
CFB_ML_MIN_P = 0.70
CFB_PAIR_ML_MIN_P = 0.75
CFB_NONML_BAND = (0.60, 0.75)


def _cfb_select(legs: list[dict]) -> list[dict]:
    """Per-game CFB selection (rules B/A above) over the legs that already qualified for this game."""
    def p(leg):
        return leg.get("prob") or 0
    ml = [l for l in legs if _market_type(l.get("side")) == "ML" and p(l) >= CFB_ML_MIN_P]
    non = [l for l in legs if _market_type(l.get("side")) != "ML" and CFB_NONML_BAND[0] <= p(l) < CFB_NONML_BAND[1]]
    best_ml = max(ml, key=p) if ml else None
    ou = [l for l in non if _market_type(l.get("side")) == "OU"]
    best_ou = max(ou, key=p) if ou else None
    best_non = max(non, key=p) if non else None
    if best_ml is not None:
        if best_ou is not None and p(best_ml) >= CFB_PAIR_ML_MIN_P:
            return [best_ml, best_ou]          # rule A
        return [best_ml]                       # rule B, moneyline first
    return [best_non] if best_non is not None else []


# ── Alternate-line shift (2026-10-03) ────────────────────────────────────────────────────────────
# Sportsbooks offer the same total / spread at other numbers (alternate lines), so a pick on a posted line L can instead be locked at a safer
# L' = L +/- k for a shorter price. For the sides this engine already qualifies (spread, O/U) the lock is moved to the line that reaches
# ALT_TARGET_P (67%) calibrated win probability, bounded by a per-sport/market band [kmin, kmax] (the fixed band) -- the target picks k inside it.
# The probability is NOT the card's own model number: on the settled ledger the stated O/U / spread probability carried no information about the
# margin (CFB O/U: fitted slope on the stated probability was negative), so the cushion is priced purely from the empirical margin spread.
# Backtest on 151 settled CFB O/U picks / 94 spread picks, margin cushion k -> actual win rate:
#     O/U    k=0 55.0%   k=4 68.9%   k=7 74.2%   k=10 80.1%       (this curve: Phi((k + 1.9) / 14.7): 54%, 65%, 72%, 78%)
#     spread k=0 52.1%   k=4 63.8%   k=7 70.2%   k=10 75.5%       (this curve: Phi((k + 0.8) / 15.7): 52%, 60%, 68%, 74%)
# so the curve is slightly conservative. The price is estimated (fair price + ALT_VIG), never a real quote -> priceSource "estimated".
ALT_TARGET_P = 0.67           # 2026-10-03: 0.75 -> 0.70 -> 0.67 (whole-point steps land every cover in 67-70%): smaller cushion, shorter price, better ROI
ALT_FLOOR_P = 0.62            # if even the cap cannot reach this, leave the posted-line pick alone
ALT_VIG = 0.045
ALT_STEP = 1.0
ALT_LINE_CFG = {
    # sport: {market: (curve, kmin, kmax)}; curve = ("normal", sigma of actual margin vs line, offset a at k=0) or ("table", ((k, win p), ...)) with linear
    # interpolation. CFB/NFL come from the settled ledger; NBA from 2,470 closing lines + final scores of the 2024-25 and 2025-26 seasons
    # (scripts/backtest_alt_lines.py nba -- side-neutral, since the ledger holds no settled NBA totals; its NBA spread legs also carry no line number
    # in the label ("BOS -ATS"), so only NBA totals are shifted for now).
    "CFB": {"OU": (("normal", 14.7, 1.9), 3.0, 9.0), "SPREAD": (("normal", 15.7, 0.8), 3.0, 10.0)},
    "NFL": {"OU": (("normal", 13.0, 0.0), 3.0, 8.0), "SPREAD": (("normal", 13.4, 0.0), 3.0, 8.0)},
    "NBA": {"OU": (("table", ((0, .500), (2, .543), (3, .564), (4, .590), (5, .612), (6, .631), (7, .651), (8, .670), (10, .711))), 3.0, 10.0)},
}
# Hockey (NHL + SHL/LIIGA/NLA/EXTRALIGA), totals only, 60-65% band (2026-10-03, owner's call): the pick may FLIP SIDE as well as move the line (OVER 5.5 -> UNDER 6.5,
# UNDER 6.5 -> OVER 5.5, OVER 4.5 -> UNDER 5.5). Every OVER anchor-1..3 and UNDER anchor+1..3 candidate is scored with that league's own hit rates and the one closest to
# HOCKEY_ALT_BAND wins; nothing below HOCKEY_ALT_FLOOR is ever picked. Ties within 2 points go to the model's own side, then the smaller move. Puck lines are not shifted.
#   NHL: conditional hit rates from 2,630 games of closing totals + results (2024-25 + 2025-26, scripts/backtest_alt_lines.py nhl), with the shootout-winning goal removed
#        (ESPN's final includes it, sportsbooks do not settle totals on it). Posted totals <= 6.0 use the 5.5 table, higher ones the 6.5 table.
#   LIIGA / SHL / NLA / EXTRALIGA: each league's OWN table (scripts/backtest_alt_lines.py euro): this season's finished games blended with a Poisson at the league's own two-season
#        mean total (last season + this season from the teams' gf/gp). No NHL numbers. There are no closing-line histories for these leagues, so the posted line is assumed to be
#        the game's median and one shift-invariant table applies at any posted line. Small samples (38-76 finished games) -> REFRESH THESE as the season fills in.
# A whole-number posted line is anchored on the half-point below it, so a lock never lands on a push.
HOCKEY_ALT_BAND = (0.60, 0.65)
HOCKEY_ALT_FLOOR = 0.55
HOCKEY_ALT_TIE = 0.02
# {j goals from the anchor line: P(OVER anchor+j) for j<0, P(UNDER anchor+j) for j>0}
HOCKEY_OU_NHL = {
    5.5: {-3: 0.961, -2: 0.848, -1: 0.731, 1: 0.611, 2: 0.769, 3: 0.851},
    6.5: {-3: 0.879, -2: 0.787, -1: 0.589, 1: 0.736, 2: 0.831, 3: 0.915},
}
HOCKEY_OU_EURO = {
    "LIIGA": {-3: 0.981, -2: 0.940, -1: 0.758, 1: 0.593, 2: 0.745, 3: 0.838},
    "SHL": {-3: 0.918, -2: 0.780, -1: 0.666, 1: 0.667, 2: 0.816, 3: 0.870},
    "NLA": {-3: 0.947, -2: 0.854, -1: 0.755, 1: 0.590, 2: 0.783, 3: 0.876},
    "EXTRALIGA": {-3: 0.908, -2: 0.804, -1: 0.676, 1: 0.663, 2: 0.812, 3: 0.893},
}
ALT_SPORTS = frozenset(ALT_LINE_CFG) | HOCKEY_SPORTS
_ND = None


def _alt_cdf(x: float) -> float:
    global _ND
    if _ND is None:
        from statistics import NormalDist
        _ND = NormalDist()
    return _ND.cdf(x)


def _fmt_line(x: float, signed: bool) -> str:
    return f"{x:+.1f}" if signed else f"{x:.1f}"


def _ml_from_dec(dec: float) -> str:
    return f"-{round(100 / (dec - 1))}" if dec < 2 else f"+{round((dec - 1) * 100)}"


def _alt_prob(curve, k: float) -> float:
    if curve[0] == "normal":
        return _alt_cdf((k + curve[2]) / curve[1])
    pts = curve[1]
    if k <= pts[0][0]:
        return pts[0][1]
    for (k0, p0), (k1, p1) in zip(pts, pts[1:]):
        if k <= k1:
            return p0 + (p1 - p0) * (k - k0) / (k1 - k0)
    return pts[-1][1]


def _alt_finish(leg: dict, new_label: str, p_alt: float, posted: float, new_line: float, k: float, label: str) -> dict:
    dec = 1.0 / (p_alt + ALT_VIG)
    out = dict(leg)
    out.update({"label": new_label, "prob": round(p_alt, 4), "dec": round(dec, 3), "ml": _ml_from_dec(dec),
                "evVal": round(p_alt * dec - 1, 4), "priceSource": "estimated",
                "altLine": {"posted": posted, "line": new_line, "shift": k, "postedLabel": label, "postedProb": leg.get("prob")}})
    return out


def _hockey_pick_alt(posted: float, sport: str = "NHL", own_side: str = "over") -> tuple[str, float, int, float]:
    """(side, line, goals moved from the anchor, probability) of the hockey total closest to the 60-65% band."""
    anchor = posted if posted != int(posted) else posted - 0.5
    table = HOCKEY_OU_EURO.get(sport) or HOCKEY_OU_NHL[5.5 if anchor <= 6.0 else 6.5]
    lo, hi = HOCKEY_ALT_BAND
    best = None
    for j, p in table.items():
        if p < HOCKEY_ALT_FLOOR:
            continue
        side, line = ("over", anchor + j) if j < 0 else ("under", anchor + j)
        if line <= 0:
            continue
        dist = 0.0 if lo <= p <= hi else (lo - p if p < lo else p - hi)
        # distances within HOCKEY_ALT_TIE of each other are a tie: prefer the model's own side, then the smaller move
        key = (round(dist / HOCKEY_ALT_TIE), side != own_side, abs(j))
        if best is None or key < best[0]:
            best = (key, side, line, j, p)
    return best[1], best[2], best[3], best[4]


def _alt_shift_hockey(leg: dict) -> dict | None:
    label = (leg.get("label") or "").strip()
    m = re.match(r"^(OVER|UNDER)\s+([0-9]+(?:\.[0-9]+)?)$", label, re.I)
    if not m:
        return None
    posted = float(m.group(2))
    side, line, j, p_alt = _hockey_pick_alt(posted, leg.get("sport") or "NHL", m.group(1).lower())
    new_label = f"{side.upper()} {_fmt_line(line, False)}"
    out = _alt_finish(leg, new_label, p_alt, posted, line, float(abs(j)), label)
    out["side"] = side
    out["altLine"]["flip"] = side != m.group(1).lower()
    return out


def _alt_shift_leg(leg: dict) -> dict | None:
    """Alternate-line version of one OU / SPREAD leg, or None to keep it as posted."""
    sport = leg.get("sport") or ""
    mkt = _market_type(leg.get("side"))
    if leg.get("altLine"):
        return None
    if sport in HOCKEY_SPORTS:
        return _alt_shift_hockey(leg) if mkt == "OU" else None
    cfg = ALT_LINE_CFG.get(sport)
    if not cfg or mkt not in cfg:
        return None
    curve, kmin, kmax = cfg[mkt]
    label = leg.get("label") or ""
    if mkt == "OU":
        m = re.match(r"^(OVER|UNDER)\s+([0-9]+(?:\.[0-9]+)?)$", label.strip(), re.I)
        if not m:
            return None
        direction, posted = m.group(1).upper(), float(m.group(2))
        sign = -1.0 if direction == "OVER" else 1.0       # over wants a LOWER number, under a HIGHER one
    else:
        m = re.match(r"^(.+?)\s+([+-][0-9]+(?:\.[0-9]+)?)$", label.strip())
        if not m:
            return None
        team, posted = m.group(1), float(m.group(2))
        sign = 1.0                                          # a spread is safer as the number goes up (-5.5 -> -1.5, +5.5 -> +9.5)
    # Whole-point steps keep a half-point line on a half-point (no push); a whole-number posted line is moved onto a half-point instead.
    k = kmin + (0.5 if posted == int(posted) else 0.0)
    while k + ALT_STEP <= kmax + 0.5 and _alt_prob(curve, k) < ALT_TARGET_P - 1e-9:
        k += ALT_STEP
    p_alt = _alt_prob(curve, k)
    if p_alt < ALT_FLOOR_P:
        return None
    new_line = posted + sign * k
    if mkt == "OU":
        if new_line <= 0:
            return None
        new_label = f"{direction} {_fmt_line(new_line, False)}"
    else:
        new_label = f"{team} {_fmt_line(new_line, True)}"
    return _alt_finish(leg, new_label, p_alt, posted, new_line, k, label)


def _apply_alt_lines(legs: list[dict]) -> list[dict]:
    """Moves every spread / O-U leg of a covered sport to its alternate line (see block above); moneylines pass through."""
    res: list[dict] = []
    for leg in legs:
        alt = _alt_shift_leg(leg)
        res.append(alt if alt else leg)
    return res


def build_qualifying(result: dict, only_sports: frozenset[str] | None = None, now=None,
                     guard_stats: dict | None = None) -> list[dict]:
    """only_sports: if given, restricts to exactly these sport tags (e.g.
    PRODUCT_SPORTS["soccer"] for the soccer early pass, {"CFB"} for the
    CFB-only early pass) -- for the dedicated early lock runs timed ahead of
    that sport/league's own earlier kickoffs.

    now / guard_stats: the pre-start guard (see LOCK_START_MARGIN_MIN). A game whose scheduled start (gl["startMs"]) is
    already past, or within the margin, contributes NO legs -- every leg that would otherwise have qualified is counted
    as skipped and logged with its matchup. `now` is injectable (epoch ms or datetime) for tests; guard_stats, if given,
    is filled with {"skipped": legs, "games": [...], "unguarded": legs}. Because the emails are built from this same
    list, a skipped game never appears in a subscriber email either."""
    def _wanted(sport: str) -> bool:
        return only_sports is None or (sport or "") in only_sports
    qualifying: list[dict] = []
    skipped_legs = 0
    skipped_games: list[str] = []
    skipped_detail: list[dict] = []   # every qualifying leg the guard refused (for the owner's pre-kickoff alert)
    unguarded_legs = 0
    for gl in result.get("gameLegs") or []:
        sport = gl.get("sport")
        if not _wanted(sport):
            continue
        guard_ok, guard_reason, _guard_mins = start_guard(sport, gl.get("startMs"), now)
        game_skipped = 0
        game_qualifying: list[dict] = []
        for m in gl.get("markets") or []:
            tier_n = m.get("tierN")
            prob = m.get("prob") or 0
            # The one exception to PREMIUM/OPTIMAL-only: a moneyline pick
            # at 75%+ model win probability qualifies regardless of tier,
            # even LEAN or SKIP -- flagged as HIGH HIT % in the email (see
            # _leg_html) rather than blended in silently. A heavy favorite
            # can fail the EV/tier bar (the price is too short to be a
            # good-value bet) while still being a very likely winner, and
            # that's worth surfacing even though it's not a normal pick.
            is_high_hit = _market_type(m.get("side")) == "ML" and prob >= _HIGH_HIT_P
            # Hockey (NHL + SHL/LIIGA/NLA/EXTRALIGA), 2026-10-03: graded on the REAL price by docs/app.html's
            # HOCKEY QUALIFICATION CUTOFFS block. A leg qualifies through the value lane (tier OPTIMAL/PREMIUM) or the explicit
            # high-probability lane (hkLane), and ONLY with a real posted price -- the blunt "ML >= 75% regardless of
            # tier/EV" rule above no longer applies to hockey (the lane replaces it).
            is_hockey = sport in HOCKEY_SPORTS
            lane_leg = False
            if is_hockey:
                if HOCKEY_REQUIRE_REAL_PRICE and m.get("priceSource") != "market":
                    continue
                lane_leg = bool(m.get("hkLane")) and tier_n not in QUALIFYING_TIERS
                is_high_hit = False
                qualifies = tier_n in QUALIFYING_TIERS or bool(m.get("hkLane"))
            else:
                qualifies = tier_n in QUALIFYING_TIERS or is_high_hit
            if qualifies and not guard_ok:
                game_skipped += 1
                skipped_detail.append({"sport": sport, "game": f"{gl.get('awA')} @ {gl.get('hA')}", "leg": m.get("label"),
                                       "startMs": parse_start_ms(gl.get("startMs")), "why": guard_reason,
                                       "prob": m.get("prob"), "tier": TIER_LABEL.get(tier_n, "?") if tier_n is not None else "?"})
                continue
            if qualifies and guard_reason.startswith("no start time"):
                unguarded_legs += 1
            if qualifies:
                game_qualifying.append({
                    "kind": "GAME", "sport": sport, "hA": gl.get("hA"), "awA": gl.get("awA"),
                    "side": m.get("side"), "label": m.get("label"), "prob": m.get("prob"),
                    "ml": m.get("ml"), "dec": m.get("dec"), "tierN": tier_n, "evVal": m.get("evVal"),
                    # True when a hockey leg qualifies ONLY through the high-probability lane (tier below OPTIMAL).
                    "lane": lane_leg,
                    # "market" | "assumed" | None: set only by the 4 European
                    # hockey cards (SHL/LIIGA/NLA/EXTRALIGA, 2026-10-02) --
                    # whether m["ml"]/m["dec"] above is a REAL bookmaker price
                    # or the old model-assumed one. Threaded through
                    # lock_game_leg's extraMeta onto the locked pick. tierN/
                    # evVal above are tier INPUTS and (by default) still
                    # computed from the assumed price -- see app.html's
                    # HOCKEY_MKT_PRICE_TIERING -- so this changes nothing
                    # about WHICH legs qualify.
                    "priceSource": m.get("priceSource"),
                    # Market-blend audit trail (2026-10-03): docs/app.html blends the model probability toward the
                    # no-vig market probability when real prices exist (HOCKEY_MKT_BLEND_ALPHA). `prob` above is the
                    # FINAL blended number (it becomes the pick's winProb); modelProb/marketProb are the two inputs
                    # and blendAlpha the weight used, persisted via lock_game_leg's extraMeta so alpha can later be
                    # refit on real results. None for every leg that had no market to blend with.
                    "modelProb": m.get("modelProb"),
                    "marketProb": m.get("marketProb"),
                    "blendAlpha": m.get("blendAlpha"),
                    # "Why this pick" persistence, added 2026-10-02: the
                    # plain-text reasoning string app.html's _attachReasoning
                    # already computed for THIS specific market (not the
                    # whole card) -- see its own comment there. Threaded
                    # through lock_game_leg's extraMeta the same way
                    # socFactors already is, below, so it lands on the
                    # locked pick's own `reasoning` field. None for any
                    # sport/card not yet wired to _attachReasoning, or for a
                    # market _evalMkts graded but this app.html build
                    # predates this feature -- lockPick() handles a null
                    # here the same as a missing manual-UI cache hit.
                    "reasoning": m.get("reasoning"),
                    # Same for every qualifying market on this game -- carried
                    # per-leg (not de-duped) so build_locks_email_html can
                    # regroup by matchup without needing a second pass over
                    # gameLegs.
                    "mcSummary": gl.get("mcSummary"), "best": gl.get("best"),
                    # Soccer only (see _autoLockCapture/_socExpectedGoals in
                    # app.html) -- per-factor xG multipliers for this
                    # matchup, threaded through lock_game_leg's extraMeta so
                    # a real per-factor backtest becomes possible once
                    # enough settled picks carry it. None for every other
                    # sport, which never passes this.
                    "socFactors": gl.get("socFactors"),
                    # The game's real scheduled start (epoch ms) -- lock_game_leg re-checks it (defence in depth) and
                    # passes it to lockPick, which stores startMs on the pick.
                    "startMs": gl.get("startMs"),
                    # MT calendar date of the game's real start: the date its pick is stamped with (lockPick's id and the
                    # settlement lookup both use the GAME's date). Used by the rolling-horizon evening passes, whose legs span
                    # more than one date; None when the start is unknown (callers then fall back to their own date).
                    "lockDate": mt_date_of_ms(gl.get("startMs")),
                })
        if game_skipped:
            skipped_legs += game_skipped
            skipped_games.append(f"{sport} {gl.get('awA')} @ {gl.get('hA')}")
            log(f"  skip: {guard_reason} -- [{sport}] {gl.get('awA')} @ {gl.get('hA')} "
                f"({game_skipped} qualifying leg(s) not locked)")
        if len(game_qualifying) > 1:
            game_qualifying = _dedupe_opposite_sides(game_qualifying)
        if sport == "CFB" and game_qualifying:
            before = len(game_qualifying)
            game_qualifying = _cfb_select(game_qualifying)
            if len(game_qualifying) < before:
                log(f"  CFB selection: {gl.get('awA')} @ {gl.get('hA')} {before} -> {len(game_qualifying)} leg(s)")
        # Same-game correlated-market cap: originally MLB-only (real ledger
        # data showed MLB routinely locking all 3 markets on one game at
        # once -- ML + run line + O/U each independently qualifying --
        # explicitly identified as a drag on overall accuracy: 3 correlated
        # shots at the same game reads as diversification but isn't).
        # Generalized to every sport after the same live-ledger audit that
        # found _dedupe_opposite_sides' bug also found this exact shape in
        # WNBA (MIN ML + MIN -2.5, same team, same directional read) and
        # CFB (HAW ML + HAW +4.0) -- MLB was never actually special, it
        # just had the highest volume so it surfaced first. Capped at 2:
        # the single best market by default, plus a 2nd ONLY when it's a
        # genuinely complementary combo (ML+O/U or spread+O/U). ML+spread
        # specifically excluded -- those two are essentially the same
        # directional read on the game (who wins/covers) priced two
        # different ways, not an independent second edge. Runs after
        # _dedupe_opposite_sides above, so what's left here is already at
        # most one leg per market type/family -- this only trims ACROSS
        # types (e.g. ML vs SPREAD), never within one.
        if len(game_qualifying) > 1:
            game_qualifying.sort(key=lambda q: (q["tierN"] or 0, q.get("evVal") or 0), reverse=True)
            best = game_qualifying[0]
            best_type = _market_type(best["side"])
            picked = [best]
            for cand in game_qualifying[1:]:
                cand_type = _market_type(cand["side"])
                if {best_type, cand_type} in ({"ML", "OU"}, {"SPREAD", "OU"}):
                    picked.append(cand)
                    break
            game_qualifying = picked
        if game_qualifying and sport in ALT_SPORTS:
            shifted = _apply_alt_lines(game_qualifying)
            for old, new in zip(game_qualifying, shifted):
                if new is not old:
                    log(f"  alt line: [{sport}] {gl.get('awA')} @ {gl.get('hA')} {old['label']} ({(old.get('prob') or 0)*100:.0f}%) -> "
                        f"{new['label']} ({new['prob']*100:.0f}%, est. {new['ml']})")
            game_qualifying = shifted
        qualifying.extend(game_qualifying)
    # Props only exist for NBA/NHL now -- NFL player props removed from
    # generation entirely 2026-09-23 (see gather_legs()'s own comment),
    # so propLegs can never contain an NFL-tagged row here; the per-game
    # NFL prop cap this block used to need (a single game could produce
    # 90+ candidate rows once real rosters flowed in) is gone with it,
    # not left behind as dead code. Neither early pass (soccer, CFB)
    # needs or has any props to filter, so props are simply included
    # only on the unscoped (full) run.
    START_GUARD_TOTALS["skipped"] += skipped_legs
    START_GUARD_TOTALS["unguarded"] += unguarded_legs
    if guard_stats is not None:
        guard_stats.update({"skipped": skipped_legs, "games": skipped_games, "unguarded": unguarded_legs,
                            "detail": skipped_detail})
    log(f"  start guard (margin {LOCK_START_MARGIN_MIN}m): {skipped_legs} legs skipped: game already started"
        f"{' (' + str(len(skipped_games)) + ' game(s))' if skipped_games else ''}"
        + (f"; {unguarded_legs} leg(s) had no start time and fail open" if unguarded_legs else ""))
    if only_sports is None:
        for p in result.get("propLegs") or []:
            if p.get("grade") not in ("PREMIUM", "OPTIMAL"):
                continue
            qualifying.append({"kind": "PROP", "sport": p.get("sportTag"), "leg": p})
    return qualifying


def lock_game_leg(page, q: dict, date_override: str | None = None, now=None) -> str:
    """Calls the real lockPick() directly with the explicit, correct sport
    tag (see module docstring on the type/betType tradeoff this mirrors
    from the app's own real lock buttons).

    date_override: stamps the pick with a specific game date instead of
    today() -- used by the evening-prior soccer lock, which runs the
    NIGHT BEFORE the games it's locking. lockPick's own deterministic id
    is `${date}_${hA}_${awA}_${type}_${betOn}` (see docs/app.html), so
    this must be the game's real calendar date, not the date this script
    happens to run on -- otherwise tonight's lock and tomorrow morning's
    normal same-day lock pass would compute two DIFFERENT ids for the
    same market (today() vs. tomorrow's date) and double-lock it instead
    of the existing dedup naturally catching the overlap."""
    lock_type = SPORT_TO_LOCKPICK_TYPE.get(q["sport"])
    if not lock_type:
        return f"skip: no lockPick type mapping for sport {q['sport']}"
    # Defence in depth for the pre-start guard (build_qualifying already filtered): never touch lockPick for a game that
    # has started / starts within LOCK_START_MARGIN_MIN, or (hockey) whose start is unknown. `now` injectable for tests.
    _ok, _why, _mins = start_guard(q["sport"], q.get("startMs"), now)
    if not _ok:
        return f"skip: {_why} ({q['sport']} {q.get('awA')} @ {q.get('hA')})"
    ml = q.get("ml")
    dec = q.get("dec")
    # Real bug, found auditing lock/settle across every sport: `type` here
    # is always the SPORT tag (see lockPick's own comment in docs/app.html
    # on why -- CL/PL_SOC/etc need it for correct classification), which
    # isn't one of lockPick's betType-shorthand strings ('OU'/'SPREAD'/
    # 'PL'/'RL'/'PROP'), so _betTypeNorm silently defaulted every leg this
    # function ever locked -- ML, SPREAD, and OU alike -- to betType='ML'.
    # Confirmed live: hundreds of real settled MLB/CFB/WNBA/tennis/soccer
    # spread and O/U legs sitting in the ledger tagged 'ML' (grading itself
    # was never affected -- autoSettle* always reads betOn's own text, not
    # betType, same fix pattern already documented there -- but the market
    # label was wrong on every one, and it fed the exact same _findSame
    # MarketLock collision the in-app UI fix (see lockPick's own comment)
    # was written to close: a bot-locked O/U leg mistagged 'ML' silently
    # blocked ever locking that game's real moneyline, and vice versa).
    # q["side"] already carries the leg's real market ('ML'/'over_X'/
    # 'under_X'/'fav'/'dog' etc, see _market_type) at the point this leg
    # was qualified -- reuse that instead of re-parsing q["label"] text.
    bet_type_override = _market_type(q.get("side"))
    sock_factors = q.get("socFactors")
    # "Why this pick" persistence, added 2026-10-02: the plain-text
    # reasoning string gather_legs() already copied onto this q dict from
    # app.html's own _attachReasoning() call (see build_qualifying's own
    # comment just above) -- threaded through the exact same extraMeta slot
    # socFactors already uses, one line below, so lockPick() lands it on
    # the locked pick's own `reasoning` field.
    reasoning_text = q.get("reasoning")
    # 2026-10-02: whether `ml`/`dec` (read above from q) are a real bookmaker
    # price ("market") or the model-assumed one ("assumed"); None for every
    # sport/card that doesn't price from a market yet -- left off the pick.
    price_source = q.get("priceSource")
    # 2026-10-03: market-blend inputs (see build_qualifying) -- model prob / no-vig market prob / alpha used.
    model_prob = q.get("modelProb")
    market_prob = q.get("marketProb")
    blend_alpha = q.get("blendAlpha")
    start_ms = parse_start_ms(q.get("startMs"))
    alt_line = q.get("altLine")
    return page.evaluate(
        """
        async ({ hA, awA, type, betOn, prob, ml, dec, dateOverride, betTypeOverride, socFactors, reasoning, priceSource, modelProb, marketProb, blendAlpha, startMs, altLine }) => {
          // Real gap, found auditing the locks-email "X of Y legs actually
          // locked" line: this used to return a single 'dup-or-failed' for
          // BOTH "this exact leg was already locked by an earlier pass
          // today" (completely normal -- the 3 dedicated morning checks
          // are deliberately redundant) AND a genuine failure, so a caller
          // had no way to tell them apart. Confirmed live: on a day the
          // 3 checks land compressed together in real time (a GitHub
          // Actions scheduling delay, not a locking problem), the first
          // two checks lock nearly everything, leaving the THIRD (the one
          // that actually emails) reporting something like "1 of 15" --
          // reads as a 93% failure rate when 14 of the 15 are actually
          // sitting in the ledger fine, just locked a few minutes earlier
          // by this same morning's own prior check. Pre-checking the
          // SAME dedup lockPick() itself already does internally (its own
          // deterministic id, `${date}_${hA}_${awA}_${type}_${betOn}`,
          // plus its date+hA+awA+betOn fallback match) lets this report
          // "already-locked" as its own real, distinct, non-failure
          // outcome instead of conflating it with an actual problem.
          const dateKey = dateOverride || today();
          const id = `${dateKey}_${hA}_${awA}_${type}_${betOn.replace(/\\s/g,'')}`;
          const preds = getP();
          // Real gap, found+fixed alongside the new one-market-per-game
          // dedup added to lockPick() itself (docs/app.html): this
          // pre-check used to only replicate lockPick's OLD exact-id/
          // exact-betOn check, so a market-level duplicate (e.g. the
          // opposite side of an already-locked spread, or a re-worded ML
          // for the same team) would pass this check as "not a dup", get
          // silently rejected by lockPick's own newer internal guard
          // (getP().length doesn't grow), and get misreported as
          // 'failed' below -- exactly the "already-locked reported as a
          // failure" conflation this same function's own comment already
          // documents fixing once before. typeof _findSameMarketLock is
          // guarded since this eval runs against whatever app.html
          // version is actually deployed.
          const marketDup = (typeof _findSameMarketLock === 'function')
            ? _findSameMarketLock(preds, dateKey, hA, awA, betTypeOverride)
            : null;
          const dup = preds.find(x => x.id === id) ||
                      preds.find(x => x.date === dateKey && x.hA === hA && x.awA === awA && x.betOn === betOn) ||
                      marketDup;
          if (dup) return 'already-locked';
          const before = getP().length;
          const hasBlend = (modelProb != null && marketProb != null);
          const extraMeta = { lockOrigin: 'auto', ...(socFactors ? { socFactors } : {}), ...(reasoning ? { reasoning } : {}), ...(priceSource ? { priceSource } : {}), ...(altLine ? { altLine } : {}), ...(hasBlend ? { modelProb, marketProb, ...(blendAlpha != null ? { blendAlpha } : {}) } : {}), ...(startMs != null ? { startMs } : {}) };
          await lockPick(hA, awA, type, betOn, prob, ml != null ? ml : '-110', dec || 1.91, dateKey, 'manual', betTypeOverride, extraMeta);
          const after = getP().length;
          return after > before ? 'locked' : 'failed';
        }
        """,
        {"hA": q["hA"], "awA": q["awA"], "type": lock_type, "betOn": q["label"], "prob": q["prob"], "ml": ml, "dec": dec, "dateOverride": date_override, "betTypeOverride": bet_type_override, "socFactors": sock_factors, "reasoning": reasoning_text, "priceSource": price_source,
         "modelProb": model_prob, "marketProb": market_prob, "blendAlpha": blend_alpha, "startMs": start_ms, "altLine": alt_line},
    )


def lock_prop_leg(page, sport: str, leg: dict) -> str:
    # Real bug, found and fixed alongside this same audit: lockProp()/
    # lockNHLProp()/lockNFLModelProp() (docs/app.html) used to have NO
    # dedup at all -- a Date.now()-based id, always unique by
    # construction -- unlike lockPick()'s real deterministic-id dedup
    # for game legs. That meant every one of this pipeline's real
    # multiple-passes-per-day (3 dedicated morning checks, by design,
    # relying on idempotent locking to make repeats safe) could have
    # been silently creating duplicate prop locks. All 3 now have a
    # real dedup check and return early (no getP() growth) on a genuine
    # duplicate, so `after > before` here now correctly means "was this
    # exact prop actually already locked" rather than being untestable.
    if sport == "NHL":
        return page.evaluate(
            """
            ({ player, stat, dir, prob, ml, line }) => {
              const before = getP().length;
              lockNHLProp(player, stat, dir, prob, ml, line);
              return getP().length > before ? 'locked' : 'already-locked';
            }
            """,
            {"player": leg.get("player"), "stat": leg.get("stat"),
             "dir": "OVER" if leg.get("over") is not False else "UNDER",
             "prob": leg.get("prob") or (leg.get("conf", 0) / 100), "ml": leg.get("ml"), "line": leg.get("line")},
        )
    if sport == "NBA":
        # Real bug, found and fixed alongside this same audit: this used to
        # call lockProp() with no `stat` arg at all, and lockProp()'s betOn
        # template never included a stat token on its own either -- so
        # every NBA prop this pipeline ever locked produced a betOn like
        # "Victor Wembanyama OVER 26.5" with no PTS/REB/AST anywhere in it.
        # autoSettlePropsESPN()'s parsePropBetOn() (docs/app.html) requires
        # a stat token to parse a betOn at all, so every one of those props
        # silently sat 'pending' forever -- live ledger evidence lines up:
        # the propDiag work above this function was added specifically
        # because real settled PROP counts were far below what gather_legs
        # was locking. lockProp() now takes an optional trailing `stat` arg
        # (docs/app.html) and _generateNBAProps' own pushed objects carry a
        # real ESPN-abbreviation `statAbbr` field (PTS/REB/AST) for exactly
        # this purpose -- passed through here as `leg.statAbbr`.
        return page.evaluate(
            """
            ({ team, player, line, over, prob, ml, sport, opp, stat }) => {
              const before = getP().length;
              lockProp(team, player, line, over, prob, ml, sport, opp, stat);
              return getP().length > before ? 'locked' : 'already-locked';
            }
            """,
            {"team": leg.get("team"), "player": leg.get("player"), "line": leg.get("line"),
             "over": leg.get("over") is not False, "prob": leg.get("prob") or (leg.get("conf", 0) / 100),
             "ml": leg.get("ml"), "sport": sport, "opp": leg.get("opp") or "",
             "stat": leg.get("statAbbr") or ""},
        )
    # NFL branch removed 2026-09-23 alongside NFL player props leaving
    # gather_legs() entirely (see its own comment) -- a leg tagged "NFL"
    # can no longer reach this function at all. lockNFLModelProp() and
    # its in-app PROPS tab (manual/personal viewing only by that point)
    # were removed entirely 2026-09-27, explicit follow-up request --
    # this branch is now permanently unreachable dead code, kept only so
    # the sport-dispatch shape here matches lock_prop_leg's own history.
    return "skip: unhandled prop sport"


# Same threshold + same side classification _evalMkts() uses in
# docs/app.html (an 'over'/'under'/'setsOver'/'setsUnder' side is OU, an
# 'rlFav'/'rlDog'/'plFav'/'plDog'/'sprdFav'/'sprdDog'/'ahFav'/'ahDog' side
# is SPREAD, everything else -- including soccer's home/draw/away team-
# name sides -- is ML) and the exact threshold _highHitBadgeHTML() uses
# on the game cards, so "HIGH HIT %" in this email means the same thing
# it means on screen.
_HIGH_HIT_P = 0.75
_OU_SIDES = {"over", "under", "setsOver", "setsUnder"}
_SPREAD_SIDES = {"rlFav", "rlDog", "plFav", "plDog", "sprdFav", "sprdDog", "ahFav", "ahDog"}


def _market_type(side: str | None) -> str:
    if side in _OU_SIDES:
        return "OU"
    if side in _SPREAD_SIDES:
        return "SPREAD"
    return "ML"


_TIER_COLOR = {3: "#ffdd00", 2: "#00e5ff", 1: "#6699ff", 0: "#666"}  # PREMIUM/OPTIMAL/LEAN/SKIP
_GRADE_COLOR = {"PREMIUM": "#ffdd00", "OPTIMAL": "#00e5ff", "LEAN": "#6699ff"}
_LANE_COLOR = "#ff9f1c"  # hockey HIGH PROB lane badge


def _leg_html(q: dict) -> str:
    """One block per qualifying leg: the tier/grade badge on its own line
    ABOVE the pick (not inline before it) -- applies identically to every
    sport and league since both branches (game markets, player props)
    share this one function, no per-sport variant to keep in sync.
    Probability and EV only -- no betting line/odds shown (explicitly
    requested) -- plus (for a moneyline pick at 75%+ model probability)
    the same HIGH HIT % tag the game cards show, so a heavy favorite
    that clears the hit-rate bar but not the EV bar still gets flagged
    here even if its tier is only LEAN. Props carry no EV field in the
    underlying data (only game markets do), so a prop leg's line only
    ever shows probability -- not a fabricated EV."""
    if q["kind"] == "GAME":
        tier_n = q["tierN"]
        tier_lbl = TIER_LABEL.get(tier_n, "?")
        color = _TIER_COLOR.get(tier_n, "#666")
        ev_val = q.get("evVal")
        ev_str = f' · EV {ev_val*100:+.1f}%' if ev_val is not None else ''
        prob = q.get("prob") or 0
        hh = ' <span style="color:#ffdd00">🔥 HIGH HIT %</span>' if (_market_type(q.get("side")) == "ML" and prob >= _HIGH_HIT_P
                                                                     and q.get("sport") not in HOCKEY_SPORTS) else ''
        if q.get("lane"):
            tier_lbl, color = HOCKEY_LANE_LABEL, _LANE_COLOR
        return (f'<div style="padding:5px 0">'
                f'<span style="background:{color};color:#000;font-weight:700;font-size:11px;padding:1px 7px;'
                f'border-radius:3px;display:inline-block;margin-bottom:3px">{tier_lbl}</span>'
                f'<div style="font-size:14px;color:#eee">{_esc(q["label"])} — {prob*100:.0f}%{ev_str}{hh}</div>'
                f'</div>')
    leg = q["leg"]
    direction = "UNDER" if leg.get("over") is False else "OVER"
    prob = leg.get("prob") if leg.get("prob") is not None else (leg.get("conf", 0) / 100)
    prob = prob or 0
    grade = leg.get("grade") or ""
    color = _GRADE_COLOR.get(grade, "#666")
    return (f'<div style="padding:5px 0">'
            f'<span style="background:{color};color:#000;font-weight:700;font-size:11px;padding:1px 7px;'
            f'border-radius:3px;display:inline-block;margin-bottom:3px">{_esc(grade)}</span>'
            f'<div style="font-size:14px;color:#eee">{_esc(leg.get("player"))} {direction} {_esc(leg.get("line"))} {_esc(leg.get("stat"))} — {prob*100:.0f}%</div>'
            f'</div>')


def _prop_matchup_key(leg: dict) -> str:
    team = leg.get("team") or leg.get("hA") or ""
    opp = leg.get("opp") or leg.get("awA") or ""
    return f"{team} vs {opp}" if opp else team


def build_locks_email_html(qualifying: list[dict], live: bool, locked_count: int | None = None) -> str:
    """Groups qualifying legs by sport/league, then by matchup within each
    sport: the matchup, every qualifying pick for it (grade, probability,
    EV, odds), and the real MC/model reasoning behind it (not necessarily
    one of the qualifying picks itself -- shown either way as context).
    Every league gets its own same-weight section header, sorted by
    display name -- no Europe/North America region grouping (explicitly
    removed; a mixed soccer email now reads as six flat league sections,
    same as any other multi-league product)."""
    by_sport: dict[str, dict[str, list[dict]]] = {}
    for q in qualifying:
        sport = q["sport"]
        matchup = f"{q['awA']} @ {q['hA']}" if q["kind"] == "GAME" else _prop_matchup_key(q["leg"])
        by_sport.setdefault(sport, {}).setdefault(matchup, []).append(q)

    # Real bug, found via audit: "actually locked" reads as "this specific
    # send only managed to lock N of them" -- misleading on a day the
    # morning's dedicated checks land close together in real time (a
    # GitHub Actions scheduling delay, not a locking problem): the first
    # couple of checks can lock nearly everything, leaving the one that
    # actually emails reporting almost no NEW locks even though the
    # picks are all really there. locked_count is now the real
    # confirmed-as-of-right-now total (new this pass + already locked by
    # an earlier pass today), not just this pass's own delta -- "are
    # locked" instead of "actually locked" reflects that.
    status_line = (
        f"LIVE — {locked_count} of {len(qualifying)} qualifying legs are locked"
        if live else
        f"DRY RUN — {len(qualifying)} legs qualified, nothing was written"
    )
    # Banner sits outside _EMAIL_WRAP_OPEN's padding (same hosted asset as
    # the receipt email in _subscribers.py -- see EMAIL_BANNER_URL there
    # for the hosting rationale) so it bleeds edge-to-edge across the
    # full 640px card width instead of sitting inset.
    banner_html = (
        f'<div style="max-width:640px;margin:0 auto"><img src="{EMAIL_BANNER_URL}" '
        f'alt="Clairvoyance Engine" width="640" '
        f'style="display:block;width:100%;max-width:640px;height:auto;border:0;'
        f'font-family:-apple-system,sans-serif;color:#999" /></div>'
    )
    parts = [banner_html, _EMAIL_WRAP_OPEN, f'<div style="font-size:12px;letter-spacing:1px;color:#555;text-transform:uppercase">{_esc(status_line)}</div>']

    high_hit = [q for q in qualifying if q["kind"] == "GAME" and _market_type(q.get("side")) == "ML"
                and (q.get("prob") or 0) >= _HIGH_HIT_P and q.get("sport") not in HOCKEY_SPORTS]
    lane_legs = [q for q in qualifying if q["kind"] == "GAME" and q.get("lane")]
    if lane_legs:
        n = len(lane_legs)
        parts.append(f'<div style="background:#fff1de;border:1px solid #e08a00;'
                      f'border-radius:4px;padding:8px 12px;margin:10px 0;font-size:13px;color:#7a4500">'
                      f'{n} {HOCKEY_LANE_LABEL} hockey pick{"s" if n != 1 else ""} today '
                      f'(likely-to-hit picks at short prices -- see "What the grades mean")</div>')
    if high_hit:
        n = len(high_hit)
        parts.append(f'<div style="background:#fff6d6;border:1px solid #e6c200;'
                      f'border-radius:4px;padding:8px 12px;margin:10px 0;font-size:13px;color:#7a5900">'
                      f'🔥 {n} HIGH HIT % moneyline pick{"s" if n != 1 else ""} today '
                      f'(75%+ model win probability, regardless of tier/EV)</div>')

    # Grade + EV legend -- explains what every reader needs to interpret the picks below before they hit any of them.
    # Rewritten 2026-10-03 (hockey real-price cutoffs): hockey picks are graded against the REAL consensus market price (not an
    # assumed one), EV is measured against that price, and a clearly-labelled HIGH PROB lane carries likely-to-hit picks at short
    # prices. The numbers below come from the HOCKEY_* constants (mirrors of docs/app.html; verify_hockey_rules.py enforces it).
    # Non-hockey sports still qualify on PREMIUM/OPTIMAL plus the 75%+ moneyline HIGH HIT % exception, described last.
    # Shown on every send, including the empty/no-picks-today one, since it's reference material.
    _pct = lambda x: f"{x * 100:.0f}%"
    _evp = lambda x: f"{x * 100:+.0f}%"
    parts.append(
        '<div style="background:#14001f;border-radius:6px;padding:14px 18px;margin:14px 0 18px">'
        '<div style="font-size:11px;letter-spacing:1.5px;color:#f20cff;text-transform:uppercase;'
        'font-weight:700;margin-bottom:8px">What the grades mean</div>'
        f'<div style="font-size:13px;color:#eee;line-height:1.6"><span style="background:{_TIER_COLOR[3]};'
        'color:#000;font-weight:700;font-size:11px;padding:1px 7px;border-radius:3px">PREMIUM</span> '
        'the model\'s highest-confidence picks -- the strongest combination of win probability and '
        f'edge. &nbsp; <span style="background:{_TIER_COLOR[2]};color:#000;font-weight:700;font-size:11px;'
        'padding:1px 7px;border-radius:3px">OPTIMAL</span> still clears our bar, just with somewhat '
        'less confidence or edge than PREMIUM. These are the two value grades: for hockey (NHL, SHL, Liiga, NLA, Extraliga) they '
        f'need a win probability of at least {_pct(HOCKEY_TIER_PROB["OPTIMAL"])} (OPTIMAL) / {_pct(HOCKEY_TIER_PROB["PREMIUM"])} (PREMIUM) '
        f'AND an edge of at least {_evp(HOCKEY_TIER_EV["OPTIMAL"])} / {_evp(HOCKEY_TIER_EV["PREMIUM"])} over the real market price.</div>'
        '<div style="font-size:13px;color:#eee;line-height:1.6;margin-top:10px">'
        '<strong style="color:#fff">EV (Expected Value)</strong> the model\'s estimated long-run profit '
        'edge over the market price, as a percentage of stake -- e.g. EV +8.1% means the model expects '
        'this pick to profit about 8.1% of stake on average if made repeatedly at this probability and '
        'price. For hockey the price is the consensus (median) of the bookmakers\' posted prices at lock time -- '
        'not an assumed number -- and it includes the bookmakers\' margin, so even a fairly priced pick shows a negative EV about equal to the margin '
        '(roughly -4% to -7%) and a positive EV means the model sees real value beyond the margin. '
        'Game picks show EV; player props don\'t carry an EV figure in the underlying data, so '
        'only probability is shown for those. A hockey pick is only ever made when a real market price exists for it.</div>'
        '<div style="font-size:13px;color:#eee;line-height:1.6;margin-top:10px">'
        f'<span style="background:{_LANE_COLOR};color:#000;font-weight:700;font-size:11px;padding:1px 7px;border-radius:3px">'
        f'{HOCKEY_LANE_LABEL}</span> hockey only: a moneyline pick (or a +1.5 puck-line underdog) the model gives at least '
        f'{_pct(HOCKEY_LANE_ML_P)} to win (cover), whose price is not worse than {_evp(HOCKEY_LANE_EV_MIN)} EV. These are '
        'built to hit often, at short prices: expect a high win rate and a return close to break-even after the bookmakers\' margin, '
        'not a value edge. They are included even when the grade would otherwise be LEAN or SKIP, and are labelled so you can '
        'tell them apart from the value picks.</div>'
        '<div style="font-size:13px;color:#eee;line-height:1.6;margin-top:10px">'
        '🔥 <strong style="color:#fff">HIGH HIT %</strong> every other sport: a '
        'moneyline pick at 75%+ model win probability is included regardless of tier -- even a '
        f'<span style="background:{_TIER_COLOR[1]};color:#000;font-weight:700;font-size:11px;padding:1px 7px;'
        f'border-radius:3px">LEAN</span> or <span style="background:{_TIER_COLOR[0]};color:#fff;font-weight:700;'
        'font-size:11px;padding:1px 7px;border-radius:3px">SKIP</span> grade. That happens when a heavy '
        'favorite\'s price is too short to be good EV, but the model still thinks it wins very often -- '
        'worth knowing about even though it\'s not a normal pick.</div>'
        '</div>'
    )

    if not qualifying:
        parts.append('<div style="padding:20px 0;color:#555;font-size:14px">No picks cleared the bar today.</div>')
        parts.append(_LOCKS_EMAIL_CLOSE)
        return "".join(parts)

    def _render_sport_section(sport: str) -> None:
        matchups = by_sport[sport]
        total_legs = sum(len(v) for v in matchups.values())
        parts.append(f'<div style="font-size:13px;letter-spacing:2px;color:#f20cff;text-transform:uppercase;'
                      f'text-shadow:0 0 8px rgba(242,12,255,.6);margin:22px 0 10px;'
                      f'border-bottom:1px solid rgba(242,12,255,.3);padding-bottom:4px">'
                      f'{_esc(SPORT_DISPLAY_NAME.get(sport, sport))} ({total_legs})</div>')
        for matchup, legs in matchups.items():
            parts.append('<div style="background:#14001f;border-radius:6px;padding:12px 14px;margin-bottom:10px">')
            parts.append(f'<div style="font-weight:700;font-size:15px;color:#fff;margin-bottom:6px">{_esc(matchup)}</div>')
            parts.append("".join(_leg_html(q) for q in legs))
            mc_summary = next((l.get("mcSummary") for l in legs if l.get("mcSummary")), None)
            best = next((l.get("best") for l in legs if l.get("best")), None)
            if mc_summary or best:
                bits = []
                if mc_summary:
                    bits.append(_esc(mc_summary))
                if best:
                    bits.append(f'Best market value on this game: {_esc(best["label"])} ({HOCKEY_LANE_LABEL if best.get("lane") else TIER_LABEL.get(best.get("tierN"), "?")})')
                parts.append(f'<div style="border-top:1px solid rgba(255,255,255,.12);margin-top:8px;padding-top:8px;'
                              f'font-size:12px;color:#bbb;line-height:1.5">{"<br>".join(bits)}</div>')
            parts.append('</div>')

    for sport in sorted(by_sport, key=lambda s: SPORT_DISPLAY_NAME.get(s, s)):
        _render_sport_section(sport)

    parts.append(_LOCKS_EMAIL_CLOSE)
    return "".join(parts)


def send_locks_email(qualifying: list[dict], live: bool, locked_count: int | None = None,
                      label: str = "", to: list[str] | None = None, date_str: str | None = None) -> None:
    # Real bug, found via audit 2026-09-24: every call site across every
    # lock function (run_lock, run_euro_early_lock, run_soccer_evening_
    # lock, run_cfb_evening_lock, run_hockey_evening_lock, run_lock_
    # segmented) passes the real recipients_for(product) list (owner +
    # every active paying subscriber) regardless of `live` -- so ANY
    # dry-run invocation (a manual workflow_dispatch test left unchecked,
    # or LIVE_MODE ever unset for a scheduled run) sent every real
    # subscriber a "[DRY RUN] Would lock..." email. This directly
    # contradicts this file's own documented SAFETY principle (scheduled
    # dry-runs "log exactly what WOULD be locked/settled and write
    # nothing") -- a dry run should never be subscriber-visible. Anyone
    # who genuinely wants to preview the real template already has
    # scripts/send_demo_emails.py (synthetic data, explicitly built for
    # that, never touches real subscribers). Dry-run now always routes to
    # the owner only, regardless of what `to` the caller passed.
    recipients = ([OWNER_EMAIL] if OWNER_EMAIL else []) if not live else (to if to is not None else ([LOCKS_EMAIL_TO] if LOCKS_EMAIL_TO else []))
    if not recipients:
        log("No recipients (LOCKS_EMAIL_TO unset and none passed) — skipping locked-picks email")
        return
    # date_str override: the evening-prior soccer lock passes the actual
    # game date (tomorrow) here -- otherwise this'd default to UTC "now",
    # which reads as tonight's date on a subject line about tomorrow's
    # games.
    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tag = f"{label} " if label else ""
    subject = f"Clairvoyance — {'Locked' if live else '[DRY RUN] Would lock'} {tag}picks for {date_str} ({len(qualifying)})"
    body_html = build_locks_email_html(qualifying, live, locked_count)
    ok, msg = _send_gmail(subject, recipients, body_html)
    log(f"Locks email ({label or 'ALL'}) sent to {len(recipients)} recipient(s)" if ok else f"Locks email ({label or 'ALL'}) send failed: {msg}")


# ─────────────────────────────────────────────────────────────────────────
# TOP PICKS DIGEST -- owner-only, parlay-focused daily summary
# ─────────────────────────────────────────────────────────────────────────
# Explicit request: a separate email, ONLY to clairvoyanceengine@gmail.com
# (never the subscriber lists send_locks_email uses), ranking that day's
# ALREADY-LOCKED picks by model win probability -- top 4 per league, top 7
# overall -- with the reasoning laid out for parlay consideration. Reads
# directly from today's real locked ledger rather than re-running
# gather_legs() a second time: by the time this runs (right after the
# final morning lock check), every qualifying leg for the day is already
# in Supabase, so this is a fresh re-pull + re-rank, not a second
# expensive browser/data warmup pass.
TOP_PICKS_EMAIL_TO = "clairvoyanceengine@gmail.com"


def gather_todays_locked_bets(page, date_iso: str | None = None) -> list[dict]:
    """Fresh Supabase pull filtered to the target date's real locked
    picks, any outcome (not just pending) -- an early kickoff may have
    already settled by 7:35am MT, and it was still one of today's best
    picks. Calls load_bet_ledger itself so this is guaranteed fresh
    regardless of what already happened earlier in the same page
    session."""
    target = date_iso or datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%d")
    load_bet_ledger(page)
    bets = page.evaluate("(d) => getP().filter(p => p.date === d)", target)
    # Pre-start locks only (2026-10-02): a pick locked after its game started (manual late lock) is not a "top pick" the model
    # called in advance and may already carry a WON/LOST badge -- leave it out of the digest (shared classifier).
    kept, late = lock_timing.split_picks(bets, lock_timing.load_index())
    if late:
        log(f"Top-picks digest: {len(late)} known-late (locked after game start) pick(s) left out")
    return kept


def _dec_to_american(dec: float) -> str:
    if dec is None or dec <= 1:
        return "?"
    if dec >= 2.0:
        return f"+{round((dec - 1) * 100)}"
    return f"{round(-100 / (dec - 1))}"


def _pick_edge_pp(bet: dict) -> float:
    """Model win probability minus the price's own implied probability,
    in percentage points -- the same real-edge framing _leg_html already
    uses elsewhere, computed here from decOdds since that's what's
    actually stored on a locked pick (no separate evVal field survives
    onto the ledger row)."""
    prob = bet.get("winProb") or 0
    dec = bet.get("decOdds") or 1.91
    implied = 1 / dec if dec else 0.5238
    return (prob - implied) * 100


def build_top_picks_digest(bets: list[dict]) -> dict:
    """Ranks purely by model win probability (not tier/EV) -- explicit
    request, since a parlay's real hit rate is bounded by its weakest
    leg's own probability, not by average edge. top7 is computed
    independently across ALL of today's locked bets, not just assembled
    from the per-league top-4 pools, so one unusually strong league can
    correctly contribute more than one leg to the overall top 7."""
    ranked = sorted(bets, key=lambda b: b.get("winProb") or 0, reverse=True)
    top7 = ranked[:7]
    by_league: dict[str, list[dict]] = {}
    for b in bets:
        lg = b.get("league") or b.get("sport") or "OTHER"
        by_league.setdefault(lg, []).append(b)
    top4_by_league = {
        lg: sorted(legs, key=lambda b: b.get("winProb") or 0, reverse=True)[:4]
        for lg, legs in by_league.items()
    }
    return {"top7": top7, "top4ByLeague": top4_by_league}


def _parlay_math(bets: list[dict]) -> dict:
    """Combined hit probability (product of each leg's own model win
    probability) and combined payout (product of each leg's decimal
    odds) for treating this set as one parlay. Shown deliberately
    alongside the individual numbers, never in place of them --
    stacking legs compounds risk fast (even at a strong 70% average per
    leg, 7 legs multiplies down to under 10% combined), and hiding that
    math would misrepresent what a 7-leg parlay actually is."""
    combined_p = 1.0
    combined_dec = 1.0
    for b in bets:
        combined_p *= (b.get("winProb") or 0.5)
        combined_dec *= (b.get("decOdds") or 1.91)
    return {"combinedProb": combined_p, "combinedDec": combined_dec, "combinedAmerican": _dec_to_american(combined_dec)}


def _digest_pick_row_html(bet: dict) -> str:
    prob = (bet.get("winProb") or 0) * 100
    edge = _pick_edge_pp(bet)
    matchup = f"{bet.get('awA') or '?'} @ {bet.get('hA') or '?'}"
    dec = bet.get("decOdds") or 1.91
    implied = (1 / dec * 100) if dec else 52.4
    edge_col = "#00c853" if edge > 0 else "#ff5252"
    outcome = bet.get("outcome")
    outcome_badge = ""
    if outcome == "win":
        outcome_badge = ' <span style="color:#00c853;font-weight:700">✓ WON</span>'
    elif outcome == "loss":
        outcome_badge = ' <span style="color:#ff5252;font-weight:700">✗ LOST</span>'
    return (
        '<div style="background:#14001f;border-radius:6px;padding:12px 14px;margin-bottom:8px">'
        f'<div style="font-weight:700;font-size:15px;color:#fff;margin-bottom:2px">{_esc(matchup)}{outcome_badge}</div>'
        f'<div style="font-size:14px;color:#e0c9ff;margin-bottom:6px">{_esc(bet.get("betOn") or "")} '
        f'<span style="color:#888">· {_esc(str(bet.get("ml") or ""))}</span></div>'
        f'<div style="font-size:13px;color:#bbb;line-height:1.5">'
        f'<strong style="color:#f20cff">{prob:.1f}%</strong> model probability vs '
        f'<strong>{implied:.1f}%</strong> implied by the price — '
        f'<strong style="color:{edge_col}">{"+" if edge >= 0 else ""}{edge:.1f}pp edge</strong>'
        f'</div>'
        '</div>'
    )


def build_top_picks_digest_html(digest: dict, date_str: str) -> str:
    top7 = digest["top7"]
    top4_by_league = digest["top4ByLeague"]
    banner_html = (
        f'<div style="max-width:640px;margin:0 auto"><img src="{EMAIL_BANNER_URL}" '
        f'alt="Clairvoyance Engine" width="640" '
        f'style="display:block;width:100%;max-width:640px;height:auto;border:0;'
        f'font-family:-apple-system,sans-serif;color:#999" /></div>'
    )
    parts = [banner_html, _EMAIL_WRAP_OPEN,
             f'<div style="font-size:12px;letter-spacing:1px;color:#555;text-transform:uppercase">TOP PICKS DIGEST — {_esc(date_str)}</div>']

    if not top7:
        parts.append('<div style="padding:20px 0;color:#555;font-size:14px">No picks locked today.</div>')
        parts.append(_LOCKS_EMAIL_CLOSE)
        return "".join(parts)

    pm = _parlay_math(top7)
    parts.append(
        # Note: this header/paragraph pair sits directly on _EMAIL_WRAP_OPEN's
        # white background (unlike the per-pick rows below, each of which
        # has its own dark #14001f card) -- colors here must be legible on
        # WHITE, matching the same convention build_locks_email_html's own
        # status_line/section headers already use (#f20cff / #555), not the
        # white/light-gray palette used inside the dark pick cards.
        '<div style="font-size:20px;font-weight:700;color:#f20cff;margin:18px 0 4px">'
        f'TOP 7 OVERALL — {_esc(date_str)} — {len(top7)} PICKS</div>'
        '<div style="font-size:13px;color:#555;line-height:1.6;margin-bottom:12px">'
        "Ranked purely by model win probability across every sport locked today — the metric that matters "
        "most for parlaying, since a parlay's real hit rate is bounded by its weakest leg, not its average "
        "edge. These are the 7 individual plays the model is most confident in today, regardless of "
        "sport or market type."
        '</div>'
        '<div style="background:#1a0028;border:1px solid rgba(242,12,255,.3);border-radius:6px;'
        'padding:12px 16px;margin-bottom:14px">'
        f'<div style="font-size:13px;color:#ccc;line-height:1.7">'
        f'<strong style="color:#fff">If parlayed together:</strong> an estimated '
        f'<strong style="color:#f20cff">{pm["combinedProb"]*100:.1f}%</strong> chance all 7 hit, '
        f'paying roughly <strong style="color:#f20cff">{_esc(pm["combinedAmerican"])}</strong> '
        f'({pm["combinedDec"]:.1f}x) if they do.<br>'
        f'<span style="color:#999">Stacking legs compounds fast — even strong individual picks multiply down '
        f'to a real long-shot combined. This is the honest math, not a recommendation to parlay all 7 at '
        f'full size.</span></div></div>'
    )
    parts.append("".join(_digest_pick_row_html(b) for b in top7))

    for lg in sorted(top4_by_league, key=lambda k: LEDGER_LEAGUE_DISPLAY_NAME.get(k, k)):
        legs = top4_by_league[lg]
        parts.append(
            f'<div style="font-size:13px;letter-spacing:2px;color:#f20cff;text-transform:uppercase;'
            f'text-shadow:0 0 8px rgba(242,12,255,.6);margin:24px 0 10px;'
            f'border-bottom:1px solid rgba(242,12,255,.3);padding-bottom:4px">'
            f'{_esc(LEDGER_LEAGUE_DISPLAY_NAME.get(lg, lg))} — {_esc(date_str)} — TOP {len(legs)}</div>'
        )
        parts.append("".join(_digest_pick_row_html(b) for b in legs))

    parts.append(_LOCKS_EMAIL_CLOSE)
    return "".join(parts)


def send_top_picks_digest_email(bets: list[dict], date_str: str | None = None) -> None:
    date_str = date_str or datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%d")
    digest = build_top_picks_digest(bets)
    subject = f"Clairvoyance — Top Picks Digest — {date_str} ({len(digest['top7'])} overall)"
    body_html = build_top_picks_digest_html(digest, date_str)
    ok, msg = _send_gmail(subject, [TOP_PICKS_EMAIL_TO], body_html)
    log(f"Top picks digest sent to {TOP_PICKS_EMAIL_TO}" if ok else f"Top picks digest send failed: {msg}")


class LockResult(NamedTuple):
    """new: genuinely newly locked this pass. already_locked: a real,
    confirmed duplicate -- this exact leg was already sitting in the
    ledger (from an earlier pass today, most commonly -- see the 3
    dedicated morning checks) -- not a failure. failed: lock_*_leg
    threw, or returned neither 'locked' nor 'already-locked' (a genuine
    problem, worth surfacing distinctly instead of folding into a vague
    catch-all).

    confirmed (new + already_locked) is the number that actually answers
    "of today's qualifying legs, how many are really locked right now" --
    this is what the locks email's headline count should use, NOT `new`
    alone, which used to be misreported as if it were the whole story
    (see the real "1 of 15" audit finding this type was added to fix)."""
    new: int
    already_locked: int
    failed: int
    # Legs refused by the pre-start guard inside lock_game_leg (game already started / starts within the margin). NOT a
    # failure: the pass did its job. Defaults to 0 so every existing LockResult(0, 0, 0) call site still works.
    skipped: int = 0
    # Labels of the legs that genuinely failed to lock (for the owner's alert); empty when none.
    failed_labels: tuple = ()

    @property
    def confirmed(self) -> int:
        return self.new + self.already_locked


def _lock_qualifying_legs(page, qualifying: list[dict], date_override: str | None = None,
                          per_leg_dates: bool = False) -> LockResult:
    """Actually calls the real lockPick()/lockProp()/etc. for each leg.
    Shared by run_lock() (single-product early passes) and
    run_lock_segmented() (the main run's per-product loop) so there's one
    place this logic lives, not two copies that could drift.

    date_override: see lock_game_leg's own docstring -- only ever passed
    by the evening-prior soccer lock, which locks GAME legs for a date
    that isn't today() yet. Props never use this (there are none in the
    evening-prior pass's qualifying list -- only NBA/NHL have prop legs
    now (NFL's removed 2026-09-23), none of which run on this path).

    Real gap, found and fixed in the same audit that added this type:
    'locked' vs 'already-locked' vs 'failed' used to collapse into a
    single non-'locked' bucket (the old 'dup-or-failed' string), so a
    genuine lock failure and a completely normal same-day re-check
    dedup were indistinguishable in both the logs and the email. Now
    logged and counted separately."""
    new = already_locked = failed = skipped = 0
    failed_labels: list[str] = []
    for q in qualifying:
        try:
            # per_leg_dates (rolling-horizon evening passes): every leg is stamped with ITS OWN game's Mountain date, so one
            # pass can lock today's and tomorrow's games; legs with an unknown start fall back to date_override.
            leg_date = (q.get("lockDate") or date_override) if per_leg_dates else date_override
            outcome = lock_game_leg(page, q, leg_date) if q["kind"] == "GAME" else lock_prop_leg(page, q["sport"], q["leg"])
            label = q.get('label') or q.get('leg', {}).get('player')
            if outcome == "locked":
                new += 1
            elif outcome == "already-locked":
                already_locked += 1
                log(f"  already locked (earlier pass today): {label}")
            elif isinstance(outcome, str) and outcome.startswith(("skip: game", "skip: no usable start")):
                skipped += 1
                START_GUARD_TOTALS["skipped"] += 1
                log(f"  skip: {outcome[6:]} -- {label}")
            else:
                failed += 1
                failed_labels.append(str(label))
                log(f"  FAILED to lock ({outcome}): {label}")
        except Exception as exc:
            failed += 1
            failed_labels.append(str(q.get("label") or q.get("leg", {}).get("player")))
            log(f"  FAILED to lock: {exc}")
    if skipped:
        log(f"  {skipped} leg(s) skipped at lock time: game already started")
    return LockResult(new, already_locked, failed, skipped, tuple(failed_labels))


def count_pending_for_dates(page, dates: list[str]) -> list[int]:
    """Pending picks Supabase really holds per game date -- one tiny id-only query per date (a few hundred bytes), NOT another complete ledger
    pull. The verify step used to re-pull the whole ~3,900-row ledger just to count a couple of dates, which on top of the session's initial pull
    doubled the Supabase egress of every lock run (the free-tier quota reached 96% of its cycle on 2026-10-03). If the scoped query fails, or
    the run is in degraded mode, the count comes from the in-page ledger (never a reload over this run's changes)."""
    counts = None if LEDGER_DEGRADED else page.evaluate(
        """
        async (ds) => {
          const out = [];
          for (const d of ds) {
            const r = await fetch(SUPABASE_URL + '/rest/v1/bets?select=id&date=eq.' + encodeURIComponent(d) + '&outcome=eq.pending', {
              headers: { apikey: SUPABASE_KEY, Authorization: 'Bearer ' + SUPABASE_KEY },
            });
            if (!r.ok) return null;
            out.push((await r.json()).length);
          }
          return out;
        }
        """,
        list(dates),
    )
    if counts is None or LEDGER_DEGRADED:
        if counts is None and not LEDGER_DEGRADED:
            log("WARNING: Supabase count query failed -- verifying against the in-page ledger only (UNVERIFIED on Supabase)")
        counts = page.evaluate(
            "(ds) => ds.map(d => getP().filter(p => p.date === d && p.outcome === 'pending').length)", list(dates))
    return counts


def verify_locks_for_date(page, date_iso: str | None = None) -> int:
    """The actual TEST behind "make sure picks locked correctly": a fresh,
    independent re-pull straight from Supabase (not just trusting the
    in-page state _lock_qualifying_legs already mutated) confirming the
    target date's locked picks are really persisted and readable. Catches
    the class of bug where lockPick() succeeds locally but
    flush_to_supabase's own write silently fails (network hiccup, etc) --
    "the function didn't throw" is not the same guarantee as "the data is
    actually there." Safe/cheap to call after every lock attempt,
    including redundant ones in the same morning.

    date_iso: defaults to today (America/Denver) -- pass the game date
    explicitly for the evening-prior soccer lock, which verifies
    TOMORROW's date, not today's."""
    target = date_iso or datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%d")
    count = count_pending_for_dates(page, [target])[0]
    log(f"VERIFY: fresh Supabase count shows {count} pick(s) locked for {target}")
    return count


def verify_locks_for_dates(page, dates: list[str]) -> int:
    """verify_locks_for_date over several dates with ONE ledger pull (the rolling-horizon passes lock picks dated today AND
    tomorrow). Returns the total number of pending picks found for those dates."""
    counts = count_pending_for_dates(page, list(dates))
    log("VERIFY: fresh Supabase count shows pending picks locked per date: "
        + ", ".join(f"{d}={c}" for d, c in zip(dates, counts)))
    return sum(counts)


# Back-compat alias -- every existing call site in this file passes no
# args and means "today"; kept as a thin wrapper rather than touching
# every call site for a rename that adds no behavior change there.
def verify_todays_locks(page) -> int:
    return verify_locks_for_date(page)


def run_lock(page, live: bool, only_sports: frozenset[str] | None = None, label: str = "",
              to: list[str] | None = None, send_email: bool = True) -> int:
    """Single-product lock pass -- used by the early soccer/CFB workflows,
    which each run their own gather_legs() call at their own scheduled
    time (not part of the main run's per-product loop). send_email=False
    for a redundant same-morning retry that already had its one email
    sent by an earlier attempt today -- still locks (idempotent -- see
    _lock_qualifying_legs/lockPick's own dedup) and still verifies, just
    doesn't duplicate the subscriber email. Returns how many legs were
    locked this pass (0 if none/dry-run), so the caller can cross-check
    against verify_todays_locks and catch a silent Supabase-write failure
    instead of trusting "the function didn't throw"."""
    log(f"=== AUTO-LOCK (PREMIUM/OPTIMAL){' — ' + label if label else ''} ===")
    result = gather_legs(page)
    guard: dict = {}
    qualifying = build_qualifying(result, only_sports=only_sports, guard_stats=guard)
    analysis = analyze_games(result, only_sports)
    rep = {"kind": f"early-{(label or 'all').lower()}", "label": label or "ALL", "dates": [datetime.now(MT).strftime("%Y-%m-%d")],
           "qualifying": len(qualifying), "new": 0, "already": 0, "failed": 0, "skipped": guard.get("skipped", 0),
           "skippedDetail": guard.get("detail", []), "failedLabels": [], "games": analysis["games"],
           "started": analysis["started"], "upcoming": analysis["upcoming"], "nextStartMs": analysis["nextStartMs"],
           "noPrice": analysis["noPrice"], "live": live, "complete": False}
    PASS_REPORTS.append(rep)
    log(f"Gathered {len(result.get('gameLegs') or [])} games' worth of markets, "
        f"{len(result.get('propLegs') or [])} prop legs total")
    prop_diag = result.get("propDiag") or {}
    if prop_diag:
        log("  prop generation: " + ", ".join(f"{sp}={detail}" for sp, detail in prop_diag.items()))
    log(f"{len(qualifying)} qualifying PREMIUM/OPTIMAL legs found" + (f" ({label.lower()} only)" if label else ""))

    for q in qualifying:
        if q["kind"] == "GAME":
            log(f"  [{q['sport']}] {q['label']} ({TIER_LABEL.get(q['tierN'], '?')})")
        else:
            leg = q["leg"]
            direction = "UNDER" if leg.get("over") is False else "OVER"
            log(f"  [{q['sport']} PROP] {leg.get('player')} {direction} {leg.get('line')} "
                f"{leg.get('stat')} ({leg.get('grade')})")

    if not live:
        log(f"[DRY RUN] Would lock {len(qualifying)} legs above (pass --live to write)")
        if send_email:
            send_locks_email(qualifying, live=False, label=label, to=to)
        return 0

    if not qualifying:
        # "complete" gates the workflow marker: nothing MORE can be locked. A leg skipped because its game started is final
        # (nothing later can lock it), so skips do not make a pass incomplete; only upcoming games with no real price do.
        rep["complete"] = not analysis["noPrice"]
        ok_email, why = _zero_pick_decision(qualifying, guard, rep["complete"])
        if send_email and ok_email:
            send_locks_email(qualifying, live=True, locked_count=0, label=label, to=to)
        elif send_email:
            log(f"Locks email ({label or 'ALL'}) suppressed (zero-pick): {why}")
        else:
            log(f"Locks email ({label or 'ALL'}) skipped -- already sent today")
        return 0

    result = _lock_qualifying_legs(page, qualifying)
    log(f"Locked {result.new} new, {result.already_locked} already locked, "
        f"{result.failed} failed -- {result.confirmed}/{len(qualifying)} qualifying legs confirmed locked")
    rep.update({"new": result.new, "already": result.already_locked, "failed": result.failed,
                "failedLabels": list(result.failed_labels), "skipped": rep["skipped"] + result.skipped})
    rep["complete"] = result.failed == 0 and not analysis["noPrice"]
    if result.new > 0:
        flush_to_supabase(page)
        log("Flushed locks to Supabase")
    # Real bug, found via audit: this always emailed based on freshly
    # re-evaluated `qualifying` regardless of whether any of it was
    # actually NEW this pass -- fine when this was truly the day's only
    # lock attempt for this product, but soccer/CFB now also have an
    # evening-prior pass (a separate workflow with no visibility into
    # this one's own send-dedup) that may have already locked and
    # emailed the exact same picks hours earlier. Without this guard, a
    # subscriber would get a second email the next morning showing "0 of
    # N actually locked" while still listing all N picks in full, as if
    # reporting something new. Only skip when there's truly nothing new
    # (result.new==0) AND something qualified (qualifying non-empty) --
    # genuinely new locks and the "nothing qualified today" case
    # (handled above) still email as before.
    if send_email and result.new == 0 and qualifying:
        log(f"Locks email ({label or 'ALL'}) skipped -- all {len(qualifying)} qualifying leg(s) "
            f"were already locked by an earlier pass today, nothing new to report")
    elif send_email:
        # Real bug, found via audit: this used to pass `locked` (this
        # pass's NEW count only) as the email's headline "X of Y legs
        # actually locked" number -- when the day's dedicated checks
        # land close together in real time (see LockResult's own
        # docstring), the check that actually emails can legitimately
        # have very few NEW locks even though nearly everything is
        # confirmed locked, reading as a near-total failure when it's
        # not. result.confirmed (new + already_locked) is the real
        # answer to "of today's qualifying legs, how many are actually
        # locked right now."
        send_locks_email(qualifying, live=True, locked_count=result.confirmed, label=label, to=to)
    else:
        log(f"Locks email ({label or 'ALL'}) skipped -- already sent today")
    return result.new


def run_euro_early_lock(page, live: bool, send_email: bool = True) -> int:
    """Combined early-morning lock pass, 2026-09-17: soccer's
    4-league product (CL/PL/La Liga/Serie A -- Bundesliga retired
    2026-09-23, MLS retired 2026-09-27) AND the 2
    early-kickoff hockey leagues (SHL/Liiga -- see EARLY_HOCKEY_SPORTS'
    own comment on why NHL stays out). ONE gather_legs() pull covers
    both -- merged purely to save a second full browser+Playwright run
    at a second scheduled time, not to blend the two products'
    subscriber-facing content: each still gets its own qualifying-legs
    list, its own lock pass, and its own separately-addressed email to
    its own real subscriber list, exactly as if run as two separate
    single-product passes (see run_lock, which this mirrors per-product).

    Replaces the old soccer-only early pass -- see european-lock-
    early.yml (renamed from soccer-lock-early.yml). The main run's own
    hockey product loop (run_lock_segmented) excludes SHL/Liiga from ITS
    email now that this pass covers them (still locks all 3 hockey
    leagues there as a safety net, same convention already used for
    soccer/CFB's own dedicated early passes)."""
    log("=== AUTO-LOCK (PREMIUM/OPTIMAL) — EUROPEAN EARLY (SOCCER + SHL/LIIGA) ===")
    result = gather_legs(page)
    log(f"Gathered {len(result.get('gameLegs') or [])} games' worth of markets, "
        f"{len(result.get('propLegs') or [])} prop legs total")
    prop_diag = result.get("propDiag") or {}
    if prop_diag:
        log("  prop generation: " + ", ".join(f"{sp}={detail}" for sp, detail in prop_diag.items()))

    total_locked = 0
    for label, only_sports, product in (
        ("SOCCER", PRODUCT_SPORTS["soccer"], "soccer"),
        ("HOCKEY", EARLY_HOCKEY_SPORTS, "hockey"),
    ):
        guard: dict = {}
        qualifying = build_qualifying(result, only_sports=only_sports, guard_stats=guard)
        analysis = analyze_games(result, only_sports)
        rep = {"kind": f"euro-early-{product}", "label": label, "dates": [datetime.now(MT).strftime("%Y-%m-%d")],
               "qualifying": len(qualifying), "new": 0, "already": 0, "failed": 0, "skipped": guard.get("skipped", 0),
               "skippedDetail": guard.get("detail", []), "failedLabels": [], "games": analysis["games"],
               "started": analysis["started"], "upcoming": analysis["upcoming"], "nextStartMs": analysis["nextStartMs"],
               "noPrice": analysis["noPrice"], "live": live, "complete": False}
        PASS_REPORTS.append(rep)
        to = recipients_for(product)
        log(f"{len(qualifying)} qualifying PREMIUM/OPTIMAL legs found ({label.lower()} only) "
            f"-> {len(to)} recipient(s)")
        for q in qualifying:
            if q["kind"] == "GAME":
                log(f"  [{q['sport']}] {q['label']} ({TIER_LABEL.get(q['tierN'], '?')})")
            else:
                leg = q["leg"]
                direction = "UNDER" if leg.get("over") is False else "OVER"
                log(f"  [{q['sport']} PROP] {leg.get('player')} {direction} {leg.get('line')} "
                    f"{leg.get('stat')} ({leg.get('grade')})")

        if not live:
            log(f"[DRY RUN] Would lock {len(qualifying)} {label} legs above (pass --live to write)")
            if send_email:
                send_locks_email(qualifying, live=False, label=label, to=to)
            continue

        if not qualifying:
            # "complete" gates the workflow marker: nothing MORE can be locked. A leg skipped because its game started is final
            # (nothing later can lock it), so skips do not make a pass incomplete; only upcoming games with no real price do.
            rep["complete"] = not analysis["noPrice"]
            ok_email, why = _zero_pick_decision(qualifying, guard, rep["complete"])
            if send_email and ok_email:
                send_locks_email(qualifying, live=True, locked_count=0, label=label, to=to)
            elif send_email:
                log(f"Locks email ({label}) suppressed (zero-pick): {why}")
            continue

        result_lock = _lock_qualifying_legs(page, qualifying)
        total_locked += result_lock.new
        rep.update({"new": result_lock.new, "already": result_lock.already_locked, "failed": result_lock.failed,
                    "failedLabels": list(result_lock.failed_labels), "skipped": rep["skipped"] + result_lock.skipped})
        rep["complete"] = result_lock.failed == 0 and not analysis["noPrice"]
        log(f"[{label}] {result_lock.new} new, {result_lock.already_locked} already locked, "
            f"{result_lock.failed} failed -- {result_lock.confirmed}/{len(qualifying)} confirmed locked")
        if send_email and result_lock.new == 0 and qualifying:
            log(f"Locks email ({label}) skipped -- all {len(qualifying)} qualifying leg(s) "
                f"were already locked by an earlier pass today, nothing new to report")
        elif send_email:
            send_locks_email(qualifying, live=True, locked_count=result_lock.confirmed, label=label, to=to)
        else:
            log(f"Locks email ({label}) skipped -- already sent today")

    if live and total_locked > 0:
        flush_to_supabase(page)
        log(f"Flushed {total_locked} total locks to Supabase")
    return total_locked


def _zero_pick_decision(qualifying: list[dict], guard: dict, complete: bool) -> tuple[bool, str]:
    """-> (email_subscribers, reason) for a pass whose qualifying list is EMPTY. Current behavior (before 2026-10-02) was to email
    the product's subscribers "No picks cleared the bar today" whenever the list was empty -- including a pass that landed after
    kickoff and found every game already started (the start guard drops them), and a pass that ran before the day's real prices
    posted. Both read as "the engine found nothing" when the truth is "this pass was too late / too early". The subscriber
    email is now sent ONLY when the pass is COMPLETE (every upcoming game it saw had real prices and nothing failed) and the
    guard skipped nothing; the owner is told about the other cases via the pre-kickoff alert instead (see owner_alert_for)."""
    if guard.get("skipped"):
        return False, f"{guard['skipped']} qualifying leg(s) skipped because their game had already started -- a late pass, not 'no picks'"
    if not complete:
        return False, "pass incomplete (upcoming games still have no real price, or a lock failed) -- a later pass may find picks"
    return True, "complete pass, nothing qualified"


def _dates_label(qualifying: list[dict], fallback: str) -> str:
    ds = sorted({q.get("lockDate") for q in qualifying if q.get("lockDate")})
    return " & ".join(ds) if ds else fallback


def _run_rolling_pass(page, live: bool, send_email: bool, to: list[str] | None, *, kind: str, label: str, title: str,
                      gather, only_sports, dry_label: str, now=None) -> int:
    """ONE implementation behind run_soccer_evening_lock / run_cfb_evening_lock / run_hockey_evening_lock (they used to be three
    copies). Rolling horizon: gathers every game on today's AND tomorrow's Mountain dates relative to the moment the pass really
    runs (see horizon_dates), locks each qualifying leg stamped with its own game date, and records a PASS_REPORT (completeness,
    skipped/failed legs, next kickoff) for the result file, the Engine Health status and the owner's pre-kickoff alert.
    Returns the number of NEW locks."""
    dates = horizon_dates(now)
    log(f"=== AUTO-LOCK (PREMIUM/OPTIMAL) — {title}, ROLLING HORIZON {' + '.join(dates)} ===")
    result = gather(page, dates)
    guard: dict = {}
    qualifying = build_qualifying(result, only_sports=only_sports, now=now, guard_stats=guard)
    analysis = analyze_games(result, only_sports, now)
    log(f"Gathered {len(result.get('gameLegs') or [])} games' worth of markets for {' + '.join(dates)} "
        f"({analysis['started']} already started, {analysis['upcoming']} upcoming"
        f"{', ' + str(len(analysis['noPrice'])) + ' upcoming with NO real price yet' if analysis['noPrice'] else ''})")
    log(f"{len(qualifying)} qualifying PREMIUM/OPTIMAL legs found ({dry_label})")
    for q in qualifying:
        log(f"  [{q['sport']}] {q['label']} ({TIER_LABEL.get(q['tierN'], '?')}) -> pick date {q.get('lockDate')}")
    rep = {"kind": kind, "label": label, "dates": dates, "qualifying": len(qualifying), "new": 0, "already": 0, "failed": 0,
           "skipped": guard.get("skipped", 0), "skippedDetail": guard.get("detail", []), "failedLabels": [],
           "games": analysis["games"], "started": analysis["started"], "upcoming": analysis["upcoming"],
           "nextStartMs": analysis["nextStartMs"], "noPrice": analysis["noPrice"], "live": live, "complete": False}
    PASS_REPORTS.append(rep)
    email_label = label
    date_str = _dates_label(qualifying, dates[-1])

    if not live:
        log(f"[DRY RUN] Would lock {len(qualifying)} legs above (pass --live to write)")
        if send_email:
            send_locks_email(qualifying, live=False, label=email_label, to=to, date_str=date_str)
        return 0

    if not qualifying:
        # "complete" gates the workflow marker: nothing MORE can be locked. A leg skipped because its game started is final
        # (nothing later can lock it), so skips do not make a pass incomplete; only upcoming games with no real price do.
        rep["complete"] = not analysis["noPrice"]
        ok_email, why = _zero_pick_decision(qualifying, guard, rep["complete"])
        if send_email and ok_email:
            send_locks_email(qualifying, live=True, locked_count=0, label=email_label, to=to, date_str=dates[-1])
        else:
            log(f"Locks email ({label}) suppressed (zero-pick): {why}" if send_email else f"Locks email ({label}) skipped -- already sent")
        return 0

    lock_result = _lock_qualifying_legs(page, qualifying, date_override=dates[-1], per_leg_dates=True)
    log(f"Locked {lock_result.new} new, {lock_result.already_locked} already locked, {lock_result.failed} failed -- "
        f"{lock_result.confirmed}/{len(qualifying)} qualifying legs confirmed locked ({' + '.join(dates)})")
    rep.update({"new": lock_result.new, "already": lock_result.already_locked, "failed": lock_result.failed,
                "failedLabels": list(lock_result.failed_labels), "skipped": rep["skipped"] + lock_result.skipped})
    rep["complete"] = lock_result.failed == 0 and not analysis["noPrice"]
    if lock_result.new > 0:
        flush_to_supabase(page)
        log("Flushed locks to Supabase")
    # Subscriber email only when this pass locked something NEW: an extra pass that finds nothing new never re-emails (the
    # pass-level dedupe the redundant slots rely on).
    if send_email and lock_result.new == 0:
        log(f"Locks email ({label}) skipped -- all {len(qualifying)} qualifying leg(s) were already locked by an earlier "
            f"pass, nothing new to report")
    elif send_email:
        send_locks_email(qualifying, live=True, locked_count=lock_result.confirmed, label=email_label, to=to, date_str=date_str)
    else:
        log(f"Locks email ({label}) skipped -- already sent")
    return lock_result.new


def run_soccer_evening_lock(page, live: bool, send_email: bool = True, to: list[str] | None = None, now=None) -> int:
    """Evening-prior lock for the European soccer leagues (CL/PL/La
    Liga/Serie A -- Bundesliga retired 2026-09-23) -- runs the NIGHT BEFORE those leagues'
    matchday, not that morning. See soccer-lock-evening.yml's own
    docstring for the full rationale; short version: real kickoffs as
    early as 7:00 AM MT (EPL) leave soccer-lock-early.yml's 6:00 AM MT
    same-day pass as little as ~1 hour of buffer, and that pass has
    genuinely run late enough before to miss kickoff entirely. Locking
    the evening before (odds are already posted 1-2+ days out for these
    leagues, confirmed live) trades that ~1 hour buffer for ~9+ hours,
    without losing real signal -- the model grades off team-aggregate
    stats (xG/Opta), never starting lineups, so it was never going to see
    same-day lineup news regardless of lock time.

    Every leg locked here is stamped with the GAME's real date (tomorrow),
    not today() -- see lock_game_leg's docstring on why that's required
    for this to dedupe correctly against soccer-lock-early.yml's own
    same-day pass the next morning, which is left completely unchanged
    and still runs as today's safety net (idempotent: anything already
    locked tonight is a no-op there, it only picks up whatever this pass
    missed -- a late odds posting, a fixture added after tonight's run,
    etc). Data comes from docs/soccer_schedule_tomorrow.json (scraped by
    a dedicated earlier step in soccer-lock-evening.yml), not any live
    fetch -- see gather_soccer_legs_for_date's own docstring."""
    return _run_rolling_pass(page, live, send_email, to, kind="soccer-evening", label="SOCCER — UPCOMING SLATE", title="SOCCER, EVENING-PRIOR",
                             gather=gather_soccer_legs_for_dates, only_sports=EURO_SOCCER_SPORTS, dry_label="soccer evening-prior only", now=now)


def run_cfb_evening_lock(page, live: bool, send_email: bool = True, to: list[str] | None = None, now=None) -> int:
    """Evening-prior lock for CFB -- runs the NIGHT BEFORE gameday, not
    that morning. See cfb-lock-evening.yml's own docstring for the full
    rationale; short version: real Saturday kickoffs cluster heavily at
    exactly 10:00 AM MT -- the SAME instant as cfb-lock-early.yml's own
    LAST catch-up slot -- and that workflow has genuinely failed all 4
    of its own scheduled slots before (confirmed live, 2026-08-28). In
    that exact scenario a subscriber could receive a "locked pick" email
    for a game already underway. Locking the evening before trades a
    near-zero worst-case margin for 12+ hours, using the same real
    market data (g.spread/g.overUnder, confirmed posted 6+ days out)
    the same-day pass already relies on -- no signal lost.

    Every leg locked here is stamped with the GAME's real date
    (tomorrow), not today() -- see lock_game_leg's docstring on why
    that's required for this to dedupe correctly against
    cfb-lock-early.yml's own same-day pass the next morning, which is
    left completely unchanged and still runs as tomorrow's safety net.
    Data comes from docs/cfb_schedule.json, already refreshed twice
    daily by the existing daily-schedules-refresh.yml -- no new data-feed
    workflow needed, see gather_cfb_legs_for_date's own docstring."""
    return _run_rolling_pass(page, live, send_email, to, kind="cfb-evening", label="CFB — UPCOMING SLATE", title="CFB, EVENING-PRIOR",
                             gather=gather_cfb_legs_for_dates, only_sports=frozenset({"CFB"}), dry_label="CFB evening-prior only", now=now)


def run_hockey_evening_lock(page, live: bool, send_email: bool = True, to: list[str] | None = None, now=None) -> int:
    """Evening-prior lock for SHL/Liiga/NLA/Extraliga combined -- runs
    the NIGHT BEFORE gameday, not that morning. Same rationale as CFB/
    soccer's own evening-prior passes: european-lock-early.yml's own
    same-morning slot (6:00 AM MT, +catch-ups through 6:45 AM) already
    covers these leagues well ahead of SHL's real ~7:15 AM MT kickoffs,
    but locking the night before trades even that margin for a much
    larger one, using the same real model inputs (blended GF/GA rates,
    standings) the same-morning pass already relies on -- no signal
    lost, and a genuine extra safety net if that morning pass and all
    its catch-ups get dropped (documented, real GitHub Actions risk,
    same class of incident cfb-lock-evening.yml/soccer-lock-evening.yml
    already exist to guard against). NHL deliberately excluded -- it
    never plays this early, see EARLY_HOCKEY_SPORTS' own comment.

    Every leg locked here is stamped with the GAME's real date
    (tomorrow), not today() -- see lock_game_leg's docstring on why
    that's required for this to dedupe correctly against european-lock-
    early.yml's own same-morning pass, which is left completely
    unchanged and still runs as tomorrow's safety net. Data comes from
    docs/liiga_schedule.json/shl_schedule.json/nla_schedule.json/
    extraliga_schedule.json, already refreshed twice daily each -- no
    new data-feed workflow needed, see
    gather_hockey_evening_legs_for_date's own docstring.

    NLA/Extraliga promoted into this same paid-product pass 2026-09-23,
    explicit request -- previously ran as a separate personal-use-only
    pass for less than a day (see EARLY_HOCKEY_SPORTS' own comment for
    the full history); now indistinguishable from SHL/Liiga here, same
    `to` recipient list (recipients_for("hockey"))."""
    return _run_rolling_pass(page, live, send_email, to, kind="hockey-evening", label="HOCKEY — UPCOMING SLATE", title="SHL/LIIGA/NLA/EXTRALIGA, EVENING-PRIOR",
                             gather=gather_hockey_legs_for_dates, only_sports=EARLY_HOCKEY_SPORTS, dry_label="SHL/Liiga/NLA/Extraliga evening-prior only", now=now)


def run_lock_segmented(page, live: bool, send_email: bool = True) -> None:
    """Main (unscoped) lock run -- ONE gather_legs() call (the expensive
    part: real browser + live data warmups), then split into a separate
    qualifying-legs list + a separately-addressed email for each of the 5
    paid sport products (see _subscribers.py; "hockey" bundles NHL+Liiga+
    SHL as of 2026-09-16 -- see PRODUCT_SPORTS' own comment), plus one
    more pass for OTHER_ALLOWED_SPORTS (NCAAH only, as of that same
    promotion -- personal-use, never sold) sent to the owner only.
    Anything outside covered_sports | OTHER_ALLOWED_SPORTS (KHL --
    explicit 2026-09-03 decision, permanently excluded; tennis, MLB,
    WNBA, CBB, and World Cup are all fully retired, so they no longer
    generate any legs here at all) is dropped entirely before either
    pass, never auto-locked by this automated run at all -- KHL's own
    manual lock/settle buttons in the app UI are untouched, this only
    scopes automation. A subscriber to
    one product only ever sees that product's email; nothing IN SCOPE is
    ever silently dropped -- every qualifying leg that survives the
    top-level filter lands in exactly one of these passes. send_email=False
    for a redundant same-morning retry (see run_lock's own docstring) --
    still locks and verifies, just skips re-sending every product's
    email.

    Hockey, 2026-09-17: SHL/Liiga (EARLY_HOCKEY_SPORTS) are excluded from
    THIS run's own hockey email -- they now get a real, earlier-timed
    email from run_euro_early_lock instead. This run still locks them as
    a safety net (same convention soccer/CFB's own early passes already
    use), it just never re-emails them; the hockey email here covers NHL
    only."""
    log("=== AUTO-LOCK (PREMIUM/OPTIMAL) — ALL PRODUCTS ===")
    result = gather_legs(page)
    log(f"Gathered {len(result.get('gameLegs') or [])} games' worth of markets, "
        f"{len(result.get('propLegs') or [])} prop legs total")
    prop_diag = result.get("propDiag") or {}
    if prop_diag:
        log("  prop generation: " + ", ".join(f"{sp}={detail}" for sp, detail in prop_diag.items()))
    covered_sports: set[str] = set().union(*PRODUCT_SPORTS.values())
    seg_guard: dict = {}
    all_qualifying_raw = build_qualifying(result, guard_stats=seg_guard)
    seg_analysis = analyze_games(result)
    seg_rep = {"kind": "main", "label": "ALL PRODUCTS", "dates": [datetime.now(MT).strftime("%Y-%m-%d")],
               "qualifying": len(all_qualifying_raw), "new": 0, "already": 0, "failed": 0,
               "skipped": seg_guard.get("skipped", 0), "skippedDetail": seg_guard.get("detail", []), "failedLabels": [],
               "games": seg_analysis["games"], "started": seg_analysis["started"], "upcoming": seg_analysis["upcoming"],
               "nextStartMs": seg_analysis["nextStartMs"], "noPrice": seg_analysis["noPrice"], "live": live, "complete": False}
    PASS_REPORTS.append(seg_rep)
    in_scope_sports = covered_sports | OTHER_ALLOWED_SPORTS
    all_qualifying = [q for q in all_qualifying_raw if q["sport"] in in_scope_sports]
    excluded = [q for q in all_qualifying_raw if q["sport"] not in in_scope_sports]
    if excluded:
        excluded_sports = sorted({q["sport"] for q in excluded})
        log(f"[excluded] {len(excluded)} qualifying leg(s) outside auto-lock scope, dropped "
            f"(not locked, not emailed): {excluded_sports}")
    total_locked = 0

    for product, sports in PRODUCT_SPORTS.items():
        qualifying = [q for q in all_qualifying if q["sport"] in sports]
        label = PRODUCT_LABEL[product]
        recipients = recipients_for(product)
        # Hockey-only, 2026-09-17: SHL/Liiga now get their own dedicated
        # early pass (run_euro_early_lock, merged into the soccer early-
        # lock workflow -- see EARLY_HOCKEY_SPORTS' own comment on why
        # NHL stays out of it) with a real email timed ahead of their
        # ~7:15 AM MT kickoffs. This run still locks BOTH subsets below
        # (idempotent safety net, same convention soccer/CFB's own early
        # passes already established), but only ever emails the NHL
        # subset from here -- otherwise SHL/Liiga picks would show up in
        # two separate emails the same day.
        if product == "hockey":
            email_qualifying = [q for q in qualifying if q["sport"] not in EARLY_HOCKEY_SPORTS]
            safety_net_qualifying = [q for q in qualifying if q["sport"] in EARLY_HOCKEY_SPORTS]
        else:
            email_qualifying = qualifying
            safety_net_qualifying = []
        log(f"[{product}] {len(qualifying)} qualifying legs -> {len(recipients)} recipient(s)")
        # Explicit request: NBA and NHL are both off-season right now
        # (confirmed live -- 2026-09-10's real run qualified 0 legs for
        # both, every single check), so their subscriber email was going
        # out empty every day: "0 qualifying picks" reads as the product
        # being broken, not dormant. Gated on real qualifying legs
        # existing (not a hardcoded date), so this resumes itself
        # automatically the moment either league's real season actually
        # starts producing real picks -- no separate code change needed
        # once that happens. Hockey checks email_qualifying (NHL only)
        # specifically, not the whole product's qualifying -- SHL/Liiga
        # being in-season shouldn't mask a real NHL off-season gap in
        # THIS run's own (NHL-only) email.
        season_inactive = product in ("nba", "hockey") and not email_qualifying
        if not live:
            # Same redundancy-skip as the live branch below (soccer/cfb
            # both have their own dedicated early/evening pass with a
            # better-timed email) -- this branch was missing it, so a
            # dry-run still logged/sent a redundant soccer/cfb preview.
            if send_email and product in ("soccer", "cfb"):
                log(f"Locks email ({label}) skipped -- {product} has its own dedicated early/evening "
                    f"pass with a better-timed email (dry-run)")
            elif send_email and not season_inactive:
                send_locks_email(email_qualifying, live=False, label=label, to=recipients)
            elif send_email:
                log(f"Locks email ({label}) skipped -- season inactive, nothing qualifying yet")
            continue
        result = _lock_qualifying_legs(page, email_qualifying) if email_qualifying else LockResult(0, 0, 0)
        total_locked += result.new
        seg_rep["new"] += result.new; seg_rep["already"] += result.already_locked; seg_rep["failed"] += result.failed
        seg_rep["failedLabels"] += list(result.failed_labels); seg_rep["skipped"] += result.skipped
        if safety_net_qualifying:
            early_result = _lock_qualifying_legs(page, safety_net_qualifying)
            total_locked += early_result.new
            seg_rep["new"] += early_result.new; seg_rep["already"] += early_result.already_locked
            seg_rep["failed"] += early_result.failed; seg_rep["failedLabels"] += list(early_result.failed_labels)
            seg_rep["skipped"] += early_result.skipped
            log(f"[{product}] SHL/Liiga/NLA/Extraliga safety-net: {early_result.new} new, "
                f"{early_result.already_locked} already locked, {early_result.failed} failed -- "
                f"{early_result.confirmed}/{len(safety_net_qualifying)} confirmed locked")
        log(f"[{product}] {result.new} new, {result.already_locked} already locked, "
            f"{result.failed} failed -- {result.confirmed}/{len(email_qualifying)} confirmed locked")
        # Real bug, found via audit -- but NOT fixed the way run_lock's
        # own version of this guard is: soccer and CFB both have their
        # own dedicated early/evening pass with a BETTER-timed, more
        # specific email (european-lock-early.yml, cfb-lock-early.yml,
        # and now an evening-prior lock for both), so this run's own
        # per-product email for those two is now always redundant with
        # it -- not just on days a locked==0 coincidence would catch.
        # This exclusion is unconditional, not "skip when nothing new",
        # because a "locked==0 means skip" guard HERE would wrongly
        # silence the once-daily cumulative email for every OTHER sport
        # too: this loop runs on all 3 of the morning's checks, and only
        # the LAST one passes send_email=True BY DESIGN -- if the first
        # check already locked everything (the normal, healthy case),
        # locked==0 at the final check is expected and that email must
        # still go out, since it's the one and only report a non-soccer/
        # CFB subscriber gets that day. Still locks as a safety net
        # either way (matches soccer/CFB's own early-pass precedent of
        # continuing to lock without emailing).
        if send_email and product in ("soccer", "cfb"):
            log(f"Locks email ({label}) skipped -- {product} has its own dedicated early/evening "
                f"pass with a better-timed email; this run still locked as a safety net ({result.new} new)")
        elif send_email and season_inactive:
            log(f"Locks email ({label}) skipped -- season inactive, nothing qualifying yet")
        elif send_email and not email_qualifying and any(d.get("sport") in sports for d in seg_guard.get("detail", [])):
            # Zero-pick email suppression (2026-10-02): this product HAD qualifying legs, but the pre-start guard dropped them
            # because their games had already started -- a late pass, not "no picks". The owner is told via the pre-kickoff alert.
            log(f"Locks email ({label}) suppressed (zero-pick): its qualifying legs were skipped because the games had "
                f"already started (a late pass, not 'no picks')")
        elif send_email:
            # Real bug, found via audit: this used to pass `locked` (this
            # pass's NEW count only) -- see LockResult's own docstring
            # for the real "1 of 15" case this caused, confirmed live:
            # the 3 dedicated morning checks landing compressed together
            # in real time (a GitHub Actions scheduling delay) meant the
            # first two locked nearly everything, leaving the THIRD
            # (this one, the one that actually emails) reporting almost
            # nothing NEW even though the day's picks were all really
            # there. result.confirmed answers the real question.
            send_locks_email(email_qualifying, live=True, locked_count=result.confirmed, label=label, to=recipients)
        else:
            log(f"Locks email ({label}) skipped -- already sent today ({result.new} locked this pass)")

    # Explicit request, 2026-09-03: stop emailing the OTHER (owner-only)
    # digest entirely -- it's been empty every single real run back when
    # this covered SHL/LIIGA/NCAAH (none of the three had any real
    # _autoLockCapture calls wired up yet). SHL and LIIGA were later
    # promoted into PRODUCT_SPORTS["hockey"] (2026-09-16, see that dict's
    # own comment) once they got real engines -- OTHER_ALLOWED_SPORTS is
    # NCAAH only now, so this pass (and its email skip) only applies to
    # NCAAH. Locking itself is untouched below -- if NCAAH ever does start
    # producing real qualifying legs, it'll still auto-lock as a safety
    # net, just without a report email.
    other_qualifying = [q for q in all_qualifying if q["sport"] in OTHER_ALLOWED_SPORTS]
    log(f"[other] {len(other_qualifying)} qualifying legs (OTHER_ALLOWED_SPORTS is now empty -- NCAAH retired 2026-10-02)")
    if live:
        result = _lock_qualifying_legs(page, other_qualifying) if other_qualifying else LockResult(0, 0, 0)
        total_locked += result.new
        seg_rep["new"] += result.new; seg_rep["already"] += result.already_locked; seg_rep["failed"] += result.failed
        log(f"[other] {result.new} new, {result.already_locked} already locked, "
            f"{result.failed} failed -- {result.confirmed}/{len(other_qualifying)} confirmed locked")

    seg_rep["complete"] = live and seg_rep["failed"] == 0 and not seg_analysis["noPrice"]
    if total_locked > 0:
        flush_to_supabase(page)
        log(f"Flushed {total_locked} total locks to Supabase")


# ── PRE-KICKOFF WATCHDOG (owner only) ──────────────────────────────────────────────────────────────────────────────────────
WATCHDOG_AHEAD_H = 4.0     # a qualifying-but-unlocked leg whose game starts within this many hours is "urgent"
WATCHDOG_BEHIND_H = 3.0    # ... or started within this many hours (still worth knowing: a late manual lock may be wanted)


def leg_locked_in_ledger(page, q: dict, date_key: str | None) -> bool:
    """Read-only twin of lock_game_leg's dedupe pre-check: is this qualifying leg (or its market) already in the loaded ledger?"""
    lock_type = SPORT_TO_LOCKPICK_TYPE.get(q["sport"])
    if not lock_type:
        return False
    return bool(page.evaluate(
        """
        ({ hA, awA, type, betOn, dateKey, betTypeOverride }) => {
          const key = dateKey || today();
          const id = `${key}_${hA}_${awA}_${type}_${betOn.replace(/\\s/g,'')}`;
          const preds = getP();
          const marketDup = (typeof _findSameMarketLock === 'function') ? _findSameMarketLock(preds, key, hA, awA, betTypeOverride) : null;
          return !!(preds.find(x => x.id === id) || preds.find(x => x.date === key && x.hA === hA && x.awA === awA && x.betOn === betOn) || marketDup);
        }
        """,
        {"hA": q["hA"], "awA": q["awA"], "type": lock_type, "betOn": q["label"], "dateKey": date_key,
         "betTypeOverride": _market_type(q.get("side"))},
    ))


def run_watchdog(page, live: bool, now=None) -> list[dict]:
    """READ-ONLY. For every product's slate (today via gather_legs, plus the rolling-horizon gatherers for today+tomorrow), list
    qualifying legs that are NOT in the ledger and whose game starts within WATCHDOG_AHEAD_H hours (or started within the last
    WATCHDOG_BEHIND_H). Emails the OWNER ONLY (never a subscriber list), deduped by the hash of the leg set. Returns the urgent list."""
    log("=== PRE-KICKOFF WATCHDOG (read-only, owner-only) ===")
    now_ms = _now_ms(now)
    dates = horizon_dates(now)
    gathered = [("main-today", gather_legs(page), None)]
    gathered.append(("hockey", gather_hockey_legs_for_dates(page, dates), EARLY_HOCKEY_SPORTS))
    gathered.append(("cfb", gather_cfb_legs_for_dates(page, dates), frozenset({"CFB"})))
    gathered.append(("soccer", gather_soccer_legs_for_dates(page, dates), EURO_SOCCER_SPORTS))
    seen: set = set()
    unlocked: list[dict] = []
    total = 0
    for name, res, only in gathered:
        # now=0: ignore the start guard here -- the watchdog WANTS to see legs of games that already started / are about to.
        for q in build_qualifying(res, only_sports=only, now=0):
            if q["kind"] != "GAME":
                continue
            key = (q["sport"], q["hA"], q["awA"], q["label"])
            if key in seen:
                continue
            seen.add(key)
            total += 1
            st = parse_start_ms(q.get("startMs"))
            if leg_locked_in_ledger(page, q, q.get("lockDate")):
                continue
            mins = None if st is None else (st - now_ms) / 60000.0
            if mins is None or -WATCHDOG_BEHIND_H * 60 <= mins <= WATCHDOG_AHEAD_H * 60:
                unlocked.append({"sport": q["sport"], "game": f"{q['awA']} @ {q['hA']}", "leg": q["label"], "startMs": st,
                                 "why": ("kickoff unknown" if mins is None else
                                         f"kicks off in {mins:.0f} min" if mins > 0 else f"started {-mins:.0f} min ago"),
                                 "prob": q.get("prob"), "tier": TIER_LABEL.get(q.get("tierN"), "?")})
    log(f"Watchdog: {total} qualifying game leg(s) across the slate, {len(unlocked)} NOT locked and within the watch window")
    for u in unlocked:
        log(f"  UNLOCKED [{u['sport']}] {u['game']} -- {u['leg']} ({u['why']})")
    if not unlocked:
        return unlocked
    rep = {"live": True, "label": "WATCHDOG", "skippedDetail": unlocked, "failedLabels": []}
    alert = owner_alert_for([rep])
    if alert is None:
        return unlocked
    h, _subject, html = alert
    first = min((u["startMs"] for u in unlocked if u["startMs"]), default=None)
    subject = (f"Clairvoyance — WATCHDOG: {len(unlocked)} qualifying pick(s) still unlocked"
               + (f", first kickoff {datetime.fromtimestamp(first / 1000.0, MT).strftime('%a %I:%M %p MT')}" if first else ""))
    html = html.replace("A lock pass landed at", "The pre-kickoff watchdog ran at").replace(
        "but could not lock them (game already started or within", "and found them still unlocked (kickoff within")
    if not live:
        log(f"[DRY RUN] Would email the owner: {subject}")
        return unlocked
    try:
        seen_hashes = json.loads((ROOT / "docs" / "automation_status.json").read_text()).get("ownerAlerts", {})
    except Exception:
        seen_hashes = {}
    if h in seen_hashes:
        log(f"watchdog alert {h} already sent ({seen_hashes[h]}) -- not repeating")
    elif send_owner_alert(subject, html):
        write_automation_status("lastWatchdog", True, f"{len(unlocked)} unlocked qualifying leg(s) reported to the owner",
                                owner_alert_hash=h)
    return unlocked


def start_local_site(directory: Path):
    """Serve `directory` (the checkout's docs/) on 127.0.0.1 on a free port, in a daemon thread. -> (app_url, server).
    GitHub Pages serves docs/ verbatim (pages-deploy.yml uploads path: docs), so this is byte-for-byte what the deployed site
    serves -- except it is exactly the commit the workflow just refreshed, with no redeploy to wait for."""
    import functools
    import http.server
    import socketserver
    import threading

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), functools.partial(_Handler, directory=str(directory)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/app.html", srv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="Actually write to Supabase. Default is dry-run (logs only).")
    ap.add_argument("--lock", action="store_true", help="Run only the auto-lock step")
    ap.add_argument("--settle", action="store_true", help="Run only the auto-settle step (no email -- see --daily-digest)")
    ap.add_argument("--daily-digest", action="store_true",
                     help="Send the once-daily settlement email covering exactly YESTERDAY's "
                          "(America/Denver) locked picks. Decoupled from --settle: intraday settle "
                          "passes keep bets accurate throughout the day but never email; this is the "
                          "one email/day, run each morning, per explicit request that settlement "
                          "emails only ever cover the prior day's results.")
    ap.add_argument("--alert-lock-missed", action="store_true",
                     help="Send a same-day alert that today's lock pass never ran (neither the "
                          "primary morning slot nor the morning catch-up window). Does not touch "
                          "the browser/Supabase -- just an email.")
    ap.add_argument("--adaptive-recalibration", action="store_true",
                     help="Run adaptiveTick() against the full real settled-bet history and commit "
                          "the resulting ensemble weights to docs/adaptive_weights.json, so the "
                          "learning actually reaches the automated lock pipeline (not just a real "
                          "user's own browser localStorage). See run_adaptive_recalibration.")
    ap.add_argument("--final-lock-check", action="store_true",
                     help="Main (all-sports) lock only: this IS the last of the day's scheduled "
                          "lock attempts -- send the subscriber locks email now, after the earlier "
                          "attempts already had a chance to lock anything qualifying. Earlier "
                          "attempts should omit this flag: they still lock + verify (idempotent, "
                          "safe to repeat), just don't email yet. Ignored for the early single-"
                          "product (soccer/CFB) passes, which always email immediately -- each is "
                          "its own dedicated once-daily run, not part of this multi-check flow.")
    ap.add_argument("--only-soccer", action="store_true",
                     help="Lock step only: restrict to all 4 soccer leagues (CL/PL/La Liga/"
                          "Serie A -- Bundesliga retired 2026-09-23, MLS retired 2026-09-27) -- "
                          "the EUROPEAN/NORTH AMERICA (MLS) email split this used to produce is "
                          "now moot, no SOC_MLS leg can qualify anymore. For the early-morning "
                          "pass timed ahead of European kickoffs.")
    ap.add_argument("--only-euro-early", action="store_true",
                     help="Lock step only: the combined European early-morning pass -- all 4 "
                          "soccer leagues (see --only-soccer) PLUS SHL/Liiga/NLA/Extraliga hockey "
                          "(real kickoffs as early as 7:15 AM MT). One shared gather_legs() pull, but "
                          "each product still gets its own separately-addressed email to its own "
                          "real subscriber list -- see run_euro_early_lock. Replaces the old "
                          "soccer-only early pass (european-lock-early.yml, renamed from "
                          "soccer-lock-early.yml 2026-09-17).")
    ap.add_argument("--only-cfb", action="store_true",
                     help="Lock step only: restrict to CFB. For the early-morning pass timed "
                          "ahead of the earliest college football kickoffs (10 AM MT+).")
    ap.add_argument("--only-soccer-tomorrow", action="store_true",
                     help="Lock step only: evening-prior lock for the European soccer leagues "
                          "(CL/PL/La Liga/Serie A -- no MLS, Bundesliga retired 2026-09-23), run the NIGHT BEFORE "
                          "their matchday instead of that morning. Reads "
                          "docs/soccer_schedule_tomorrow.json (scraped separately) and stamps "
                          "every locked pick with the game's real (tomorrow's) date. See "
                          "run_soccer_evening_lock's own docstring / soccer-lock-evening.yml.")
    ap.add_argument("--only-cfb-tomorrow", action="store_true",
                     help="Lock step only: evening-prior lock for CFB, run the NIGHT BEFORE "
                          "gameday instead of that morning. Reads docs/cfb_schedule.json "
                          "(already refreshed twice daily, no separate scrape needed) and stamps "
                          "every locked pick with the game's real (tomorrow's) date. See "
                          "run_cfb_evening_lock's own docstring / cfb-lock-evening.yml.")
    ap.add_argument("--only-hockey-tomorrow", action="store_true",
                     help="Lock step only: evening-prior lock for SHL/Liiga/NLA/Extraliga "
                          "combined, run the NIGHT BEFORE gameday instead of that morning. Reads "
                          "docs/liiga_schedule.json/shl_schedule.json/nla_schedule.json/"
                          "extraliga_schedule.json (already refreshed twice daily each, no "
                          "separate scrape needed) and stamps every locked pick with the game's "
                          "real (tomorrow's) date. NHL deliberately excluded -- it never plays "
                          "this early, same reasoning as EARLY_HOCKEY_SPORTS. See "
                          "run_hockey_evening_lock's own docstring / hockey-lock-evening.yml.")
    ap.add_argument("--top-picks-digest", action="store_true",
                     help="Sends the owner-only Top Picks Digest (top 4 per league + top 7 overall "
                          "for the day, ranked by model win probability, with parlay math) to "
                          "clairvoyanceengine@gmail.com only -- never the subscriber lists. Reads "
                          "today's already-locked picks fresh from Supabase; run this AFTER the "
                          "day's real lock pass, not standalone. Does not touch the browser's other "
                          "lock/settle logic -- combine with --lock or run as its own invocation.")
    ap.add_argument("--app-url", default=APP_URL, help="Override the app URL (e.g. a local server for testing).")
    ap.add_argument("--serve-local", action="store_true",
                     help="Serve THIS checkout's docs/ on a local port and drive the app from it instead of the deployed GitHub "
                          "Pages copy. The lock then reads the schedule/odds JSON exactly as committed (including whatever "
                          "scripts/lock_prep.py just refreshed), with no dependence on a Pages redeploy having finished. Falls "
                          "back to the deployed URL if the local server cannot start.")
    ap.add_argument("--result-file", default=None,
                     help="Write this lock pass's machine-readable result (complete/ok + per-pass report) to this JSON path. The "
                          "lock workflows read `complete` from it to decide whether to record their success marker.")
    ap.add_argument("--watchdog", action="store_true",
                     help="READ-ONLY pre-kickoff watchdog: gathers today's + tomorrow's slate for every product, finds qualifying "
                          "legs that are still NOT locked and start soon, and emails the OWNER ONLY. Never locks, never emails "
                          "subscribers; the only thing it writes (with --live) is the owner-alert dedupe hash in "
                          "docs/automation_status.json.")
    args = ap.parse_args()

    if args.alert_lock_missed:
        to = OWNER_EMAIL or LOCKS_EMAIL_TO
        if not to:
            log("alert-lock-missed: no OWNER_EMAIL/LOCKS_EMAIL_TO configured -- cannot send")
            return
        today_mt = datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%d")
        body = (f"{_EMAIL_WRAP_OPEN}"
                f'<div style="font-size:16px;color:#ff9090;font-weight:700">⚠ Lock did not run today ({today_mt})</div>'
                f'<div style="margin-top:10px;font-size:14px;color:#ccc">None of the main lock slots (pre-dawn 11:27 PM MT nominal, '
                f'the 3:07 AM backup, the 7:07 AM check) produced a real lock pass today, and none of the day\'s settle-only fires '
                f'(4:44pm MT) land inside the old 9am-12pm MT catch-up window to auto-retry it -- no new picks were locked, '
                f'and no locks email went out. This is a same-day alert so it gets noticed today, not whenever '
                f'someone happens to check the app. Run a manual workflow_dispatch with mode=catch-up to recover '
                f'today\'s lock now.</div>'
                f"{_EMAIL_WRAP_CLOSE}")
        ok, msg = _send_gmail(f"Clairvoyance — Lock FAILED to run {today_mt}", to, body)
        log(f"Lock-missed alert sent to {to}" if ok else f"Lock-missed alert failed: {msg}")
        return

    do_lock, do_settle = (args.lock, args.settle) if (args.lock or args.settle) else (True, True)
    if args.daily_digest or args.adaptive_recalibration or args.top_picks_digest or args.watchdog:
        do_lock = do_settle = False
    if sum([args.only_soccer, args.only_cfb, args.only_soccer_tomorrow, args.only_cfb_tomorrow,
            args.only_euro_early, args.only_hockey_tomorrow]) > 1:
        raise SystemExit("--only-soccer, --only-cfb, --only-soccer-tomorrow, --only-cfb-tomorrow, "
                          "--only-euro-early, and --only-hockey-tomorrow are mutually exclusive")
    only_sports = PRODUCT_SPORTS["soccer"] if args.only_soccer else frozenset({"CFB"}) if args.only_cfb else None
    label = "SOCCER" if args.only_soccer else "CFB" if args.only_cfb else ""
    # Early passes route to that product's real (owner + paying
    # subscribers) list; soccer's early pass shares the same "soccer"
    # subscriber list the main run uses, so a soccer subscriber gets both
    # without needing two separate purchases. (Until MLS's 2026-09-27
    # retirement, this comment described the main run's MLS pass
    # specifically -- soccer's product is now just the 4 European leagues,
    # covered by the early pass alone.)
    early_to = recipients_for("soccer") if args.only_soccer else recipients_for("cfb") if args.only_cfb else None

    from playwright.sync_api import sync_playwright

    local_srv = None
    if args.serve_local:
        try:
            args.app_url, local_srv = start_local_site(ROOT / "docs")
            log(f"Serving this checkout's docs/ locally: {args.app_url} (the lock reads exactly the committed schedule/odds JSON)")
        except Exception as exc:
            log(f"WARNING: could not start the local site ({exc}) -- falling back to the deployed copy {args.app_url}")

    is_main_run = not (args.only_soccer or args.only_cfb or args.only_soccer_tomorrow or args.only_cfb_tomorrow
                       or args.only_euro_early or args.only_hockey_tomorrow)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 1000})
        page = context.new_page()
        # Real gap, found via audit: this script had zero visibility into
        # browser-console output -- a real silent-failure case (CFB manual
        # trigger 2026-08-29: "Locked 17/19 legs" + "Flushed locks to
        # Supabase" logged, but a fresh Supabase re-pull showed 0 of them
        # actually persisted) would have logged its real cause as a
        # console.warn (syncBetsToSupabase's own '[CV] Supabase sync ...
        # failed' path) that never reached this script at all. Only
        # forwards warnings/errors -- console.log is too noisy (the app
        # logs routine state on nearly every render).
        page.on("console", lambda msg: log(f"[browser {msg.type}] {msg.text}")
                if msg.type in ("warning", "error") else None)
        install_espn_relay(page)
        # Real bug, found and fixed: the app has ~20 setInterval-based
        # background timers (live score tickers, per-sport settle polling,
        # data-sync pulls -- 30s to 90s cadence) that start automatically
        # on page load for a normal interactive session. In this headless
        # one-shot session they serve no purpose (this script calls every
        # settle/lock function explicitly and exits when done) and are a
        # real hazard: once one of these intervals fires mid-run, its own
        # fetch() to ESPN/NHL gets routed through the SAME synchronous
        # Python relay handler as this script's own deliberate calls --
        # concurrent blocking requests.get() calls dispatched through
        # Playwright's route handler can pile up and stall the whole
        # session. Confirmed live: a settle run that used to finish in
        # ~7 minutes instead sat with near-zero CPU progress for 30+
        # minutes once enough settle functions started actually succeeding
        # (i.e. once the relay fix above started working, taking long
        # enough for these timers to kick in). Neutralizing setInterval
        # before any of the page's own scripts run removes the whole
        # class of risk instead of hunting down and disabling ~20 named
        # interval variables one at a time.
        page.add_init_script("window.setInterval = () => 0;")
        log(f"Loading {args.app_url} …")
        page.goto(with_nologo(args.app_url), wait_until="load", timeout=60000)
        page.wait_for_timeout(3000)

        bet_count = load_bet_ledger(page)
        log(f"Loaded {bet_count} real bets from Supabase into headless session")

        # Only the MAIN workflow (auto-lock-settle.yml) commits these files. The dedicated early/evening lock passes used to write
        # them too -- uncommitted -- which left tracked files dirty, and a dirty tree makes the `git rebase origin/main` fallback in
        # their marker-commit steps (and in _commit_and_push) refuse to run ("unstaged changes"), silently skipping the marker write
        # whenever the first push was rejected by a concurrent bot commit. Skipping the writes here removes that failure mode.
        ledger_fp_before = None
        if is_main_run and not args.watchdog:
            try:
                backed_up = write_ledger_backup(page)
                log(f"Wrote docs/picks_backup.json ({backed_up} bets)")
            except Exception as exc:
                log(f"WARNING: ledger backup write failed: {exc}")
            try:
                write_landing_performance(page)
            except Exception as exc:
                log(f"WARNING: landing performance snapshot failed: {exc}")
            ledger_fp_before = ledger_fingerprint(page) if args.live else None

        if do_settle:
            # No email here by design -- intraday settle passes exist to
            # keep bets accurate as soon as real results are available,
            # not to report to anyone. See --daily-digest for the actual
            # once-daily "yesterday's results" email, run separately each
            # morning per explicit request.
            try:
                settled = run_settle(page, args.live)
                if args.live:
                    write_automation_status("lastSettle", True, f"{len(settled)} bet(s) settled")
            except Exception as exc:
                log(f"settle step failed: {exc}")
                if args.live:
                    write_automation_status("lastSettle", False, f"error: {exc}")
                raise
        if args.watchdog:
            try:
                run_watchdog(page, args.live)
            except Exception as exc:
                log(f"watchdog failed: {exc}")
                raise
        elif do_lock:
            today_mt = datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%d")
            n_reports_before = len(PASS_REPORTS)

            def _evening(label: str, status_key: str, runner, product: str) -> None:
                """Rolling-horizon evening-prior pass (see _run_rolling_pass): locks every not-yet-started game on today's AND
                tomorrow's Mountain dates, however late GitHub ran the workflow. Catch-up gating lives in the workflow's own
                marker step (which now reads this pass's `complete` flag from --result-file), not here."""
                dates = horizon_dates()
                try:
                    locked_this_pass = runner(page, args.live, send_email=True, to=recipients_for(product))
                    verified_count = verify_locks_for_dates(page, dates) if args.live else None
                    if args.live:
                        ok = not (locked_this_pass > 0 and (verified_count or 0) == 0)
                        detail = (f"{label} rolling-horizon pass completed, {locked_this_pass} locked this pass, "
                                  f"{verified_count} pick(s) verified for {' + '.join(dates)}{guard_note()}")
                        finish_live_pass(status_key, ok, detail, PASS_REPORTS[n_reports_before:], args.result_file)
                except Exception as exc:
                    log(f"{label} evening-prior lock step failed: {exc}")
                    if args.live:
                        write_automation_status(status_key, False, f"error: {exc}")
                    raise

            if args.only_soccer_tomorrow:
                # Evening-prior pass -- different gather function than the today()-based flow below, but rolling-horizon since
                # 2026-10-02 (today AND tomorrow), so it is correct whether the delayed run lands before or after midnight MT.
                _evening("soccer", "lastSoccerEveningLock", run_soccer_evening_lock, "soccer")
            elif args.only_cfb_tomorrow:
                # Same shape for CFB: gather_cfb_legs_for_dates reads docs/cfb_schedule.json (whole season, twice-daily refresh).
                _evening("CFB", "lastCfbEveningLock", run_cfb_evening_lock, "cfb")
            elif args.only_hockey_tomorrow:
                # Same shape for SHL/Liiga/NLA/Extraliga; NHL excluded, it never plays this early.
                _evening("SHL/Liiga/NLA/Extraliga", "lastHockeyEveningLock", run_hockey_evening_lock, "hockey")
            elif args.only_euro_early:
                # Combined early pass -- soccer (4 leagues) + SHL/Liiga/NLA/Extraliga, one gather_legs() pull, two
                # separately-addressed emails. See run_euro_early_lock's own docstring.
                try:
                    locked_this_pass = run_euro_early_lock(page, args.live, send_email=True)
                    verified_count = verify_todays_locks(page) if args.live else None
                    if args.live:
                        write_failed = locked_this_pass > 0 and not verified_count
                        finish_live_pass(
                            "lastLock", not write_failed,
                            f"EURO EARLY lock pass completed, {locked_this_pass} locked this pass, "
                            f"{verified_count} pick(s) verified for {today_mt}{guard_note()}"
                            + (" -- WRITE MAY HAVE SILENTLY FAILED" if write_failed else ""),
                            PASS_REPORTS[n_reports_before:], args.result_file)
                except Exception as exc:
                    log(f"EURO EARLY lock step failed: {exc}")
                    if args.live:
                        write_automation_status("lastLock", False, f"EURO EARLY error: {exc}")
                    raise
            elif only_sports is None:
                # Main (all-sports) lock: per explicit request, 3 scheduled
                # attempts each morning, each a real test that picks locked
                # correctly (not just "did the function throw") -- but the
                # subscriber email only goes out ONCE, after the LAST of
                # the 3 checks, so it reflects everything all 3 attempts
                # together managed to lock rather than racing to report
                # after just the first. Locking itself
                # (_lock_qualifying_legs/lockPick) is already idempotent,
                # so every attempt safely re-locks anything a dropped/
                # failed earlier attempt missed regardless of whether it
                # emails. --final-lock-check (passed only by the
                # workflow's last scheduled attempt) controls the email;
                # last_lock_email_date.txt is kept only as a backup dedup
                # in case that flag is ever passed more than once in a day.
                email_marker_path = ROOT / "data" / "last_lock_email_date.txt"
                already_emailed_today = False
                if args.live:
                    already_emailed_today = read_marker_from_origin("data/last_lock_email_date.txt") == today_mt
                want_email = args.final_lock_check and not already_emailed_today
                try:
                    run_lock_segmented(page, args.live, send_email=want_email)
                    # The actual verification test: a fresh, independent
                    # Supabase re-pull confirming today's picks are really
                    # persisted, not just that lockPick() didn't throw.
                    verified_count = verify_todays_locks(page) if args.live else None
                    if args.live:
                        detail = f"lock pass completed, {verified_count} pick(s) verified for {today_mt}{guard_note()}"
                        finish_live_pass("lastLock", True, detail, PASS_REPORTS[n_reports_before:], args.result_file)
                        if want_email:
                            try:
                                email_marker_path.write_text(today_mt)
                                _commit_and_push(["data/last_lock_email_date.txt"],
                                                  f"chore: record lock email sent for {today_mt}")
                            except Exception as exc:
                                log(f"failed to record lock-email marker: {exc}")
                except Exception as exc:
                    log(f"lock step failed: {exc}")
                    if args.live:
                        write_automation_status("lastLock", False, f"error: {exc}")
                    raise
            else:
                # Early single-product pass (soccer / CFB) -- its own dedicated run at a time chosen for that sport's earliest
                # kickoffs, not part of the main flow's "wait for the final check" pattern -- always emails immediately.
                try:
                    locked_this_pass = run_lock(page, args.live, only_sports=only_sports, label=label,
                                                 to=early_to, send_email=True)
                    verified_count = verify_todays_locks(page) if args.live else None
                    if args.live:
                        # Real bug, found live 2026-08-29: a CFB run logged
                        # "Locked 17/19 legs" + "Flushed locks to Supabase"
                        # and still reported lastLock ok=True, but a fresh
                        # Supabase re-pull showed 0 of those 17 actually
                        # persisted. locked_this_pass > 0 with verified_count
                        # still 0 means every leg this pass locked failed to
                        # persist -- surface that as ok=False instead of a
                        # false "completed" status.
                        write_failed = locked_this_pass > 0 and not verified_count
                        finish_live_pass(
                            "lastLock", not write_failed,
                            f"{label} lock pass completed, {locked_this_pass} locked this pass, "
                            f"{verified_count} pick(s) verified for {today_mt}{guard_note()}"
                            + (" -- WRITE MAY HAVE SILENTLY FAILED" if write_failed else ""),
                            PASS_REPORTS[n_reports_before:], args.result_file)
                except Exception as exc:
                    log(f"{label} lock step failed: {exc}")
                    if args.live:
                        write_automation_status("lastLock", False, f"{label} error: {exc}")
                    raise
        if args.daily_digest:
            try:
                send_daily_settlement_digest(page, args.live)
            except Exception as exc:
                log(f"daily digest failed: {exc}")
                raise
        if args.adaptive_recalibration:
            try:
                run_adaptive_recalibration(page, args.live)
            except Exception as exc:
                log(f"adaptive recalibration failed: {exc}")
                raise
        if args.top_picks_digest:
            try:
                today_mt = datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%d")
                bets = gather_todays_locked_bets(page, today_mt)
                log(f"Top picks digest: {len(bets)} locked pick(s) found for {today_mt}")
                if args.live:
                    send_top_picks_digest_email(bets, today_mt)
                else:
                    digest = build_top_picks_digest(bets)
                    log(f"[DRY RUN] Would send top picks digest: {len(digest['top7'])} overall, "
                        f"{sum(len(v) for v in digest['top4ByLeague'].values())} across "
                        f"{len(digest['top4ByLeague'])} league(s) (pass --live to actually send)")
            except Exception as exc:
                log(f"top picks digest failed: {exc}")
                raise

        # The backup/landing JSON above were written BEFORE this run settled/locked anything, so a settle or lock that changed the
        # ledger left picks_backup.json (and the public Live Track Record JSON) one full pass behind -- and the cheap settle gate
        # (scripts/settle_gate.py, which reads only that file) would see just-settled picks as still pending and fire a redundant
        # heavy run. Rewrite them now, from the same in-page ledger (no extra Supabase read), but only for a LIVE run that really
        # changed it. The workflow's "Commit ledger backup + landing performance JSON" step commits whatever is on disk.
        if is_main_run and not args.watchdog and ledger_fp_before is not None:
            try:
                if ledger_fingerprint(page) != ledger_fp_before:
                    backed_up = write_ledger_backup(page)
                    log(f"Ledger changed during this pass -- rewrote docs/picks_backup.json ({backed_up} bets)")
                    write_landing_performance(page)
            except Exception as exc:
                log(f"WARNING: post-pass ledger backup refresh failed: {exc}")

        if args.live and not args.watchdog:
            try:
                # the main workflow's own step commits the files it wrote; every other pass has to commit them itself
                persist_ledger_backup(page, commit=not is_main_run)
            except Exception as exc:
                log(f"WARNING: ledger backup persist failed: {exc}")

        browser.close()

    if local_srv is not None:
        try:
            local_srv.shutdown()
            local_srv.server_close()
        except Exception:
            pass

    log("Done." if args.live else "Done (dry-run — nothing was written).")


if __name__ == "__main__":
    main()
