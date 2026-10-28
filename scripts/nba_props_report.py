#!/usr/bin/env python3
"""NBA player-props REVIEW (owner request 2026-10-28: "observe how player props do until 20 games are played in the NBA season").

NBA props are still generated, locked and settled by the engine, but they are OFF the subscriber emails (`EMAIL_NBA_PROPS = False` in auto_lock_settle.py) until the season has ~20 games of data.
This module is the observation side of that:

  * games_status()   how many games each team has played (docs/nba_schedule.json) and when the median team reaches TARGET_GAMES;
  * summarize()      the settled props from the committed ledger backup (docs/picks_backup.json -- no Supabase call): record vs the model's own probability and the price break-even,
                     by stat / OVER-UNDER / adjusted-vs-plain line / week / before-vs-after the 10-26 featured-player gate, the posted-line result of every adjusted pick, concentration
                     (several props on one player or game) and DATA QUALITY (settled picks with no stored box-score stat, stored results the stat disagrees with);
  * weekly_section_html()   section 6 of the Monday Weekly Health Digest (no extra email);
  * check_milestone()       the ONE-TIME "20 games reached -- turn the prop emails back on" email, run by the daily health check (state: data/nba_props_milestone.json).

Read-only CLI:   python3 scripts/nba_props_report.py [--ledger FILE] [--schedule FILE] [--json]
Grading rules mirror scripts/alt_line_scorecard.py and docs/app.html _altGradeAt: a prop grades on the player's stored box-score stat (`playerResult`); the posted-line result of an adjusted pick is
only trusted when re-grading the LOCKED line from the same stat reproduces the stored outcome.
"""
from __future__ import annotations

import html as _html
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

SEASON_START = "2026-10-01"       # NBA picks of this season (last season's playoff props are older)
GATE_DATE = "2026-10-26"          # featured-player gate (team top-3 PTS / top-2 REB, AST with 26+ mpg) pushed this day
TARGET_GAMES = 20                 # prop emails resume once the MEDIAN team has played this many games
MIN_SAMPLE = 50                   # the owner's rule of thumb before judging anything (project_nba_props_rules)
STATE_REL = "data/nba_props_milestone.json"
EMAIL_SWITCH = "EMAIL_NBA_PROPS = False  ->  True   (scripts/auto_lock_settle.py)"


# ── games played ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def team_games(schedule: dict) -> Counter:
    """Final regular-season games each team has played, from the ESPN schedule file."""
    c: Counter = Counter()
    for g in (schedule or {}).get("games", []):
        if g.get("state") != "post" or g.get("preseason") or g.get("seasonType") not in (2, None):
            continue
        for k in ("home", "away"):
            if g.get(k):
                c[g[k]] += 1
    return c


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    return 0.0 if not n else (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2)


def games_status(schedule: dict, target: int = TARGET_GAMES, today: date | None = None) -> dict:
    c = team_games(schedule)
    days = sorted({g["day"] for g in (schedule or {}).get("games", []) if g.get("state") == "post" and not g.get("preseason") and g.get("seasonType") in (2, None) and g.get("day")})
    out = {"teams": len(c), "target": target, "ready": False, "min": 0, "median": 0.0, "max": 0, "at_target": 0,
           "first_day": days[0] if days else None, "last_day": days[-1] if days else None, "per_day": None, "projected": None}
    if not c:
        return out
    vals = list(c.values())
    out.update(min=min(vals), median=_median(vals), max=max(vals), at_target=sum(1 for v in vals if v >= target))
    out["ready"] = out["median"] >= target
    d0, d1 = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    span = max(1, (d1 - d0).days + 1)
    out["per_day"] = (sum(vals) / len(vals)) / span                  # games per team per calendar day since the first final
    if not out["ready"] and out["per_day"]:
        need = target - out["median"]
        out["projected"] = (d1 + timedelta(days=math.ceil(need / out["per_day"]))).isoformat()
    return out


# ── grading ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _dir(p: dict) -> str | None:
    m = re.search(r"\b(OVER|UNDER)\b", str(p.get("betOn") or ""), re.I)
    return m.group(1).upper() if m else None


def _stat(p: dict) -> str:
    m = re.search(r"\b(PTS\+REB\+AST|PTS|REB|AST|3PM|STL|BLK)\b", str(p.get("betOn") or ""), re.I)
    return m.group(1).upper() if m else "?"


def _num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def grade_at(p: dict, line: float) -> str | None:
    v, d = _num(p.get("playerResult")), _dir(p)
    if v is None or d is None or line is None:
        return None
    return "push" if v == line else ("win" if ((v > line) == (d == "OVER")) else "loss")


def locked_line(p: dict) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*$", str(p.get("betOn") or ""))
    return float(m.group(1)) if m else _num(p.get("line"))


