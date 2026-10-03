#!/usr/bin/env python3
"""
snapshot_cfb_prior.py -- manual FALLBACK tool (was snapshot_cfb_2025.py, hardcoded to 2025). Builds docs/cfb_team_stats_prior.json,
the previous season's final CFB team offense/defense/special-teams (same schema as docs/cfb_team_stats.json) that docs/app.html's cfbMC
blends in at a fixed 15% weight.

NORMALLY YOU NEVER RUN THIS. The producer (scripts/fetch_cfb.py write_stats -> _season.roll_prior_snapshot) derives the prior file
by itself: the first time it replaces last season's cfb_team_stats.json with a NEWER season's stats, the file being replaced is saved
as cfb_team_stats_prior.json (once; never overwritten mid-season). Run this only when that rollover could not happen -- e.g. the file
was never seeded, or a whole season was skipped -- or to regenerate the snapshot after an extraction fix:

    python3 scripts/snapshot_cfb_prior.py                  # season = football_season_year("cfb") - 1  (derived from today's date)
    python3 scripts/snapshot_cfb_prior.py --season 2025    # pin a season explicitly
    python3 scripts/snapshot_cfb_prior.py --out /tmp/x.json  # write elsewhere (e.g. to inspect first)

Not part of any scheduled workflow. Takes a few minutes (full 11-conference roster build + ~140 ESPN team calls).
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _season import football_season_year  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "cfb_team_stats_prior.json"


def build_payload(season: int, stats: dict) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "season": season,
        "note": (f"Previous-season ({season}) final team stats, built by scripts/snapshot_cfb_prior.py. "
                 "Normally rolled automatically by fetch_cfb.py (see _season.roll_prior_snapshot). Never overwritten mid-season."),
        "teams": stats,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None, help="season to snapshot (default: last season, derived from today's date)")
    ap.add_argument("--out", default=None, help=f"output path (default {OUT_PATH})")
    args = ap.parse_args()
    season = args.season if args.season is not None else football_season_year("cfb") - 1
    out_path = Path(args.out) if args.out else OUT_PATH

    from fetch_cfb import build_roster, fetch_all_team_stats  # noqa: E402  (needs playwright for build_roster)
    print("[snapshot] Building full 11-conference roster (this takes a few minutes)…")
    roster = build_roster()
    total = sum(len(v) for v in roster.values())
    print(f"[snapshot] Roster built: {total} teams across {len(roster)} conferences")

    print(f"[snapshot] Fetching season={season} team stats for every roster team…")
    stats = fetch_all_team_stats(roster, season=season)
    if len(stats) < total * 0.5:
        print(f"[snapshot] only {len(stats)}/{total} teams have season={season} stats -- refusing to write a mostly-empty snapshot",
              file=sys.stderr)
        sys.exit(1)

    tmp = out_path.with_name(out_path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(build_payload(season, stats), indent=2))
    os.replace(tmp, out_path)
    print(f"[snapshot] Wrote {len(stats)} teams (season={season}) to {out_path}")


if __name__ == "__main__":
    main()
