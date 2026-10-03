#!/usr/bin/env python3
"""
daily_health_check.py — durable replacement for what used to be a
session-only "check the daily jobs actually ran" reminder. Runs once a
day via .github/workflows/daily-health-check.yml, checks every other
scheduled daily workflow in this repo for two failure modes a human
watching casually would otherwise have to notice themselves:

  1. MISSING -- no run at all in the last ~26 hours (the real 2026-08-06
     incident this project already has on record: two Playwright-heavy
     jobs landing ~22min apart queue-starved each other so badly one of
     them never got a runner at all -- GitHub never "fails" that, it just
     silently never runs).
  2. FAILED -- the most recent run happened, but its conclusion wasn't
     "success".

Added 2026-10-03 (data-freshness audit): the DATA REFRESH workflows used to be unmonitored -- a refresh could fail or land 17h late
with every dashboard still green.  Two new checks (both fail-open, unit-tested offline in test_health_freshness.py):

  3. REFRESH WORKFLOWS (REFRESH_MONITORED): last-SUCCESS age + consecutive failures per refresh workflow (scheduled-refresh,
     daily-schedules-refresh, the four hockey refreshes, opta, cfb/nfl stats, live-tracker, daily-player-stats-refresh...).
  4. DATA FILE STAMPS (FRESHNESS_FILES): reads the generated-at stamp inside the key docs/*.json files and compares it with
     warn/stale thresholds = refresh cadence + the 3-6h GitHub cron delay (same numbers as docs/app.html's _FRESH_SRC header line).
     STALE emails; AGING is logged (and shown in the email when one is sent anyway) but a warn-only day sends nothing.

Deliberately silent on a clean day (matches "hands-off" -- nobody wants a
daily "all good" email); only sends anything when there's a real finding.
Uses the repo's own default GITHUB_TOKEN (Actions API read access), not a
personal one -- nothing extra to configure.
"""
from __future__ import annotations
import os
import re
import sys
import urllib.request
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _gmail_email import send_email  # noqa: E402

REPO = "Purple-Wraith/clairvoyance-backend"
ROOT = Path(__file__).resolve().parent.parent
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ALERT_TO = os.environ.get("SOCIAL_CARD_EMAIL_TO", "") or os.environ.get("LOCKS_EMAIL_TO", "")

# (workflow filename, human label, how many hours old a run can be before
# it's considered "missing" -- a bit more than 24h to absorb normal
# schedule jitter, per this repo's own documented pattern of GH delaying
# scheduled runs 30-90+ minutes past nominal cron time). Real gap, found
# and fixed: this 26h window is fine for once-a-day, not-time-critical
# jobs, but for CFB/soccer's early locks it meant a genuinely late run
# (or a fully-dropped schedule, confirmed to happen live 2026-08-29)
# wouldn't get flagged until up to 26h after the fact -- long past
# useful for a same-day catch. Those two are checked by
# check_lock_markers() below instead (the real ground-truth marker
# files cfb-lock-early.yml/soccer-lock-early.yml's own catch-up logic
# uses, not a guess from run recency), on a tighter, earlier schedule.
# Kept in MONITORED (at 26h) too as a backup signal for genuinely
# missing runs, since the marker check alone can't tell "workflow never
# ran" apart from "ran, found 0 qualifying legs, correctly wrote today's
# date anyway" -- both look the same to the marker file.
MONITORED = [
    # Label corrected 2026-10-02 (was "all 8 products", stale from before
    # MLB/WNBA/CBB/tennis/World Cup were retired) -- there are only 5 real
    # paid products now (nfl/cfb/nba/hockey/soccer, see PRODUCT_SPORTS in
    # auto_lock_settle.py). Cosmetic only -- this is a human-readable
    # label, not a check condition.
    ("auto-lock-settle.yml", "Main Auto-Lock (5 products)", 26),
    ("european-lock-early.yml", "European Early Lock (Soccer + SHL/Liiga)", 26),
    ("cfb-lock-early.yml", "CFB Early Lock", 26),
    ("send-expiry-reminders.yml", "Expiry Reminders", 26),
    ("social-cards-daily.yml", "Social Cards Daily", 26),
    ("pick-of-day-social-daily.yml", "Pick-of-Day Social", 26),
    # Real gap found in a 2026-09-09 audit: the evening ("tomorrow's
    # slate") locks are the same subscriber-facing/paid-product locking
    # logic as their early-lock counterparts above, writing their own
    # last_cfb_evening_lock_date.txt/last_soccer_evening_lock_date.txt
    # markers -- but neither workflow was in this list or in
    # LOCK_MARKERS below, so a silent failure or dropped schedule on
    # either would have gone completely undetected. Added at the same
    # 26h backup-signal age as the early locks; not yet given a tighter
    # LOCK_MARKERS entry like the early locks have, since this script's
    # own two daily runs (see daily-health-check.yml) both fire before
    # these evening locks' own ~10:30-11:30pm MT catch-up window closes
    # -- would need a third, later scheduled run to check that marker
    # meaningfully same-night, which this pass didn't add.
    ("cfb-lock-evening.yml", "CFB Evening Lock (Tomorrow's Slate)", 26),
    ("soccer-lock-evening.yml", "Soccer Evening Lock (Tomorrow's Slate)", 26),
    # Added 2026-09-18 alongside the new SHL/Liiga evening-prior lock --
    # same backup-signal treatment as its CFB/soccer siblings above.
    # Label updated 2026-09-23: same workflow/file, now also covers
    # NLA/Extraliga's personal-use safety-net lock (see
    # EARLY_HOCKEY_SPORTS_PERSONAL in auto_lock_settle.py) alongside the
    # original paid SHL/Liiga product.
    ("hockey-lock-evening.yml", "Hockey Evening Lock — SHL/Liiga/NLA/Extraliga (Tomorrow's Slate)", 26),
]

