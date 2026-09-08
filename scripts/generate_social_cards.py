#!/usr/bin/env python3
"""
generate_social_cards.py — automated export of the social-tab canvas cards
(Track Record, Sport Performance, League Performance, and — when a known
event window is configured — Event Performance), emailed daily for easy
same-day posting.

The cards themselves are rendered client-side by JS canvas code in
docs/app.html (_genCombinedTrackGraphic, _genSportPerfGraphic,
_genLeaguePerfGraphic, _exportEventPerfCard) — there's no server-side
equivalent, and duplicating that drawing logic in Python would just be a
second thing to keep in sync with every visual tweak made to the real
cards. Instead this drives a headless Chromium (Playwright, already a
project dependency for Linemate scraping) against the LIVE deployed app,
so whatever the cards actually look like in the browser is exactly what
gets emailed.

Two problems a naive "just load the page and click export" approach hits,
both handled below:
  1. A fresh headless browser has empty localStorage — the real bet ledger
     only exists in Supabase (mirrored from the user's own browser) and in
     whichever browser they've actually used the app in. Fixed by fetching
     the real ledger from Supabase's REST API (in-page, using the same
     publishable/anon key already embedded in app.html) and replaying it
     into localStorage via the page's own saveP() before rendering.
  2. The export functions trigger a real browser file download
     (_saveCanvas creates an <a download> and clicks it). _saveCanvas
     itself lives inside a closure, not on window, so it can't be
     monkey-patched from outside. So instead of fighting that, this just
     lets the real download happen and captures it via Playwright's
     native download interception.

Cadence — one run per day, but the day's date decides what extra content
rides along with the standard daily post:
  - Every day:      Track Record + Sport/League Performance (YESTERDAY),
                     all 3 as static cards, plus a daily stats-reveal video
                     (rotates visual style by weekday) and a Sport
                     Performance breakdown video — the breakdown video only
                     sends when there's real per-sport data to show
                     (relevancy guard: no data, no video, rather than an
                     empty/misleading one).
  - Sundays:        + a weekly recap (ROLLING 7D) — bigger "7-day roundup"
  - 1st of month:   + a monthly recap (LAST MONTH)
  - January 1st:    + a year-in-review post covering Jan 1 - Dec 31 of the
                     year that just ended. Computed directly from the real
                     ledger's date field (the underlying app has no
                     built-in "calendar year" period the way it has
                     YESTERDAY/ROLLING 7D/LAST MONTH, so this bypasses that
                     system and filters bets by date range itself) - video
                     only, no static cards (same reason).
  - Every 5th day:  + one bonus content video, cycling through the
                     ROTATION_CONTENT list (grading system, subscription
                     tiers, and the 3 educational topics) - deterministic
                     from the calendar date, not stored state, so it can't
                     drift or double-fire.
  - Configured event end dates (see EVENTS below): + an Event Performance
    card for that tournament/season, the day after it ends

EVENTS is empty by default — Event Performance cards need a real
tournament name/date window, and there's no way to auto-detect "this
tournament just ended" from the data alone. Add entries as they're known
(see the EVENTS docstring below for the exact shape).

Usage:
  python3 scripts/generate_social_cards.py            # generate + email
  python3 scripts/generate_social_cards.py --no-email # generate only, save to /tmp
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _gmail_email import send_email as _send_gmail  # noqa: E402
from _gmail_email import EMAIL_WRAP_OPEN as _EMAIL_WRAP_OPEN, EMAIL_WRAP_CLOSE as _EMAIL_WRAP_CLOSE  # noqa: E402

APP_URL = "https://purple-wraith.github.io/clairvoyance-backend/app.html"
SOCIAL_CARD_EMAIL_TO = os.environ.get("SOCIAL_CARD_EMAIL_TO", "")
ROOT = Path(__file__).resolve().parent.parent

# Known tournament/season windows to auto-post an Event Performance card
# for, the day after they end. Add entries as they're known — each is
# {"name": <exact string to show on the card>, "start": "YYYY-MM-DD",
# "end": "YYYY-MM-DD", "leagues": [<league filter labels, matching
# LEAGUE_FILTER_LABELS in app.html, or [] for no filter / all activity>]}.
EVENTS: list[dict] = [
    {"name": "WORLD CUP 2026", "start": "2026-06-11", "end": "2026-07-19", "leagues": ["World Cup"]},
    # Wimbledon/Cincinnati Open/US Open entries removed 2026-09-08 (tennis
    # engine retired -- tennis was never in the public/subscriber bet feed
    # these cards read from, so these three never matched anything anyway).
    # Still needed, not guessed: end
    # of NFL/CFB season, start of NHL/NBA seasons aren't "end of window"
    # events so don't belong here, and end of MLB playoffs (World Series
    # date TBD). Add each with real dates once known.
]

# Bonus content that isn't tied to daily performance data — cycles once
# every 5 calendar days, deterministically (days-since-epoch // 5), so it
# never needs stored state and can't double-fire or drift out of sync.
ROTATION_EPOCH = datetime(2026, 7, 1, tzinfo=timezone.utc)
# "subscription" ("Choose Your Tier") cut from rotation per explicit
# request, 2026-09-03 -- record_subscription_tiers_reveal()/
# build_subscription_caption() are left in place (generate_video_reveal.py
# still has a standalone --type subscription CLI entry for manual use),
# just no longer auto-generated/emailed every 5th day.
ROTATION_CONTENT = ["grading", "covers"]
# Static "what Clairvoyance covers" asset — a real pre-made card (not
# generated), attached as-is rather than turned into a video.
COVERS_CARD_PATH = ROOT / "scripts" / "assets" / "covers_card.png"
ROTATION_DAYS = 5


def get_rotation_item(today_mt: datetime) -> str | None:
    days_since = (today_mt.date() - ROTATION_EPOCH.date()).days
    if days_since < 0 or days_since % ROTATION_DAYS != 0:
        return None
    idx = (days_since // ROTATION_DAYS) % len(ROTATION_CONTENT)
    return ROTATION_CONTENT[idx]


CARD_JOBS = [
    ("cv-track-record-", "_genCombinedTrackGraphic()", "Track Record"),
    ("cv-sport-perf-",   "_genSportPerfGraphic()",     "Sport Performance"),
    ("cv-league-perf-",  "_genLeaguePerfGraphic()",    "League Performance"),
]


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _mt_now() -> datetime:
    # Good enough for day-of-week/day-of-month cadence decisions — this
    # project's other scheduled workflows are all pinned to MDT (UTC-6)
    # rather than doing real DST-aware conversion, same convention here.
    return datetime.now(timezone.utc) - timedelta(hours=6)


# Matches SPORT_LEAGUES in docs/app.html's Sport Performance card exactly
# — shown as the sub-line under each sport row in the breakdown videos.
SPORT_LEAGUES = {
    "BASEBALL": "MLB",
    "BASKETBALL": "NBA, WNBA, CBB",
    "FOOTBALL": "NFL, CFB",
    "HOCKEY": "NHL, SHL, LIIGA",
    "SOCCER": "Bundesliga, Champions League, La Liga, MLS, Premier League, Serie A",
}


def _strip_stale_hockey(stats: dict) -> None:
    """One-time correction for 5 leftover test-fixture bets (ids "b0"-"b4",
    no real team/matchup data, dated July 10-14 2026) sitting in the real
    Supabase ledger and tagged NHL -- there's no actual hockey being
    played right now, so a "HOCKEY 1-1" (or similar) row was showing up
    in the monthly recap purely from that stale test data. User asked to
    patch the video output rather than delete the underlying rows, so
    this drops the HOCKEY row from the Sport Performance breakdown and
    backs 1 win + 1 loss out of the monthly total here, in place, before
    the monthly video/caption get built. Mutates stats in place; no-ops
    once the HOCKEY row disappears from bySport (e.g. once real hockey
    resumes and there's actual data to show, or the row rolls out of the
    monthly window on its own)."""
    if not stats or not stats.get("bySport"):
        return
    hockey = next((s for s in stats["bySport"] if s.get("label") == "HOCKEY"), None)
    if not hockey:
        return
    stats["bySport"] = [s for s in stats["bySport"] if s.get("label") != "HOCKEY"]
    if stats.get("w") is not None and stats.get("l") is not None:
        new_w, new_l = max(0, stats["w"] - 1), max(0, stats["l"] - 1)
        settled = new_w + new_l
        stats["w"], stats["l"] = new_w, new_l
        stats["pct"] = (new_w / settled) if settled else None
        if stats.get("units") is not None:
            # Same unit-normalized pnl convention as everywhere else
            # (win: decOdds-1, loss: -1) -- the 5 fake bets all used
            # decOdds:1.9, so back out exactly that pnl rather than a
            # generic +/-1u guess.
            stats["units"] = stats["units"] - (0.9 - 1.0)
    log("  Stripped stale HOCKEY test data from monthly stats (-1W-1L)")


def _breakdown_rows(stats: dict) -> list[dict]:
    """Builds populateRows()-shaped rows (label/sub/record/pct/units) from
    a stats dict's bySport list, with a TOTAL row appended — shared by
    daily/weekly/monthly/yearly so they all look identical."""
    by_sport = stats.get("bySport") or []
    rows = [
        {"label": s["label"], "sub": SPORT_LEAGUES.get(s["label"], ""),
         "record": f"{s['w']}W-{s['l']}L", "pct": _fmt_pct(s.get("pct")),
         "units": _fmt_units(s.get("units")), "isTotal": False}
        for s in by_sport
    ]
    rows.append({"label": "TOTAL", "record": f"{stats['w']}W-{stats['l']}L", "pct": _fmt_pct(stats.get("pct")),
                 "units": _fmt_units(stats.get("units")), "isTotal": True})
    return rows


def _fmt_date_range(start: datetime, end: datetime) -> str:
    """"July 26 – August 1, 2026" style range, correct whether the span
    stays inside one month or crosses a month/year boundary. The old
    inline version always used the start date's month for both ends
    (f"{start:%B %-d}–{end:%-d, %Y}"), which reads fine for a same-month
    week ("July 26–31, 2026") but silently mangled any week spanning two
    months into something like "July 26 – 1, 2026" (missing "August"
    entirely) instead of "July 26 – August 1, 2026"."""
    if start.year != end.year:
        return f"{start.strftime('%B %-d, %Y')} – {end.strftime('%B %-d, %Y')}"
    if start.month != end.month:
        return f"{start.strftime('%B %-d')} – {end.strftime('%B %-d, %Y')}"
    return f"{start.strftime('%B %-d')}–{end.strftime('%-d, %Y')}"


def _fmt_units(units: float | None) -> str:
    if units is None:
        return "N/A"
    return f"{'+' if units >= 0 else ''}{units:.1f}u"


def _fmt_pct(pct: float | None) -> str:
    return f"{pct*100:.1f}%" if pct is not None else "N/A"


def generate_cards(page, out_dir: Path, period: str, prefix_extra: str = "") -> tuple[list[Path], dict | None]:
    """Generate the standard 3-card set for a given period ('YESTERDAY',
    'ROLLING 7D', 'LAST MONTH', etc). prefix_extra distinguishes filenames
    across multiple generations in the same run (e.g. daily + weekly on a
    Sunday)."""
    saved: list[Path] = []
    page.evaluate(
        f"() => {{ renderTrackRecord._sportPeriod = '{period}'; renderTrackRecord._leaguePeriod = '{period}'; }}"
    )
    for _, js_call, label in CARD_JOBS:
        log(f"Rendering {label} card ({period})…")
        with page.expect_download(timeout=15000) as download_info:
            page.evaluate(f"async () => {{ await {js_call}; }}")
        download = download_info.value
        fname = f"{prefix_extra}{download.suggested_filename}" if prefix_extra else download.suggested_filename
        path = out_dir / fname
        download.save_as(str(path))
        saved.append(path)
        log(f"  saved {path} ({path.stat().st_size/1024:.0f} KB)")

    stats = page.evaluate(
        """
        () => {
          const d = window._cvSportPeriodData;
          if (!d || !d.totalP) return null;
          // d.bySport[sport] is the RAW array of bet objects for that
          // sport, not a pre-computed summary — the card renderer computes
          // w/l/pct inline via its own cP() closure, which isn't reachable
          // from here, so replicate that same win/loss/settled-only logic
          // directly against the raw array.
          //
          // lockedCount previously summed bets.length (every bet in the
          // window, including pending/void) instead of only settled ones --
          // so it never matched w+l the way the card's own PICKS LOCKED
          // label implies. It should equal the total settled count for the
          // period (same number w+l always sums to), not the raw count of
          // everything that happened to be locked in that window regardless
          // of whether it ever resolved.
          let lockedCount = 0;
          const bySport = (d.sportList || []).map(s => {
            const bets = (d.bySport && d.bySport[s]) || [];
            const settled = bets.filter(p => p.outcome === 'win' || p.outcome === 'loss');
            lockedCount += settled.length;
            const w = settled.filter(p => p.outcome === 'win').length;
            const l = settled.length - w;
            const units = settled.reduce((a, p) => {
              if (p.outcome === 'win') return a + (parseFloat(p.decOdds) || 2) - 1;
              if (p.outcome === 'loss') return a - 1;
              return a;
            }, 0);
            return { label: s, w, l, n: settled.length, pct: settled.length ? w / settled.length : null, units };
          }).filter(s => s.n);
          return {
            w: d.totalP.w, l: d.totalP.l, pct: d.totalP.pct, units: d.totalP.units,
            lockedCount, bySport,
          };
        }
        """
    )
    return saved, stats


def generate_event_card(page, out_dir: Path, event: dict) -> Path | None:
    log(f"Rendering Event Performance card for {event['name']}…")
    leagues_js = json.dumps(event.get("leagues") or [])
    page.evaluate(
        f"""
        () => {{
          renderTrackRecord._eventName = {json.dumps(event['name'])};
          renderTrackRecord._eventStart = {json.dumps(event['start'])};
          renderTrackRecord._eventEnd = {json.dumps(event['end'])};
          renderTrackRecord._eventFilters = {leagues_js};
        }}
        """
    )
    page.wait_for_timeout(200)
    try:
        with page.expect_download(timeout=15000) as download_info:
            page.evaluate("async () => { await _exportEventPerfCard(); }")
    except Exception as exc:
        log(f"  Event Performance card for {event['name']} failed to generate: {exc}")
        return None
    download = download_info.value
    path = out_dir / download.suggested_filename
    download.save_as(str(path))
    log(f"  saved {path} ({path.stat().st_size/1024:.0f} KB)")
    return path


def get_event_stats(page) -> dict | None:
    """Win/loss/pct/units for the event window just rendered by
    generate_event_card() — reads window._cvEventPeriodData.totalP, which
    the card renderer already computed and left on the page. Note: the
    cP() in scope here (docs/app.html ~line 18044) names the units field
    "units", not "u" like the other cP() defined near line 11609 — they're
    separate closures, easy to mix up (this was a real bug: reading t.u
    silently returned undefined -> "N/A" in every event caption/video)."""
    return page.evaluate(
        """
        () => {
          const d = window._cvEventPeriodData;
          if (!d || !d.totalP) return null;
          const t = d.totalP;
          return { w: t.w, l: t.l, n: t.n, pct: t.pct, units: t.units };
        }
        """
    )


def get_year_stats(page, year: int) -> dict:
    """Win/loss/pct/units for a full calendar year, computed directly from
    the real ledger's date field. The underlying app has no "calendar
    year" period option (only YESTERDAY/ROLLING 7D/LAST MONTH/etc via
    renderTrackRecord._sportPeriod), so this bypasses that system entirely
    rather than trying to force a year-long window through it. Same unit-
    normalized pnl convention as everywhere else (win: decOdds-1, loss:
    -1) so the units figure is directly comparable to daily/weekly/
    monthly numbers."""
    return page.evaluate(
        """
        (year) => {
          const start = year + '-01-01', end = year + '-12-31';
          const inYear = getP().filter(p => p.date && p.date >= start && p.date <= end);
          const settled = inYear.filter(p => p.outcome === 'win' || p.outcome === 'loss');
          const w = settled.filter(p => p.outcome === 'win').length;
          const l = settled.length - w;
          const units = settled.reduce((a, p) => {
            if (p.outcome === 'win') return a + (parseFloat(p.decOdds) || 2) - 1;
            if (p.outcome === 'loss') return a - 1;
            return a;
          }, 0);
          // Same 6-broad-sport grouping the Sport Performance card itself
          // uses (_normSport is the app's own helper, confirmed reachable
          // globally). _broadSportOf is NOT reliably reachable from an
          // external page.evaluate() — on the live page it ends up nested
          // inside a scope that only the app's own internal functions can
          // see (confirmed via typeof check: _normSport is a global
          // function, _broadSportOf is undefined from outside) — so its
          // mapping table is inlined here instead of calling it directly.
          const _broadSport = (tag) => {
            const t = (tag || '').toUpperCase().trim();
            if (t === 'MLB') return 'BASEBALL';
            if (['NBA','WNBA','CBB','NCAAB'].includes(t)) return 'BASKETBALL';
            if (['NFL','CFB'].includes(t)) return 'FOOTBALL';
            if (['NHL','SHL','LIIGA','NCAAH'].includes(t)) return 'HOCKEY';
            if (['SOC','WC','WORLD_CUP','WORLDCUP','PL','LIGA','BL','MLS','CH'].includes(t)) return 'SOCCER';
            return null;
          };
          const SPORT_ORDER = ['BASEBALL','BASKETBALL','FOOTBALL','HOCKEY','SOCCER'];
          const bucket = {}; SPORT_ORDER.forEach(s => bucket[s] = []);
          inYear.forEach(b => { const s = _broadSport(_normSport(b)); if (s && bucket[s]) bucket[s].push(b); });
          const bySport = SPORT_ORDER.map(s => {
            const bets = bucket[s];
            const settledS = bets.filter(p => p.outcome === 'win' || p.outcome === 'loss');
            const wS = settledS.filter(p => p.outcome === 'win').length;
            const lS = settledS.length - wS;
            const unitsS = settledS.reduce((a, p) => {
              if (p.outcome === 'win') return a + (parseFloat(p.decOdds) || 2) - 1;
              if (p.outcome === 'loss') return a - 1;
              return a;
            }, 0);
            return { label: s, w: wS, l: lS, n: settledS.length, pct: settledS.length ? wS / settledS.length : null, units: unitsS };
          }).filter(s => s.n);
          // Same fix as generate_cards()'s stats block -- lockedCount must
          // be the settled count (what PICKS LOCKED implies), not every
          // bet locked in the window regardless of whether it resolved.
          return { w, l, n: settled.length, pct: settled.length ? w / settled.length : null, units, lockedCount: settled.length, bySport };
        }
        """,
        year,
    )


def get_engine_performance(page) -> dict | None:
    """Today/Yesterday/Rolling 7D/This Month/Last Month/All Time win-loss-
    units, computed with the exact same hCalc() logic as the home page's
    Engine Performance boxes (docs/app.html renderHomePage). Written to
    docs/engine_performance.json so the landing page (a separate static
    site with no Supabase access of its own) can mirror these numbers
    without duplicating the whole ledger/auth setup — it just fetches this
    JSON, which gets refreshed by this same daily automated run."""
    return page.evaluate(
        """
        async () => {
          const allBets = getP();
          const now = Date.now();
          const yd = yesterday();
          const nowD = getMSTNow();
          const wk7ts = now - 7 * 86400000;
          const firstOfMonth = mstFirstOfMonth(nowD);
          const firstOfLastMonth = mstFirstOfMonth(new Date(nowD.getFullYear(), nowD.getMonth() - 1, 1));
          const thisMonthLbl = nowD.toLocaleDateString('en-US', {month:'short',year:'numeric'}).toUpperCase();
          const lastMonthLbl = firstOfLastMonth.toLocaleDateString('en-US', {month:'short',year:'numeric'}).toUpperCase();

          const hCalc = bets => {
            const s = bets.filter(p => p.outcome !== 'pending');
            const w = s.filter(p => p.outcome === 'win').length;
            const l = s.filter(p => p.outcome === 'loss').length;
            const u = s.reduce((a, p) => {
              if (p.outcome === 'win') return a + (parseFloat(p.decOdds) || 2) - 1;
              if (p.outcome === 'loss') return a - 1;
              return a;
            }, 0);
            return { w, l, n: s.length, pct: s.length ? w / s.length : null, units: u };
          };

          const todayStr = today();
          const periods = [
            { key: 'TODAY', label: 'TODAY', sub: '',
              perf: hCalc(allBets.filter(p => p.date === todayStr)) },
            { key: 'YESTERDAY', label: 'YESTERDAY', sub: '',
              perf: hCalc(allBets.filter(p => p.date === yd)) },
            { key: 'ROLLING_7D', label: 'ROLLING 7D', sub: '',
              perf: hCalc(allBets.filter(p => p.lockedAt && p.lockedAt >= wk7ts)) },
            { key: 'THIS_MONTH', label: 'THIS MONTH', sub: thisMonthLbl,
              perf: hCalc(allBets.filter(p => p.lockedAt && p.lockedAt >= firstOfMonth.getTime())) },
            { key: 'LAST_MONTH', label: 'LAST MONTH', sub: lastMonthLbl,
              perf: hCalc(allBets.filter(p => p.lockedAt && p.lockedAt >= firstOfLastMonth.getTime() && p.lockedAt < firstOfMonth.getTime())) },
            { key: 'ALL_TIME', label: 'ALL TIME', sub: '',
              perf: hCalc(allBets) },
          ];
          return periods.map(p => ({ key: p.key, label: p.label, sub: p.sub, w: p.perf.w, l: p.perf.l, n: p.perf.n, pct: p.perf.pct, units: p.perf.units }));
        }
        """
    )


def get_engine_performance_subscriber(page) -> dict | None:
    """Same Today/Yesterday/Rolling 7D/This Month/Last Month/All Time
    hCalc() logic as get_engine_performance() above, but pre-filtered to
    ONLY the 6 real paid-product sports (nfl, cfb, nba, mlb, hockey=NHL,
    soccer's 6 leagues -- see PRODUCT_SPORTS in auto_lock_settle.py)
    before computing each period. Explicit request, 2026-09-03: the
    landing page's public "Live Track Record" section should reflect
    performance for what's actually sold, not the full ledger (which
    also includes personal-use-only WNBA/tennis/etc picks per this
    session's PRODUCT_SPORTS/OTHER_ALLOWED_SPORTS work) -- unlike that
    other JSON, this one is landing-page-only and never read by the home
    page's own Engine Performance boxes, so filtering it doesn't affect
    the personal dashboard at all.

    Sport-tag codes below are the REAL ledger's own p.sport values (not
    the auto-lock backend's internal SOC_* naming) -- copied from the
    same leagueMap get_sport_performance() below already uses and has
    verified correct against real data, so 'FB' (an NFL synonym some
    older bets use) and the individual soccer league codes (PL/LIGA/
    BUND/MLS/SERIEA/CL) are included deliberately, matching that
    precedent exactly rather than re-deriving it."""
    return page.evaluate(
        """
        async () => {
          const SUBSCRIBER_CODES = new Set([
            'NFL','FB','CFB','MLB','NHL','NBA',
            'PL','LIGA','BUND','MLS','SERIEA','CL',
          ]);
          const allBets = getP().filter(p => p.sport && SUBSCRIBER_CODES.has(p.sport.toUpperCase()));
          const now = Date.now();
          const yd = yesterday();
          const nowD = getMSTNow();
          const wk7ts = now - 7 * 86400000;
          const firstOfMonth = mstFirstOfMonth(nowD);
          const firstOfLastMonth = mstFirstOfMonth(new Date(nowD.getFullYear(), nowD.getMonth() - 1, 1));
          const thisMonthLbl = nowD.toLocaleDateString('en-US', {month:'short',year:'numeric'}).toUpperCase();
          const lastMonthLbl = firstOfLastMonth.toLocaleDateString('en-US', {month:'short',year:'numeric'}).toUpperCase();

          const hCalc = bets => {
            const s = bets.filter(p => p.outcome !== 'pending');
            const w = s.filter(p => p.outcome === 'win').length;
            const l = s.filter(p => p.outcome === 'loss').length;
            const u = s.reduce((a, p) => {
              if (p.outcome === 'win') return a + (parseFloat(p.decOdds) || 2) - 1;
              if (p.outcome === 'loss') return a - 1;
              return a;
            }, 0);
            return { w, l, n: s.length, pct: s.length ? w / s.length : null, units: u };
          };

          const todayStr = today();
          const periods = [
            { key: 'TODAY', label: 'TODAY', sub: '',
              perf: hCalc(allBets.filter(p => p.date === todayStr)) },
            { key: 'YESTERDAY', label: 'YESTERDAY', sub: '',
              perf: hCalc(allBets.filter(p => p.date === yd)) },
            { key: 'ROLLING_7D', label: 'ROLLING 7D', sub: '',
              perf: hCalc(allBets.filter(p => p.lockedAt && p.lockedAt >= wk7ts)) },
            { key: 'THIS_MONTH', label: 'THIS MONTH', sub: thisMonthLbl,
              perf: hCalc(allBets.filter(p => p.lockedAt && p.lockedAt >= firstOfMonth.getTime())) },
            { key: 'LAST_MONTH', label: 'LAST MONTH', sub: lastMonthLbl,
              perf: hCalc(allBets.filter(p => p.lockedAt && p.lockedAt >= firstOfLastMonth.getTime() && p.lockedAt < firstOfMonth.getTime())) },
            { key: 'ALL_TIME', label: 'ALL TIME', sub: '',
              perf: hCalc(allBets) },
          ];
          return periods.map(p => ({ key: p.key, label: p.label, sub: p.sub, w: p.perf.w, l: p.perf.l, n: p.perf.n, pct: p.perf.pct, units: p.perf.units }));
        }
        """
    )


def get_sport_performance(page) -> dict | None:
    """Today / Last Month / This Month / All Time win-loss-units, broken
    down by league, computed with the exact same leagueMap categorization
    and calc logic as the home page's own "// PERIOD PERFORMANCE — LEAGUE"
    table (docs/app.html renderHomePage) -- THIS MONTH matches that table's
    own default toggle (window._homeLgPerfPeriod defaults to 'month', i.e.
    current month-to-date, not the prior completed month), TODAY matches
    that table's 'today' toggle (p.date === todayStr, the real-world game
    date, same as the home page's own TODAY summary card), and LAST MONTH
    matches the same firstOfLastMonth/firstOfMonth window
    get_engine_performance() above already uses for its own LAST_MONTH
    bucket. Written to docs/sport_performance.json for the same reason
    engine_performance.json exists -- so the landing page can mirror these
    numbers without its own Supabase access."""
    return page.evaluate(
        """
        async () => {
          const allBets = getP();
          const nowD = getMSTNow();
          const firstOfMonth = mstFirstOfMonth(nowD);
          const firstOfLastMonth = mstFirstOfMonth(new Date(nowD.getFullYear(), nowD.getMonth() - 1, 1));
          const thisMonthLbl = nowD.toLocaleDateString('en-US', {month:'short',year:'numeric'}).toUpperCase();
          const lastMonthLbl = firstOfLastMonth.toLocaleDateString('en-US', {month:'short',year:'numeric'}).toUpperCase();

          // Deliberate copy of renderHomePage's own leagueMap (docs/app.html)
          // -- no shared constant between the two, so if a league is ever
          // added/renamed there, update both or they'll silently drift on
          // which leagues this snapshot covers. World Cup deliberately
          // dropped from THIS copy only (unlike the home page's own table,
          // which keeps it) -- not a real recurring league on a Last
          // Month/All Time snapshot the way the other rows are.
          const leagueMap = [
            {lbl:'NFL',codes:['NFL','FB']},{lbl:'CFB',codes:['CFB']},{lbl:'MLB',codes:['MLB']},
            {lbl:'NHL',codes:['NHL']},{lbl:'College Hockey',codes:['NCAAH','COLLEGE HOCKEY']},
            {lbl:'SHL',codes:['SHL']},{lbl:'LIIGA',codes:['LIIGA']},
            {lbl:'NBA',codes:['NBA']},{lbl:'WNBA',codes:['WNBA']},{lbl:'CBB',codes:['CBB','NCAAB']},
            {lbl:'Champions League',codes:['CL','CH']},
            {lbl:'Premier League',codes:['PL']},{lbl:'La Liga',codes:['LIGA']},
            {lbl:'Bundesliga',codes:['BUND','BL']},{lbl:'MLS',codes:['MLS']},{lbl:'Serie A',codes:['SERIEA']},
          ];

          const byLeague = bets => leagueMap.map(lm => {
            const lb = bets.filter(p => lm.codes.some(c => p.sport && p.sport.toUpperCase() === c.toUpperCase()));
            if (!lb.length) return null;
            const w = lb.filter(p => p.outcome === 'win').length, l = lb.filter(p => p.outcome === 'loss').length;
            const u = lb.reduce((a, p) => {
              if (p.outcome === 'win') return a + (parseFloat(p.decOdds) || 2) - 1;
              if (p.outcome === 'loss') return a - 1;
              return a;
            }, 0);
            return { lbl: lm.lbl, w, l, n: lb.length, pct: lb.length ? w / lb.length : null, units: u };
          }).filter(Boolean).sort((a, b) => b.n - a.n);

          const settled = allBets.filter(p => p.outcome !== 'pending');
          const thisMonthBets = settled.filter(p => p.lockedAt && p.lockedAt >= firstOfMonth.getTime());
          const lastMonthBets = settled.filter(p => p.lockedAt && p.lockedAt >= firstOfLastMonth.getTime() && p.lockedAt < firstOfMonth.getTime());
          // Matches p.date, not lockedAt -- same real-world game date the
          // home page's own 'today' toggle and TODAY summary card use, not
          // a "locked in the last 24h" count.
          const todayBets = settled.filter(p => p.date === today());

          return {
            today: { label: 'TODAY', leagues: byLeague(todayBets) },
            lastMonth: { label: lastMonthLbl, leagues: byLeague(lastMonthBets) },
            thisMonth: { label: thisMonthLbl, leagues: byLeague(thisMonthBets) },
            allTime: { label: 'ALL TIME', leagues: byLeague(settled) },
          };
        }
        """
    )


def run(out_dir: Path, force: set[str] | None = None, json_only: bool = False) -> dict:
    """json_only=True stops right after writing engine_performance.json/
    sport_performance.json (skips card/video generation and email
    entirely) -- lets refresh_landing_performance.py reuse this same
    browser-launch + Supabase-load + snapshot logic for a lightweight,
    frequent-cadence refresh, without duplicating any of it or paying
    for card/video work nothing needs on that cadence."""
    from playwright.sync_api import sync_playwright

    force = force or set()
    out_dir.mkdir(parents=True, exist_ok=True)
    now_mt = _mt_now()
    yesterday_mt = now_mt - timedelta(days=1)
    # `force` (from --force, review-only) bypasses the date gate for a
    # given period without touching the real cadence for every other
    # day — e.g. forcing "weekly" for a one-off review doesn't also
    # force monthly/yearly, and doesn't change what fires on a normal
    # unforced run.
    is_sunday = now_mt.weekday() == 6 or "weekly" in force
    is_first_of_month = now_mt.day == 1 or "monthly" in force
    is_new_year = (now_mt.month == 1 and now_mt.day == 1) or "yearly" in force
    # Deterministic, days-since-epoch cadence (same pattern as
    # get_rotation_item) — no stored state, can't drift or double-fire.
    is_biweekly = (now_mt.date() - ROTATION_EPOCH.date()).days % 14 == 0 or "alltime" in force

    result = {"daily": None, "weekly": None, "monthly": None, "yearly": None, "events": [], "alltime": None}

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 1000}, accept_downloads=True)
        page = context.new_page()
        log(f"Loading {APP_URL} …")
        page.goto(APP_URL, wait_until="load", timeout=60000)
        page.wait_for_timeout(3000)

        bet_count = page.evaluate(
            """
            async () => {
              // PostgREST caps a single response at 1000 rows by default —
              // the real ledger is already past that, so an unpaginated
              // fetch here would silently undercount getP() (which feeds
              // both the cards and the milestone streak/count check).
              // Page through with the Range header until a batch comes
              // back short.
              const rows = [];
              let offset = 0;
              const page_size = 1000;
              while (true) {
                // outcome=neq._removed excludes soft-deleted rows (real
                // DELETEs are blocked by RLS, see _deleteBetsFromSupabase
                // in docs/app.html -- removal is a PATCH to outcome:
                // '_removed' on the row's own column, not inside raw,
                // which this headless pull never touched before, so a
                // "removed" test/bad bet kept showing up in every daily
                // video/breakdown indefinitely even after being wiped in
                // the live app.
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
        if bet_count is None or bet_count < 0:
            raise RuntimeError("Failed to load bet ledger from Supabase in-page — check SUPABASE_URL/KEY are still valid in app.html")
        log(f"Loaded {bet_count} real bets from Supabase into headless session")

        page.evaluate("() => { try { renderTrackRecord(); } catch(e) {} }")
        page.wait_for_timeout(400)
        page.evaluate("async () => { if (document.fonts && document.fonts.ready) await document.fonts.ready; }")

        # Engine Performance snapshot (Yesterday/Rolling 7D/This Month/Last
        # Month/All Time) — feeds the landing page's own Engine Performance
        # section via a plain fetch, so it stays in sync with the home
        # page's own numbers on every daily run without needing its own
        # Supabase access.
        try:
            engine_perf = get_engine_performance(page)
            if engine_perf:
                perf_path = ROOT / "docs" / "engine_performance.json"
                perf_path.write_text(json.dumps({
                    "generated_at": now_mt.strftime("%Y-%m-%d %H:%M MT"),
                    "periods": engine_perf,
                }, indent=2))
                log(f"Wrote {perf_path}")
        except Exception as e:
            log(f"WARNING: engine performance snapshot failed: {e}")

        # Subscriber-scoped Engine Performance snapshot -- same periods, but
        # filtered to only the 6 real paid-product sports. Explicit request,
        # 2026-09-03: the landing page's public Live Track Record should not
        # include personal-use-only WNBA/tennis/etc performance. Separate
        # file so engine_performance.json (also read by nothing else, but
        # kept as the "real, full" snapshot) stays the complete picture.
        try:
            engine_perf_sub = get_engine_performance_subscriber(page)
            if engine_perf_sub:
                perf_sub_path = ROOT / "docs" / "engine_performance_subscriber.json"
                perf_sub_path.write_text(json.dumps({
                    "generated_at": now_mt.strftime("%Y-%m-%d %H:%M MT"),
                    "periods": engine_perf_sub,
                }, indent=2))
                log(f"Wrote {perf_sub_path}")
        except Exception as e:
            log(f"WARNING: subscriber-scoped engine performance snapshot failed: {e}")

        # Sport performance snapshot (Today / This Month / All Time, by league) --
        # feeds the landing page's own Sport Performance section, same
        # rationale as engine_performance.json above.
        try:
            sport_perf = get_sport_performance(page)
            if sport_perf:
                sport_perf_path = ROOT / "docs" / "sport_performance.json"
                sport_perf_path.write_text(json.dumps({
                    "generated_at": now_mt.strftime("%Y-%m-%d %H:%M MT"),
                    **sport_perf,
                }, indent=2))
                log(f"Wrote {sport_perf_path}")
        except Exception as e:
            log(f"WARNING: sport performance snapshot failed: {e}")

        if json_only:
            browser.close()
            return result

        # Daily (always)
        cards, stats = generate_cards(page, out_dir, "YESTERDAY")
        result["daily"] = {"cards": cards, "stats": stats}

        # Weekly (Sundays)
        if is_sunday:
            cards, stats = generate_cards(page, out_dir, "ROLLING 7D", prefix_extra="weekly-")
            result["weekly"] = {"cards": cards, "stats": stats}

        # Monthly (1st of month — covers the month that just ended)
        if is_first_of_month:
            cards, stats = generate_cards(page, out_dir, "LAST MONTH", prefix_extra="monthly-")
            result["monthly"] = {"cards": cards, "stats": stats}

        # Year in review (Jan 1 — covers the year that just ended)
        if is_new_year:
            year_stats = get_year_stats(page, now_mt.year - 1)
            result["yearly"] = {"stats": year_stats}

        # All Time (every 14 days) — a real period the app itself already
        # supports (renderTrackRecord._sportPeriod), generated the exact
        # same way as weekly/monthly. Since Launch retired (redundant
        # with All Time for this app's purposes).
        if is_biweekly:
            cards, stats = generate_cards(page, out_dir, "ALL TIME", prefix_extra="alltime-")
            result["alltime"] = {"cards": cards, "stats": stats}

        # Events — the day after a configured window ends
        for event in EVENTS:
            end_date = datetime.strptime(event["end"], "%Y-%m-%d").date()
            if yesterday_mt.date() == end_date:
                path = generate_event_card(page, out_dir, event)
                if path:
                    event_stats = get_event_stats(page)
                    result["events"].append({"event": event, "card": path, "stats": event_stats})

        browser.close()

    return result


def build_daily_caption(stats: dict | None, date_ref: datetime) -> dict[str, str]:
    # date_ref is always yesterday relative to the run — callers must pass
    # the actual reporting date (not "today"), so this line stays accurate
    # regardless of when the workflow happens to fire.
    date_str = date_ref.strftime("%B %-d, %Y")
    tally_line = ""
    if stats and stats.get("w") is not None:
        tally_line = (
            f"Final tally: {stats['w']}W-{stats['l']}L · {_fmt_pct(stats.get('pct'))} win rate · "
            f"{_fmt_units(stats.get('units'))}\n\n"
        )
    ig = (
        f"Yesterdays Performance\n\n{date_str}\n\nThis is Clairvoyance.\n\n{tally_line}"
        f"Every pick graded. Every line evaluated for edge. No guesswork.\n\n"
        f"Follow for daily signals, subscribe for exclusive graded picks, and intelligence briefs.\n\n"
        f"clairvoyanceengine.info\nIG @clairvoyanceengine\nX @clairvoyanceeng\n\n"
        f"#foryou #sportsbetting #bettingtips #bettingpicks"
    )
    x = (
        f"Yesterdays Performance\n\nThis is Clairvoyance.\n\n{tally_line}"
        f"clairvoyanceengine.info\n\n#sportsbetting #bettingtips #bettingpicks"
    )
    return {"instagram": ig, "x": x}


def build_weekly_caption(stats: dict | None, week_end: datetime) -> dict[str, str]:
    week_start = week_end - timedelta(days=6)
    range_str = _fmt_date_range(week_start, week_end)
    tally_line = ""
    if stats and stats.get("w") is not None:
        tally_line = (
            f"Final tally: {stats['w']}W-{stats['l']}L · {_fmt_pct(stats.get('pct'))} win rate · "
            f"{_fmt_units(stats.get('units'))}\n\n"
        )
    ig = (
        f"This Week in Review — {range_str}\n\nThis is Clairvoyance.\n\n{tally_line}"
        f"Seven days. Every pick graded, every line evaluated for edge. No guesswork.\n\n"
        f"Follow for daily signals, subscribe for exclusive graded picks, and intelligence briefs.\n\n"
        f"clairvoyanceengine.info\nIG @clairvoyanceengine\nX @clairvoyanceeng\n\n"
        f"#foryou #sportsbetting #bettingtips #bettingpicks #weeklyrecap"
    )
    x = (
        f"This Week in Review — {range_str}\n\nThis is Clairvoyance.\n\n{tally_line}"
        f"Seven days. Every pick graded, every line evaluated for edge.\n\n"
        f"clairvoyanceengine.info\n\n#sportsbetting #bettingtips #weeklyrecap"
    )
    return {"instagram": ig, "x": x}


def build_alltime_caption(stats: dict | None) -> dict[str, str]:
    tally_line = ""
    if stats and stats.get("w") is not None:
        tally_line = (
            f"Final tally: {stats['w']}W-{stats['l']}L · {_fmt_pct(stats.get('pct'))} win rate · "
            f"{_fmt_units(stats.get('units'))}\n\n"
        )
    ig = (
        f"All Time — Every Pick, Every Result\n\nThis is Clairvoyance.\n\n{tally_line}"
        f"The full track record, public from day one. No cherry-picking, no deleted losses.\n\n"
        f"Follow for daily signals, subscribe for exclusive graded picks, and intelligence briefs.\n\n"
        f"clairvoyanceengine.info\nIG @clairvoyanceengine\nX @clairvoyanceeng\n\n"
        f"#foryou #sportsbetting #bettingtips #bettingpicks"
    )
    x = (
        f"All Time — Every Pick, Every Result\n\nThis is Clairvoyance.\n\n{tally_line}"
        f"The full track record, public from day one.\n\n"
        f"clairvoyanceengine.info\n\n#sportsbetting #bettingpicks"
    )
    return {"instagram": ig, "x": x}


def build_monthly_caption(stats: dict | None, month_ref: datetime) -> dict[str, str]:
    last_month = (month_ref.replace(day=1) - timedelta(days=1))
    month_str = last_month.strftime("%B %Y")
    tally_line = ""
    if stats and stats.get("w") is not None:
        tally_line = (
            f"Final tally: {stats['w']}W-{stats['l']}L · {_fmt_pct(stats.get('pct'))} win rate · "
            f"{_fmt_units(stats.get('units'))}\n\n"
        )
    ig = (
        f"{month_str} in the Books\n\nThis is Clairvoyance.\n\n{tally_line}"
        f"A full month tracked, graded, and public. No cherry-picking, no deleted losses.\n\n"
        f"Follow for daily signals, subscribe for exclusive graded picks, and intelligence briefs.\n\n"
        f"clairvoyanceengine.info\nIG @clairvoyanceengine\nX @clairvoyanceeng\n\n"
        f"#foryou #sportsbetting #bettingtips #bettingpicks #monthlyrecap"
    )
    x = (
        f"{month_str} in the Books\n\nThis is Clairvoyance.\n\n{tally_line}"
        f"A full month tracked, graded, and public. No cherry-picking.\n\n"
        f"clairvoyanceengine.info\n\n#sportsbetting #bettingpicks #monthlyrecap"
    )
    return {"instagram": ig, "x": x}


def build_yearly_caption(stats: dict | None, year: int) -> dict[str, str]:
    tally_line = ""
    if stats and stats.get("w") is not None:
        tally_line = (
            f"Final tally: {stats['w']}W-{stats['l']}L · {_fmt_pct(stats.get('pct'))} win rate · "
            f"{_fmt_units(stats.get('units'))}\n\n"
        )
    ig = (
        f"{year} in the Books\n\nThis is Clairvoyance.\n\n{tally_line}"
        f"A full year tracked, graded, and public. Every pick, every result — no cherry-picking, no deleted losses.\n\n"
        f"Follow for daily signals, subscribe for exclusive graded picks, and intelligence briefs.\n\n"
        f"clairvoyanceengine.info\nIG @clairvoyanceengine\nX @clairvoyanceeng\n\n"
        f"#foryou #sportsbetting #bettingtips #bettingpicks #yearinreview"
    )
    x = (
        f"{year} in the Books\n\nThis is Clairvoyance.\n\n{tally_line}"
        f"A full year tracked, graded, and public. No cherry-picking.\n\n"
        f"clairvoyanceengine.info\n\n#sportsbetting #bettingpicks #yearinreview"
    )
    return {"instagram": ig, "x": x}


def build_event_caption(event: dict, stats: dict | None = None) -> dict[str, str]:
    event_hashtag = "#" + "".join(w.capitalize() for w in event["name"].split())
    tally_line = ""
    if stats and stats.get("w") is not None:
        tally_line = (
            f"Final tally: {stats['w']}W-{stats['l']}L · {_fmt_pct(stats.get('pct'))} win rate · "
            f"{_fmt_units(stats.get('units'))}\n\n"
        )
    ig = (
        f"{event['name']} is in the books.\n\nThis is Clairvoyance.\n\n{tally_line}"
        f"Every pick tracked. Every result public. No guesswork.\n\n"
        f"Follow for daily signals, subscribe for exclusive graded picks, and intelligence briefs.\n\n"
        f"clairvoyanceengine.info\nIG @clairvoyanceengine\nX @clairvoyanceeng\n\n"
        f"#foryou #sportsbetting #bettingtips #bettingpicks {event_hashtag}"
    )
    x = (
        f"{event['name']} is in the books.\n\nThis is Clairvoyance.\n\n{tally_line}"
        f"Clairvoyance Engine doesn't miss. 🎯\n\n"
        f"#SportsBetting {event_hashtag}"
    )
    return {"instagram": ig, "x": x}


def build_grading_caption() -> dict[str, str]:
    ig = (
        "Not all picks are created equal.\n\nThis is Clairvoyance.\n\n"
        "Every signal gets graded before it goes public — some clear the bar, some don't make the cut at all. "
        "That's the point. We're not chasing volume, we're chasing quality.\n\n"
        "No hype. No \"lock of the century.\" Just picks that clear the bar, and ones that don't get thrown out.\n\n"
        "Follow for daily signals, subscribe for exclusive graded picks, and intelligence briefs.\n\n"
        "clairvoyanceengine.info\nIG @clairvoyanceengine\nX @clairvoyanceeng\n\n"
        "#foryou #sportsbetting #bettingtips #bettingpicks"
    )
    x = (
        "Not all picks are created equal.\n\nEvery signal gets graded before it goes public. "
        "If the numbers don't clear our bar, it doesn't get posted.\n\n"
        "clairvoyanceengine.info\n\n#sportsbetting #bettingpicks #handicapping"
    )
    return {"instagram": ig, "x": x}


def build_subscription_caption() -> dict[str, str]:
    ig = (
        "Full access. No guesswork.\n\nThis is Clairvoyance.\n\n"
        "Every tier gets real graded picks, real game lines, and Discord access — the only difference is how much of the model you want.\n\n"
        "clairvoyanceengine.info\nIG @clairvoyanceengine\nX @clairvoyanceeng\n\n"
        "#foryou #sportsbetting #bettingtips #bettingpicks"
    )
    x = (
        "Full access. No guesswork.\n\nBase, Plus, Insider — or a Day/Weekend Pass if you just want in for a slate.\n\n"
        "clairvoyanceengine.info\n\n#sportsbetting #bettingpicks"
    )
    return {"instagram": ig, "x": x}


def build_educational_caption(topic: dict) -> dict[str, str]:
    body = " ".join(topic["lines"])
    ig = (
        f"{topic['title']}\n\nThis is Clairvoyance.\n\n{body}\n\n"
        f"Follow for daily signals, subscribe for exclusive graded picks, and intelligence briefs.\n\n"
        f"clairvoyanceengine.info\nIG @clairvoyanceengine\nX @clairvoyanceeng\n\n"
        f"#foryou #sportsbetting #bettingtips #bettingpicks"
    )
    x = f"{topic['title']}\n\n{body}\n\nclairvoyanceengine.info\n\n#sportsbetting #bettingpicks"
    return {"instagram": ig, "x": x}


def build_covers_caption() -> dict[str, str]:
    # League/sport count updated 2026-09-03 to match the current pinned
    # card + landing page Coverage section exactly (11 subscriber-facing
    # leagues across 5 sports: NFL/CFB/NBA/NHL/MLB + 6 soccer leagues) --
    # was stale at "20 leagues across 6 sports" from before WNBA/tennis/
    # CBB/College Hockey/SHL/LIIGA/KHL were split out as personal-use-only
    # (see project_subscriber_vs_personal_leagues memory).
    ig = (
        "One engine. Every sport that matters.\n\nThis is Clairvoyance.\n\n"
        "11 leagues across 5 sports, every pick graded, every result tracked publicly — model outputs, not gut feelings.\n\n"
        "Follow for daily signals, subscribe for exclusive graded picks, and intelligence briefs.\n\n"
        "clairvoyanceengine.info\nIG @clairvoyanceengine\nX @clairvoyanceeng\n\n"
        "#foryou #sportsbetting #bettingtips #bettingpicks"
    )
    x = (
        "One engine. Every sport that matters.\n\n11 leagues, 5 sports, every pick graded.\n\n"
        "clairvoyanceengine.info\n\n#sportsbetting #bettingpicks"
    )
    return {"instagram": ig, "x": x}


def _caption_block(title: str, text: str) -> str:
    html_text = text.replace("\n", "<br>")
    return (
        f'<h3 style="margin-bottom:4px;color:#1a1a2e">{title}</h3>'
        f'<div style="background:#14001f;border-radius:6px;padding:12px 16px;color:#eee;'
        f'font-family:monospace;font-size:13px;white-space:pre-wrap;margin-bottom:20px">{html_text}</div>'
    )


def send_email(subject: str, cards: list[Path], captions: dict[str, str], intro: str = "") -> None:
    if not SOCIAL_CARD_EMAIL_TO:
        log(f"No recipient set (SOCIAL_CARD_EMAIL_TO) — skipping send for: {subject}")
        return
    filename_list_html = "".join(f"<li>{p.name}</li>" for p in cards)
    body_html = (
        _EMAIL_WRAP_OPEN +
        f"<p>{intro}</p>"
        f"<p>Cards attached:</p><ul>{filename_list_html}</ul>"
        f"<p>Captions below, ready to copy-paste:</p>"
        f"{_caption_block('Instagram caption', captions['instagram'])}"
        f"{_caption_block('X caption', captions['x'])}"
        + _EMAIL_WRAP_CLOSE
    )
    ok, msg = _send_gmail(subject, SOCIAL_CARD_EMAIL_TO, body_html, attachments=cards)
    if not ok:
        raise RuntimeError(f"Gmail send failed for '{subject}': {msg}")
    log(f"Email sent: {subject} ({len(cards)} attachment(s))")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-email", action="store_true", help="generate cards only, skip sending")
    parser.add_argument("--out-dir", default="/tmp/cv_social_cards")
    parser.add_argument("--force", default="", help="comma-separated periods to force past today's date "
                         "gate for review (weekly,monthly,yearly,alltime) — does not affect the real "
                         "cadence for anything not explicitly listed")
    parser.add_argument("--json-only", action="store_true",
                         help="refresh docs/engine_performance.json + docs/sport_performance.json only -- "
                              "skips card/video generation and email entirely. For a lightweight, frequent "
                              "intraday refresh separate from the once-daily full run (see "
                              "landing-performance-refresh.yml).")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    now_mt = _mt_now()
    yesterday_mt = now_mt - timedelta(days=1)
    force = {p.strip() for p in args.force.split(",") if p.strip()}

    if args.json_only:
        run(out_dir, force=force, json_only=True)
        return

    result = run(out_dir, force=force)

    # Daily
    daily = result["daily"]
    captions = build_daily_caption(daily["stats"], yesterday_mt)
    log("Daily captions:\n--- IG ---\n" + captions["instagram"] + "\n--- X ---\n" + captions["x"])

    # IG gets the video, X gets a still of the same fully-settled content —
    # Track Record/Sport Perf/League Perf PNG cards are no longer attached
    # at all (superseded by the video+still pair).
    daily_attachments = []
    stats = daily["stats"] or {}
    if stats.get("w") is not None:
        try:
            from generate_video_reveal import record_stats_reveal, record_stats_still, record_breakdown_reveal, record_breakdown_still, STATS_VARIANT_NAMES
            # Rotate the visual style by weekday (same index basis as the
            # caption hook rotation) so consecutive daily videos don't all
            # look identical.
            variant = STATS_VARIANT_NAMES[yesterday_mt.weekday() % len(STATS_VARIANT_NAMES)]
            video_path = out_dir / f"cv-reveal-{yesterday_mt.strftime('%Y%m%d')}.mp4"
            record_stats_reveal(
                headline="YESTERDAY'S PERFORMANCE",
                record=f"{stats['w']}W-{stats['l']}L",
                pct=_fmt_pct(stats.get("pct")),
                units=_fmt_units(stats.get("units")),
                locked=str(stats.get("lockedCount", "—")),
                out_path=video_path,
                variant=variant,
                date_str=yesterday_mt.strftime("%B %-d, %Y"),
            )
            log(f"Daily video variant: {variant}")
            daily_attachments.append(video_path)
            log(f"Video reveal generated: {video_path}")

            still_path = out_dir / f"cv-reveal-still-{yesterday_mt.strftime('%Y%m%d')}.png"
            record_stats_still(
                headline="YESTERDAY'S PERFORMANCE",
                record=f"{stats['w']}W-{stats['l']}L",
                pct=_fmt_pct(stats.get("pct")),
                units=_fmt_units(stats.get("units")),
                locked=str(stats.get("lockedCount", "—")),
                out_path=still_path,
                variant=variant,
                date_str=yesterday_mt.strftime("%B %-d, %Y"),
            )
            daily_attachments.append(still_path)
            log(f"Stats still generated: {still_path}")

            if stats.get("bySport"):
                rows = _breakdown_rows(stats)
                breakdown_path = out_dir / f"cv-breakdown-{yesterday_mt.strftime('%Y%m%d')}.mp4"
                record_breakdown_reveal("SPORT PERFORMANCE", rows, breakdown_path)
                daily_attachments.append(breakdown_path)
                log(f"Breakdown video generated: {breakdown_path}")

                breakdown_still_path = out_dir / f"cv-breakdown-still-{yesterday_mt.strftime('%Y%m%d')}.png"
                record_breakdown_still("SPORT PERFORMANCE", rows, breakdown_still_path)
                daily_attachments.append(breakdown_still_path)
                log(f"Breakdown still generated: {breakdown_still_path}")
        except Exception as exc:
            # Video is a bonus on top of the cards, not a hard requirement
            # for the daily post — don't let a video-pipeline hiccup (e.g.
            # ffmpeg conversion issue) block the cards/captions email that
            # actually matters every day.
            log(f"Video reveal generation failed (non-fatal, skipping): {exc}")

    if not args.no_email:
        send_email(
            f"Clairvoyance — Daily Social Cards ({yesterday_mt.strftime('%B %d, %Y')})",
            daily_attachments, captions,
            intro="Today's social cards are attached, ready to post:",
        )

    # Weekly
    if result["weekly"]:
        captions = build_weekly_caption(result["weekly"]["stats"], yesterday_mt)
        log("Weekly captions:\n--- IG ---\n" + captions["instagram"])
        weekly_attachments = []
        w_stats = result["weekly"]["stats"] or {}
        if w_stats.get("w") is not None:
            try:
                from generate_video_reveal import record_big_recap_reveal, record_big_recap_still
                week_start = yesterday_mt - timedelta(days=6)
                range_str = _fmt_date_range(week_start, yesterday_mt)
                recap_path = out_dir / f"cv-weekly-recap-{yesterday_mt.strftime('%Y%m%d')}.mp4"
                record_big_recap_reveal(
                    tag="WEEKLY RECAP", date_range=range_str,
                    record=f"{w_stats['w']}W-{w_stats['l']}L", pct=_fmt_pct(w_stats.get("pct")),
                    units=_fmt_units(w_stats.get("units")), extra_val=str(w_stats.get("lockedCount", "—")), extra_lbl="PICKS LOCKED",
                    out_path=recap_path,
                )
                weekly_attachments.append(recap_path)
                log(f"Weekly recap video generated: {recap_path}")

                w_still_path = out_dir / f"cv-weekly-recap-still-{yesterday_mt.strftime('%Y%m%d')}.png"
                record_big_recap_still(
                    tag="WEEKLY RECAP", date_range=range_str,
                    record=f"{w_stats['w']}W-{w_stats['l']}L", pct=_fmt_pct(w_stats.get("pct")),
                    units=_fmt_units(w_stats.get("units")), extra_val=str(w_stats.get("lockedCount", "—")), extra_lbl="PICKS LOCKED",
                    out_path=w_still_path,
                )
                weekly_attachments.append(w_still_path)
                log(f"Weekly recap still generated: {w_still_path}")
            except Exception as exc:
                log(f"Weekly recap video generation failed (non-fatal, skipping): {exc}")
            if w_stats.get("bySport"):
                try:
                    from generate_video_reveal import record_breakdown_reveal, record_breakdown_still
                    rows = _breakdown_rows(w_stats)
                    w_breakdown_path = out_dir / f"cv-breakdown-weekly-{yesterday_mt.strftime('%Y%m%d')}.mp4"
                    w_range_str = _fmt_date_range(yesterday_mt - timedelta(days=6), yesterday_mt).upper()
                    record_breakdown_reveal("SPORT PERFORMANCE — ROLLING 7D", rows, w_breakdown_path, date_range=w_range_str)
                    weekly_attachments.append(w_breakdown_path)
                    log(f"Weekly breakdown video generated: {w_breakdown_path}")

                    w_bd_still_path = out_dir / f"cv-breakdown-weekly-still-{yesterday_mt.strftime('%Y%m%d')}.png"
                    record_breakdown_still("SPORT PERFORMANCE — ROLLING 7D", rows, w_bd_still_path, date_range=w_range_str)
                    weekly_attachments.append(w_bd_still_path)
                    log(f"Weekly breakdown still generated: {w_bd_still_path}")
                except Exception as exc:
                    log(f"Weekly breakdown video generation failed (non-fatal, skipping): {exc}")
        if not args.no_email:
            send_email(
                f"Clairvoyance — Weekly Recap ({yesterday_mt.strftime('%B %d, %Y')})",
                weekly_attachments, captions,
                intro="It's Sunday — here's the 7-day roundup, good pinned-post material:",
            )

    # Monthly
    if result["monthly"]:
        m_stats = result["monthly"]["stats"] or {}
        _strip_stale_hockey(m_stats)
        captions = build_monthly_caption(m_stats, now_mt)
        log("Monthly captions:\n--- IG ---\n" + captions["instagram"])
        monthly_attachments = []
        last_month_end = now_mt.replace(day=1) - timedelta(days=1)
        if m_stats.get("w") is not None:
            try:
                from generate_video_reveal import record_big_recap_reveal, record_big_recap_still
                recap_path = out_dir / f"cv-monthly-recap-{last_month_end.strftime('%Y%m')}.mp4"
                record_big_recap_reveal(
                    tag="MONTHLY RECAP", date_range=last_month_end.strftime("%B %Y").upper(),
                    record=f"{m_stats['w']}W-{m_stats['l']}L", pct=_fmt_pct(m_stats.get("pct")),
                    units=_fmt_units(m_stats.get("units")), extra_val=str(m_stats.get("lockedCount", "—")), extra_lbl="PICKS LOCKED",
                    out_path=recap_path,
                )
                monthly_attachments.append(recap_path)
                log(f"Monthly recap video generated: {recap_path}")

                m_still_path = out_dir / f"cv-monthly-recap-still-{last_month_end.strftime('%Y%m')}.png"
                record_big_recap_still(
                    tag="MONTHLY RECAP", date_range=last_month_end.strftime("%B %Y").upper(),
                    record=f"{m_stats['w']}W-{m_stats['l']}L", pct=_fmt_pct(m_stats.get("pct")),
                    units=_fmt_units(m_stats.get("units")), extra_val=str(m_stats.get("lockedCount", "—")), extra_lbl="PICKS LOCKED",
                    out_path=m_still_path,
                )
                monthly_attachments.append(m_still_path)
                log(f"Monthly recap still generated: {m_still_path}")
            except Exception as exc:
                log(f"Monthly recap video generation failed (non-fatal, skipping): {exc}")
            if m_stats.get("bySport"):
                try:
                    from generate_video_reveal import record_breakdown_reveal, record_breakdown_still
                    rows = _breakdown_rows(m_stats)
                    m_breakdown_path = out_dir / f"cv-breakdown-monthly-{last_month_end.strftime('%Y%m')}.mp4"
                    record_breakdown_reveal("SPORT PERFORMANCE — LAST MONTH", rows, m_breakdown_path,
                                             date_range=last_month_end.strftime("%B %Y").upper())
                    monthly_attachments.append(m_breakdown_path)
                    log(f"Monthly breakdown video generated: {m_breakdown_path}")

                    m_bd_still_path = out_dir / f"cv-breakdown-monthly-still-{last_month_end.strftime('%Y%m')}.png"
                    record_breakdown_still("SPORT PERFORMANCE — LAST MONTH", rows, m_bd_still_path,
                                            date_range=last_month_end.strftime("%B %Y").upper())
                    monthly_attachments.append(m_bd_still_path)
                    log(f"Monthly breakdown still generated: {m_bd_still_path}")
                except Exception as exc:
                    log(f"Monthly breakdown video generation failed (non-fatal, skipping): {exc}")
        if not args.no_email:
            send_email(
                f"Clairvoyance — Monthly Recap ({last_month_end.strftime('%B %Y')})",
                monthly_attachments, captions,
                intro="First of the month — last month's recap is ready:",
            )

    # Year in review (Jan 1 — video only, no static cards, since the
    # underlying app has no calendar-year period to render them against)
    if result["yearly"]:
        prior_year = now_mt.year - 1
        y_stats = result["yearly"]["stats"] or {}
        captions = build_yearly_caption(y_stats, prior_year)
        log(f"Yearly captions ({prior_year}):\n--- IG ---\n" + captions["instagram"])
        yearly_attachments = []
        if y_stats.get("w") is not None:
            try:
                from generate_video_reveal import record_big_recap_reveal, record_big_recap_still
                recap_path = out_dir / f"cv-yearly-recap-{prior_year}.mp4"
                record_big_recap_reveal(
                    tag="YEAR IN REVIEW", date_range=f"JANUARY 1 – DECEMBER 31, {prior_year}",
                    record=f"{y_stats['w']}W-{y_stats['l']}L", pct=_fmt_pct(y_stats.get("pct")),
                    units=_fmt_units(y_stats.get("units")), extra_val=str(y_stats.get("lockedCount", "—")), extra_lbl="PICKS LOCKED",
                    out_path=recap_path, duration_s=8.0,
                )
                yearly_attachments.append(recap_path)
                log(f"Yearly recap video generated: {recap_path}")

                y_still_path = out_dir / f"cv-yearly-recap-still-{prior_year}.png"
                record_big_recap_still(
                    tag="YEAR IN REVIEW", date_range=f"JANUARY 1 – DECEMBER 31, {prior_year}",
                    record=f"{y_stats['w']}W-{y_stats['l']}L", pct=_fmt_pct(y_stats.get("pct")),
                    units=_fmt_units(y_stats.get("units")), extra_val=str(y_stats.get("lockedCount", "—")), extra_lbl="PICKS LOCKED",
                    out_path=y_still_path, duration_s=8.0,
                )
                yearly_attachments.append(y_still_path)
                log(f"Yearly recap still generated: {y_still_path}")
            except Exception as exc:
                log(f"Yearly recap video generation failed (non-fatal, skipping): {exc}")
            if y_stats.get("bySport"):
                try:
                    from generate_video_reveal import record_breakdown_reveal, record_breakdown_still
                    rows = _breakdown_rows(y_stats)
                    y_breakdown_path = out_dir / f"cv-breakdown-yearly-{prior_year}.mp4"
                    record_breakdown_reveal(f"SPORT PERFORMANCE — {prior_year}", rows, y_breakdown_path,
                                             date_range=f"JANUARY 1 – DECEMBER 31, {prior_year}")
                    yearly_attachments.append(y_breakdown_path)
                    log(f"Yearly breakdown video generated: {y_breakdown_path}")

                    y_bd_still_path = out_dir / f"cv-breakdown-yearly-still-{prior_year}.png"
                    record_breakdown_still(f"SPORT PERFORMANCE — {prior_year}", rows, y_bd_still_path,
                                            date_range=f"JANUARY 1 – DECEMBER 31, {prior_year}")
                    yearly_attachments.append(y_bd_still_path)
                    log(f"Yearly breakdown still generated: {y_bd_still_path}")
                except Exception as exc:
                    log(f"Yearly breakdown video generation failed (non-fatal, skipping): {exc}")
        if yearly_attachments and not args.no_email:
            send_email(
                f"Clairvoyance — {prior_year} Year In Review",
                yearly_attachments, captions,
                intro=f"Happy New Year — here's the full {prior_year} recap:",
            )
        elif not yearly_attachments:
            log(f"Year-end recap skipped: no settled bets found for {prior_year}")

    # All Time (every 14 days)
    if result["alltime"]:
        at_stats = result["alltime"]["stats"] or {}
        captions = build_alltime_caption(at_stats)
        log("All Time captions:\n--- IG ---\n" + captions["instagram"])
        at_attachments = []
        if at_stats.get("bySport"):
            try:
                from generate_video_reveal import record_breakdown_reveal, record_breakdown_still
                rows = _breakdown_rows(at_stats)
                at_breakdown_path = out_dir / f"cv-breakdown-alltime-{now_mt.strftime('%Y%m%d')}.mp4"
                record_breakdown_reveal("SPORT PERFORMANCE — ALL TIME", rows, at_breakdown_path)
                at_attachments.append(at_breakdown_path)

                at_still_path = out_dir / f"cv-breakdown-alltime-still-{now_mt.strftime('%Y%m%d')}.png"
                record_breakdown_still("SPORT PERFORMANCE — ALL TIME", rows, at_still_path)
                at_attachments.append(at_still_path)
            except Exception as exc:
                log(f"All Time breakdown video generation failed (non-fatal, skipping): {exc}")
        if not args.no_email:
            send_email(
                "Clairvoyance — All Time Track Record",
                at_attachments, captions,
                intro="Full all-time track record, good pinned-post material:",
            )

    # Events
    for ev in result["events"]:
        captions = build_event_caption(ev["event"], ev.get("stats"))
        log(f"Event captions ({ev['event']['name']}):\n--- IG ---\n" + captions["instagram"])
        event_attachments = [ev["card"]]
        ev_stats = ev.get("stats") or {}
        if ev_stats.get("w") is not None:
            try:
                from generate_video_reveal import record_stats_reveal, record_stats_still
                slug = re.sub(r"[^a-z0-9]+", "-", ev["event"]["name"].lower()).strip("-")
                event_video_path = out_dir / f"cv-event-{slug}-{yesterday_mt.strftime('%Y%m%d')}.mp4"
                record_stats_reveal(
                    headline=ev["event"]["name"].title(),
                    record=f"{ev_stats['w']}W-{ev_stats['l']}L",
                    pct=_fmt_pct(ev_stats.get("pct")),
                    units=_fmt_units(ev_stats.get("units")),
                    locked=str(ev_stats.get("n", "—")),
                    out_path=event_video_path,
                    variant="glitch",
                )
                event_attachments.append(event_video_path)
                log(f"Event glitch video generated: {event_video_path}")

                event_still_path = out_dir / f"cv-event-{slug}-still-{yesterday_mt.strftime('%Y%m%d')}.png"
                record_stats_still(
                    headline=ev["event"]["name"].title(),
                    record=f"{ev_stats['w']}W-{ev_stats['l']}L",
                    pct=_fmt_pct(ev_stats.get("pct")),
                    units=_fmt_units(ev_stats.get("units")),
                    locked=str(ev_stats.get("n", "—")),
                    out_path=event_still_path,
                    variant="glitch",
                )
                event_attachments.append(event_still_path)
                log(f"Event still generated: {event_still_path}")
            except Exception as exc:
                log(f"Event video generation failed (non-fatal, skipping): {exc}")
        if not args.no_email:
            send_email(
                f"Clairvoyance — {ev['event']['name']} Wrap-Up",
                event_attachments, captions,
                intro=f"{ev['event']['name']} just wrapped — final performance card is ready:",
            )

    # Rotation content (grading system, educational series) — every 5th day
    # since launch, deterministic from the date. "subscription" (Choose
    # Your Tier) cut from this rotation 2026-09-03 -- see ROTATION_CONTENT.
    rotation_item = get_rotation_item(now_mt)
    if rotation_item:
        log(f"Rotation content today: {rotation_item}")
        try:
            from generate_video_reveal import (
                record_grading_tiers_reveal,
                record_educational_reveal, EDUCATIONAL_TOPICS,
            )
            if rotation_item == "covers":
                # Static pre-made asset, not a generated video — attach as-is.
                subject = "Clairvoyance — What We Cover"
                intro = "Rotation content — What Clairvoyance covers, ready to post:"
                captions = build_covers_caption()
                log(f"Rotation asset (static image): {COVERS_CARD_PATH}")
                if not args.no_email:
                    send_email(subject, [COVERS_CARD_PATH], captions, intro=intro)
            else:
                rotation_path = out_dir / f"cv-rotation-{rotation_item}-{now_mt.strftime('%Y%m%d')}.mp4"
                if rotation_item == "grading":
                    record_grading_tiers_reveal(rotation_path)
                    subject = "Clairvoyance — Pick Grading System"
                    intro = "Rotation content — Pick Grading System, ready to post:"
                    captions = build_grading_caption()
                else:
                    topic = EDUCATIONAL_TOPICS[rotation_item]
                    record_educational_reveal(topic["tag"], topic["title"], topic["lines"], rotation_path)
                    subject = f"Clairvoyance — Educational: {topic['title']}"
                    intro = "Rotation content — educational post, ready to post:"
                    captions = build_educational_caption(topic)
                log(f"Rotation video generated: {rotation_path}")
                if not args.no_email:
                    send_email(subject, [rotation_path], captions, intro=intro)
        except Exception as exc:
            log(f"Rotation content generation failed (non-fatal, skipping): {exc}")

    log("Done.")


if __name__ == "__main__":
    main()
