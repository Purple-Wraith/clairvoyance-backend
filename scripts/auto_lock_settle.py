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
picks via the real lockPick()/lockProp()/lockNHLProp()/lockNFLModelProp()
functions -- then flushes everything back to Supabase via the app's own
syncBetsToSupabase().

Auto-lock scope: ML, spread, and O/U for every sport/league with a real
proprietary model (NBA, NHL, NFL, CFB, and 6 soccer leagues).
NCAAH only has a market-read-back model (no proprietary edge to
grade), matching how the rest of the app already treats it -- ML only
there via _epGatherESPNCacheLegs, not extended here. Player props covered
for NBA/NHL/NFL (the three sports with real live prop engines built
this session). Tennis engine retired 2026-09-08, and MLB/WNBA/CBB/World
Cup retired 2026-09-08 (removed from app.html and this pipeline
entirely, not merely kept off paid products) -- none are fetched,
locked, or settled anywhere in this pipeline anymore.

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

# _LOCKS_EMAIL_CLOSE (the disclaimer-bearing close, defined in
# _gmail_email.py so _subscribers.py's receipt email can share the exact
# same copy without a circular import) is used at BOTH return points in
# build_locks_email_html() below, including the "no legs qualified
# today" early return -- structurally impossible to add a new return
# path that skips it.

ROOT = Path(__file__).resolve().parent.parent
APP_URL = "https://purple-wraith.github.io/clairvoyance-backend/app.html"
# Separate from SOCIAL_CARD_EMAIL_TO on purpose -- this is a personal daily
# betting reference, not public social content, so it can (and probably
# should) go to a different inbox. Falls back to the social recipient only
# so this doesn't silently go nowhere if the dedicated secret isn't set yet.
LOCKS_EMAIL_TO = os.environ.get("LOCKS_EMAIL_TO", "") or os.environ.get("SOCIAL_CARD_EMAIL_TO", "")

# Human-readable section headers for the email, keyed by the same sport
# tags SPORT_TO_LOCKPICK_TYPE/_autoLockCapture use.
SPORT_DISPLAY_NAME = {
    "NBA": "NBA", "NHL": "NHL", "NFL": "NFL", "CFB": "CFB",
    "SOC_BL": "Bundesliga", "SOC_LIGA": "La Liga", "SOC_MLS": "MLS", "SOC_PL": "Premier League",
    "SOC_ITA": "Serie A", "SOC_CL": "Champions League",
}

# Display names keyed by the LEDGER's own `league` field (lockPick()'s
# _leagueMap in docs/app.html -- BUND/LIGA/PL/SERIEA/CL/MLS, no "SOC_"
# prefix) -- a DIFFERENT tag scheme from SPORT_DISPLAY_NAME above, which
# is keyed by _autoLockCapture's sport tag instead. The top-picks digest
# groups by the stored `league` field (what's actually on a locked bet
# row), so it needs this mapping, not SPORT_DISPLAY_NAME.
LEDGER_LEAGUE_DISPLAY_NAME = {
    "NBA": "NBA", "NHL": "NHL", "NFL": "NFL", "CFB": "CFB",
    "KHL": "KHL", "SHL": "SHL", "LIIGA": "Liiga", "NCAAH": "College Hockey",
    "CL": "Champions League", "PL": "Premier League", "LIGA": "La Liga", "BUND": "Bundesliga",
    "MLS": "MLS", "SERIEA": "Serie A",
}

# Maps the sport tag docs/app.html's _autoLockCapture() attaches to each
# game leg (e.g. 'SOC_PL' for Premier League, to keep 'PL' unambiguous --
# lockPick's own type='PL' means NHL puck line) to the exact `type` string
# lockPick() itself expects to resolve the correct sportTag.
SPORT_TO_LOCKPICK_TYPE = {
    "NBA": "NBA", "NHL": "NHL", "NFL": "NFL", "CFB": "CFB",
    "SOC_BL": "BUND", "SOC_LIGA": "LIGA", "SOC_MLS": "MLS", "SOC_PL": "PL_SOC",
    "SOC_ITA": "SERIEA", "SOC_CL": "CL",
}
# The 5 European leagues, kept separate from SOC_MLS as its own constant
# purely so build_locks_email_html can split the combined soccer email into
# an "EUROPEAN" section and a "NORTH AMERICA (MLS)" section -- soccer-lock-
# early.yml itself now gathers both (see PRODUCT_SPORTS["soccer"] below),
# it just labels which of the two a given qualifying leg belongs to.
EURO_SOCCER_SPORTS = frozenset({"SOC_CL", "SOC_PL", "SOC_LIGA", "SOC_BL", "SOC_ITA"})

