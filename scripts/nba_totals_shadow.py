"""NBA totals SHADOW LOG (owner, 2026-10-27): record the NBA totals the model leans toward but does NOT lock, so the "lower the totals bar to ~55%" question can be
answered with real results instead of a guess.

Background (NBA adjusted-line assessment, 2026-10-27): an NBA total is auto-locked only when the card grades it OPTIMAL+ at the posted line (stored altLine.postedProb has been
~61%+ on every auto-locked total), and the model's OVER leans only reach ~54-58%, so no NBA OVER has ever auto-locked. Lowering the bar to ~55% would admit those OVERs and mild UNDERs.
Rather than lowering it blind, every live lock pass that covers NBA appends here each NBA total that:
    * did NOT qualify (tier below OPTIMAL -- qualifying totals are already in the ledger), and
    * the model rates SHADOW_MIN_P (55%) or better at the POSTED line (rawProb when the stated probability was clamped, else prob -- the same number real picks store as altLine.postedProb),
together with the adjusted line it WOULD have been locked at (same _alt_shift_leg the real locks use). Nothing is locked, emailed, shown, or counted anywhere -- record only.

THE FILE  data/nba_totals_shadow.jsonl  (append-only, `merge=union` in .gitattributes like data/emailed_picks.jsonl), one JSON object per line:
    {"id": "2026-10-28_BOS_NY_UNDER231.5", "date": "2026-10-28", "hA": "BOS", "awA": "NY", "startMs": ..., "side": "under", "label": "UNDER 231.5", "posted": 231.5,
     "p": 0.571, "tierN": 1, "evVal": 0.02, "altLabel": "UNDER 239.5", "altLine": 239.5, "shift": 8.0, "altP": 0.67, "t": "<iso utc>"}
  One row per id (game + side + posted line); a later pass that sees the same line again adds nothing. A moved posted line is a new row (both kept; the report uses the FIRST per game+side).

THE REPORT  python3 scripts/nba_totals_shadow.py [--min-p 0.55]
  Grades every row whose game is final from ESPN's public NBA scoreboard (one call per Eastern date, same source as scripts/fetch_nba.py) and prints win-loss at the posted line and at the
  adjusted line, split by OVER / UNDER and by probability bucket. Read-only: writes nothing. Revisit at ~100 settled NBA totals (owner's plan, 2026-10-27).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
STATE_REL = "data/nba_totals_shadow.jsonl"
SHADOW_MIN_P = 0.55
MT = ZoneInfo("America/Denver")
ET = ZoneInfo("America/New_York")
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={ymd}&limit=50"


def posted_p(m: dict):
    """The model's own number at the posted line: pre-clamp rawProb when the stated probability was clamped, else prob (matches altLine.postedProb on real picks)."""
    p = m.get("rawProb") if m.get("rawProb") is not None else m.get("prob")
    return float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) else None


def row_id(date: str, hA: str, awA: str, label: str) -> str:
    return f"{date}_{hA}_{awA}_{label.replace(' ', '')}"


def read_ids(text: str) -> set[str]:
    ids = set()
    for ln in (text or "").splitlines():
        try:
            ids.add(json.loads(ln)["id"])
        except Exception:
            continue
    return ids


def shadow_rows(games: list[dict], qualifying_tiers, shift_fn, existing_ids: set[str], now_iso: str, min_p: float = SHADOW_MIN_P) -> list[dict]:
    """games: NBA game legs ALREADY filtered by the caller to the ones a lock pass could lock (regular season, today's MT date, not started), each with its MT `date`.
    shift_fn(leg) -> the adjusted-line version of an O/U leg (auto_lock_settle._alt_shift_leg) or None."""
    out, seen = [], set(existing_ids)
    for gl in games:
        for m in gl.get("markets") or []:
            side = str(m.get("side") or "").lower()
            if side not in ("over", "under") or m.get("tierN") in qualifying_tiers:
                continue
            p = posted_p(m)
            label = str(m.get("label") or "").strip()
            if p is None or p < min_p or not label:
                continue
            rid = row_id(gl["date"], gl.get("hA") or "", gl.get("awA") or "", label)
            if rid in seen:
                continue
            seen.add(rid)
            try:
                posted = float(label.split()[-1])
            except ValueError:
                continue
            alt = shift_fn({"sport": "NBA", "side": m.get("side"), "label": label, "prob": m.get("prob"), "rawProb": m.get("rawProb"),
                            "dec": m.get("dec"), "ml": m.get("ml"), "reasoning": ""})
            al = (alt or {}).get("altLine") or {}
            out.append({"id": rid, "date": gl["date"], "hA": gl.get("hA"), "awA": gl.get("awA"), "startMs": gl.get("startMs"), "side": side, "label": label,
                        "posted": posted, "p": round(p, 4), "tierN": m.get("tierN"), "evVal": m.get("evVal"),
                        "altLabel": (alt or {}).get("label"), "altLine": al.get("line"), "shift": al.get("shift"), "altP": (alt or {}).get("prob"), "t": now_iso})
    return out


