#!/usr/bin/env python3
"""Offline look-ahead audit: for every settled pick in docs/picks_backup.json, was it locked before or after its game's
scheduled start, and what is the record for each timing class?  Read-only; touches nothing.

    python3 scripts/audit_lock_timing.py [--no-history]

Uses the shared classifier scripts/lock_timing.py (the same one every public figure uses): stored pick.startMs first, else the
game matched in the live schedule files (SHL/LIIGA/NLA/EXTRALIGA/NHL/CFB/NFL), else docs/game_start_history.json (soccer + older
games recovered from git history by scripts/build_game_start_history.py; --no-history skips it to show what the live files alone
can classify).  Classes: pre / during (locked 0..game-length after start) / after (later) / unknown (no start data -> INCLUDED in
public figures).  Mirrors _pickTiming()/_TB_DUR_MIN in docs/app.html."""
import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lock_timing as lt  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-history", action="store_true")
    a = ap.parse_args()
    picks = json.loads((lt.ROOT / "docs" / "picks_backup.json").read_text())
    idx = lt.load_index(include_history=not a.no_history)
    tab = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
    for p in picks:
        if not lt.settled(p):
            continue
        lg = lt.norm_sport(p) or "?"
        c = tab[lg][lt.classify(p, idx)]
        c[0] += 1
        c[1] += p["outcome"] == "win"
    print(f"{'league':10s} " + " ".join(f"{k:>22s}" for k in ("pre", "during", "after", "unknown")))
    for lg in sorted(tab):
        print(f"{lg:10s} " + " ".join(f"{(str(v[1]) + '-' + str(v[0] - v[1]) + ' ' + format(v[1] / v[0] * 100, '.1f') + '% n=' + str(v[0])) if (v := tab[lg][k])[0] else '-':>22s}" for k in ("pre", "during", "after", "unknown")))


if __name__ == "__main__":
    main()
