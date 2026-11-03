"""What happened to every qualifying-but-unlocked leg the pre-kickoff watchdog saw -- the data behind the daily health check's "unlocked legs" count (owner 2026-11-02).

The watchdog is the only place that sees a leg qualify without a lock (the ledger only holds locked picks), and since the same day's fixes it stays quiet about legs that are held on purpose, legs the lock passes
would never qualify, and games that have already started.  Quiet must not mean invisible: each leg's outcome is recorded here and the daily health check counts yesterday's.

Statuses (per leg, per Mountain game date):
    owed         pre-start, the lock passes qualify it and it is still unlocked (the watchdog also emails the owner and auto-locks it when the kickoff is close enough)
    held         held on purpose by the confirmed-lineup gate
    unqualified  the watchdog's observe-only view qualifies it but the passes' own run (real clock, lineup gate, selection) does not
    missed       was 'owed' when last seen before kickoff and the game then started with the leg still unlocked: a real miss
    late         first seen only after kickoff (the watchdog ran late), so nothing is known about it before the start
    cleared      no longer unlocked (locked by a pass or the auto-lock, or no longer qualifying) -- not counted

State is a small committed JSON file written ONLY when a leg's status changes, so the watchdog (a run every ~20-30 minutes) does not create a commit per run.  Pure functions: no git, no network.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STATE_REL = "data/watchdog_legs.json"
MT = ZoneInfo("America/Denver")
KEEP_DAYS = 10
BEHIND_MS = 3 * 3600 * 1000          # same window as the watchdog's WATCHDOG_BEHIND_H: a started leg stays visible (and so un-'cleared') this long
PRE = ("owed", "held", "unqualified", "late")
COUNTED = ("owed", "held", "unqualified", "missed", "late")


def _day(start_ms) -> str | None:
    try:
        return datetime.fromtimestamp(float(start_ms) / 1000.0, MT).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def leg_key(sport, game, leg) -> str:
    return f"{sport}|{game}|{leg}"


def load(path: Path) -> dict:
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) and isinstance(d.get("days"), dict) else {"days": {}}
    except Exception:
        return {"days": {}}


def save(path: Path, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def code_for(owed: bool, reason: str) -> str:
    """The status a watchdog classification (watchdog_classify -> (owed, reason)) means BEFORE kickoff."""
    if owed:
        return "owed"
    if reason.startswith("held"):
        return "held"
    return "unqualified"


def update(state: dict, seen: list[dict], now_ms: float) -> bool:
    """Fold this watchdog run into `state`.  seen = every leg currently unlocked and inside the watch window, each {sport, game, leg, startMs, code} where code is the pre-start status from `code_for`.
    -> True when anything changed (the only time the file is worth writing)."""
    changed = False
    days = state.setdefault("days", {})
    present: set[tuple[str, str]] = set()
    for u in seen:
        st = u.get("startMs")
        day = _day(st)
        if day is None:
            continue
        key = leg_key(u["sport"], u["game"], u["leg"])
        present.add((day, key))
        rec = days.setdefault(day, {}).get(key)
        started = st is not None and float(st) <= now_ms
        if rec is None:
            new = "late" if started else u["code"]
            days[day][key] = {"sport": u["sport"], "game": u["game"], "leg": u["leg"], "startMs": st, "status": new}
            changed = True
            continue
        cur = rec["status"]
        if started:
            new = "missed" if cur == "owed" else cur                      # held / unqualified / late / missed stay what they were; only an 'owed' leg becomes a real miss
            if cur == "cleared":
                new = "late"                                              # reappeared unlocked after being cleared and the game started: nothing known before the start
        else:
            new = u["code"]                                               # still pre-start: the latest view wins (a hold can lift, a leg can become qualified)
        if new != cur:
            rec["status"] = new
            changed = True
    # a record that is no longer unlocked is cleared (locked, or no longer qualifying) -- but only while it would still be in the watch window, so a started leg that simply aged out keeps its status
    for day, recs in days.items():
        for key, rec in recs.items():
            if (day, key) in present or rec["status"] not in PRE:
                continue
            st = rec.get("startMs")
            if st is None or float(st) + BEHIND_MS >= now_ms:
                rec["status"] = "cleared"
                changed = True
    cutoff = sorted(days)[-KEEP_DAYS] if len(days) > KEEP_DAYS else None
    if cutoff:
        for d in [d for d in days if d < cutoff]:
            del days[d]
            changed = True
    return changed


def summary(state: dict, day: str) -> dict:
    """Counts for one Mountain game date: {total, owed, held, unqualified, missed, late, legs: {status: [ 'game leg', ...]}} (cleared legs are not counted)."""
    recs = (state.get("days") or {}).get(day) or {}
    out: dict = {"total": 0, **{s: 0 for s in COUNTED}, "legs": {s: [] for s in COUNTED}}
    for rec in recs.values():
        s = rec.get("status")
        if s in COUNTED:
            out[s] += 1
            out["total"] += 1
            out["legs"][s].append(f"{rec.get('game')} {rec.get('leg')}")
    return out