def render(rows: list[dict]) -> str:
    return "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows)


# ── report ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _grade(side: str, line, total) -> str | None:
    if line is None or total is None:
        return None
    d = (total - line) if side == "over" else (line - total)
    return "win" if d > 0 else "loss" if d < 0 else "push"


def _finals_for(et_dates: set[str]) -> dict:
    """{(home, away, et_date): total} for every final game on those Eastern dates."""
    out = {}
    for d in sorted(et_dates):
        try:
            with urllib.request.urlopen(ESPN_URL.format(ymd=d.replace("-", "")), timeout=20) as r:
                js = json.load(r)
        except Exception as exc:
            print(f"  (ESPN {d}: {exc})", file=sys.stderr)
            continue
        for ev in js.get("events") or []:
            comp = (ev.get("competitions") or [{}])[0]
            if not ((comp.get("status") or {}).get("type") or {}).get("completed"):
                continue
            teams = {c.get("homeAway"): c for c in comp.get("competitors") or []}
            try:
                h, a = teams["home"], teams["away"]
                out[(h["team"]["abbreviation"], a["team"]["abbreviation"], d)] = int(h["score"]) + int(a["score"])
            except (KeyError, ValueError, TypeError):
                continue
    return out


def _et_date(r: dict) -> str:
    ms = r.get("startMs")
    if isinstance(ms, (int, float)) and ms > 0:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
    return r["date"]


def report(path: Path, min_p: float) -> int:
    rows = []
    for ln in (path.read_text().splitlines() if path.exists() else []):
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    first = {}
    for r in rows:                              # first row per game + side (a moved posted line later in the day is ignored)
        first.setdefault((r["date"], r["hA"], r["awA"], r["side"]), r)
    rows = [r for r in first.values() if (r.get("p") or 0) >= min_p]
    if not rows:
        print(f"No NBA shadow totals recorded yet ({path.relative_to(ROOT)}).")
        return 0
    finals = _finals_for({_et_date(r) for r in rows})
    graded = []
    for r in rows:
        tot = finals.get((r["hA"], r["awA"], _et_date(r)))
        if tot is None:
            continue
        graded.append({**r, "total": tot, "gPost": _grade(r["side"], r["posted"], tot), "gAlt": _grade(r["side"], r.get("altLine"), tot)})

    def rec(xs, key):
        w = sum(x[key] == "win" for x in xs); l = sum(x[key] == "loss" for x in xs); p = sum(x[key] == "push" for x in xs)
        return f"{w}-{l}" + (f"-{p}" if p else "") + (f"  ({w / (w + l):.0%})" if w + l else "")

    now = datetime.now(MT).strftime("%Y-%m-%d %H:%M MT")
    print(f"NBA totals shadow log -- {now}: {len(rows)} recorded (p >= {min_p:.0%}), {len(graded)} graded, {len(rows) - len(graded)} not final yet")
    groups = [("ALL", graded), ("OVER", [g for g in graded if g["side"] == "over"]), ("UNDER", [g for g in graded if g["side"] == "under"])]
    for lo, hi in ((0.55, 0.57), (0.57, 0.59), (0.59, 0.62), (0.62, 1.01)):
        groups.append((f"p {lo:.0%}-{min(hi, 1):.0%}", [g for g in graded if lo <= g["p"] < hi]))
    print(f"  {'group':<14}{'n':>4}   {'POSTED line':<16}{'ADJUSTED line':<16}")
    for name, xs in groups:
        if xs:
            print(f"  {name:<14}{len(xs):>4}   {rec(xs, 'gPost'):<16}{rec(xs, 'gAlt'):<16}")
    print("  Adjusted-line break-even at the estimated price is about 71.5%. Small n: judge nothing before ~100 graded rows.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Grade the NBA totals shadow log (read-only).")
    ap.add_argument("--min-p", type=float, default=SHADOW_MIN_P)
    ap.add_argument("--file", default=str(ROOT / STATE_REL))
    a = ap.parse_args()
    return report(Path(a.file), a.min_p)


if __name__ == "__main__":
    sys.exit(main())