# The 5 paid products (confirmed structure, see _subscribers.py) -- each
# maps to the exact sport tags gather_legs()/_autoLockCapture use. Soccer
# bundles all 6 leagues (5 European + MLS) as one purchase. Hockey is
# NHL-only.
# Explicit decision 2026-09-03: KHL, SHL, LIIGA, and College Hockey
# (NCAAH) are real, live engine features kept running for PERSONAL USE
# ONLY -- never sold, and explicitly routed to the owner-only "other" pass
# (see run_lock_segmented below) rather than bundled into any paid
# product, even though SHL/LIIGA/NCAAH have no real game cards wired up
# yet. KHL/SHL/LIIGA were removed from hockey's sport set (down to NHL
# only); NCAAH was never added to any product's set. Their pick
# generation/lock/settle logic elsewhere in this file (and app.html) is
# untouched -- this mapping only controls what ships to paying
# subscribers, not what the engine runs. User plans to build SHL/LIIGA/
# NCAAH out for real later -- when that happens, this is the mapping to
# revisit for making them paid.
# Explicit decision 2026-09-08: tennis, MLB, WNBA, CBB, and World Cup
# were all retired from the engine entirely -- removed from app.html and
# this pipeline, not merely kept off paid products. "mlb" was dropped
# from PRODUCT_SPORTS/PRODUCT_LABEL, leaving 5 paid products instead of
# 6 (WNBA had already never been a paid product).
PRODUCT_SPORTS: dict[str, frozenset[str]] = {
    "nfl": frozenset({"NFL"}),
    "cfb": frozenset({"CFB"}),
    "nba": frozenset({"NBA"}),
    "hockey": frozenset({"NHL"}),
    "soccer": EURO_SOCCER_SPORTS | frozenset({"SOC_MLS"}),
}
# Explicit request, 2026-09-03 (revised same day): auto-lock for
# nfl/cfb/nba/nhl/soccer goes to a paying subscriber's inbox;
# SHL, LIIGA, and NCAAH (College Hockey) also auto-lock now, but route to
# the owner-only "other" email instead -- personal use, never sold. Only
# KHL is excluded from the automated pipeline entirely (its own manual
# lock/settle buttons in the app UI still work for personal use -- this
# only scopes run_lock_segmented's automated pass). SHL/LIIGA/NCAAH have
# no real _autoLockCapture calls yet, so they're a no-op today, but the
# allow-list is already correct for when that changes. (Tennis, MLB,
# WNBA, CBB, and World Cup were all fully retired 2026-09-08 -- none of
# them have any pick generation left anywhere in this pipeline, so none
# belong in this allow-list at all anymore.)
OTHER_ALLOWED_SPORTS: frozenset[str] = frozenset({"SHL", "LIIGA", "NCAAH"})
PRODUCT_LABEL: dict[str, str] = {
    "nfl": "NFL", "cfb": "CFB", "nba": "NBA",
    "hockey": "HOCKEY", "soccer": "SOCCER",
}
# OPTIMAL=2, PREMIUM=3 in _evalMkts()'s own tierN scale.
QUALIFYING_TIERS = {2, 3}
TIER_LABEL = {0: "SKIP", 1: "LEAN", 2: "OPTIMAL", 3: "PREMIUM"}


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
        if subprocess.run(["git", "-C", str(ROOT), *_GIT_IDENTITY, "rebase", "origin/main"], capture_output=True).returncode == 0:
            subprocess.run(["git", "-C", str(ROOT), "push", "origin", "main"], capture_output=True)
        else:
            subprocess.run(["git", "-C", str(ROOT), "rebase", "--abort"], capture_output=True)
            log(f"_commit_and_push: push failed and rebase also failed: {push_res.stderr}")


def write_automation_status(kind: str, ok: bool, detail: str) -> None:
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
    }
    try:
        path.write_text(json.dumps(status, indent=2))
        _commit_and_push(["docs/automation_status.json"], f"chore: {kind} status ({'ok' if ok else 'FAILED'})")
    except Exception as exc:
        log(f"write_automation_status failed: {exc}")


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
        () => {
          if (typeof adaptiveTick !== 'function') return null;
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


def load_bet_ledger(page) -> int:
    """Same paginated Supabase pull generate_social_cards.py already uses
    (PostgREST caps a single response at 1000 rows), loaded straight into
    the page's own getP()/saveP() store so every real client function
    (settlement, dedup checks, sync) operates on the real live ledger.
    Uses docs/app.html's OWN already-declared SUPABASE_URL/SUPABASE_KEY
    consts (the anon key, safe client-side by RLS design, same one every
    other Supabase call in the app already uses) rather than injecting a
    separate credential from Python."""
    count = page.evaluate(
        """
        async () => {
          const rows = [];
          let offset = 0;
          const page_size = 1000;
          while (true) {
            const r = await fetch(SUPABASE_URL + '/rest/v1/bets?select=raw&order=date.desc&outcome=neq._removed', {
              headers: {
                apikey: SUPABASE_KEY, Authorization: 'Bearer ' + SUPABASE_KEY,
                Range: offset + '-' + (offset + page_size - 1),
              }
            });
            if (!r.ok) return -1;
            const batch = await r.json();
            rows.push(...batch);
            if (batch.length < page_size) break;
            offset += page_size;
          }
          const preds = rows.map(x => x.raw).filter(Boolean);
          saveP(preds);
          return preds.length;
        }
        """
    )
    if count is None or count < 0:
        raise RuntimeError("Failed to load bet ledger from Supabase in-page — check SUPABASE_URL/KEY")
    return count


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
                betOn: p.betOn, sport: p.sport, outcome: p.outcome, date: p.date,
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


def build_settlement_email_html(settled: list[dict], unresolved: list[dict] | None = None) -> str:
    by_sport: dict[str, list[dict]] = {}
    for b in settled:
        by_sport.setdefault(b.get("sport") or "?", []).append(b)

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
                      + "".join(f'<li>{_esc(b.get("sport"))}: {_esc(b.get("betOn"))}</li>' for b in unresolved)
                      + '</ul></div>')
    if not settled:
        parts.append('<div style="padding:20px 0;color:#555;font-size:14px">No bets settled this run.</div>')
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
    parts.append(_EMAIL_WRAP_CLOSE)
    return "".join(parts)


