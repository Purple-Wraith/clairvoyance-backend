#!/usr/bin/env python3
"""Scorecard for alternate-line locks (CFB / NFL / NBA spreads and O/Us, and hockey totals, moved to a safer line; see ALT_LINE_CFG in scripts/auto_lock_settle.py).

For every SETTLED pick that carries an `altLine` it grades two things from the final score: the pick as locked (the alternate line) and the same bet at the
POSTED line it was moved from.  Then it prints win rate (LOCKED ADJ. = the line it was locked at) and units for both, how many results the shift flipped either way, and the average win probability
the engine expected, so the cushion is judged on real results, not on the backtest.

  python3 scripts/alt_line_scorecard.py                # live ledger (Supabase, read-only); falls back to docs/picks_backup.json
  python3 scripts/alt_line_scorecard.py --backup       # committed backup only
  python3 scripts/alt_line_scorecard.py --sport CFB    # one sport
  python3 scripts/alt_line_scorecard.py --no-espn      # only picks the automation has already settled (default also grades PENDING picks whose game ESPN
                                                       # shows as final -- marked provisional -- so you do not wait for the next settle pass)

Prices: alternate-line prices are ESTIMATES (fair + vig, priceSource 'estimated'), so units here are indicative; the posted-line price is not stored, so the
posted line is graded at the original price when the pick stored it (altLine.postedDec, newer picks) else the standard -110 (1.909).  Win rate is the clean number.  Small samples: read n first.
"""
from __future__ import annotations
import argparse, json, re, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STD_DEC = 1.909
ESPN = {"NFL": "football/nfl", "CFB": "football/college-football", "NBA": "basketball/nba"}
ESPN_ABBR = {"WAS": "WSH", "WSH": "WSH", "JAC": "JAX"}     # ledger code -> ESPN code where they differ
_score_cache: dict = {}


