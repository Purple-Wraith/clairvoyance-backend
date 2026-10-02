#!/usr/bin/env python3
"""Offline look-ahead audit: for every settled pick in docs/picks_backup.json, how long before/after the game's scheduled
start was it locked, and what is the record for each timing class?  Read-only; touches nothing.

    python3 scripts/audit_lock_timing.py [--soccer-history]

Start time = pick.startMs when present (picks locked after the 2026-10-02 pre-start guard), else matched to a schedule game
(same two teams in either orientation, game's America/Denver date == pick.date):
  SHL/LIIGA/NLA/EXTRALIGA  docs/<league>_schedule.json (by name)   NHL/CFB/NFL  docs/{nhl,cfb,nfl}_schedule.json (by abbr)
  --soccer-history         PL/LIGA/CL/SERIEA/BUND/MLS from every committed version of docs/soccer_schedule*.json (git history)
Classes: pre (locked before start) / during (0..game-length after start) / after (later) / unknown (no start data).
Mirrors _pickTiming()/_TB_DUR_MIN in docs/app.html."""
import argparse, collections, json, subprocess, sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
MT = ZoneInfo("America/Denver")
DUR_MIN = {"NHL": 150, "SHL": 150, "LIIGA": 150, "NLA": 150, "EXTRALIGA": 150, "NBA": 150, "NFL": 195, "CFB": 210,
           "PL": 120, "LIGA": 120, "CL": 120, "SERIEA": 120, "BUND": 120, "MLS": 120}


def iso_ms(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000


def mt_date(ms):
    return datetime.fromtimestamp(ms / 1000, MT).strftime("%Y-%m-%d")


def league_of(p):
    sp, lg = (p.get("sport") or "").upper(), (p.get("league") or "").upper()
    return lg if sp in ("FOOTBALL", "SOCCER", "BASKETBALL", "HOCKEY", "BASEBALL") and lg else (sp or lg)


def build_index(soccer_history):
    idx = {}  # (league, mt date, frozenset teams) -> start ms
    def add(lg, g, hk, ak):
        if g.get("date") and g.get(hk) and g.get(ak):
            ms = iso_ms(g["date"]); idx[(lg, mt_date(ms), frozenset([g[hk], g[ak]]))] = ms
    for lg in ("SHL", "LIIGA", "NLA", "EXTRALIGA"):
        for g in json.load((ROOT / "docs" / f"{lg.lower()}_schedule.json").open())["games"]:
            add(lg, g, "homeName", "awayName")
    for lg in ("NHL",):
        for g in json.load((ROOT / "docs" / "nhl_schedule.json").open())["games"]:
            add(lg, g, "home", "away")
    for lg in ("CFB", "NFL"):
        for w in json.load((ROOT / "docs" / f"{lg.lower()}_schedule.json").open())["weeks"].values():
            for g in w:
                add(lg, g, "home", "away")
    if soccer_history:
        key = {"pl": "PL", "liga": "LIGA", "cl": "CL", "ita": "SERIEA", "bl": "BUND", "mls": "MLS"}
        for f in ("docs/soccer_schedule.json", "docs/soccer_schedule_tomorrow.json"):
            for h in subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--", f], capture_output=True, text=True).stdout.split():
                try:
                    j = json.loads(subprocess.run(["git", "-C", str(ROOT), "show", f"{h}:{f}"], capture_output=True, text=True).stdout)
                except Exception:
                    continue
                for lk, gs in (j.get("leagues") or {}).items():
                    for g in gs:
                        add(key.get(lk, lk), g, "home", "away")
    return idx


def timing(p, lg, idx):
    st = p.get("startMs") or idx.get((lg, p.get("date"), frozenset([p.get("hA"), p.get("awA")])))
    if not st or not p.get("lockedAt"):
        return "unknown"
    lead = (st - p["lockedAt"]) / 60000
    return "pre" if lead > 0 else ("during" if lead > -DUR_MIN.get(lg, 150) else "after")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--soccer-history", action="store_true"); a = ap.parse_args()
    picks = json.load((ROOT / "docs" / "picks_backup.json").open())
    idx = build_index(a.soccer_history)
    tab = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
    for p in picks:
        if p.get("outcome") not in ("win", "loss"):
            continue
        lg = league_of(p)
        if lg not in DUR_MIN:
            continue
        c = tab[lg][timing(p, lg, idx)]; c[0] += 1; c[1] += p["outcome"] == "win"
    print(f"{'league':10s} " + " ".join(f"{k:>22s}" for k in ("pre", "during", "after", "unknown")))
    for lg in sorted(tab):
        print(f"{lg:10s} " + " ".join(f"{(str(v[1]) + '-' + str(v[0] - v[1]) + ' ' + format(v[1] / v[0] * 100, '.1f') + '% n=' + str(v[0])) if (v := tab[lg][k])[0] else '-':>22s}" for k in ("pre", "during", "after", "unknown")))


if __name__ == "__main__":
    main()
