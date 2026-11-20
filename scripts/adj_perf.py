"""
adj_perf.py -- the app's ADJUSTED LINES tracker table (Analytics -> ADJ. LINES, docs/app.html _altRows/_altStats/_altTableHTML) computed from the ledger backup,
for the social card (gen_adjusted_scorecard.py).  A Python port of the app's logic, kept in step with it (see also alt_line_scorecard.py, which owns the line grading):

  * every pick that carries `altLine` {line, posted, shift, ...} (finite line + posted), except removed ones, grouped by league;
  * LOCKED ADJ. LINE   = the stored outcome of each settled pick (the line it was locked at);
  * SAME PICKS AT POSTED = the same picks regraded at the ORIGINAL posted line from the final score -- only where regrading the locked line reproduces the stored outcome
                           (otherwise the posted result is unknown and that pick is left out of this column, exactly like the app);
  * SAVED = lost at the posted line but won at the locked line; COST = the reverse;  win % = wins / (wins + losses), pushes shown only in the record (the app's _altPct).
No window: all time, like the app's default.  Records and win % only (adjusted-line prices are estimates, so no units).

Stdlib only.  Reads docs/picks_backup.json (the committed ledger backup), never Supabase -- so a pick settled in the last few minutes can lag the live app.
"""
from __future__ import annotations

import json
from pathlib import Path

import ad_coverage as ac
import alt_line_scorecard as als
import lock_timing as lt

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "docs" / "picks_backup.json"

# lock_timing sport code -> display name (same names every other card uses)
_CODE_TO_NAME = {"NFL": "NFL", "CFB": "CFB", "NBA": "NBA", "NHL": "NHL", "SHL": "SHL", "LIIGA": "Liiga", "NLA": "NLA", "EXTRALIGA": "Extraliga",
                 "PL": "Premier League", "LIGA": "La Liga", "SERIEA": "Serie A", "CL": "Champions League"}


def _finite(v) -> bool:
    try:
        return float(v) == float(v) and abs(float(v)) != float("inf")
    except (TypeError, ValueError):
        return False


def alt_of(p: dict):
    """The pick's altLine when it is a real adjusted-line record (the app's _altOf), else None."""
    a = p.get("altLine")
    return a if isinstance(a, dict) and _finite(a.get("line")) and _finite(a.get("posted")) else None


def _blank() -> dict:
    return dict(n=0, pending=0, settled=0, aw=0, al=0, ap=0, both=0, bpw=0, bpl=0, baw=0, bal=0, saved=0, cost=0, shift_sum=0.0, shift_n=0)


def tally(p: dict, s: dict) -> None:
    """Add one adjusted-line pick to a stats dict (the app's _altRows + _altStats)."""
    al = alt_of(p)
    s["n"] += 1
    if _finite(al.get("shift")):
        s["shift_sum"] += abs(float(al["shift"])); s["shift_n"] += 1
    out = p.get("outcome")
    if out not in ("win", "loss", "push"):
        s["pending"] += 1
        return
    s["settled"] += 1
    s["aw"] += out == "win"; s["al"] += out == "loss"; s["ap"] += out == "push"
    ra = als.grade(p, float(al["line"]))
    if ra is not None and ra != out:                       # the recomputed locked-line result disagrees with the stored one: the posted result is not trusted
        return
    posted = als.grade(p, float(al["posted"]), al.get("postedLabel") if al.get("flip") else None)   # a side-flipped total: the POSTED pick was the other side
    if posted is None:
        return
    s["both"] += 1
    s["baw"] += out == "win"; s["bal"] += out == "loss"
    s["bpw"] += posted == "win"; s["bpl"] += posted == "loss"
    s["saved"] += out == "win" and posted == "loss"
    s["cost"] += out == "loss" and posted == "win"


def load(backup: Path = BACKUP) -> dict:
    """-> {'leagues': {display name: stats}, 'overall': stats, 'rows': [(display name, stats)] sorted like the app (most picks first)}"""
    rows = json.loads(Path(backup).read_text())
    by = {n: _blank() for n in ac.ALL_LEAGUES}
    overall = _blank()
    for p in rows:
        if alt_of(p) is None or p.get("outcome") == "_removed":
            continue
        name = _CODE_TO_NAME.get(lt.norm_sport(p))
        if name is None:                                    # an adjusted pick outside the 12 covered leagues never reaches the card
            continue
        tally(p, by[name]); tally(p, overall)
    order = sorted((n for n in by if by[n]["n"]), key=lambda n: (-by[n]["n"], n))
    return {"leagues": by, "overall": overall, "rows": [(n, by[n]) for n in order]}


def pct(w: int, l: int) -> str:
    """The app's _altPct: whole-number win % of wins + losses, '--' with none."""
    return f"{round(w / (w + l) * 100)}%" if (w + l) else "--"


def rec(w: int, l: int, p: int = 0) -> str:
    return f"{w}-{l}" + (f"-{p}" if p else "")
