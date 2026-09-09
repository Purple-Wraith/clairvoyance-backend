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

Deliberately silent on a clean day (matches "hands-off" -- nobody wants a
daily "all good" email); only sends anything when there's a real finding.
Uses the repo's own default GITHUB_TOKEN (Actions API read access), not a
personal one -- nothing extra to configure.
"""
from __future__ import annotations
import os
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
    ("auto-lock-settle.yml", "Main Auto-Lock (all 8 products)", 26),
    ("soccer-lock-early.yml", "Soccer Early Lock", 26),
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
]

# (marker file, human label, cutoff hour in MT past which today's date
# should already be recorded). Matches each workflow's own catch-up
# window with headroom: soccer's last catch-up is 6:45am MT (earliest
# kickoff 7:00am), CFB's is 10:00am MT (earliest kickoff 10:00am) -- both
# cutoffs here sit right after those, so a real miss is caught the same
# morning, not up to a day later.
#
# last_pick_of_day_date.txt added after a real rigorous audit
# (2026-09-03) found this exact gap: MONITORED's check_workflow() below
# only checks "did a run happen recently and succeed" -- a run that hits
# the workflow's own "already sent" skip gate ALSO reports success, so a
# day where the real primary trigger silently never fired (confirmed via
# real run history: happened on 4 of the last 4 real days) still looked
# "healthy" as long as some later fallback or a manual dispatch
# eventually caught it. This marker check is the same date-specific
# ground truth the soccer/CFB checks already use -- it catches "still
# missing" AND "took until a very late fallback," not just "never ran at
# all." Cutoff 13 (1pm MT) sits after pick-of-day-social-daily.yml's own
# last fallback slot (11:40am MT nominal) with headroom for GitHub's own
# documented scheduling delay.
LOCK_MARKERS = [
    (ROOT / "data" / "last_soccer_lock_date.txt", "Soccer Early Lock", 8),
    (ROOT / "data" / "last_cfb_lock_date.txt", "CFB Early Lock", 11),
    (ROOT / "data" / "last_pick_of_day_date.txt", "Pick-of-Day Social Email", 13),
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


def main() -> None:
    problems = check_lock_markers()

    if not GITHUB_TOKEN:
        print("GITHUB_TOKEN not set -- can't check Actions API, skipping workflow-run checks.")
    else:
        problems += [p for p in (check_workflow(f, l, h) for f, l, h in MONITORED) if p]

    if not problems:
        print("All monitored workflows healthy -- no alert sent.")
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
        '</div>'
    )
    ok, msg = send_email("Clairvoyance -- daily health check found an issue", ALERT_TO, body)
    print(f"Alert email sent to {ALERT_TO}" if ok else f"Alert email FAILED: {msg}")


if __name__ == "__main__":
    main()