def posted_result(p: dict) -> str | None:
    """Result at the POSTED line of an adjusted pick, or None when there is no stat / the stat does not reproduce the stored result at the locked line."""
    a = p.get("altLine")
    if not isinstance(a, dict) or _num(a.get("posted")) is None or p.get("outcome") not in ("win", "loss", "push"):
        return None
    at_locked = grade_at(p, locked_line(p))
    if at_locked is None or at_locked != p["outcome"]:
        return None
    return grade_at(p, _num(a["posted"]))


def wilson(w: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    ph = w / n
    d = 1 + z * z / n
    c = ph + z * z / (2 * n)
    r = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return max(0.0, (c - r) / d), min(1.0, (c + r) / d)


# ── summary ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def load_props(ledger: list[dict]) -> list[dict]:
    return [p for p in ledger if isinstance(p, dict) and p.get("sport") == "NBA" and p.get("betType") == "PROP"
            and (p.get("date") or "") >= SEASON_START and p.get("outcome") != "_removed"]


def _rec(rows: list[dict]) -> dict:
    wl = [p for p in rows if p.get("outcome") in ("win", "loss")]
    w = sum(1 for p in wl if p["outcome"] == "win")
    n = len(wl)
    probs = [_num(p.get("winProb")) for p in wl if _num(p.get("winProb")) is not None]
    be = [1 / d for d in (_num(p.get("decOdds")) for p in wl) if d and d > 1]
    units = sum(((_num(p.get("decOdds")) or 0) - 1) if p["outcome"] == "win" else -1 for p in wl if (_num(p.get("decOdds")) or 0) > 1)
    lo, hi = wilson(w, n)
    return {"n": n, "w": w, "l": n - w, "push": sum(1 for p in rows if p.get("outcome") == "push"), "pending": sum(1 for p in rows if p.get("outcome") == "pending"),
            "pct": (w / n) if n else None, "lo": lo, "hi": hi, "model": (sum(probs) / len(probs)) if probs else None, "breakeven": (sum(be) / len(be)) if be else None, "units": units}


def _week_label(d: str, start: date) -> str:
    k = (date.fromisoformat(d) - start).days // 7
    a = start + timedelta(days=7 * k)
    b = a + timedelta(days=6)
    return f"{a.strftime('%b')} {a.day}-{b.strftime('%b ') if b.month != a.month else ''}{b.day}"


def summarize(ledger: list[dict]) -> dict:
    props = load_props(ledger)
    start = min((date.fromisoformat(p["date"]) for p in props), default=date.fromisoformat(SEASON_START))
    settled = [p for p in props if p.get("outcome") in ("win", "loss", "push")]
    by = lambda key: {k: _rec(v) for k, v in sorted(_group(props, key).items())}   # noqa: E731
    adj = [p for p in props if isinstance(p.get("altLine"), dict)]
    posted = [(p, posted_result(p)) for p in adj if p.get("outcome") in ("win", "loss")]
    graded = [(p, r) for p, r in posted if r is not None]
    missing = [p for p in props if p.get("outcome") in ("win", "loss") and _num(p.get("playerResult")) is None and not p.get("voidReason")]
    mismatch = []
    for p in props:
        if p.get("outcome") in ("win", "loss", "push") and _num(p.get("playerResult")) is not None:
            g = grade_at(p, locked_line(p))
            if g is not None and g != p["outcome"]:
                mismatch.append(p)
    per_player = Counter(p.get("player") or p.get("betOn") for p in props)
    per_day_player = Counter((p.get("date"), p.get("player")) for p in props)
    per_game = Counter((p.get("date"), tuple(sorted([str(p.get("hA")), str(p.get("awA"))]))) for p in props)
    return {
        "total": len(props), "overall": _rec(props),
        "by_stat": by(lambda p: _stat(p)), "by_dir": by(lambda p: _dir(p) or "?"),
        "by_line": by(lambda p: "ADJUSTED line" if isinstance(p.get("altLine"), dict) else "plain line"),
        "by_week": by(lambda p: _week_label(p["date"], start)),
        "by_gate": by(lambda p: "since the featured-player gate (10-26)" if p["date"] >= GATE_DATE else "before the gate"),
        "adjusted": {"n": len(adj), "graded": len(graded),
                     "locked_w": sum(1 for p, _ in graded if p["outcome"] == "win"),
                     "posted_w": sum(1 for _, r in graded if r == "win"), "posted_l": sum(1 for _, r in graded if r == "loss"), "posted_push": sum(1 for _, r in graded if r == "push"),
                     "saved": sum(1 for p, r in graded if p["outcome"] == "win" and r == "loss"), "cost": sum(1 for p, r in graded if p["outcome"] == "loss" and r == "win"),
                     "ungraded": len(posted) - len(graded)},
        "quality": {"settled": len(settled), "missing_stat": [p["id"] for p in missing], "mismatch": [p["id"] for p in mismatch],
                    "void": sum(1 for p in props if p.get("voidReason"))},
        "concentration": {"players_3plus": sorted([(k, v) for k, v in per_player.items() if v >= 3], key=lambda x: -x[1]),
                          "same_player_same_day": sum(1 for v in per_day_player.values() if v > 1),
                          "most_props_one_game": max(per_game.values(), default=0)},
    }


def _group(rows: list[dict], key) -> dict:
    g: dict = defaultdict(list)
    for p in rows:
        g[key(p)].append(p)
    return g


# ── rendering ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _pct(x, d=0):
    return "-" if x is None else f"{x * 100:.{d}f}%"


def _line(label: str, r: dict) -> str:
    rec = f"{r['w']}-{r['l']}" + (f"-{r['push']}" if r["push"] else "")
    pend = f", {r['pending']} pending" if r["pending"] else ""
    return f"{label:<42}{rec:<9}{_pct(r['pct']):>5}  (model {_pct(r['model'])}, break-even {_pct(r['breakeven'])}, {r['units']:+.1f}u est.){pend}"


def verdict(s: dict) -> list[str]:
    o = s["overall"]
    out = []
    if o["n"] < MIN_SAMPLE:
        out.append(f"Sample is {o['n']} settled W/L props -- below the {MIN_SAMPLE} you set as the minimum to judge anything; read the splits as direction only.")
    if o["n"]:
        out.append(f"Overall {o['w']}-{o['l']} ({_pct(o['pct'])}, 95% range {_pct(o['lo'])}-{_pct(o['hi'])}) vs the model's own {_pct(o['model'])} and a {_pct(o['breakeven'])} break-even at the prices stored "
                   f"(adjusted-line prices are estimates).")
    q = s["quality"]
    if q["missing_stat"]:
        out.append(f"DATA: {len(q['missing_stat'])} settled prop(s) have no stored box-score stat (the next settle pass fills them): " + ", ".join(i[:40] for i in q["missing_stat"][:5]))
    if q["mismatch"]:
        out.append(f"DATA: {len(q['mismatch'])} stored result(s) disagree with their own stored stat -- look at: " + ", ".join(i[:40] for i in q["mismatch"][:5]))
    c = s["concentration"]
    if c["players_3plus"]:
        out.append("Concentration: " + ", ".join(f"{k} x{v}" for k, v in c["players_3plus"][:5]) + f"; {c['same_player_same_day']} player-day(s) with 2+ props; most props on one game: {c['most_props_one_game']}.")
    return out


def render_text(s: dict, g: dict) -> str:
    L = ["NBA PLAYER PROPS REVIEW", "=" * 60]
    L.append(f"Games played: median team {g['median']:g} of {g['target']} (min {g['min']}, max {g['max']}; {g['at_target']}/{g['teams']} teams at {g['target']}).")
    if g["ready"]:
        L.append(f"-> {g['target']}-GAME MARK REACHED: prop emails can resume ({EMAIL_SWITCH}).")
    elif g["projected"]:
        L.append(f"-> median team reaches {g['target']} around {g['projected']} at {g['per_day'] * 7:.1f} games/team/week.")
    L += ["", f"Props this season: {s['total']} locked.", _line("ALL", s["overall"])]
    for title, key in (("By stat", "by_stat"), ("By side", "by_dir"), ("By line type", "by_line"), ("By week", "by_week"), ("Featured-player gate", "by_gate")):
        L += ["", title + ":"] + ["  " + _line(k, r) for k, r in s[key].items()]
    a = s["adjusted"]
    L += ["", f"Adjusted-line picks: {a['n']} locked, {a['graded']} gradable on the posted line ({a['ungraded']} settled but no stat yet)."]
    if a["graded"]:
        L.append(f"  on the ADJUSTED line {a['locked_w']}-{a['graded'] - a['locked_w']}  |  on the POSTED line {a['posted_w']}-{a['posted_l']}" + (f"-{a['posted_push']}" if a["posted_push"] else "")
                 + f"  |  cushion saved {a['saved']}, cost {a['cost']}")
    L += ["", "Notes:"] + ["  - " + v for v in verdict(s)]
    return "\n".join(L)


def weekly_section_html(root: Path | None = None) -> str:
    """Section 6 of the Monday Weekly Health Digest.  Never raises: the digest must not be lost to a props problem."""
    root = root or ROOT
    try:
        ledger = json.loads((root / "docs" / "picks_backup.json").read_text())
        sched = json.loads((root / "docs" / "nba_schedule.json").read_text())
        s, g = summarize(ledger), games_status(sched)
    except Exception as exc:
        return f"<h3>6. NBA props review</h3><p><em>Unavailable: {_html.escape(str(exc))}</em></p>"
    e = _html.escape
    if g["ready"]:
        head = f'<p style="color:#0a0"><strong>{g["target"]}-game mark reached</strong> (median team {g["median"]:g}): prop emails can resume -- {e(EMAIL_SWITCH)}.</p>'
    else:
        proj = f", median team reaches {g['target']} around <strong>{g['projected']}</strong>" if g["projected"] else ""
        head = f'<p>Games played: median team <strong>{g["median"]:g}</strong> of {g["target"]} (min {g["min"]}, max {g["max"]}){proj}. Prop emails stay OFF until then.</p>'
    rows = ["<tr><th align=left>Slice</th><th>W-L</th><th>Win %</th><th>Model</th><th>Break-even</th></tr>"]
    for label, r in [("ALL props", s["overall"])] + [(k, r) for key in ("by_stat", "by_dir", "by_line", "by_gate") for k, r in s[key].items()]:
        rec = f"{r['w']}-{r['l']}" + (f"-{r['push']}" if r["push"] else "")
        rows.append(f"<tr><td>{e(label)}</td><td align=center>{rec}</td><td align=center>{_pct(r['pct'])}</td><td align=center>{_pct(r['model'])}</td><td align=center>{_pct(r['breakeven'])}</td></tr>")
    a = s["adjusted"]
    adj = (f"<p>Adjusted-line picks gradable on the posted line: {a['graded']} -- adjusted line {a['locked_w']}-{a['graded'] - a['locked_w']}, posted line {a['posted_w']}-{a['posted_l']} "
           f"(cushion saved {a['saved']}, cost {a['cost']}).</p>") if a["graded"] else ""
    notes = "".join(f"<li>{e(v)}</li>" for v in verdict(s))
    return f"<h3>6. NBA props review</h3>{head}<table style='border-collapse:collapse'>{''.join(rows)}</table>{adj}<ul>{notes}</ul>"


# ── 20-game milestone (one-time email) ─────────────────────────────────────────────────────────────────────────────────
def check_milestone(root: Path | None = None, send=None, to: str = "", today: str | None = None, write: bool = True) -> str:
    """-> 'not-yet' | 'already-sent' | 'sent' | 'failed' | 'no-recipient'.  Emails ONCE when the median team reaches TARGET_GAMES; the state file keeps it from ever repeating."""
    root = root or ROOT
    sched = json.loads((root / "docs" / "nba_schedule.json").read_text())
    g = games_status(sched)
    state_path = root / STATE_REL
    try:
        state = json.loads(state_path.read_text())
    except Exception:
        state = {}
    if state.get("sent"):
        return "already-sent"
    if not g["ready"]:
        return "not-yet"
    if not to:
        return "no-recipient"
    if send is None:
        from _gmail_email import send_email as send
    try:
        summary_html = weekly_section_html(root)
    except Exception:
        summary_html = ""
    body = (f'<div style="font-family:monospace;font-size:14px;color:#1a1a2e"><p><strong>The NBA season has reached {g["target"]} games</strong> (median team {g["median"]:g}; '
            f'min {g["min"]}, max {g["max"]}; {g["at_target"]}/{g["teams"]} teams at {g["target"]}).</p>'
            f"<p>This is the mark you set for bringing NBA player props back into the subscriber emails. They are still being generated, locked and tracked; only the emails are off.</p>"
            f"<p><strong>To turn them back on:</strong> {_html.escape(EMAIL_SWITCH)} -- or ask Claude to do it after looking at the review below.</p>{summary_html}"
            f"<p>(This is a one-time notice; it will not repeat.)</p></div>")
    ok, msg = send(f"Clairvoyance -- NBA props: {g['target']} games reached, emails can resume", to, body)
    if not ok:
        print(f"::warning::NBA props milestone email failed: {msg}")
        return "failed"
    if write:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"sent": today or datetime.now().strftime("%Y-%m-%d"), "median": g["median"], "min": g["min"], "max": g["max"], "target": g["target"]}, indent=2))
    return "sent"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="NBA player-props review (read-only).")
    ap.add_argument("--ledger", default=str(ROOT / "docs" / "picks_backup.json"))
    ap.add_argument("--schedule", default=str(ROOT / "docs" / "nba_schedule.json"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    summ, gs = summarize(json.loads(Path(a.ledger).read_text())), games_status(json.loads(Path(a.schedule).read_text()))
    print(json.dumps({"games": gs, "props": summ}, indent=1, default=str) if a.json else render_text(summ, gs))