def send_settlement_email(settled: list[dict], live: bool, forced_date: str | None = None,
                           unresolved: list[dict] | None = None) -> None:
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
    body_html = build_settlement_email_html(settled, unresolved=unresolved)
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
            betOn: p.betOn, sport: p.sport, outcome: p.outcome, date: p.date,
            hA: p.hA, awA: p.awA, hScore: p.hScore, aScore: p.aScore,
            playerResult: p.playerResult,
          }))
        """,
        yesterday,
    )
    settled = [r for r in rows if r.get("outcome") != "pending"]
    unresolved = [r for r in rows if r.get("outcome") == "pending"]
    if unresolved:
        log(f"WARNING: {len(unresolved)} bet(s) from {yesterday} still unresolved after final digest check: "
            + ", ".join(r.get("betOn") or "?" for r in unresolved))
    log(f"Digest for {yesterday}: {len(settled)} settled, {len(unresolved)} unresolved")
    send_settlement_email(settled, live, forced_date=yesterday, unresolved=unresolved)


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
                  if (localIso !== todayIso || g.state === 'post') return;
                  try { _nflGameCard2(g); } catch (e) {}
                }));
              }
            }
          } catch (e) {}
          const warmups = [];
          if (typeof renderNBAGames === 'function') { try { renderNBAGames(); } catch (e) {} }
          if (typeof renderNHLGames === 'function') warmups.push(renderNHLGames().catch(() => {}));
          if (typeof renderGenericWeek === 'function') {
            warmups.push(renderGenericWeek('cfb-week-list', 'football/college-football', 'CFB').catch(() => {}));
            warmups.push(renderGenericWeek('nfl-week-list', 'football/nfl', 'NFL').catch(() => {}));
          }
          if (typeof renderLeagueMatches === 'function') {
            ['bl', 'liga', 'mls', 'pl', 'ita', 'cl'].forEach(k => warmups.push(renderLeagueMatches(k).catch(() => {})));
          }
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
          try {
            // Real bug, found + fixed via a rigorous audit, 2026-09-03:
            // this checked window._NFL_DATA, which is ALWAYS undefined --
            // _NFL_DATA is declared `let _NFL_DATA=null` at the top level
            // of app.html's classic (non-module) <script>, and a
            // top-level `let`/`const` never becomes a `window` own-
            // property the way `var` or a `function` declaration does.
            // Confirmed live with a fresh headless Playwright session,
            // matching this script's own real timing (goto + 40s wait):
            // window._NFL_DATA stayed null while the bare `_NFL_DATA`
            // identifier (same page.evaluate() global scope every
            // classic <script> tag shares) was already fully populated
            // (32 teams, 25 real schedule weeks). The `typeof
            // _nflModelPropsForGame === 'function'` half of this check
            // was always true (function declarations DO attach to
            // window), so this branch's `else` -- mislabeled "fn
            // missing" -- fired every single automated run regardless of
            // real data readiness, silently skipping ALL NFL player prop
            // generation for the entire lifetime of this feature. Fixed
            // by referencing the bare `_NFL_DATA` identifier instead of
            // `window._NFL_DATA` (this page.evaluate() callback runs in
            // the exact same global scope app.html's own scripts do, so
            // the bare identifier resolves correctly). NBA/WNBA/NHL's own
            // readiness checks above were audited too and are NOT
            // affected -- they read real `window._nbaTodayGames`/
            // `window._wnbaGameData`/`window._nhlTodayGames` properties
            // (explicit `window.x = ...` assignments elsewhere in
            // app.html), not a `let`-scoped bare variable.
            if (typeof _nflModelPropsForGame === 'function' && typeof _NFL_DATA !== 'undefined' && _NFL_DATA) {
              const games = [];
              Object.values(_NFL_DATA.weeks || {}).forEach(list => (list || []).forEach(g => games.push(g)));
              let generated = 0;
              games.forEach(g => { try { const p = _nflModelPropsForGame(g); p.forEach(pp => propLegs.push({ ...pp, sportTag: 'NFL', _nflGame: g })); generated += p.length; } catch (e) {} });
              propDiag.nfl = { games: games.length, generated };
            } else propDiag.nfl = { skipped: 'nfl data not ready' };
          } catch (e) { propDiag.nfl = { error: e.message }; }

          return { gameLegs, propLegs, propDiag };
        }
        """
    )


# Evening-prior-lock leagues: the 5 European leagues named for this
# feature (CL/PL/La Liga/Bundesliga/Serie A) -- MLS deliberately excluded,
# it kicks off at normal US evening times and never had the early-kickoff
# problem this exists for. Local key -> _autoLockCapture's sport tag
# (SOC_<KEY>, matching docs/app.html's leagueKey.toUpperCase() convention).
EURO_LEAGUE_KEY_TO_SPORT = {"cl": "SOC_CL", "pl": "SOC_PL", "liga": "SOC_LIGA", "bl": "SOC_BL", "ita": "SOC_ITA"}


def gather_soccer_legs_for_date(page, target_date_iso: str) -> dict:
    """Evening-prior-lock version of gather_legs(), narrowed to just the 5
    European soccer leagues and a single target date instead of "today".

    Unlike the main gather_legs() (which drives the live page's own render
    functions -- renderNBAGames(), renderLeagueMatches(), etc. -- all of
    which are hardcoded to "today" by design, see _fetchLeagueScoreboard's
    own comment on why), this calls docs/app.html's _renderSocMatchCard(g,
    leagueKey) directly, one game object at a time, sourced from
    docs/soccer_schedule_tomorrow.json (written by scrape_soccer_schedule.py
    --tomorrow) instead of any live/today-scoped fetch. _renderSocMatchCard
    itself has no "today" dependency -- it computes xG/Monte-Carlo/EV
    purely from the game object it's given and fires the same
    _autoLockCapture() hook every other sport's card renderer uses, so
    this reuses the exact same evaluation logic as the live site with zero
    duplicated grading code, just fed tomorrow's games instead of today's.

    The snapshot's own `date` field is checked against target_date_iso
    (converted to ESPN's YYYYMMDD form) before use -- same "reject a stale
    snapshot rather than silently grading the wrong day's games" guard
    loadSoccerScheduleSnapshot() already applies to the live site's
    same-day seed."""
    target_date_espn = target_date_iso.replace("-", "")
    return page.evaluate(
        """
        async ({ targetDateEspn, leagueKeys }) => {
          window._autoLockLegs = [];
          try {
            const r = await fetch('soccer_schedule_tomorrow.json', { cache: 'no-store' });
            if (r.ok) {
              const d = await r.json();
              if (d && d.date === targetDateEspn && d.leagues) {
                leagueKeys.forEach(key => {
                  (d.leagues[key] || []).forEach(g => {
                    if (!g.home || !g.away) return;
                    try { _renderSocMatchCard(g, key); } catch (e) {}
                  });
                });
              } else if (d) {
                console.warn('[CV evening-lock] soccer_schedule_tomorrow.json date mismatch, skipping:', d.date, 'expected', targetDateEspn);
              }
            } else {
              console.warn('[CV evening-lock] soccer_schedule_tomorrow.json fetch failed:', r.status);
            }
          } catch (e) { console.warn('[CV evening-lock] snapshot load error:', e.message); }
          await new Promise(r => setTimeout(r, 300));
          return { gameLegs: window._autoLockLegs || [], propLegs: [] };
        }
        """,
        {"targetDateEspn": target_date_espn, "leagueKeys": list(EURO_LEAGUE_KEY_TO_SPORT.keys())},
    )