# (marker file, human label, cutoff hour in MT past which today's date
# should already be recorded). Matches each workflow's own catch-up
# window with headroom: soccer's last catch-up is 6:45am MT (earliest
# kickoff 7:00am), CFB's is 10:00am MT (earliest kickoff 10:00am) -- both
# cutoffs here sit right after those, so a real miss is caught the same
# morning, not up to a day later.
#
# last_pick_of_day_{am,pm}_date.txt added after a real rigorous audit
# (2026-09-03) found this exact gap: MONITORED's check_workflow() below
# only checks "did a run happen recently and succeed" -- a run that hits
# the workflow's own "already sent" skip gate ALSO reports success, so a
# day where the real primary trigger silently never fired (confirmed via
# real run history: happened on 4 of the last 4 real days) still looked
# "healthy" as long as some later fallback or a manual dispatch
# eventually caught it. This marker check is the same date-specific
# ground truth the soccer/CFB checks already use -- it catches "still
# missing" AND "took until a very late fallback," not just "never ran at
# all."
#
# Split into two markers, 2026-09-17, when pick-of-day-social-daily.yml
# itself split into an AM slate and a PM slate (a single daily run
# structurally favored whichever sports lock earliest and disregarded
# NFL/NBA/NHL -- see that workflow's own schedule comment) -- each slate
# writes its own marker file now, so a dropped AM run can't be masked by
# a healthy PM run or vice versa.
#
# AM cutoff moved 13 -> 10 (1pm -> 10am MT), 2026-09-18, when the AM
# slate's own schedule moved from 10:40/11:10/11:40am MT to
# 6:50/7:07/7:50am MT (SHL's real ~7:15am MT kickoffs meant the old
# timing was already too late to catch it before the kickoff-time
# filter excludes it as started -- see pick-of-day-social-daily.yml's
# own schedule comment). Cutoff 10 (10am MT) sits comfortably after the
# AM slate's own new last fallback (7:50am MT nominal) with headroom
# for GitHub's own documented scheduling delay.
#
# PM cutoff moved 18 -> 16 (6pm -> 4pm MT), same day, when the PM
# slate's own schedule shifted 2 hours earlier to 2:00/2:30/3:00pm MT --
# sits after that slate's new last fallback (3:00pm MT nominal) with the
# same headroom. NOTE, not yet fixed: this script's own two scheduled
# runs (daily-health-check.yml, ~11:15am and ~2:33pm MT) both fire
# BEFORE 4pm MT, so this specific marker can't actually be evaluated by
# either of them same-day -- only a later manual/triggered run of this
# script would see it. A third daily-health-check.yml pass after 4pm MT
# would close that gap; not added here since it wasn't asked for.
LOCK_MARKERS = [
    (ROOT / "data" / "last_soccer_lock_date.txt", "European Early Lock (Soccer + SHL/Liiga)", 8),
    (ROOT / "data" / "last_cfb_lock_date.txt", "CFB Early Lock", 11),
    (ROOT / "data" / "last_pick_of_day_am_date.txt", "Pick-of-Day Social Email (AM slate)", 10),
    (ROOT / "data" / "last_pick_of_day_pm_date.txt", "Pick-of-Day Social Email (PM slate)", 16),
]


