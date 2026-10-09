#!/usr/bin/env python3
"""docs/kickoffs.json -- every upcoming game start the engine locks (any sport), merged by minute, for the scheduler Worker.

The Worker (scheduler/worker.js) reads this file and runs the pre-kickoff watchdog about 40 minutes before EACH start, so a pick that only qualifies on the last odds readings is locked before
the game -- evening NHL/NBA, early-morning European soccer / hockey, Saturday CFB, Sunday NFL -- instead of depending on a fixed list of times.  Built by pages-deploy.yml right before each
deploy (generated into the deploy artifact, never committed), from the schedule files the refresh workflows already keep current.

In scope (matches the lock pipeline): NHL, SHL / Liiga / NLA / Extraliga, NBA REGULAR season, NFL regular season, CFB, soccer PL / La Liga / Serie A / Champions League.
Out of scope (never locked): NBA/NFL preseason, MLS, Bundesliga, anything already started or final.  Window: from 1 hour ago to 72 hours ahead.

    python3 scripts/build_kickoffs.py [--docs DIR] [--out FILE]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOOKAHEAD_H = 72
LOOKBACK_H = 1
ISO = "%Y-%m-%dT%H:%MZ"
SOCCER_LEAGUES = {"pl": "PL", "liga": "LIGA", "ita": "SERIEA", "cl": "CL"}     # bl / mls are retired


def _parse(s):
    try:
        return datetime.strptime(str(s)[:16] + "Z", ISO).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def collect(docs: Path, now: datetime | None = None) -> list[dict]:
    """-> [{"t": "YYYY-MM-DDTHH:MMZ", "sports": [..]}] sorted by time, one entry per distinct start minute."""
    now = now or datetime.now(timezone.utc)
    lo, hi = now - timedelta(hours=LOOKBACK_H), now + timedelta(hours=LOOKAHEAD_H)
    by_t: dict[str, set] = {}

    def add(sport: str, date) -> None:
        dt = _parse(date)
        if dt and lo <= dt <= hi:
            by_t.setdefault(dt.strftime(ISO), set()).add(sport)

    # flat game lists: state 'pre' only
    for fname, sport in (("nhl_schedule.json", "NHL"), ("shl_schedule.json", "SHL"), ("liiga_schedule.json", "LIIGA"),
                         ("nla_schedule.json", "NLA"), ("extraliga_schedule.json", "EXTRALIGA"), ("nba_schedule.json", "NBA")):
        d = _load(docs / fname)
        for g in (d or {}).get("games") or []:
            if not isinstance(g, dict) or g.get("state") != "pre":
                continue
            if sport == "NBA" and (g.get("preseason") or g.get("postponed")):
                continue
            add(sport, g.get("date"))
    # football: {week label: [games]}
    for fname, sport in (("nfl_schedule.json", "NFL"), ("cfb_schedule.json", "CFB")):
        d = _load(docs / fname)
        for label, games in ((d or {}).get("weeks") or {}).items():
            if sport == "NFL" and "preseason" in str(label).lower():
                continue
            for g in games or []:
                if isinstance(g, dict) and g.get("state") == "pre" and not (sport == "NFL" and g.get("seasonType") == 1):
                    add(sport, g.get("date"))
    # soccer: today + tomorrow snapshots, {league key: [games]}
    for fname in ("soccer_schedule.json", "soccer_schedule_tomorrow.json"):
        d = _load(docs / fname)
        for key, games in ((d or {}).get("leagues") or {}).items():
            if key not in SOCCER_LEAGUES:
                continue
            for g in games or []:
                if isinstance(g, dict) and g.get("status") == "pre":
                    add(SOCCER_LEAGUES[key], g.get("date"))
    return [{"t": t, "sports": sorted(s)} for t, s in sorted(by_t.items())]


def build(docs: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {"generated_at": now.strftime(ISO), "lookahead_h": LOOKAHEAD_H, "starts": collect(docs, now)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", default=str(ROOT / "docs"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    docs = Path(a.docs)
    out = Path(a.out) if a.out else docs / "kickoffs.json"
    payload = build(docs)
    out.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"[kickoffs] {len(payload['starts'])} distinct start times in the next {LOOKAHEAD_H}h -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
