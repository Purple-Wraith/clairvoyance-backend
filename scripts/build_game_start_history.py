#!/usr/bin/env python3
"""Rebuild docs/game_start_history.json: scheduled start times for the games behind every pick in the ledger backup, recovered
from the committed git history of the schedule snapshots (soccer + any league whose live schedule file no longer lists an old
game). scripts/lock_timing.py reads it as a fallback behind the live schedule files, and docs/app.html's _tbLoad reads the same
file, so the server-side public figures and the in-app view classify identically.

    python3 scripts/build_game_start_history.py [--ledger docs/picks_backup.json] [--dry-run]

Only games that match a ledger pick (same Mountain date + both team names, either orientation) are kept, so the file stays small
(a few hundred entries). Newest snapshot wins when a game's start moved. Safe to re-run any time; it only ever ADDS or refreshes
entries (existing entries whose game no longer matches a pick are kept, since a pick can be re-synced later). Read-only on git.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lock_timing as lt  # noqa: E402

ROOT = lt.ROOT
# (path, kind, home key, away key). kind: flat = {"games":[...]}, weeks = {"weeks":{wk:[...]}}, soccer = {"leagues":{lg:[...]}}
SOURCES = [
    ("docs/shl_schedule.json", "flat", "homeName", "awayName"), ("docs/liiga_schedule.json", "flat", "homeName", "awayName"),
    ("docs/nla_schedule.json", "flat", "homeName", "awayName"), ("docs/extraliga_schedule.json", "flat", "homeName", "awayName"),
    ("docs/nhl_schedule.json", "flat", "home", "away"),
    ("docs/cfb_schedule.json", "weeks", "home", "away"), ("docs/nfl_schedule.json", "weeks", "home", "away"),
    ("docs/soccer_schedule.json", "soccer", "home", "away"), ("docs/soccer_schedule_tomorrow.json", "soccer", "home", "away"),
    ("docs/mls_schedule.json", "soccer", "home", "away"),
]


def _git(*args) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True).stdout


def _games(doc: dict, kind: str):
    if kind == "flat":
        yield from doc.get("games", [])
    elif kind == "weeks":
        for wk in (doc.get("weeks") or {}).values():
            yield from wk or []
    else:
        for gs in (doc.get("leagues") or {}).values():
            yield from gs or []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=str(ROOT / "docs" / "picks_backup.json"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    picks = json.loads(Path(a.ledger).read_text())
    wanted = {lt.game_key(p["date"], p["hA"], p["awA"]) for p in picks if p.get("date") and p.get("hA") and p.get("awA")}
    found: dict = {}
    for path, kind, hk, ak in SOURCES:
        hashes = _git("log", "--format=%H", "--", path).split()  # newest first
        seen_here = 0
        for h in reversed(hashes):  # oldest -> newest so the newest snapshot overwrites
            try:
                doc = json.loads(_git("show", f"{h}:{path}"))
            except ValueError:
                continue
            for g in _games(doc, kind):
                if not g.get("date") or not g.get(hk) or not g.get(ak):
                    continue
                ms = lt.iso_ms(g["date"])
                if ms is None:
                    continue
                k = lt.game_key(lt.mt_date(ms), g[hk], g[ak])
                if k in wanted:
                    if k not in found:
                        seen_here += 1
                    found[k] = ms
        print(f"{path}: {len(hashes)} versions, +{seen_here} pick-matching games")
    out_path = ROOT / "docs" / lt.HISTORY_FILE
    existing = lt.load_history_index(ROOT)
    merged = {**existing, **found}
    covered = sum(1 for k in wanted if k in merged)
    print(f"ledger games: {len(wanted)}; with a recovered start: {covered}; history entries: {len(merged)}")
    if a.dry_run:
        return
    doc = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "note": ("Scheduled start (epoch ms) of games behind ledger picks, recovered from git history of the schedule "
                 "snapshots by scripts/build_game_start_history.py. Key = America/Denver date | team | team (names sorted). "
                 "Read by scripts/lock_timing.py and docs/app.html (_tbLoad) as a fallback behind the live schedule files."),
        "games": {k: merged[k] for k in sorted(merged)},
    }
    out_path.write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n")
    print(f"wrote {out_path} ({out_path.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
