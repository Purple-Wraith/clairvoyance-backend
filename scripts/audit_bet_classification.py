"""Audits the real Supabase bet ledger for cross-sport mistagged bets --
a real, historical bug where a bet's `sport`/`league` field doesn't match
either team's real roster for that sport (e.g. a real 2026-06-28 bet:
hA:"WSH", awA:"POR", betOn:"POR +1.5", sport:"NHL" -- Portland has no NHL
team; this is Washington Wizards vs Portland Trail Blazers, an NBA game).

ROOT CAUSE: docs/app.html's _classifyTeamsSport() (its team-code-set
scoring fallback, used when a lock path doesn't pass an explicit sport
hint) has real ambiguous collisions across MLB/NHL/NBA/WNBA -- several
3-letter codes are valid members of more than one league's roster set
(WSH is both NHL Capitals and WNBA Mystics; POR is only NBA; etc), and
ties break on fixed array order, not game context. docs/app.html already
has a live-schedule-preference fix and a dedicated audit tool for this
(auditBetClassification()/applyBetClassificationFix(), same file) --
but that tool only operates on a browser's local getP()/saveP() cache,
never the canonical Supabase ledger directly, so already-mistagged
historical rows in Supabase were never corrected by it.

This script is a direct Python port of auditBetClassification()'s own
detection logic (roster sets, _seasonImplausible's date windows,
_classifyTeamsSport's abbreviation-scoring fallback), run against the
real ledger instead of local storage. Same safety posture as that
in-app tool: DRY RUN ONLY by default, reports high-confidence mismatches
(impossible team codes for the stored sport, or an off-season date for
it) and never writes anything without --apply. Ambiguous-but-plausible
cases (the stored sport IS a valid home for both codes, in-season) are
never flagged -- matching the in-app tool's own "never auto-write a
guess" principle.

Usage:
  python3 scripts/audit_bet_classification.py            # report only
  python3 scripts/audit_bet_classification.py --apply    # also PATCH Supabase
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request

SUPABASE_URL = "https://vhwkbeblforpnliowpam.supabase.co"
SUPABASE_KEY = "sb_publishable_AEgqvpaIh0MWpDdXI35BYw_lD8HzBlM"

SCOPED_SPORTS = {"MLB", "NHL", "NBA", "WNBA"}
ROSTER_SETS = {
    "MLB": {"ARI", "ATL", "BAL", "BOS", "CHC", "CWS", "CIN", "CLE", "COL", "DET", "HOU", "KC",
            "LAA", "LAD", "MIA", "MIL", "MIN", "NYM", "NYY", "OAK", "PHI", "PIT", "SD", "SEA",
            "SF", "STL", "TB", "TEX", "TOR", "WSN"},
    "NHL": {"ANA", "ARI", "UTA", "BOS", "BUF", "CAR", "CBJ", "CGY", "CHI", "COL", "DAL", "DET",
            "EDM", "FLA", "LAK", "MIN", "MTL", "NJD", "NSH", "NYI", "NYR", "OTT", "PHI", "PIT",
            "SEA", "SJS", "STL", "TBL", "TOR", "VAN", "VGK", "WPG", "WSH"},
    "NBA": {"ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GS", "GSW", "HOU",
            "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NY", "NYK", "OKC", "ORL",
            "PHI", "PHX", "POR", "SAC", "SA", "SAS", "TOR", "UTA", "WAS", "WLA"},
    "WNBA": {"ATL", "CHI", "CON", "DAL", "IND", "LA", "LAS", "LV", "LVA", "MIN", "NY", "NYL",
             "PDX", "PHX", "SEA", "TOR", "WSH", "WAS", "GS", "GSV"},
}
# Same scoring order/tie-break as _classifyTeamsSport's _cand array --
# first candidate to exceed the current best score wins ties.
CANDIDATE_ORDER = ["WNBA", "MLB", "NHL", "NBA"]


def log(msg: str) -> None:
    print(f"[audit_bet_classification] {msg}", file=sys.stderr)


def fetch_all_bets() -> list[dict]:
    rows: list[dict] = []
    offset, page_size = 0, 1000
    while True:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/bets?select=id,raw&order=id",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Range": f"{offset}-{offset + page_size - 1}",
            },
        )
        with urllib.request.urlopen(req) as r:
            batch = json.loads(r.read())
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def season_implausible(sport: str, date_str: str | None) -> bool:
    if not date_str:
        return False
    m = re.match(r"\d{4}-(\d{2}-\d{2})", date_str)
    if not m:
        return False
    md = m.group(1)

    def in_range(frm: str, to: str) -> bool:
        return (md >= frm and md <= to) if frm <= to else (md >= frm or md <= to)

    if sport == "NHL":
        return in_range("06-28", "09-15")
    if sport == "NBA":
        return in_range("06-28", "09-15")
    if sport == "MLB":
        return in_range("11-10", "02-10")
    if sport == "WNBA":
        return in_range("11-01", "04-20")
    return False


def classify_teams_sport(h: str, a: str) -> str:
    h, a = (h or "").upper(), (a or "").upper()
    best, best_score = None, 0
    for sport in CANDIDATE_ORDER:
        score = (h in ROSTER_SETS[sport]) + (a in ROSTER_SETS[sport])
        if score > best_score:
            best_score, best = score, sport
    return best if best_score > 0 else "UNKNOWN"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="PATCH Supabase with the corrected sport/league (default: report only)")
    args = ap.parse_args()

    rows = fetch_all_bets()
    log(f"pulled {len(rows)} raw ledger rows")

    checked = skipped_ambiguous = 0
    mismatches = []
    for row in rows:
        raw = row.get("raw") or {}
        h_a, aw_a = raw.get("hA"), raw.get("awA")
        sport = raw.get("sport")
        if not h_a or not aw_a or sport not in SCOPED_SPORTS:
            continue
        checked += 1
        h, a = h_a.upper(), aw_a.upper()
        roster = ROSTER_SETS[sport]
        team_codes_impossible = h not in roster and a not in roster
        date_impossible = season_implausible(sport, raw.get("date"))
        if not team_codes_impossible and not date_impossible:
            continue
        correct = classify_teams_sport(h_a, aw_a)
        if correct in ("UNKNOWN", sport):
            skipped_ambiguous += 1
            continue
        reason = " + ".join(filter(None, [
            f"impossible team codes for {sport}" if team_codes_impossible else None,
            f"{sport} off-season on this date" if date_impossible else None,
        ]))
        mismatches.append({
            "id": row["id"], "bet_id": raw.get("id"), "date": raw.get("date"),
            "hA": h_a, "awA": aw_a, "betOn": raw.get("betOn"), "outcome": raw.get("outcome"),
            "storedSport": sport, "storedLeague": raw.get("league"), "correctSport": correct,
            "reason": reason,
        })

    log(f"checked {checked} team bets across {SCOPED_SPORTS}, {skipped_ambiguous} ambiguous cases skipped (not flagged)")
    print(f"\n{len(mismatches)} high-confidence mismatch(es) found:\n")
    for m in mismatches:
        print(f"  row id={m['id']}  {m['date']}  {m['hA']} vs {m['awA']}  betOn={m['betOn']!r}  "
              f"outcome={m['outcome']}  stored={m['storedSport']}/{m['storedLeague']}  "
              f"-> should be {m['correctSport']}  ({m['reason']})")

    if not mismatches:
        return

    if not args.apply:
        print("\nDry run only -- nothing written. Re-run with --apply to PATCH these rows in Supabase.")
        return

    print(f"\n--apply passed: writing corrected sport/league for {len(mismatches)} row(s)...")
    for m in mismatches:
        body = json.dumps({"raw": {"sport": m["correctSport"], "league": m["correctSport"]}}).encode()
        # PATCH only sport/league inside the jsonb `raw` column via a merge,
        # not a full-column replace -- Supabase/PostgREST doesn't support a
        # partial jsonb merge via PATCH body directly, so this reads the
        # full raw object, patches those 2 keys, and writes it back whole.
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/bets?id=eq.{m['id']}&select=raw",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
        )
        with urllib.request.urlopen(req) as r:
            current = json.loads(r.read())[0]["raw"]
        current["sport"] = m["correctSport"]
        current["league"] = m["correctSport"]
        patch_req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/bets?id=eq.{m['id']}",
            data=json.dumps({"raw": current}).encode(),
            method="PATCH",
            headers={
                "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json", "Prefer": "return=minimal",
            },
        )
        urllib.request.urlopen(patch_req)
        log(f"  patched row id={m['id']} -> {m['correctSport']}")
    print("Done.")


if __name__ == "__main__":
    main()
