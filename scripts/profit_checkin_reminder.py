#!/usr/bin/env python3
"""MERGED INTO THE WEEKLY EMAIL 2026-10-27 (owner: too many emails): the Monday check-in is now section 5 of the Weekly Health Digest (scripts/weekly_health_digest.py calls
weekly_section_html below); the Sunday-evening reminder was dropped and the scheduled workflow / Worker entry removed. The standalone sender below is kept only for a manual workflow_dispatch test.

profit_checkin_reminder.py -- OWNER-ONLY email reminding the owner to do the PROFIT tab check-in at the END and at the START of every week (owner request 2026-10-10).

  END   (Sunday evening Mountain time): "enter each book's ENDING balance for the week that just finished"
  START (Monday morning Mountain time): "confirm each book's STARTING balance for the new week (it should equal last week's ending)"

Weeks start Monday (America/Denver), the same as the PROFIT tab. Never emails a subscriber. `--kind auto` (what the workflow runs) works out from the Mountain-time clock which reminder is due,
so a GitHub cron that lands hours late can never send "end of week" on a Wednesday; outside both windows it does nothing. Each (week, kind) is sent once (data/profit_checkin_reminders.json), so the
Worker's on-time dispatch and the workflow's own cron fallback can both run without double-mailing.

    python3 scripts/profit_checkin_reminder.py --kind auto [--dry-run] [--now 2026-10-11T20:00]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
MT = ZoneInfo("America/Denver")
STATE_REL = "data/profit_checkin_reminders.json"
BOOKS = ("DraftKings", "PrizePicks", "Hard Rock")
APP_URL = "https://purple-wraith.github.io/clairvoyance-backend/app.html"
# windows (Mountain time) in which a reminder still makes sense: END from Sunday 16:00 until Monday 06:00, START from Monday 05:00 until Tuesday 06:00
END_FROM_H, END_TO_H, START_FROM_H, START_TO_H = 16, 6, 5, 6


def week_monday(d):
    return d - timedelta(days=d.weekday())


def due_kind(now_mt: datetime):
    """('end'|'start'|None, monday_of_the_week_the_reminder_is_about). END is about the week that is finishing; START about the week that is beginning."""
    dow, hour = now_mt.weekday(), now_mt.hour
    day = now_mt.date()
    if dow == 6 and hour >= END_FROM_H:                      # Sunday evening
        return "end", week_monday(day)
    if dow == 0 and hour < END_TO_H:                         # Monday small hours: still the end of the week that just closed
        return "end", week_monday(day) - timedelta(days=7)
    if dow == 0 and hour >= START_FROM_H:                    # Monday from 05:00
        return "start", week_monday(day)
    if dow == 1 and hour < START_TO_H:                       # a late start reminder (Tuesday small hours): still about the week that began yesterday
        return "start", week_monday(day)
    return None, None


def _rng(monday):
    sunday = monday + timedelta(days=6)
    return f"{monday.strftime('%b')} {monday.day}–{sunday.strftime('%b') if sunday.month != monday.month else ''}{' ' if sunday.month != monday.month else ''}{sunday.day}"


def build_email(kind: str, monday):
    rng = _rng(monday)
    if kind == "end":
        subject = f"Profit tracker — end-of-week check-in (week of {rng})"
        head = "End of week: enter your ENDING balances"
        steps = ("Open the PROFIT tab → <b>PERIOD CHECK-IN</b> → WEEK (the week of " + rng + ").",
                 "For each book enter the <b>ENDING AMOUNT</b> your sportsbook app shows right now.",
                 "The STARTING amount is pre-filled from last week; change it only if it is wrong.",
                 "Log any deposit or withdrawal you made this week first, so profit is not distorted.")
    else:
        subject = f"Profit tracker — start-of-week check-in (week of {rng})"
        head = "New week: confirm your STARTING balances"
        steps = ("Open the PROFIT tab → <b>PERIOD CHECK-IN</b> → WEEK (the week of " + rng + ").",
                 "For each book enter the <b>STARTING AMOUNT</b> your sportsbook app shows now (it should match last week's ending).",
                 "Leave ENDING blank — the week is just starting.",
                 "Log any deposit or withdrawal as it happens during the week.")
    lis = "".join(f"<li style='margin:4px 0'>{s}</li>" for s in steps)
    books = ", ".join(BOOKS)
    from _gmail_email import EMAIL_WRAP_OPEN, EMAIL_WRAP_CLOSE
    html = (EMAIL_WRAP_OPEN + f"<h2 style='margin:0 0 8px;font-size:18px'>{head}</h2>"
            f"<p style='margin:0 0 10px;color:#444'>Books to check in: <b>{books}</b>.</p><ol style='padding-left:20px;margin:0 0 14px'>{lis}</ol>"
            f"<p style='margin:0 0 6px'><a href='{APP_URL}' style='color:#7a00c8'>Open the app → PROFIT tab</a></p>"
            "<p style='margin:12px 0 0;color:#888;font-size:12px'>Owner-only reminder. It is sent at the end (Sunday evening) and the start (Monday morning) of every week, Mountain time.</p>"
            + EMAIL_WRAP_CLOSE)
    return subject, html


def weekly_section_html(now_mt: datetime | None = None) -> str:
    """The single Monday check-in block (inside the Weekly Health Digest): close out last week's ENDING balances and confirm this week's STARTING ones in one pass."""
    now_mt = now_mt or datetime.now(MT)
    this_week = week_monday(now_mt.date())
    last_rng, this_rng = _rng(this_week - timedelta(days=7)), _rng(this_week)
    steps = (f"Open the PROFIT tab → <b>PERIOD CHECK-IN</b> → WEEK.",
             f"Last week ({last_rng}): enter the <b>ENDING AMOUNT</b> your sportsbook app shows for each book. Log any deposit or withdrawal first so profit is not distorted.",
             f"This week ({this_rng}): confirm the <b>STARTING AMOUNT</b> — it should equal last week's ending. Leave ENDING blank.")
    lis = "".join(f"<li style='margin:4px 0'>{x}</li>" for x in steps)
    return (f"<h3>5. Profit tracker check-in</h3><p style='margin:0 0 8px'>Books: <b>{', '.join(BOOKS)}</b>.</p>"
            f"<ol style='padding-left:20px;margin:0 0 10px'>{lis}</ol>"
            f"<p style='margin:0'><a href='{APP_URL}' style='color:#7a00c8'>Open the app → PROFIT tab</a></p>")


def load_state(path: Path = ROOT / STATE_REL) -> dict:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) and isinstance(d.get("sent"), list) else {"sent": []}
    except Exception:
        return {"sent": []}


def save_state(state: dict, path: Path = ROOT / STATE_REL) -> None:
    state["sent"] = sorted(set(state["sent"]))[-40:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1) + "\n")


def owner_address() -> str:
    return os.environ.get("LOCKS_EMAIL_TO", "") or os.environ.get("SOCIAL_CARD_EMAIL_TO", "")


def run(kind: str, now: datetime | None = None, dry: bool = False, force: bool = False, send=None, state_path: Path = ROOT / STATE_REL, to: str | None = None) -> tuple[str, str]:
    """Returns (outcome, detail): outcome in sent | dry-run | skipped | failed."""
    now_mt = (now or datetime.now(MT)).astimezone(MT) if (now and now.tzinfo) else (now.replace(tzinfo=MT) if now else datetime.now(MT))
    due, monday = due_kind(now_mt)
    if kind == "auto":
        kind = due
    elif not force and due != kind:
        kind, monday = None, None
    if kind is not None and monday is None:                 # forced outside any window (manual test): about the current week
        monday = week_monday(now_mt.date())
    if kind is None or monday is None:
        return "skipped", f"no reminder due at {now_mt:%a %H:%M} MT"
    key = f"{monday.isoformat()}:{kind}"
    state = load_state(state_path)
    subject, html = build_email(kind, monday)
    if key in state["sent"] and not force:
        return "skipped", f"{key} already sent"
    if dry:
        return "dry-run", f"would send '{subject}'"
    addr = to or owner_address()
    if not addr:
        return "failed", "no owner address configured (LOCKS_EMAIL_TO / SOCIAL_CARD_EMAIL_TO)"
    if send is None:
        from _gmail_email import send_email as send
    ok, msg = send(subject, addr, html)
    if not ok:
        return "failed", str(msg)
    state["sent"].append(key)
    save_state(state, state_path)
    return "sent", subject


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["auto", "start", "end"], default="auto")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="send even outside the window / if already sent (manual test)")
    ap.add_argument("--now", help="override the Mountain-time clock, e.g. 2026-10-11T20:00")
    a = ap.parse_args(argv)
    now = datetime.fromisoformat(a.now) if a.now else None
    if a.kind == "auto" and not a.force:        # retired schedule: a stale Worker (not yet redeployed) still dispatches kind=auto -- never send the standalone email
        print("[profit_checkin_reminder] skipped: merged into the Weekly Health Digest (2026-10-27); use --kind end|start --force for a manual test")
        return 0
    outcome, detail = run(a.kind, now, dry=a.dry_run, force=a.force)
    print(f"[profit_checkin_reminder] {outcome}: {detail}")
    return 1 if outcome == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