def gather_cfb_legs_for_date(page, target_date_iso: str) -> dict:
    """Evening-prior-lock version for CFB, same rationale as
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
        async (targetDateIso) => {
          window._autoLockLegs = [];
          try {
            const r = await fetch('cfb_schedule.json', { cache: 'no-store' });
            if (r.ok) {
              const sched = await r.json();
              Object.values(sched.weeks || {}).forEach(week => (week || []).forEach(g => {
                const d = new Date(g.date);
                const localIso = isNaN(d) ? (g.date || '').slice(0, 10) : d.toLocaleDateString('sv-SE', { timeZone: 'America/Denver' });
                if (localIso !== targetDateIso || g.state === 'post') return;
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
        target_date_iso,
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


def build_qualifying(result: dict, only_sports: frozenset[str] | None = None) -> list[dict]:
    """only_sports: if given, restricts to exactly these sport tags (e.g.
    PRODUCT_SPORTS["soccer"] for the soccer early pass, {"CFB"} for the
    CFB-only early pass) -- for the dedicated early lock runs timed ahead of
    that sport/league's own earlier kickoffs."""
    def _wanted(sport: str) -> bool:
        return only_sports is None or (sport or "") in only_sports
    qualifying: list[dict] = []
    for gl in result.get("gameLegs") or []:
        sport = gl.get("sport")
        if not _wanted(sport):
            continue
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
            if tier_n in QUALIFYING_TIERS or is_high_hit:
                game_qualifying.append({
                    "kind": "GAME", "sport": sport, "hA": gl.get("hA"), "awA": gl.get("awA"),
                    "side": m.get("side"), "label": m.get("label"), "prob": m.get("prob"),
                    "ml": m.get("ml"), "dec": m.get("dec"), "tierN": tier_n, "evVal": m.get("evVal"),
                    # Same for every qualifying market on this game -- carried
                    # per-leg (not de-duped) so build_locks_email_html can
                    # regroup by matchup without needing a second pass over
                    # gameLegs.
                    "mcSummary": gl.get("mcSummary"), "best": gl.get("best"),
                })
        if len(game_qualifying) > 1:
            game_qualifying = _dedupe_opposite_sides(game_qualifying)
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
        qualifying.extend(game_qualifying)
    # Props only exist for NBA/NHL/NFL -- neither early pass (soccer,
    # CFB) needs or has any to filter, so props are simply included only on
    # the unscoped (full) run.
    if only_sports is None:
        # NFL-specific cap, found necessary the day the real-roster fix
        # landed: before that fix, propDiag.nfl was always near-empty (the
        # old team-"leaders" data source never had more than a handful of
        # players), so this loop's total NFL volume was naturally tiny.
        # Once real rosters flowed in (9-15 skill players/team x up to 8
        # categories each, including the new Anytime TD market), a single
        # game can produce 90+ candidate prop rows -- with no per-game cap,
        # every PREMIUM/OPTIMAL one of those would lock, which could mean
        # dozens of correlated same-game prop picks a day. GAME legs
        # already have an equivalent same-game correlated-market cap right
        # above this block; props never did. Capped at the best 5 per game
        # by grade then likelihood, mirroring that same philosophy.
        NFL_PROPS_PER_GAME_CAP = 5
        _grade_rank = {"PREMIUM": 0, "OPTIMAL": 1}
        nfl_props_by_game: dict[str, list[dict]] = {}
        for p in result.get("propLegs") or []:
            if p.get("grade") not in ("PREMIUM", "OPTIMAL"):
                continue
            if p.get("sportTag") == "NFL":
                game_id = (p.get("_nflGame") or {}).get("id") or f"{p.get('team')}_{p.get('opp')}"
                nfl_props_by_game.setdefault(game_id, []).append(p)
            else:
                qualifying.append({"kind": "PROP", "sport": p.get("sportTag"), "leg": p})
        for game_id, props in nfl_props_by_game.items():
            props.sort(key=lambda p: (_grade_rank.get(p.get("grade"), 2), -(p.get("likelihood") or 0)))
            for p in props[:NFL_PROPS_PER_GAME_CAP]:
                qualifying.append({"kind": "PROP", "sport": p.get("sportTag"), "leg": p})
    return qualifying


