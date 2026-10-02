"""Shared scraper-health logging for the hockey Flashscore scrapers
(fetch_shl.py/fetch_liiga.py/fetch_nla.py/fetch_extraliga.py).

Added 2026-10-02 after a real incident: the SHL/Liiga/NLA/Extraliga
scrapers silently captured far fewer completed games than they should
have, for weeks, with zero error raised (one run captured 27 results,
the pagination fix brought it to 34 -- see fetch_shl.py's
_extract_match_row/fetch_results comments for the underlying bug). There
was no historical record of "how many rows did this scrape capture" to
have caught that sooner. This module is that record: a flat rolling log
the Engine Health tab (docs/app.html, renderEngineHealth) reads to show
a per-league trend and flag an anomalous drop.

Kept as its own tiny module (not inlined in each fetch_*.py) since all
4 callers need the exact same append-and-trim logic -- a copy-pasted
version in each file is just 4 chances for the trim logic to drift.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "docs" / "scraper_health.json"
ROLLING_WINDOW = 60  # keep the last N entries per league, not unbounded


def log_scrape(league: str, fixtures: int, results: int) -> None:
    """Append one record for this run to docs/scraper_health.json.

    Best-effort only -- this must never be the thing that breaks a real
    scrape/data-write it's piggybacking on. Callers also wrap their own
    call to this in try/except (belt-and-suspenders, explicit ask), but
    this function never raises on its own either.
    """
    try:
        try:
            existing = json.loads(LOG_PATH.read_text()) if LOG_PATH.exists() else []
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []
        existing.append({
            "league": league,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fixtures": fixtures,
            "results": results,
        })
        # Trim to the last ROLLING_WINDOW entries PER LEAGUE (not a flat
        # trim of the whole file, which could let one noisy league's
        # frequent runs push another league's history out entirely).
        by_league: dict[str, list[dict]] = {}
        for rec in existing:
            by_league.setdefault(rec.get("league", "?"), []).append(rec)
        trimmed: list[dict] = []
        for recs in by_league.values():
            trimmed.extend(recs[-ROLLING_WINDOW:])
        trimmed.sort(key=lambda r: r.get("ts", ""))
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text(json.dumps(trimmed, indent=2))
    except Exception:
        pass
