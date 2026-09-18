#!/usr/bin/env python3
"""
verify_lock_workflows.py — durable replacement for the manual "did the 3
lock workflows actually go live and lock+send today" check that used to
run as a session-only reminder (dies with the session, auto-expires
after 7 days regardless). This is the deep, content-aware version: not
just "did it run and succeed" (see daily_health_check.py for that shallow
net across all 6 daily workflows) but "did it actually resolve --live
and produce the real lock/email output", by grepping the run's own log --
the exact same manual check this replaces (gh run list -> gh run view
--log -> grep for the invoked command line + success markers).

Catches three distinct failure modes, all real risks documented
elsewhere in this repo:
  1. Didn't fire at all today (the 2026-08-06 queue-starvation incident --
     GitHub never "fails" that, the run just never gets a runner).
  2. Fired but errored (status completed, conclusion != success).
  3. Fired, succeeded, but silently stayed dry-run or errored after
     starting -- caught by grepping the actual log content, not just the
     GitHub-reported conclusion, since a script that runs to completion
     without an unhandled exception still reports "success" even if
     LIVE_MODE didn't apply or a step failed inside a try/except.

Silent on a clean day, matching the hands-off preference already
established for daily_health_check.py -- only emails when it finds a
real problem.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _gmail_email import send_email  # noqa: E402

ALERT_TO = os.environ.get("LOCKS_EMAIL_TO", "") or os.environ.get("SOCIAL_CARD_EMAIL_TO", "")


def _gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


# Real false alarm, found 2026-09-17: this used to be a raw UTC calendar-
# date string match (createdAt.startswith(_today_utc())). Every real lock
# workflow this file checks fires on its own America/Denver-based morning
# schedule (same "today" every other script in this pipeline uses --
# auto_lock_settle.py's own today_mt is always ZoneInfo("America/Denver")),
# all comfortably BEFORE this script's own ~20:30 UTC (~2:30pm MT) daily
# run -- so a UTC-date match happens to work when this only ever runs on
# its own schedule. It breaks the moment anyone (a manual re-check, a
# workflow_dispatch test) runs this after ~6pm MT: at that point the UTC
# calendar date has already rolled to tomorrow, so a same-MT-day run that
# genuinely happened hours earlier no longer starts with "today's" UTC
# date string, producing a real false "no run found today" alert --
# confirmed live, a 9:58pm MT manual dispatch flagged all 3 workflows as
# missing despite each having multiple real successful runs that same MT
# day. Comparing the run's own createdAt (converted to MT) against
# today's MT date instead matches how "today" is defined everywhere else
# in this pipeline, and is correct regardless of what time this script
# itself happens to run.
def _is_today_mt(created_at_iso: str) -> bool:
    mt = ZoneInfo("America/Denver")
    created_mt = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00")).astimezone(mt)
    return created_mt.date() == datetime.now(mt).date()


def _recent_runs(workflow: str, limit: int = 8) -> list[dict]:
    out = _gh("run", "list", "--workflow", workflow, "--limit", str(limit),
               "--json", "databaseId,createdAt,event,status,conclusion")
    return json.loads(out)


def _log_text(run_id: int) -> str:
    return _gh("run", "view", str(run_id), "--log")


def _check_run(run: dict, label: str, expect_markers: list[str]) -> str | None:
    """Runs the deep content check on one already-identified run.
    Returns a problem string, or None if it looks healthy."""
    if run["status"] != "completed":
        return f"{label}: run {run['databaseId']} still {run['status']} (not completed)"
    if run["conclusion"] != "success":
        return f"{label}: run {run['databaseId']} concluded '{run['conclusion']}', not success"

    try:
        log = _log_text(run["databaseId"])
    except Exception as exc:
        return f"{label}: couldn't fetch log for run {run['databaseId']} ({exc})"

    # Real bug, found investigating a false-positive alert: this used to
    # also require "##[group]Run" on the same line. That's genuinely how
    # the invocation line looks for CFB/Soccer Early Lock's own dedicated
    # workflows (a single-command step, so GH Actions gives it its own
    # "##[group]Run python3 scripts/auto_lock_settle.py ..." header) --
    # but the main Auto Lock + Settle workflow's invocation lives inside a
    # multi-command bash `run: |` block with its own if/echo branching, so
    # the actual resolved command (e.g. "python3 scripts/
    # auto_lock_settle.py --lock --final-lock-check --live") only ever
    # appears as its own ANSI-highlighted echoed line, never paired with
    # "##[group]Run" on that same line -- confirmed live against run
    # 33537433293's real log (2026-09-01's actual final lock-check pass,
    # which genuinely ran --live and worked). The old regex could never
    # match that pattern, so this always reported "no invocation found"
    # for the main lock specifically, regardless of what really happened.
    cmd_lines = [l for l in log.splitlines() if "python3 scripts/auto_lock_settle.py" in l]
    if not cmd_lines:
        return f"{label}: run {run['databaseId']} succeeded but no auto_lock_settle.py invocation found in log at all"
    invoked = cmd_lines[-1]
    if "--live" not in invoked:
        return f"{label}: run {run['databaseId']} did NOT include --live (stayed dry-run) -- invoked: {invoked.split('auto_lock_settle.py',1)[-1].strip()}"

    missing = [m for m in expect_markers if m not in log]
    if missing:
        return f"{label}: run {run['databaseId']} ran --live but missing expected log line(s): {missing}"

    return None


def _find_invocation_run(todays: list[dict], label: str) -> tuple[dict | None, str | None]:
    """CFB/Soccer Early Lock each have multiple catch-up cron slots beyond
    the first -- confirmed live (2026-09-01 audit): once today's real lock
    already succeeded, every later catch-up slot's "Run <sport>-only
    auto-lock" step reports should_run=0 and is skipped entirely at the
    workflow level, with ZERO auto_lock_settle.py invocation anywhere in
    that run's log -- by design, not a failure. Real bug this fixes: the
    original version of these two checks just grabbed todays[0] (gh run
    list's default order is most-recent-first), which on any day with more
    than one catch-up slot fired is very likely one of these legitimately-
    empty skip runs, not the one real invocation -- producing a false "no
    auto_lock_settle.py invocation found in log at all" alert for a lock
    that actually happened fine earlier that day (confirmed: this exact
    false positive fired for both CFB and Soccer Early Lock on 2026-09-01).

    Searches ALL of today's runs for the one that actually contains a real
    invocation, in whichever order gh returned them -- there should be
    exactly one per day; a completed+success run with no invocation is an
    expected no-op skip, not scanned further. A completed-but-NOT-success
    run is a real problem and gets surfaced immediately rather than
    silently passed over while searching for an invocation elsewhere.
    Only returns (None, None) -- "nothing to verify content on, and
    nothing wrong either" -- when every run found today is a legitimate
    success-with-no-invocation skip."""
    any_failed = None
    for run in todays:
        if run["status"] != "completed":
            continue
        if run["conclusion"] != "success":
            any_failed = any_failed or run
            continue
        try:
            log = _log_text(run["databaseId"])
        except Exception:
            continue
        cmd_lines = [l for l in log.splitlines() if "python3 scripts/auto_lock_settle.py" in l and "##[group]Run" in l]
        if cmd_lines:
            return run, None
    if any_failed:
        return None, f"{label}: run {any_failed['databaseId']} concluded '{any_failed['conclusion']}', not success"
    if not any(r["status"] == "completed" for r in todays):
        return None, f"{label}: today's run(s) still in progress, none completed yet"
    return None, None


def check_cfb_early() -> str | None:
    runs = _recent_runs("CFB Auto Lock (Early)", limit=20)
    todays = [r for r in runs if r["event"] == "schedule" and _is_today_mt(r["createdAt"])]
    if not todays:
        return "CFB Early Lock: no schedule-triggered run found today"
    run, problem = _find_invocation_run(todays, "CFB Early Lock")
    if problem:
        return problem
    if not run:
        return None  # every run today legitimately skipped (should_run=0) -- nothing to verify content on
    return _check_run(run, "CFB Early Lock",
                       ["=== AUTO-LOCK (PREMIUM/OPTIMAL) — CFB ===", "Locks email (CFB)"])


def check_euro_early() -> str | None:
    """Renamed from check_soccer_early 2026-09-17 when soccer-lock-
    early.yml was merged with SHL/Liiga into european-lock-early.yml
    (see run_euro_early_lock in auto_lock_settle.py) -- checks the SAME
    workflow by its new name, and now verifies BOTH products' own
    "Locks email (...)" line landed in the log (send_locks_email logs
    that unconditionally whenever recipients exist, even for 0
    qualifying legs, so requiring both is safe on a real off-day for
    either product, not just a lucky coincidence)."""
    runs = _recent_runs("European Lock (Early)", limit=20)
    todays = [r for r in runs if r["event"] == "schedule" and _is_today_mt(r["createdAt"])]
    if not todays:
        return "European Early Lock: no schedule-triggered run found today"
    run, problem = _find_invocation_run(todays, "European Early Lock")
    if problem:
        return problem
    if not run:
        return None
    return _check_run(run, "European Early Lock",
                       ["=== AUTO-LOCK (PREMIUM/OPTIMAL) — EUROPEAN EARLY (SOCCER + SHL/LIIGA) ===",
                        "Locks email (SOCCER)", "Locks email (SHL/LIIGA)"])


def check_main_lock() -> str | None:
    """This workflow has 12 nominal cron slots/day (3 lock checks, digest,
    adaptive-recalibration, and 7 settle-only catch-ups), plus its own
    catch-up retries on top of that when a slot's marker shows it hasn't
    fired yet -- confirmed real days with well over 8 runs before this
    check's own 17:30 UTC trigger time. Real bug this fixes: `limit=8`
    only pulled the 8 MOST RECENT runs -- on a busy day (heavy catch-up
    retries, or extra manual/workflow_dispatch runs like this repo saw
    during 2026-09-01's testing) the actual lock-pass run can scroll
    entirely out of an 8-run window well before this check ever runs,
    producing a false "none was identifiable as the lock pass" alert for
    a lock that genuinely happened earlier that same day. Bumped well
    past any realistic single day's run count."""
    runs = _recent_runs("Auto Lock + Settle", limit=40)
    todays = [r for r in runs if r["event"] == "schedule" and _is_today_mt(r["createdAt"])]
    if not todays:
        return "Main Lock: no schedule-triggered run found today at all"

    for run in todays:
        if run["status"] != "completed":
            continue
        try:
            log = _log_text(run["databaseId"])
        except Exception:
            continue
        # Same "##[group]Run" fix as _check_run above -- this workflow's
        # real invocation line never carries that marker (see the comment
        # there for why), so this independent copy of the same match needed
        # the identical fix.
        cmd_lines = [l for l in log.splitlines() if "python3 scripts/auto_lock_settle.py" in l]
        if cmd_lines and "--lock" in cmd_lines[-1] and "--settle" not in cmd_lines[-1]:
            # Found today's lock pass -- run the full content check on it.
            problem = _check_run(run, "Main Lock", ["=== AUTO-LOCK (PREMIUM/OPTIMAL) — ALL PRODUCTS ==="])
            if problem:
                return problem
            email_count = log.count("Locks email (")
            if email_count < 5:
                return f"Main Lock: run {run['databaseId']} only shows {email_count} 'Locks email' line(s), expected ~9 (8 products + OTHER)"
            return None

    return "Main Lock: today's schedule runs exist but none was identifiable as the lock pass (all settle, or none completed)"


def main() -> None:
    checks = [check_main_lock, check_cfb_early, check_euro_early]
    problems = []
    for check in checks:
        try:
            result = check()
        except Exception as exc:
            result = f"{check.__name__}: check itself errored ({exc})"
        if result:
            problems.append(result)

    if not problems:
        print("All 3 lock workflows confirmed live and successful today.")
        return

    print(f"{len(problems)} issue(s) found:")
    for p in problems:
        print(f"::error::{p}")

    if not ALERT_TO:
        print("No alert recipient configured -- can't email this.")
        return

    body = (
        '<div style="font-family:monospace;font-size:14px;color:#1a1a2e">'
        '<p><strong>Lock workflow verification found issue(s) today:</strong></p>'
        '<ul>' + "".join(f"<li>{p}</li>" for p in problems) + '</ul>'
        '<p>Check the Actions tab for full run logs.</p>'
        '</div>'
    )
    ok, msg = send_email("Clairvoyance -- lock workflow verification FAILED", ALERT_TO, body)
    print(f"Alert email sent to {ALERT_TO}" if ok else f"Alert email FAILED: {msg}")


if __name__ == "__main__":
    main()