def lock_game_leg(page, q: dict, date_override: str | None = None) -> str:
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
    ml = q.get("ml")
    dec = q.get("dec")
    return page.evaluate(
        """
        async ({ hA, awA, type, betOn, prob, ml, dec, dateOverride }) => {
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
            ? _findSameMarketLock(preds, dateKey, hA, awA,
                type === 'PL' ? 'PL' : type === 'RL' ? 'RL' : type === 'OU' ? 'OU' : type === 'PROP' ? 'PROP' : type === 'SPREAD' ? 'SPREAD' : 'ML')
            : null;
          const dup = preds.find(x => x.id === id) ||
                      preds.find(x => x.date === dateKey && x.hA === hA && x.awA === awA && x.betOn === betOn) ||
                      marketDup;
          if (dup) return 'already-locked';
          const before = getP().length;
          await lockPick(hA, awA, type, betOn, prob, ml != null ? ml : '-110', dec || 1.91, dateKey, 'manual');
          const after = getP().length;
          return after > before ? 'locked' : 'failed';
        }
        """,
        {"hA": q["hA"], "awA": q["awA"], "type": lock_type, "betOn": q["label"], "prob": q["prob"], "ml": ml, "dec": dec, "dateOverride": date_override},
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
        return page.evaluate(
            """
            ({ team, player, line, over, prob, ml, sport, opp }) => {
              const before = getP().length;
              lockProp(team, player, line, over, prob, ml, sport, opp);
              return getP().length > before ? 'locked' : 'already-locked';
            }
            """,
            {"team": leg.get("team"), "player": leg.get("player"), "line": leg.get("line"),
             "over": leg.get("over") is not False, "prob": leg.get("prob") or (leg.get("conf", 0) / 100),
             "ml": leg.get("ml"), "sport": sport, "opp": leg.get("opp") or ""},
        )
    if sport == "NFL":
        # Reuses the real lockNFLModelProp(idx) by staging the exact leg it
        # expects into window._nflModelPropsCurrent, rather than
        # duplicating its internal prop-object construction here.
        return page.evaluate(
            """
            (leg) => {
              const g = leg._nflGame;
              const row = { ...leg, team: leg.team, opp: leg.opp, cat: leg.stat || leg.cat, pick: leg.over === false ? 'UNDER' : 'OVER', likelihood: Math.round((leg.prob || 0.6) * 100), grade: leg.grade };
              window._nflModelPropsCurrent = window._nflModelPropsCurrent || [];
              window._nflModelPropsCurrent.push(row);
              const before = getP().length;
              lockNFLModelProp(window._nflModelPropsCurrent.length - 1);
              return getP().length > before ? 'locked' : 'already-locked';
            }
            """,
            leg,
        )
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
        hh = ' <span style="color:#ffdd00">🔥 HIGH HIT %</span>' if _market_type(q.get("side")) == "ML" and prob >= _HIGH_HIT_P else ''
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
                and (q.get("prob") or 0) >= _HIGH_HIT_P]
    if high_hit:
        n = len(high_hit)
        parts.append(f'<div style="background:#fff6d6;border:1px solid #e6c200;'
                      f'border-radius:4px;padding:8px 12px;margin:10px 0;font-size:13px;color:#7a5900">'
                      f'🔥 {n} HIGH HIT % moneyline pick{"s" if n != 1 else ""} today '
                      f'(75%+ model win probability, regardless of tier/EV)</div>')

    # Grade + EV legend -- explains what every reader needs to interpret
    # the picks below before they hit any of them: what PREMIUM/OPTIMAL
    # mean (the only two tiers/grades a pick normally qualifies on --
    # QUALIFYING_TIERS is {2, 3} for game legs, and build_qualifying()
    # only ever includes PREMIUM/OPTIMAL-graded props), what EV means,
    # and the one deliberate exception: a HIGH HIT % moneyline pick can
    # appear even at LEAN or SKIP tier (see build_qualifying()'s
    # is_high_hit branch) -- called out explicitly here so a LEAN/SKIP
    # badge showing up doesn't read as a mistake. Shown on every send,
    # including the empty/no-picks-today one, since it's reference
    # material, not something tied to today's specific picks.
    parts.append(
        '<div style="background:#14001f;border-radius:6px;padding:14px 18px;margin:14px 0 18px">'
        '<div style="font-size:11px;letter-spacing:1.5px;color:#f20cff;text-transform:uppercase;'
        'font-weight:700;margin-bottom:8px">What the grades mean</div>'
        f'<div style="font-size:13px;color:#eee;line-height:1.6"><span style="background:{_TIER_COLOR[3]};'
        'color:#000;font-weight:700;font-size:11px;padding:1px 7px;border-radius:3px">PREMIUM</span> '
        'the model\'s highest-confidence picks -- the strongest combination of win probability and '
        f'edge. &nbsp; <span style="background:{_TIER_COLOR[2]};color:#000;font-weight:700;font-size:11px;'
        'padding:1px 7px;border-radius:3px">OPTIMAL</span> still clears our bar, just with somewhat '
        'less confidence or edge than PREMIUM. These two grades are the only ones a pick normally '
        'qualifies on.</div>'
        '<div style="font-size:13px;color:#eee;line-height:1.6;margin-top:10px">'
        '<strong style="color:#fff">EV (Expected Value)</strong> the model\'s estimated long-run profit '
        'edge over the listed price, as a percentage of stake -- e.g. EV +8.1% means the model expects '
        'this pick to profit about 8.1% of stake on average if made repeatedly at this probability and '
        'price. Game picks show EV; player props don\'t carry an EV figure in the underlying data, so '
        'only probability is shown for those.</div>'
        '<div style="font-size:13px;color:#eee;line-height:1.6;margin-top:10px">'
        '🔥 <strong style="color:#fff">HIGH HIT %</strong> the one exception to PREMIUM/OPTIMAL-only: a '
        'moneyline pick at 75%+ model win probability is included regardless of tier -- even a '
        f'<span style="background:{_TIER_COLOR[1]};color:#000;font-weight:700;font-size:11px;padding:1px 7px;'
        f'border-radius:3px">LEAN</span> or <span style="background:{_TIER_COLOR[0]};color:#fff;font-weight:700;'
        'font-size:11px;padding:1px 7px;border-radius:3px">SKIP</span> grade. That happens when a heavy '
        'favorite\'s price is too short to be good EV, but the model still thinks it wins very often -- '
        'worth knowing about even though it\'s not a normal pick.</div>'
        '</div>'
    )

    if not qualifying:
        parts.append('<div style="padding:20px 0;color:#555;font-size:14px">No PREMIUM/OPTIMAL legs cleared the bar today.</div>')
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
                    bits.append(f'Best market value on this game: {_esc(best["label"])} ({TIER_LABEL.get(best.get("tierN"), "?")})')
                parts.append(f'<div style="border-top:1px solid rgba(255,255,255,.12);margin-top:8px;padding-top:8px;'
                              f'font-size:12px;color:#bbb;line-height:1.5">{"<br>".join(bits)}</div>')
            parts.append('</div>')

    for sport in sorted(by_sport, key=lambda s: SPORT_DISPLAY_NAME.get(s, s)):
        _render_sport_section(sport)

    parts.append(_LOCKS_EMAIL_CLOSE)
    return "".join(parts)


def send_locks_email(qualifying: list[dict], live: bool, locked_count: int | None = None,
                      label: str = "", to: list[str] | None = None, date_str: str | None = None) -> None:
    recipients = to if to is not None else ([LOCKS_EMAIL_TO] if LOCKS_EMAIL_TO else [])
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
    return page.evaluate("(d) => getP().filter(p => p.date === d)", target)


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

    @property
    def confirmed(self) -> int:
        return self.new + self.already_locked


def _lock_qualifying_legs(page, qualifying: list[dict], date_override: str | None = None) -> LockResult:
    """Actually calls the real lockPick()/lockProp()/etc. for each leg.
    Shared by run_lock() (single-product early passes) and
    run_lock_segmented() (the main run's per-product loop) so there's one
    place this logic lives, not two copies that could drift.

    date_override: see lock_game_leg's own docstring -- only ever passed
    by the evening-prior soccer lock, which locks GAME legs for a date
    that isn't today() yet. Props never use this (there are none in the
    evening-prior pass's qualifying list -- only NBA/NHL/NFL have
    prop legs, none of which run on this path).

    Real gap, found and fixed in the same audit that added this type:
    'locked' vs 'already-locked' vs 'failed' used to collapse into a
    single non-'locked' bucket (the old 'dup-or-failed' string), so a
    genuine lock failure and a completely normal same-day re-check
    dedup were indistinguishable in both the logs and the email. Now
    logged and counted separately."""
    new = already_locked = failed = 0
    for q in qualifying:
        try:
            outcome = lock_game_leg(page, q, date_override) if q["kind"] == "GAME" else lock_prop_leg(page, q["sport"], q["leg"])
            label = q.get('label') or q.get('leg', {}).get('player')
            if outcome == "locked":
                new += 1
            elif outcome == "already-locked":
                already_locked += 1
                log(f"  already locked (earlier pass today): {label}")
            else:
                failed += 1
                log(f"  FAILED to lock ({outcome}): {label}")
        except Exception as exc:
            failed += 1
            log(f"  FAILED to lock: {exc}")
    return LockResult(new, already_locked, failed)


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
    n = load_bet_ledger(page)
    count = page.evaluate(
        "(d) => getP().filter(p => p.date === d && p.outcome === 'pending').length",
        target,
    )
    log(f"VERIFY: fresh Supabase pull ({n} total bets) shows {count} pick(s) locked for {target}")
    return count


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
    qualifying = build_qualifying(result, only_sports=only_sports)
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
        if send_email:
            send_locks_email(qualifying, live=True, locked_count=0, label=label, to=to)
        else:
            log(f"Locks email ({label or 'ALL'}) skipped -- already sent today")
        return 0

    result = _lock_qualifying_legs(page, qualifying)
    log(f"Locked {result.new} new, {result.already_locked} already locked, "
        f"{result.failed} failed -- {result.confirmed}/{len(qualifying)} qualifying legs confirmed locked")
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