def check_lock_markers() -> list[str]:
    now_mt = datetime.now(ZoneInfo("America/Denver"))
    today_mt = now_mt.strftime("%Y-%m-%d")
    problems = []
    for path, label, cutoff_hour in LOCK_MARKERS:
        if now_mt.hour < cutoff_hour:
            continue  # too early in the day to expect this yet
        try:
            recorded = path.read_text().strip()
        except Exception:
            recorded = ""
        if recorded != today_mt:
            problems.append(
                f"{label}: no successful live lock recorded for {today_mt} as of "
                f"{now_mt.strftime('%H:%M')} MT (marker shows {recorded or 'nothing'}) -- "
                f"the dedicated workflow's own catch-up slots and the live-tracker "
                f"watchdog may all have missed today"
            )
    return problems


def _api_get(path: str) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def check_workflow(filename: str, label: str, max_age_hours: int) -> str | None:
    """Returns a short problem description, or None if this workflow looks
    healthy (a real run within the window, and it succeeded)."""
    try:
        data = _api_get(f"/repos/{REPO}/actions/workflows/{filename}/runs?per_page=5")
    except Exception as exc:
        return f"{label}: couldn't check ({exc})"

    runs = data.get("workflow_runs") or []
    if not runs:
        return f"{label}: no runs found at all"

    latest = runs[0]
    created = datetime.fromisoformat(latest["created_at"].replace("Z", "+00:00"))
    age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    if age_hours > max_age_hours:
        return f"{label}: last run was {age_hours:.1f}h ago (expected within {max_age_hours}h) -- may have been queue-starved or the schedule didn't fire"

    if latest.get("status") == "completed" and latest.get("conclusion") != "success":
        return f"{label}: most recent run {latest.get('conclusion')} ({latest.get('html_url')})"

    return None


# ── Refresh-workflow monitoring (2026-10-03) ──────────────────────────────────────────────────────────────────────────────
# (workflow file, label, max hours since the last SUCCESSFUL run, consecutive failed runs that count as "failing").
# Limits = cadence + the 3-6h GitHub cron delay observed in the 2026-10-02 run-history audit.  cancelled/skipped runs (concurrency
# groups) are ignored, they are normal.
REFRESH_MONITORED = [
    ("scheduled-refresh.yml", "Main data refresh (data.json) 3x/day", 20, 1),
    ("daily-schedules-refresh.yml", "Daily schedules refresh", 20, 1),
    ("soccer-schedule-tomorrow.yml", "Soccer tomorrow-slate schedule", 36, 1),
    ("shl-schedule-refresh.yml", "SHL schedule refresh", 20, 1),
    ("liiga-schedule-refresh.yml", "Liiga schedule refresh", 20, 1),
    ("nla-schedule-refresh.yml", "NLA schedule refresh", 20, 1),
    ("extraliga-schedule-refresh.yml", "Extraliga schedule refresh", 20, 1),
    ("opta-soccer-stats-daily.yml", "Opta soccer stats", 36, 1),
    ("daily-player-stats-refresh.yml", "Daily player & prop stats refresh", 36, 1),
    ("cfb-stats-weekly.yml", "CFB team stats", 36, 1),
    ("cfb-rankings-weekly.yml", "CFB power rankings (weekly)", 9 * 24, 1),
    ("cfb-roster-monthly.yml", "CFB rosters (monthly)", 35 * 24, 1),
    ("nfl-stats-weekly.yml", "NFL stats (weekly)", 9 * 24, 1),
    ("nfl-roster-weekly.yml", "NFL rosters (weekly)", 9 * 24, 1),
    # Runs every 30 min 12:00-05:00 UTC only (7h nightly gap + delay) and flakes are cheap there: alert on 3 in a row.
    ("live-tracker.yml", "Live tracker (live_data.json)", 14, 3),
]

