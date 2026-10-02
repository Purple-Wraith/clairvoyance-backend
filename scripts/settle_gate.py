#!/usr/bin/env python3
"""CHEAP gate for the European-hockey settle slots of .github/workflows/auto-lock-settle.yml.

Every heavy settle run pulls the whole bet ledger from Supabase (egress!).  These gated slots exist only to settle a European
hockey pick soon after its game ends, so they must cost NOTHING unless there is something to settle.  This script decides that
from files already in the checkout -- NO Supabase, NO network, NO browser:

    docs/picks_backup.json            the ledger as of the last full pass (pending picks live here)
    docs/{shl,liiga,nla,extraliga}_schedule.json   the games (a freshly re-scraped /results/ merge, see refresh_hockey_results.py)

Two stages (exit code 0 = GO, 1 = nothing to do; any internal error fails OPEN = GO, because skipping a settle silently is worse
than one extra ledger pull):

  --stage candidates   Is there ANY pending European-hockey pick (dated within the last LOOKBACK_DAYS Mountain days up to today)
                       whose game might be over?  A pick qualifies when its matching schedule game is already final, or started
                       at least MIN_GAME_MIN minutes ago (so a re-scrape could find the final), or no matching game is in the file
                       at all (unknown -> can't rule it out).  If none: the whole run is a no-op -- no browser install, no scrape,
                       no Supabase.
  --stage final        After the results re-scrape: is there a pending pick whose game is NOW final (state 'post', numeric scores,
                       same Mountain date as the pick) AND that the app's own settle logic can grade?  Only then does the heavy
                       settle job (one ledger pull) run.  Mirrors _autoSettleFlashscoreHockey in docs/app.html line for line:
                       team match in either orientation, pick.date == the game's Mountain date, and the _classifyBetOn rules
                       (OU needs a trailing number; a spread's team must be one of the two teams; a moneyline's betOn must equal a
                       team name, optionally with the manual " ML" suffix) -- a pick the app would skip cannot re-fire this gate forever.

KNOWN LIMIT (by design, documented): picks_backup.json is rewritten by every full pass (settle/digest/lock runs of the main
workflow, which now refresh it AFTER settling/locking too) but NOT by the dedicated lock workflows (european-lock-early,
hockey-lock-evening, ...).  A pick locked by one of those after the last full pass is invisible to this gate until the next full
pass refreshes the backup; the always-on slots (22:44Z, 05:27Z, 18:21Z digest) still settle everything regardless.

    python3 scripts/settle_gate.py --stage candidates
    python3 scripts/settle_gate.py --stage final
    python3 scripts/settle_gate.py --stage final --now 2026-10-02T22:20:00Z --verbose     # frozen clock
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
MT = ZoneInfo("America/Denver")

# sport tag (what docs/app.html _normSport returns for these leagues) -> schedule file
SCHEDULE_FILES = {"SHL": "shl_schedule.json", "LIIGA": "liiga_schedule.json", "NLA": "nla_schedule.json",
                  "EXTRALIGA": "extraliga_schedule.json"}
# A pick older than this many Mountain days is not this gate's business (the always-on slots settle every pending date).
LOOKBACK_DAYS = 7
# A hockey game is rarely over before this many minutes after its start -- below it the re-scrape cannot find a final yet.
MIN_GAME_MIN = 120

_BROAD = {"FOOTBALL", "BASKETBALL", "HOCKEY", "SOCCER", "BASEBALL"}


def sport_tag(p: dict):
    """The European-hockey tag of a ledger pick, or None. Mirrors the relevant part of _normSport in docs/app.html: the explicit
    sport field wins unless it is a broad bucket (HOCKEY...) and a league is present, in which case the league wins."""
    sport = str(p.get("sport") or "").upper().strip()
    league = str(p.get("league") or "").upper().strip()
    raw = league if (sport in _BROAD and league) else (sport or league)
    return raw if raw in SCHEDULE_FILES else None


def classify_bet_on(bet_on) -> dict:
    """Port of _classifyBetOn (docs/app.html)."""
    s = str(bet_on or "").strip()
    if re.match(r"^(over|under)", s, re.I):
        m = re.search(r"(\d+\.?\d*)\s*$", s)
        if m:
            return {"type": "OU", "over": bool(re.match(r"^over", s, re.I)), "line": float(m.group(1))}
    m = re.match(r"^(.+?)\s+([+-]\d+\.?\d*)\s*$", s)
    if m:
        return {"type": "SPREAD", "team": m.group(1).strip(), "line": float(m.group(2))}
    return {"type": "ML"}


def app_can_grade(pick: dict, game: dict) -> bool:
    """True when _autoSettleFlashscoreHockey would set an outcome for this pick against this FINAL game (mirror, see module doc)."""
    hn, an = game.get("homeName"), game.get("awayName")
    cls = classify_bet_on(pick.get("betOn"))
    if cls["type"] == "OU":
        return True
    if cls["type"] == "SPREAD" and cls["team"] in (hn, an):
        return True
    b = pick.get("betOn")
    return b in (hn, an) or b in (f"{hn} ML", f"{an} ML")  # the app accepts the manual "Team ML" spelling too (see _autoSettleFlashscoreHockey)


def game_mt_date(game: dict):
    try:
        dt = datetime.strptime(str(game.get("date")), "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None, None
    return dt.astimezone(MT).strftime("%Y-%m-%d"), dt


def is_final(game: dict) -> bool:
    return (game.get("state") == "post" and all(isinstance(game.get(k), (int, float)) and not isinstance(game.get(k), bool)
                                                for k in ("homeScore", "awayScore")))


def same_game(pick: dict, game: dict) -> bool:
    hn, an = game.get("homeName"), game.get("awayName")
    return (pick.get("hA") == hn and pick.get("awA") == an) or (pick.get("hA") == an and pick.get("awA") == hn)


def pending_picks(picks, today_mt: str):
    """Pending European-hockey picks dated [today - LOOKBACK_DAYS, today] (Mountain). -> list of (tag, pick)."""
    lo = (datetime.strptime(today_mt, "%Y-%m-%d") - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    out = []
    for p in picks or []:
        if not isinstance(p, dict) or p.get("outcome") != "pending":
            continue
        tag = sport_tag(p)
        d = p.get("date")
        if tag and isinstance(d, str) and lo <= d <= today_mt:
            out.append((tag, p))
    return out


def evaluate(picks, schedules: dict, now: datetime, stage: str) -> dict:
    """Pure decision. schedules: {tag: schedule doc ({'games': [...]})}. -> {"go": bool, "reasons": [...], "counts": {...}}."""
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    today_mt = now.astimezone(MT).strftime("%Y-%m-%d")
    pend = pending_picks(picks, today_mt)
    res = {"go": False, "stage": stage, "today": today_mt, "pending": len(pend), "reasons": []}
    for tag, p in pend:
        games = [g for g in ((schedules.get(tag) or {}).get("games") or []) if isinstance(g, dict)]
        matches = [g for g in games if same_game(p, g) and game_mt_date(g)[0] == p.get("date")]
        label = f"{tag} {p.get('awA')} @ {p.get('hA')} [{p.get('betOn')}] {p.get('date')}"
        if stage == "final":
            for g in matches:
                if is_final(g) and app_can_grade(p, g):
                    res["go"] = True
                    res["reasons"].append(f"FINAL {label}: {g['homeName']} {g['homeScore']} - {g['awayName']} {g['awayScore']}")
                    break
            continue
        # stage == "candidates": can this pick's game be over (or can we not tell)?
        if not matches:
            res["go"] = True
            res["reasons"].append(f"NO GAME IN FILE {label} -- cannot rule out a final")
            continue
        for g in matches:
            _d, start = game_mt_date(g)
            if is_final(g):
                res["go"] = True
                res["reasons"].append(f"ALREADY FINAL {label}")
                break
            if start is not None and (now - start) >= timedelta(minutes=MIN_GAME_MIN):
                res["go"] = True
                res["reasons"].append(f"STARTED {int((now - start).total_seconds() // 60)}m AGO {label}")
                break
    return res


def load_inputs(docs: Path):
    picks = json.loads((docs / "picks_backup.json").read_text())
    schedules = {}
    for tag, fn in SCHEDULE_FILES.items():
        try:
            schedules[tag] = json.loads((docs / fn).read_text())
        except Exception:
            schedules[tag] = {"games": []}
    return picks, schedules


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["candidates", "final"], required=True)
    ap.add_argument("--docs", default=str(DOCS), help="docs directory (default: this checkout's)")
    ap.add_argument("--now", default=None, help="frozen clock for tests, e.g. 2026-10-02T22:20:00Z")
    ap.add_argument("--verbose", action="store_true", help="print every reason, not just the first few")
    a = ap.parse_args(argv)
    try:
        now = (datetime.fromisoformat(a.now.replace("Z", "+00:00")) if a.now else datetime.now(timezone.utc))
        picks, schedules = load_inputs(Path(a.docs))
        r = evaluate(picks, schedules, now, a.stage)
    except Exception as exc:  # fail OPEN: one extra ledger pull beats a silently skipped settle
        print(f"settle_gate[{a.stage}]: GO (gate error, failing open): {exc}")
        return 0
    shown = r["reasons"] if a.verbose else r["reasons"][:6]
    print(f"settle_gate[{a.stage}]: {'GO' if r['go'] else 'NO-OP'} -- {r['pending']} pending European-hockey pick(s) in "
          f"[{r['today']}-{LOOKBACK_DAYS}d, {r['today']}]; {len(r['reasons'])} qualifying")
    for line in shown:
        print(f"  - {line}")
    if len(r["reasons"]) > len(shown):
        print(f"  ... and {len(r['reasons']) - len(shown)} more")
    return 0 if r["go"] else 1


if __name__ == "__main__":
    sys.exit(main())