def run_soccer_evening_lock(page, live: bool, send_email: bool = True, to: list[str] | None = None) -> int:
    """Evening-prior lock for the 5 European soccer leagues (CL/PL/La
    Liga/Bundesliga/Serie A) -- runs the NIGHT BEFORE those leagues'
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
    tomorrow_iso = (datetime.now(ZoneInfo("America/Denver")) + timedelta(days=1)).strftime("%Y-%m-%d")
    log(f"=== AUTO-LOCK (PREMIUM/OPTIMAL) — SOCCER, EVENING-PRIOR FOR {tomorrow_iso} ===")
    result = gather_soccer_legs_for_date(page, tomorrow_iso)
    qualifying = build_qualifying(result, only_sports=EURO_SOCCER_SPORTS)
    log(f"Gathered {len(result.get('gameLegs') or [])} games' worth of markets for {tomorrow_iso}")
    log(f"{len(qualifying)} qualifying PREMIUM/OPTIMAL legs found (soccer evening-prior only)")

    for q in qualifying:
        log(f"  [{q['sport']}] {q['label']} ({TIER_LABEL.get(q['tierN'], '?')})")

    label = "SOCCER — TOMORROW'S SLATE"
    if not live:
        log(f"[DRY RUN] Would lock {len(qualifying)} legs above for {tomorrow_iso} (pass --live to write)")
        if send_email:
            send_locks_email(qualifying, live=False, label=label, to=to, date_str=tomorrow_iso)
        return 0

    if not qualifying:
        if send_email:
            send_locks_email(qualifying, live=True, locked_count=0, label=label, to=to, date_str=tomorrow_iso)
        else:
            log("Locks email (SOCCER evening-prior) skipped -- already sent tonight")
        return 0

    result = _lock_qualifying_legs(page, qualifying, date_override=tomorrow_iso)
    log(f"Locked {result.new} new, {result.already_locked} already locked, {result.failed} failed -- "
        f"{result.confirmed}/{len(qualifying)} qualifying legs confirmed locked for {tomorrow_iso}")
    if result.new > 0:
        flush_to_supabase(page)
        log("Flushed locks to Supabase")
    # Same guard as run_lock/run_lock_segmented -- mainly defends against
    # a manual workflow_dispatch re-run after tonight's real slot already
    # succeeded (the YAML's own marker check normally prevents this
    # function being invoked twice in one night, but doesn't cover a
    # manual re-trigger).
    if send_email and result.new == 0 and qualifying:
        log(f"Locks email (SOCCER evening-prior) skipped -- all {len(qualifying)} qualifying "
            f"leg(s) were already locked earlier tonight, nothing new to report")
    elif send_email:
        send_locks_email(qualifying, live=True, locked_count=result.confirmed, label=label, to=to, date_str=tomorrow_iso)
    else:
        log("Locks email (SOCCER evening-prior) skipped -- already sent tonight")
    return result.new


def run_cfb_evening_lock(page, live: bool, send_email: bool = True, to: list[str] | None = None) -> int:
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
    tomorrow_iso = (datetime.now(ZoneInfo("America/Denver")) + timedelta(days=1)).strftime("%Y-%m-%d")
    log(f"=== AUTO-LOCK (PREMIUM/OPTIMAL) — CFB, EVENING-PRIOR FOR {tomorrow_iso} ===")
    result = gather_cfb_legs_for_date(page, tomorrow_iso)
    qualifying = build_qualifying(result, only_sports=frozenset({"CFB"}))
    log(f"Gathered {len(result.get('gameLegs') or [])} games' worth of markets for {tomorrow_iso}")
    log(f"{len(qualifying)} qualifying PREMIUM/OPTIMAL legs found (CFB evening-prior only)")

    for q in qualifying:
        log(f"  [{q['sport']}] {q['label']} ({TIER_LABEL.get(q['tierN'], '?')})")

    label = "CFB — TOMORROW'S SLATE"
    if not live:
        log(f"[DRY RUN] Would lock {len(qualifying)} legs above for {tomorrow_iso} (pass --live to write)")
        if send_email:
            send_locks_email(qualifying, live=False, label=label, to=to, date_str=tomorrow_iso)
        return 0

    if not qualifying:
        if send_email:
            send_locks_email(qualifying, live=True, locked_count=0, label=label, to=to, date_str=tomorrow_iso)
        else:
            log("Locks email (CFB evening-prior) skipped -- already sent tonight")
        return 0

    result = _lock_qualifying_legs(page, qualifying, date_override=tomorrow_iso)
    log(f"Locked {result.new} new, {result.already_locked} already locked, {result.failed} failed -- "
        f"{result.confirmed}/{len(qualifying)} qualifying legs confirmed locked for {tomorrow_iso}")
    if result.new > 0:
        flush_to_supabase(page)
        log("Flushed locks to Supabase")
    if send_email and result.new == 0 and qualifying:
        log(f"Locks email (CFB evening-prior) skipped -- all {len(qualifying)} qualifying "
            f"leg(s) were already locked earlier tonight, nothing new to report")
    elif send_email:
        send_locks_email(qualifying, live=True, locked_count=result.confirmed, label=label, to=to, date_str=tomorrow_iso)
    else:
        log("Locks email (CFB evening-prior) skipped -- already sent tonight")
    return result.new


def run_lock_segmented(page, live: bool, send_email: bool = True) -> None:
    """Main (unscoped) lock run -- ONE gather_legs() call (the expensive
    part: real browser + live data warmups), then split into a separate
    qualifying-legs list + a separately-addressed email for each of the 5
    paid sport products (see _subscribers.py), plus one more pass for
    OTHER_ALLOWED_SPORTS (SHL/LIIGA/NCAAH -- personal-use, never
    sold) sent to the owner only. Anything outside covered_sports |
    OTHER_ALLOWED_SPORTS (KHL -- explicit 2026-09-03 decision; tennis,
    MLB, WNBA, CBB, and World Cup are all fully retired, so they no
    longer generate any legs here at all) is dropped entirely before
    either pass, never auto-locked by this automated run at all -- KHL's
    own manual lock/settle buttons in the app UI are untouched, this only
    scopes automation. A subscriber to
    one product only ever sees that product's email; nothing IN SCOPE is
    ever silently dropped -- every qualifying leg that survives the
    top-level filter lands in exactly one of these passes. send_email=False
    for a redundant same-morning retry (see run_lock's own docstring) --
    still locks and verifies, just skips re-sending every product's
    email."""
    log("=== AUTO-LOCK (PREMIUM/OPTIMAL) — ALL PRODUCTS ===")
    result = gather_legs(page)
    log(f"Gathered {len(result.get('gameLegs') or [])} games' worth of markets, "
        f"{len(result.get('propLegs') or [])} prop legs total")
    prop_diag = result.get("propDiag") or {}
    if prop_diag:
        log("  prop generation: " + ", ".join(f"{sp}={detail}" for sp, detail in prop_diag.items()))
    covered_sports: set[str] = set().union(*PRODUCT_SPORTS.values())
    all_qualifying_raw = build_qualifying(result)
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
        log(f"[{product}] {len(qualifying)} qualifying legs -> {len(recipients)} recipient(s)")
        if not live:
            if send_email:
                send_locks_email(qualifying, live=False, label=label, to=recipients)
            continue
        result = _lock_qualifying_legs(page, qualifying) if qualifying else LockResult(0, 0, 0)
        total_locked += result.new
        log(f"[{product}] {result.new} new, {result.already_locked} already locked, "
            f"{result.failed} failed -- {result.confirmed}/{len(qualifying)} confirmed locked")
        # Real bug, found via audit -- but NOT fixed the way run_lock's
        # own version of this guard is: soccer and CFB both have their
        # own dedicated early/evening pass with a BETTER-timed, more
        # specific email (soccer-lock-early.yml, cfb-lock-early.yml, and
        # now an evening-prior lock for both), so this run's own
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
            send_locks_email(qualifying, live=True, locked_count=result.confirmed, label=label, to=recipients)
        else:
            log(f"Locks email ({label}) skipped -- already sent today ({result.new} locked this pass)")

    other_qualifying = [q for q in all_qualifying if q["sport"] in OTHER_ALLOWED_SPORTS]
    log(f"[other] {len(other_qualifying)} qualifying legs (SHL/LIIGA/NCAAH, personal-use -- owner only)")
    owner_to = [OWNER_EMAIL] if OWNER_EMAIL else None
    if not live:
        if send_email:
            send_locks_email(other_qualifying, live=False, label="OTHER", to=owner_to)
    else:
        result = _lock_qualifying_legs(page, other_qualifying) if other_qualifying else LockResult(0, 0, 0)
        total_locked += result.new
        log(f"[other] {result.new} new, {result.already_locked} already locked, "
            f"{result.failed} failed -- {result.confirmed}/{len(other_qualifying)} confirmed locked")
        # No early/evening-pass exclusion here (unlike soccer/cfb above)
        # -- SHL/LIIGA/NCAAH have no dedicated pass of their own,
        # so this unscoped run's final check IS their only report, same as every
        # other non-soccer/CFB product. Reverted an over-broad
        # locked==0-means-skip guard here for the same reason explained
        # on the per-product loop above: it would wrongly silence this
        # on a normal day where an earlier check already locked
        # everything.
        if send_email:
            send_locks_email(other_qualifying, live=True, locked_count=result.confirmed, label="OTHER", to=owner_to)
        else:
            log(f"Locks email (OTHER) skipped -- already sent today ({result.new} locked this pass)")

    if total_locked > 0:
        flush_to_supabase(page)
        log(f"Flushed {total_locked} total locks to Supabase")


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
                     help="Lock step only: restrict to all 6 soccer leagues (CL/PL/La Liga/"
                          "Bundesliga/Serie A/MLS) -- the resulting email splits legs into an "
                          "EUROPEAN section and a NORTH AMERICA (MLS) section. For the "
                          "early-morning pass timed ahead of European kickoffs.")
    ap.add_argument("--only-cfb", action="store_true",
                     help="Lock step only: restrict to CFB. For the early-morning pass timed "
                          "ahead of the earliest college football kickoffs (10 AM MT+).")
    ap.add_argument("--only-soccer-tomorrow", action="store_true",
                     help="Lock step only: evening-prior lock for the 5 European soccer leagues "
                          "(CL/PL/La Liga/Bundesliga/Serie A -- no MLS), run the NIGHT BEFORE "
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
    ap.add_argument("--top-picks-digest", action="store_true",
                     help="Sends the owner-only Top Picks Digest (top 4 per league + top 7 overall "
                          "for the day, ranked by model win probability, with parlay math) to "
                          "clairvoyanceengine@gmail.com only -- never the subscriber lists. Reads "
                          "today's already-locked picks fresh from Supabase; run this AFTER the "
                          "day's real lock pass, not standalone. Does not touch the browser's other "
                          "lock/settle logic -- combine with --lock or run as its own invocation.")
    ap.add_argument("--app-url", default=APP_URL, help="Override the app URL (e.g. a local server for testing).")
    args = ap.parse_args()

    if args.alert_lock_missed:
        to = OWNER_EMAIL or LOCKS_EMAIL_TO
        if not to:
            log("alert-lock-missed: no OWNER_EMAIL/LOCKS_EMAIL_TO configured -- cannot send")
            return
        today_mt = datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%d")
        body = (f"{_EMAIL_WRAP_OPEN}"
                f'<div style="font-size:16px;color:#ff9090;font-weight:700">⚠ Lock did not run today ({today_mt})</div>'
                f'<div style="margin-top:10px;font-size:14px;color:#ccc">None of the 3 dedicated 7:05-8:05am MT '
                f'lock checks, nor the 9am-12pm MT catch-up window, produced a real lock pass today -- no new '
                f'picks were locked, and no locks email went out. This is a same-day alert so it gets noticed '
                f'today, not whenever someone happens to check the app.</div>'
                f"{_EMAIL_WRAP_CLOSE}")
        ok, msg = _send_gmail(f"Clairvoyance — Lock FAILED to run {today_mt}", to, body)
        log(f"Lock-missed alert sent to {to}" if ok else f"Lock-missed alert failed: {msg}")
        return

    do_lock, do_settle = (args.lock, args.settle) if (args.lock or args.settle) else (True, True)
    if args.daily_digest or args.adaptive_recalibration or args.top_picks_digest:
        do_lock = do_settle = False
    if sum([args.only_soccer, args.only_cfb, args.only_soccer_tomorrow, args.only_cfb_tomorrow]) > 1:
        raise SystemExit("--only-soccer, --only-cfb, --only-soccer-tomorrow, and --only-cfb-tomorrow are mutually exclusive")
    only_sports = PRODUCT_SPORTS["soccer"] if args.only_soccer else frozenset({"CFB"}) if args.only_cfb else None
    label = "SOCCER" if args.only_soccer else "CFB" if args.only_cfb else ""
    # Early passes route to that product's real (owner + paying
    # subscribers) list; soccer's early pass shares the same "soccer"
    # subscriber list the main run's MLS pass uses, so a soccer subscriber
    # gets both without needing two separate purchases.
    early_to = recipients_for("soccer") if args.only_soccer else recipients_for("cfb") if args.only_cfb else None

    from playwright.sync_api import sync_playwright

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
        page.goto(args.app_url, wait_until="load", timeout=60000)
        page.wait_for_timeout(3000)

        bet_count = load_bet_ledger(page)
        log(f"Loaded {bet_count} real bets from Supabase into headless session")

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
        if do_lock:
            today_mt = datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%d")
            if args.only_soccer_tomorrow:
                # Evening-prior pass -- entirely separate from the
                # today()-based flow below (different gather function,
                # different verify target date: tomorrow, not today).
                # Catch-up dedup lives in soccer-lock-evening.yml's own
                # marker-file check (same pattern as soccer-lock-
                # early.yml), not here, so this always locks+verifies+
                # emails when invoked.
                tomorrow_mt = (datetime.now(ZoneInfo("America/Denver")) + timedelta(days=1)).strftime("%Y-%m-%d")
                try:
                    locked_this_pass = run_soccer_evening_lock(page, args.live, send_email=True,
                                                                 to=recipients_for("soccer"))
                    verified_count = verify_locks_for_date(page, tomorrow_mt) if args.live else None
                    if args.live:
                        ok = not (locked_this_pass > 0 and (verified_count or 0) == 0)
                        detail = f"evening-prior lock pass completed, {verified_count} pick(s) verified for {tomorrow_mt}"
                        write_automation_status("lastSoccerEveningLock", ok, detail)
                except Exception as exc:
                    log(f"soccer evening-prior lock step failed: {exc}")
                    if args.live:
                        write_automation_status("lastSoccerEveningLock", False, f"error: {exc}")
                    raise
            elif args.only_cfb_tomorrow:
                # Evening-prior pass for CFB -- same shape as the soccer
                # branch above. Runs every day regardless of day-of-week
                # (Thursday/Friday CFB games get evening-prior locked
                # too, not just Saturday's -- gather_cfb_legs_for_date
                # just targets "tomorrow", whatever day that is); weekday
                # games already had comfortable same-day margin (evening
                # kickoffs), Saturday's 10am MT cluster was the one real
                # risk this exists to fix.
                tomorrow_mt = (datetime.now(ZoneInfo("America/Denver")) + timedelta(days=1)).strftime("%Y-%m-%d")
                try:
                    locked_this_pass = run_cfb_evening_lock(page, args.live, send_email=True,
                                                              to=recipients_for("cfb"))
                    verified_count = verify_locks_for_date(page, tomorrow_mt) if args.live else None
                    if args.live:
                        ok = not (locked_this_pass > 0 and (verified_count or 0) == 0)
                        detail = f"evening-prior lock pass completed, {verified_count} pick(s) verified for {tomorrow_mt}"
                        write_automation_status("lastCfbEveningLock", ok, detail)
                except Exception as exc:
                    log(f"CFB evening-prior lock step failed: {exc}")
                    if args.live:
                        write_automation_status("lastCfbEveningLock", False, f"error: {exc}")
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
                    try:
                        already_emailed_today = email_marker_path.read_text().strip() == today_mt
                    except Exception:
                        already_emailed_today = False
                want_email = args.final_lock_check and not already_emailed_today
                try:
                    run_lock_segmented(page, args.live, send_email=want_email)
                    # The actual verification test: a fresh, independent
                    # Supabase re-pull confirming today's picks are really
                    # persisted, not just that lockPick() didn't throw.
                    verified_count = verify_todays_locks(page) if args.live else None
                    if args.live:
                        detail = f"lock pass completed, {verified_count} pick(s) verified for {today_mt}"
                        write_automation_status("lastLock", True, detail)
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
                # Early single-product pass (soccer 6am / CFB 9am) -- its
                # own dedicated once-daily run at a time chosen for that
                # sport's earliest kickoffs, not part of the main flow's
                # "wait for the final check" pattern -- always emails
                # immediately, same as before.
                try:
                    locked_this_pass = run_lock(page, args.live, only_sports=only_sports, label=label,
                                                 to=early_to, send_email=True)
                    verified_count = verify_todays_locks(page) if args.live else None
                    if args.live:
                        # Real bug, found live 2026-08-29: a CFB run logged
                        # "Locked 17/19 legs" + "Flushed locks to Supabase"
                        # and still reported lastLock ok=True, but a fresh
                        # Supabase re-pull showed 0 of those 17 actually
                        # persisted (root cause still under investigation --
                        # see the new browser console-forwarding above).
                        # verify_todays_locks existed specifically to catch
                        # this class of silent write failure, but its count
                        # was only ever logged as text, never acted on.
                        # locked_this_pass > 0 with verified_count still 0
                        # means every leg this pass locked failed to
                        # persist -- surface that as ok=False instead of a
                        # false "completed" status.
                        write_failed = locked_this_pass > 0 and not verified_count
                        write_automation_status(
                            "lastLock", not write_failed,
                            f"{label} lock pass completed, {locked_this_pass} locked this pass, "
                            f"{verified_count} pick(s) verified for {today_mt}"
                            + (" -- WRITE MAY HAVE SILENTLY FAILED" if write_failed else ""))
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

        browser.close()

    log("Done." if args.live else "Done (dry-run — nothing was written).")


if __name__ == "__main__":
    main()