# (docs file, label, stamp key, warn hours, stale hours).  data.json / schedules / euro hockey = the app header line's _FRESH_SRC
# numbers.  Daily producers get 30/54h; weekly / monthly producers get cadence + slack.
FRESHNESS_FILES = [
    ("data.json", "Engine data (data.json)", "generated", 14, 26),
    ("nhl_schedule.json", "NHL schedule/odds", "generated_at", 20, 36),
    ("cfb_schedule.json", "CFB schedule", "generated_at", 20, 36),
    ("nfl_schedule.json", "NFL schedule", "generated_at", 20, 36),
    ("soccer_schedule.json", "Soccer schedule", "generated_at", 20, 36),
    ("shl_schedule.json", "SHL schedule/odds", "generated_at", 12, 28),
    ("liiga_schedule.json", "Liiga schedule/odds", "generated_at", 12, 28),
    ("nla_schedule.json", "NLA schedule/odds", "generated_at", 12, 28),
    ("extraliga_schedule.json", "Extraliga schedule/odds", "generated_at", 12, 28),
    ("live_data.json", "Live feed (live_data.json)", "ts", 1.5, 4),           # special-cased in check_data_freshness
    ("cfb_team_stats.json", "CFB team stats", "generated_at", 30, 54),
    ("nfl_injuries.json", "NFL injuries", "generated_at", 30, 54),
    ("nfl_transactions.json", "NFL transactions", "generated_at", 30, 54),
    ("player_stats.json", "Player stats", "generatedAt", 30, 54),
    ("cfb_power.json", "CFB power rankings", "generated_at", 9 * 24, 16 * 24),
    ("nfl_team_stats.json", "NFL team stats", "generated_at", 9 * 24, 16 * 24),
    ("nfl_standings.json", "NFL standings", "generated_at", 9 * 24, 16 * 24),
    ("nfl_player_stats.json", "NFL player stats", "generated_at", 9 * 24, 16 * 24),
]
# live_data.json is only rewritten while the live-tracker cron runs (12:00-05:00 UTC); off-window it is legitimately idle.
LIVE_TRACKER_IDLE_UTC_HOURS = range(6, 12)
# When no game is live the feed is only a deadman for the tracker itself.
LIVE_IDLE_WARN_H, LIVE_IDLE_STALE_H = 14, 26


def parse_stamp(v) -> datetime | None:
    """A file stamp -> aware UTC datetime.  Understands ISO-8601 (with Z / offset) and 'YYYY-MM-DD HH:MM UTC' (same tolerance as
    docs/app.html's _parseGenAt: any 'YYYY-MM-DD HH:MM' is read as UTC).  None if unusable."""
    if not v or not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", v)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fmt_age(h: float) -> str:
    return f"{max(1, round(h * 60))}m" if h < 1 else (f"{round(h)}h" if h < 48 else f"{round(h / 24)}d")


def check_data_freshness(docs_dir: Path | None = None, now: datetime | None = None, files=None) -> list[tuple[str, str]]:
    """-> [(level, message)], level 'alert' (older than the stale threshold) or 'note' (older than warn, or unreadable).
    Pure file reads (the checkout's docs/), never raises."""
    docs_dir = docs_dir or (ROOT / "docs")
    now = now or datetime.now(timezone.utc)
    out: list[tuple[str, str]] = []
    for fname, label, key, warn_h, stale_h in (files if files is not None else FRESHNESS_FILES):
        try:
            raw = (docs_dir / fname).read_text()
        except Exception as exc:
            out.append(("note", f"{label}: {fname} unreadable ({exc.__class__.__name__})"))
            continue
        try:
            obj = json.loads(raw)
            val = obj.get(key) if isinstance(obj, dict) else None
        except Exception:
            m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"([^"]+)"', raw[:4000])
            obj, val = {}, (m.group(1) if m else None)
        stamp = parse_stamp(val)
        if stamp is None:
            out.append(("note", f"{label}: no readable '{key}' stamp in {fname}"))
            continue
        if fname == "live_data.json":
            if now.hour in LIVE_TRACKER_IDLE_UTC_HOURS:
                continue
            if not (isinstance(obj, dict) and obj.get("hasLiveGames")):
                warn_h, stale_h = LIVE_IDLE_WARN_H, LIVE_IDLE_STALE_H
        age_h = (now - stamp).total_seconds() / 3600
        if age_h >= stale_h:
            out.append(("alert", f"{label}: STALE -- {fname} stamp is {_fmt_age(age_h)} old (stale at {_fmt_age(stale_h)})"))
        elif age_h >= warn_h:
            out.append(("note", f"{label}: aging -- {fname} stamp is {_fmt_age(age_h)} old (warn at {_fmt_age(warn_h)})"))
    return out


