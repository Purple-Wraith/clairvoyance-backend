#!/usr/bin/env python3
"""IMPACT NOTE (analysis only; writes nothing): how many of the engine's past automatically-locked NHL picks would the confirmed-goalie gate (scripts/lineups.py)
have held or repriced, if the goalie recorded in docs/nhl_schedule.json had been known at the 45-minute sweep?

    python3 scripts/backtest_lineup_gate.py            # prints the table

Inputs (all in the repo): docs/picks_backup.json (the locked picks: winProb, decOdds, modelProb, blendAlpha, the "Projected a-b" line in `reasoning`),
docs/nhl_schedule.json (games[].goalies: ESPN's per-side starter, kept after puck drop; 'confirmed' = announced), docs/data.json (MoneyPuck goalie rows = what the
model rates each team on).

CAVEATS, stated so the numbers are not over-read:
  * The model's goalie rating is TODAY's MoneyPuck table, not the table as it stood on each lock day (the repo keeps no history of it), so "the goalie the model
    assumed" is approximate -- early-season games-played counts move quickly.
  * `goalies` in nhl_schedule.json is the last value ESPN showed, which can differ from the goalie who actually played (ESPN's own 'confirmed' tag is not infallible),
    and it is a snapshot of the schedule file, not necessarily what the sweep would have seen 45 minutes out.
  * Only picks with lockOrigin 'auto' since 2026-10-03 (when real market prices and the blend began) are used; n is small.
Two settings are reported side by side: the default (a market-blended pick moves only by the model's (1 - alpha) share of the swing) and NHL_STALE_MARKET_ASSUME
(the posted price is assumed not to know the goalie news yet, so the full model-level swing applies).
"""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import lineups as L  # noqa: E402
import auto_lock_settle as A  # noqa: E402

SINCE = "2026-10-03"
LEAD_MIN = 45


def side_of(p: dict) -> str:
    bt, on = str(p.get("betType") or "").upper(), str(p.get("betOn") or "").upper()
    if on.startswith("OVER"):
        return "over"
    if on.startswith("UNDER"):
        return "under"
    if bt in ("SPREAD", "RL", "PL") or re.search(r"[+-]\d+(\.\d+)?$", on):
        return "plDog" if "+" in on.split()[-1] else "plFav"
    return "mlFav"


def leg_from_pick(p: dict) -> dict:
    m = re.search(r"Projected\s+([0-9.]+)\s*[–-]\s*([0-9.]+)", p.get("reasoning") or "")
    mc = f"MC PROJ: {p['awA']} {m.group(1)} – {p['hA']} {m.group(2)} (Total)" if m else None
    prob, dec = float(p["winProb"]), float(p["decOdds"])
    q = {"kind": "GAME", "sport": "NHL", "hA": p["hA"], "awA": p["awA"], "side": side_of(p), "label": p["betOn"], "prob": prob, "dec": dec,
         "evVal": prob * dec - 1, "startMs": p["startMs"], "mcSummary": mc, "modelProb": p.get("modelProb"), "marketProb": p.get("marketProb"),
         "blendAlpha": p.get("blendAlpha"), "priceSource": p.get("priceSource")}
    q["tierN"] = A.lineup_requalify(q, prob, q["evVal"])[1]
    return q


def scoreboard_for(game: dict) -> dict:
    """A scoreboard payload (the shape ESPN serves) rebuilt from the schedule file's record of one game."""
    comps = []
    for side in ("home", "away"):
        g = (game.get("goalies") or {}).get(side)
        c = {"homeAway": side, "team": {"abbreviation": game[side]}}
        if g:
            c["probables"] = [{"name": "probableStartingGoalie", "status": {"type": "confirmed" if g["status"] == "confirmed" else "expected"},
                               "athlete": {"fullName": g["name"]}}]
        comps.append(c)
    return {"events": [{"id": str(game["id"]), "date": game["date"], "competitions": [{"status": {"type": {"state": "pre"}}, "competitors": comps}]}]}


def main() -> int:
    picks = json.loads((ROOT / "docs" / "picks_backup.json").read_text())
    sched = json.loads((ROOT / "docs" / "nhl_schedule.json").read_text())["games"]
    model = L.ModelData.load(ROOT / "docs")
    rows = [p for p in picks if p.get("sport") == "NHL" and p.get("lockOrigin") == "auto" and str(p.get("date")) >= SINCE and p.get("startMs") and p.get("decOdds")]
    print(f"NHL auto-locked picks since {SINCE}: {len(rows)}  (settled: {sum(1 for p in rows if p.get('outcome') in ('win', 'loss'))})")
    res = {"default": [], "stale": []}
    n_conf = n_diff = 0
    for p in rows:
        start = float(p["startMs"])
        game = next((g for g in sched if g["home"] == p["hA"] and g["away"] == p["awA"] and abs(L.parse_iso_ms(g["date"][:16] + ":00Z") - start) < 3 * 3600e3), None)
        if not game or not game.get("goalies"):
            continue
        sides = game["goalies"]
        conf = all((sides.get(s) or {}).get("status") == "confirmed" for s in ("home", "away"))
        n_conf += conf
        for key, stale in (("default", False), ("stale", True)):
            with mock.patch.object(L, "NHL_STALE_MARKET_ASSUME", stale):
                ctx = L.LineupContext(L.LineupSource(http_get=lambda url, g=game: copy.deepcopy(scoreboard_for(g))), model, A.lineup_requalify)
                d = L.assess_leg(leg_from_pick(p), ctx, start - LEAD_MIN * 60000)
            res[key].append((p, game, conf, d))
        d0 = res["default"][-1][3]
        if d0 and any("model rates" in n for n in d0["lineup"]["notes"]):
            n_diff += 1
    seen = len(res["default"])
    print(f"with a schedule goalie record: {seen};  both goalies 'confirmed' in the file: {n_conf};  a confirmed goalie differing from the model's rated primary: {n_diff}")
    for key, title in (("default", "DEFAULT: market-blended pick moves by (1 - alpha) of the model-level swing"),
                       ("stale", "STALE-MARKET: full model-level swing applied")):
        out = res[key]
        hold = [r for r in out if r[3] and r[3]["action"] == "hold"]
        rep = [r for r in out if r[3] and r[3]["action"] == "reprice"]
        adverse = [r for r in out if r[3] and (r[3]["lineup"].get("adj") or {}).get("swing")]
        print(f"\n{title}\n  assessed (in window, matched): {sum(1 for r in out if r[3])}  held: {len(hold)}  repriced: {len(rep)}")
        if adverse:
            sw = [abs(r[3]["lineup"]["adj"]["swing"]) * 100 for r in adverse]
            print(f"  adverse swings: n={len(sw)} mean {sum(sw) / len(sw):.2f} pts, max {max(sw):.2f} pts")
        for r in hold + rep:
            p, g, conf, d = r
            adj = d["lineup"]["adj"]
            print(f"  {d['action'].upper():8s} {p['date']} {p['awA']}@{p['hA']} {p['betOn']:<10s} {adj['pBefore'] * 100:.1f}% -> {adj['pAfter'] * 100:.1f}%  outcome={p.get('outcome')}"
                  f"  goalies {g['goalies']['away']['name']}({g['goalies']['away']['status'][:4]}) / {g['goalies']['home']['name']}({g['goalies']['home']['status'][:4]})")
        if hold:
            w = sum(1 for r in hold if r[0].get("outcome") == "win")
            l = sum(1 for r in hold if r[0].get("outcome") == "loss")
            print(f"  held picks' recorded results: {w}W-{l}L (pending {len(hold) - w - l})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