def espn_finals(sport: str, date: str) -> dict:
    """{(away, home): (away_score, home_score)} for games ESPN shows as FINAL on `date` (YYYY-MM-DD)."""
    key = (sport, date)
    if key in _score_cache:
        return _score_cache[key]
    out: dict = {}
    path = ESPN.get(sport)
    if path:
        try:
            q = "?dates=" + date.replace("-", "") + ("&groups=80&limit=300" if sport == "CFB" else "")
            d = json.load(urllib.request.urlopen(urllib.request.Request(f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard{q}", headers={"User-Agent": "Mozilla/5.0"}), timeout=30))
            for e in d.get("events", []):
                if e["status"]["type"]["state"] != "post":
                    continue
                c = {x["homeAway"]: x for x in e["competitions"][0]["competitors"]}
                out[(c["away"]["team"]["abbreviation"].upper(), c["home"]["team"]["abbreviation"].upper())] = (float(c["away"]["score"]), float(c["home"]["score"]))
        except Exception as exc:
            print(f"[scorecard] ESPN {sport} {date} unavailable ({exc.__class__.__name__})", file=sys.stderr)
    _score_cache[key] = out
    return out


def provisional(p: dict) -> bool:
    """Fill a PENDING pick's score from ESPN when its game is final; sets hScore/aScore + a provisional outcome at the line it was locked at."""
    fin = espn_finals((p.get("sport") or "").upper(), p.get("date") or "")
    h, a = str(p.get("hA") or "").upper(), str(p.get("awA") or "").upper()
    sc = fin.get((ESPN_ABBR.get(a, a), ESPN_ABBR.get(h, h)))
    if not sc:
        return False
    p["aScore"], p["hScore"] = sc
    res = grade(p, float(p["altLine"]["line"]))
    if res is None:
        return False
    p["outcome"], p["_provisional"] = res, True
    return True


def load(backup_only: bool) -> tuple[list[dict], str]:
    if not backup_only:
        try:
            src = (ROOT / "docs" / "app.html").read_text(encoding="utf-8")
            url = re.search(r"const SUPABASE_URL='([^']+)'", src).group(1)
            key = re.search(r"const SUPABASE_KEY='([^']+)'", src).group(1)
            req = urllib.request.Request(url + "/rest/v1/bets?select=id,outcome,raw&raw->>altLine=not.is.null&limit=1000", headers={"apikey": key, "Authorization": "Bearer " + key})
            rows = json.load(urllib.request.urlopen(req, timeout=60))
            picks = []
            for r in rows:
                p = dict(r["raw"]); p["outcome"] = r.get("outcome") or p.get("outcome"); picks.append(p)
            return picks, "Supabase (live)"
        except Exception as exc:  # fall through to the backup
            print(f"[scorecard] Supabase unavailable ({exc.__class__.__name__}); using docs/picks_backup.json", file=sys.stderr)
    bk = json.loads((ROOT / "docs" / "picks_backup.json").read_text())
    return [p for p in bk if p.get("altLine")], "docs/picks_backup.json"


def margin_total(p: dict):
    """-> (home_score, away_score) or None."""
    h, a = p.get("hScore"), p.get("aScore")
    try:
        return float(h), float(a)
    except (TypeError, ValueError):
        return None


def grade(p: dict, line: float, bet_override: str | None = None):
    """Result of the pick's bet at `line`: 'win' | 'loss' | 'push' | None (cannot grade). `bet_override` grades a different bet text (the POSTED pick of a side-flipped total)."""
    sc = margin_total(p)
    if sc is None:
        return None
    h, a = sc
    bt = (p.get("betType") or "").upper()
    bet = str(bet_override if bet_override is not None else (p.get("betOn") or ""))
    if bt in ("OU", "O/U", "TOTAL"):
        side = "OVER" if bet.upper().startswith("OVER") else "UNDER"
        tot = h + a
        if tot == line:
            return "push"
        return "win" if ((tot > line) == (side == "OVER")) else "loss"
    if bt == "SPREAD":
        team = re.match(r"\s*([A-Za-z0-9&.'-]+)", bet).group(1).upper()
        is_home = team == str(p.get("hA") or "").upper()
        if not is_home and team != str(p.get("awA") or "").upper():
            return None
        margin = (h - a) if is_home else (a - h)           # the picked team's margin
        v = margin + line
        return "push" if v == 0 else ("win" if v > 0 else "loss")
    return None


def units(res: str | None, dec: float) -> float:
    return {"win": dec - 1, "loss": -1.0}.get(res or "", 0.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", action="store_true"); ap.add_argument("--sport"); ap.add_argument("--no-espn", action="store_true")
    a = ap.parse_args()
    picks, source = load(a.backup)
    if a.sport:
        picks = [p for p in picks if (p.get("sport") or "").upper() == a.sport.upper()]
    n_prov = 0
    if not a.no_espn:
        for p in picks:
            if p.get("outcome") == "pending" and provisional(p):
                n_prov += 1
    settled = [p for p in picks if p.get("outcome") in ("win", "loss", "push")]
    print(f"Alternate-line scorecard  ({source})\n  alternate-line picks: {len(picks)} | graded: {len(settled)} (of which {n_prov} provisional from ESPN finals, not yet settled by the automation) | still waiting on a final: {len(picks) - len(settled)}")
    rows = []
    for p in settled:
        al = p["altLine"]
        alt_res = grade(p, float(al["line"])); post_res = grade(p, float(al["posted"]), al.get("postedLabel") if al.get("flip") else None)   # a side-flipped total: the POSTED pick was the other side
        # trust the stored outcome for the alternate line; the recomputed one is a cross-check
        rows.append((p, p["outcome"], post_res, alt_res))
    if not rows:
        print("  nothing to grade yet.")
        return 0
    bad = [r for r in rows if r[3] and r[3] != r[1]]
    if bad:
        print(f"  WARNING: {len(bad)} pick(s) where the recomputed alternate-line result differs from the stored outcome: {[r[0]['id'] for r in bad][:3]}")
    groups: dict[str, list] = {"ALL": rows}
    for r in rows:
        groups.setdefault(f"{r[0].get('sport')} {r[0].get('betType')}", []).append(r)
    print(f"\n  {'group':<14}{'n':>4}  {'LOCKED ADJ. w%':>15}{'POSTED w%':>11}{'saved':>7}{'cost':>6}{'ADJ. u':>8}{'POSTED u':>9}{'expected':>9}")
    for g, rs in groups.items():
        n = len(rs)
        aw = sum(r[1] == "win" for r in rs); al_ = sum(r[1] == "loss" for r in rs)
        pw = sum(r[2] == "win" for r in rs); pl = sum(r[2] == "loss" for r in rs)
        saved = sum(r[1] == "win" and r[2] == "loss" for r in rs)    # the shift turned a loss into a win
        cost = sum(r[1] == "loss" and r[2] == "win" for r in rs)     # (cannot happen: a safer line never loses where the posted line wins)
        au = sum(units(r[1], float(r[0].get("decOdds") or STD_DEC)) for r in rs); pu = sum(units(r[2], float(r[0]["altLine"].get("postedDec") or STD_DEC)) for r in rs)   # original price when the pick stored it
        exp = sum(float(r[0].get("winProb") or 0) for r in rs) / n
        f = lambda w, l: f"{w / (w + l) * 100:5.1f}%" if (w + l) else "   --"
        print(f"  {g:<14}{n:>4}  {f(aw, al_):>15}{f(pw, pl):>11}{saved:>7}{cost:>6}{au:>+8.1f}{pu:>+9.1f}{exp * 100:>8.1f}%")
    print("\n  saved = lost at the posted line but won at the locked adj. line;  cost = the reverse.  ADJ. u uses the estimated adjusted-line price, POSTED u uses the original price (or -110 if not stored).")
    print("  Judge the shift on win%; the price is bought with the cushion, so units can be lower even when win% is higher.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