def check_refresh_workflow(filename: str, label: str, max_success_age_h: float, failures_to_alert: int = 1,
                           api_get=None, now: datetime | None = None) -> tuple[str, str] | None:
    """Last-success age + consecutive-failure check for one refresh workflow.  -> (level, message) or None when healthy.
    Looks at the last 10 COMPLETED runs, ignoring cancelled/skipped ones.  An API error is only a 'note' (a transient 5xx must not
    email), a missing/old success or failing streak is an 'alert'."""
    api_get = api_get or _api_get
    now = now or datetime.now(timezone.utc)
    try:
        data = api_get(f"/repos/{REPO}/actions/workflows/{filename}/runs?status=completed&per_page=10")
    except Exception as exc:
        return ("note", f"{label}: couldn't check runs ({exc})")
    runs = [r for r in (data.get("workflow_runs") or []) if r.get("conclusion") not in ("cancelled", "skipped", "neutral", None)]
    if not runs:
        return ("alert", f"{label}: no completed runs found at all")
    msgs: list[str] = []
    streak = 0
    for r in runs:
        if r.get("conclusion") == "success":
            break
        streak += 1
    if streak >= failures_to_alert:
        msgs.append(f"last {streak} run(s) FAILED ({runs[0].get('html_url')})")
    success = next((r for r in runs if r.get("conclusion") == "success"), None)
    if success is None:
        msgs.append(f"no successful run among the last {len(runs)}")
    else:
        stamp = parse_stamp(success.get("updated_at") or success.get("created_at"))
        if stamp is not None:
            age_h = (now - stamp).total_seconds() / 3600
            if age_h > max_success_age_h:
                msgs.append(f"last SUCCESS was {_fmt_age(age_h)} ago (limit {_fmt_age(max_success_age_h)})")
    return ("alert", f"{label}: " + "; ".join(msgs)) if msgs else None


def check_refresh_health(now: datetime | None = None, api_get=None) -> list[tuple[str, str]]:
    out = []
    for fname, label, max_h, fails in REFRESH_MONITORED:
        try:
            res = check_refresh_workflow(fname, label, max_h, fails, api_get=api_get, now=now)
        except Exception as exc:  # fail-open
            res = ("note", f"{label}: check crashed ({exc})")
        if res:
            out.append(res)
    return out


def main() -> None:
    problems = check_lock_markers()
    notes: list[str] = []

    if not GITHUB_TOKEN:
        print("GITHUB_TOKEN not set -- can't check Actions API, skipping workflow-run checks.")
    else:
        problems += [p for p in (check_workflow(f, l, h) for f, l, h in MONITORED) if p]
        for level, msg in check_refresh_health():
            (problems if level == "alert" else notes).append(msg)

    # Data-file stamps need no token: they read the checked-out docs/.
    try:
        for level, msg in check_data_freshness():
            (problems if level == "alert" else notes).append(msg)
    except Exception as exc:  # fail-open
        notes.append(f"data freshness check crashed ({exc})")

    for n in notes:
        print(f"::warning::{n}")
    if not problems:
        print("All monitored workflows and data files healthy -- no alert sent." if not notes else
              f"No alert-level issue ({len(notes)} watch item(s) logged above) -- no alert sent.")
        return

    print(f"{len(problems)} issue(s) found:")
    for p in problems:
        print(f"  - {p}")

    if not ALERT_TO:
        print("No alert recipient configured (SOCIAL_CARD_EMAIL_TO/LOCKS_EMAIL_TO unset) -- can't email this.")
        return

    body = (
        '<div style="font-family:monospace;font-size:14px;color:#1a1a2e">'
        '<p><strong>Daily health check found issue(s):</strong></p>'
        '<ul>' + "".join(f"<li>{p}</li>" for p in problems) + '</ul>'
        + ('<p>Also watching (not alert-level yet):</p><ul>' + "".join(f"<li>{n}</li>" for n in notes) + '</ul>' if notes else '')
        + '</div>'
    )
    ok, msg = send_email("Clairvoyance -- daily health check found an issue", ALERT_TO, body)
    print(f"Alert email sent to {ALERT_TO}" if ok else f"Alert email FAILED: {msg}")


if __name__ == "__main__":
    main()
