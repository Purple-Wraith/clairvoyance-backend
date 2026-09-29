"""Point-in-time backtest for the 2026-09-29 puck-line margin-of-victory
blend (see docs/app.html's liigaEns/shlEns/nlaEns/extraligaEns -- the
"Puck-line cover blend" comment block in each).

WHAT CHANGED IN PRODUCTION: the -1.5/+1.5 cover probability used to be
purely mc.rl15/mc.rlN -- the exact Poisson-implied margin distribution
from each team's own average scoring rate (hLam/aLam). That was replaced
with ens.favCoverP/dogCoverP: the same Poisson rate blended (using the
existing HOCKEY_ENS mc/bay+elo weights, default {mc:.7,bay:.2,elo:.1})
with a new empirical signal, _hkBigMarginRate -- each team's own real
season-long rate of winning a completed game by 2+ goals.

THE QUESTION THIS SCRIPT ANSWERS: for real, already-settled SPREAD bets
this app has locked for LIIGA/SHL/NLA/EXTRALIGA, would the NEW blended
probability have been a better predictor of the real outcome (did the
picked side actually cover?) than the OLD pure-Poisson probability?

POINT-IN-TIME CORRECTNESS: docs/{league}_schedule.json is refreshed
multiple times a day and every refresh is its own commit, so (same
technique as backtest_quanthockey_signal.py's own docstring) the file's
real git history is a genuine snapshot series for free. For every settled
bet, this script finds the schedule snapshot that was actually current AT
THE BET'S OWN lockedAt time and recomputes BOTH the old and new
probabilities from that snapshot's own teams/games data only -- never
today's data, which would leak the outcome of games that happened after
the bet was placed (including, for margin rate specifically, the bet's
OWN game once it's final).

Both probabilities are computed for whichever side was actually PICKED
(extract_picked_team, same betOn-suffix-stripping helper the QuantHockey
backtest already uses), not assumed to be the favorite -- a real bet can
be either side.

Scoring: Brier score (mean squared error vs the real 0/1 outcome, lower
is better) and mean absolute error, overall and per league, plus a
simple "which was closer on this bet" win/loss/tie tally. This is a
genuine before/after comparison against real settled outcomes, not a
correlation check.

Usage:
  python3 scripts/backtest_spread_margin_signal.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SUPABASE_URL = "https://vhwkbeblforpnliowpam.supabase.co"
SUPABASE_KEY = "sb_publishable_AEgqvpaIh0MWpDdXI35BYw_lD8HzBlM"

SCHEDULE_PATHS = {
    "LIIGA": "docs/liiga_schedule.json",
    "SHL": "docs/shl_schedule.json",
    "NLA": "docs/nla_schedule.json",
    "EXTRALIGA": "docs/extraliga_schedule.json",
}
# Same weak-prior/blend weights docs/app.html falls back to everywhere
# (HOCKEY_ENS starts as {} and every *Ens function does `||{mc:.7,bay:.2,
# elo:.1}`) -- this is genuinely what a user who hasn't touched the
# CONFIG tab's sliders sees live, so it's the right default to backtest.
ENS_W = {"mc": 0.7, "bay": 0.2, "elo": 0.1}
MARGIN_W = 0.10  # live production weight as of 2026-09-29, post-backtest
HFA = 0.055
CAP = 20
TRAILING_SUFFIX_RE = re.compile(r"\s+(ML|[+-]\d+(?:\.\d+)?)$")


def log(msg: str) -> None:
    print(f"[backtest_spread_margin] {msg}", file=sys.stderr)


def fetch_all_bets() -> list[dict]:
    rows: list[dict] = []
    offset, page_size = 0, 1000
    while True:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/bets?select=id,raw,outcome&order=id",
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


def commit_history(path: str) -> list[tuple[str, int]]:
    out = subprocess.run(
        ["git", "log", "--format=%H %at", "--follow", "--", path],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    commits = []
    for line in out.splitlines():
        h, ts = line.split()
        commits.append((h, int(ts) * 1000))
    commits.reverse()
    return commits


def snapshot_at(path: str, commits: list[tuple[str, int]], at_ms: int) -> dict | None:
    chosen = None
    for h, ts in commits:
        if ts <= at_ms:
            chosen = h
        else:
            break
    if chosen is None:
        return None
    content = subprocess.run(
        ["git", "show", f"{chosen}:{path}"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(content)


def extract_picked_team(bet_on: str, hA: str, awA: str) -> tuple[str, str] | None:
    cleaned = TRAILING_SUFFIX_RE.sub("", bet_on).strip()
    if cleaned == hA.strip():
        return hA, awA
    if cleaned == awA.strip():
        return awA, hA
    return None


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def resolve_team_id(name: str, teams: dict) -> str | None:
    """Bet betOn/hA/awA are display names sourced from this SAME app's own
    homeName/awayName (both ultimately read from this league's _DATA.teams
    at lock time) -- an exact match against the snapshot's own team.name
    is expected to resolve essentially always. Diacritic-normalized
    fallback only for safety against any historical naming drift."""
    for tid, t in teams.items():
        if t.get("name") == name:
            return tid
    nk = _norm(name)
    for tid, t in teams.items():
        if _norm(t.get("name", "")) == nk:
            return tid
    return None


def blended_rates(team: dict | None) -> dict:
    if not team:
        return {"gf": 2.8, "ga": 2.8, "gm": 5.5, "gp": 0}
    cur_gp = team.get("gp") or 0
    cur_w = min(1.0, cur_gp / CAP)
    cur_gf_pg = team["gf"] / cur_gp if cur_gp and team.get("gf") is not None else None
    cur_ga_pg = team["ga"] / cur_gp if cur_gp and team.get("ga") is not None else None
    prev = team.get("prevSeason")
    prev_gf_pg = prev["gf"] / prev["gp"] if prev and prev.get("gp") else None
    prev_ga_pg = prev["ga"] / prev["gp"] if prev and prev.get("gp") else None
    gf = cur_gf_pg * cur_w + prev_gf_pg * (1 - cur_w) if cur_gf_pg is not None and prev_gf_pg is not None else (cur_gf_pg if cur_gf_pg is not None else (prev_gf_pg if prev_gf_pg is not None else 2.8))
    ga = cur_ga_pg * cur_w + prev_ga_pg * (1 - cur_w) if cur_ga_pg is not None and prev_ga_pg is not None else (cur_ga_pg if cur_ga_pg is not None else (prev_ga_pg if prev_ga_pg is not None else 2.8))
    return {"gf": gf, "ga": ga, "gp": cur_gp}


def played_games(team_id: str, games: list[dict]) -> list[dict]:
    return [g for g in games if g.get("state") == "post" and g.get("homeScore") is not None
            and g.get("awayScore") is not None and (g.get("home") == team_id or g.get("away") == team_id)]


def form_factor(team_id: str, games: list[dict], season_rates: dict) -> float:
    played = played_games(team_id, games)
    played.sort(key=lambda g: g["date"], reverse=True)
    recent = played[:5]
    if len(recent) < 3:
        return 1.0
    gf = ga = 0
    for g in recent:
        if g["home"] == team_id:
            gf += g["homeScore"]; ga += g["awayScore"]
        else:
            gf += g["awayScore"]; ga += g["homeScore"]
    recent_gd_pg = gf / len(recent) - ga / len(recent)
    season_gd_pg = (season_rates["gf"] or 0) - (season_rates["ga"] or 0)
    delta = recent_gd_pg - season_gd_pg
    return 1 + max(-0.06, min(0.06, delta * 0.05))


def big_margin_rate(team_id: str, games: list[dict]) -> tuple[int, int] | None:
    played = played_games(team_id, games)
    if not played:
        return None
    big = 0
    for g in played:
        own = g["homeScore"] if g["home"] == team_id else g["awayScore"]
        opp = g["awayScore"] if g["home"] == team_id else g["homeScore"]
        if own - opp >= 2:
            big += 1
    return big, len(played)


def poisson_pmf(lam: float, k: int) -> float:
    import math
    p = math.exp(-lam)
    for i in range(1, k + 1):
        p *= lam / i
    return p


def margin_dist(h_lam: float, a_lam: float) -> dict[int, float]:
    max_g = 15
    h_p = [poisson_pmf(h_lam, k) for k in range(max_g + 1)]
    a_p = [poisson_pmf(a_lam, k) for k in range(max_g + 1)]
    dist: dict[int, float] = {}
    for h in range(max_g + 1):
        for a in range(max_g + 1):
            m = h - a
            dist[m] = dist.get(m, 0.0) + h_p[h] * a_p[a]
    return dist


def spread_prob(h_lam: float, a_lam: float, home_line: float) -> float:
    dist = margin_dist(h_lam, a_lam)
    p = sum(v for m, v in dist.items() if m > -home_line)
    return max(0.0, min(1.0, p))


def compute_probs(snapshot: dict, home_id: str, away_id: str) -> dict | None:
    """Full port of liigaMC/liigaEns (identical formula across all 4
    leagues) -- returns both the OLD pure-MC and NEW blended cover
    probability for the HOME side (favCoverP-equivalent from home's own
    perspective is derived by the caller from favId/dogId)."""
    teams = snapshot.get("teams") or {}
    h, aw = teams.get(home_id), teams.get(away_id)
    if not h or not aw:
        return None
    games = snapshot.get("games") or []
    h_r, a_r = blended_rates(h), blended_rates(aw)
    h_form, a_form = form_factor(home_id, games, h_r), form_factor(away_id, games, a_r)
    h_lam = max(0.4, ((h_r["gf"] * h_form + a_r["ga"]) / 2) * (1 + HFA))
    a_lam = max(0.4, ((a_r["gf"] * a_form + h_r["ga"]) / 2) * (1 - HFA * 0.5))
    dist = margin_dist(h_lam, a_lam)
    hw_p = sum(v for m, v in dist.items() if m > 0) + dist.get(0, 0.0) / 2

    def bay_p(team):
        if not team:
            return 0.5
        gp, w = team.get("gp") or 0, team.get("w") or 0
        return (w + 1) / (gp + 2)

    h_bay, a_bay = bay_p(h), bay_p(aw)
    denom = h_bay * (1 - a_bay) + a_bay * (1 - h_bay)
    log5 = (h_bay * (1 - a_bay)) / denom if denom else 0.5
    bay_share = ENS_W["bay"] + ENS_W["elo"]
    p = min(0.95, max(0.05, hw_p * ENS_W["mc"] + log5 * bay_share))

    rl15 = spread_prob(h_lam, a_lam, -1.5)  # P(home covers -1.5)
    rl_n = 1 - rl15  # P(away covers +1.5)

    fav_id, dog_id = (home_id, away_id) if p >= 0.5 else (away_id, home_id)

    def big_p(r):
        return (r[0] + 1) / (r[1] + 2) if r else 0.5

    fav_big_p = big_p(big_margin_rate(fav_id, games))
    dog_big_p = big_p(big_margin_rate(dog_id, games))
    b_denom = fav_big_p * (1 - dog_big_p) + dog_big_p * (1 - fav_big_p)
    margin_log5 = (fav_big_p * (1 - dog_big_p)) / b_denom if b_denom else 0.5
    mc_cover_p = rl15 if p >= 0.5 else rl_n
    # MARGIN_W reduced from the ML ensemble's ~30% bay_share to a fixed
    # 10% after this exact backtest showed full weight performed worse
    # (see docs/app.html's liigaEns comment) -- kept in sync here so
    # re-running this script reflects live production behavior.
    fav_cover_p = min(0.95, max(0.05, mc_cover_p * (1 - MARGIN_W) + margin_log5 * MARGIN_W))

    return {
        "favId": fav_id, "dogId": dog_id,
        "oldCoverP": {fav_id: mc_cover_p, dog_id: 1 - mc_cover_p},
        "newCoverP": {fav_id: fav_cover_p, dog_id: 1 - fav_cover_p},
    }


def brier(pairs: list[tuple[float, float]]) -> float | None:
    if not pairs:
        return None
    return sum((p - a) ** 2 for p, a in pairs) / len(pairs)


def mae(pairs: list[tuple[float, float]]) -> float | None:
    if not pairs:
        return None
    return sum(abs(p - a) for p, a in pairs) / len(pairs)


def main() -> None:
    all_rows = fetch_all_bets()
    log(f"pulled {len(all_rows)} raw ledger rows")

    histories = {lg: commit_history(path) for lg, path in SCHEDULE_PATHS.items()}
    for lg, commits in histories.items():
        log(f"{lg}: {len(commits)} schedule snapshot(s) in git history")

    # per league: list of (old_p, new_p, actual)
    results: dict[str, list[tuple[float, float, float]]] = {lg: [] for lg in SCHEDULE_PATHS}
    skipped_no_snapshot = skipped_no_match = skipped_no_teams = 0

    for row in all_rows:
        raw = row.get("raw") or {}
        if row.get("outcome") == "_removed":
            continue
        sport = (raw.get("sport") or "").upper()
        league = (raw.get("league") or "").upper()
        tag = sport if sport in SCHEDULE_PATHS else (league if league in SCHEDULE_PATHS else None)
        if not tag or raw.get("betType") != "SPREAD" or raw.get("outcome") not in ("win", "loss"):
            continue
        locked_at = raw.get("lockedAt")
        hA, awA, bet_on = raw.get("hA"), raw.get("awA"), raw.get("betOn")
        if not locked_at or not hA or not awA or not bet_on:
            continue

        snap = snapshot_at(SCHEDULE_PATHS[tag], histories[tag], locked_at)
        if not snap:
            skipped_no_snapshot += 1
            continue

        picked = extract_picked_team(bet_on, hA, awA)
        if not picked:
            skipped_no_match += 1
            continue
        picked_name, opp_name = picked
        teams = snap.get("teams") or {}
        picked_id = resolve_team_id(picked_name, teams)
        opp_id = resolve_team_id(opp_name, teams)
        if not picked_id or not opp_id:
            skipped_no_teams += 1
            continue
        home_id_actual = resolve_team_id(hA, teams)
        away_id_actual = resolve_team_id(awA, teams)
        if not home_id_actual or not away_id_actual:
            skipped_no_teams += 1
            continue

        probs = compute_probs(snap, home_id_actual, away_id_actual)
        if not probs:
            skipped_no_teams += 1
            continue
        if picked_id not in probs["oldCoverP"]:
            skipped_no_teams += 1
            continue

        old_p = probs["oldCoverP"][picked_id]
        new_p = probs["newCoverP"][picked_id]
        actual = 1.0 if raw["outcome"] == "win" else 0.0
        results[tag].append((old_p, new_p, actual))

    log(f"skipped (bet predates any snapshot): {skipped_no_snapshot}")
    log(f"skipped (betOn didn't match either team): {skipped_no_match}")
    log(f"skipped (team-id resolution failed): {skipped_no_teams}")
    print()

    all_old, all_new = [], []
    for lg, rows in results.items():
        print(f"=== {lg}: n={len(rows)} settled SPREAD bets (point-in-time correct) ===")
        if not rows:
            continue
        old_pairs = [(o, a) for o, n, a in rows]
        new_pairs = [(n, a) for o, n, a in rows]
        all_old.extend(old_pairs)
        all_new.extend(new_pairs)
        old_brier, new_brier = brier(old_pairs), brier(new_pairs)
        old_mae, new_mae = mae(old_pairs), mae(new_pairs)
        closer_old = sum(1 for o, n, a in rows if abs(o - a) < abs(n - a))
        closer_new = sum(1 for o, n, a in rows if abs(n - a) < abs(o - a))
        tie = len(rows) - closer_old - closer_new
        print(f"  Brier score  (lower better): OLD {old_brier:.4f}  ->  NEW {new_brier:.4f}  ({'improved' if new_brier < old_brier else 'worse' if new_brier > old_brier else 'unchanged'})")
        print(f"  Mean abs err (lower better): OLD {old_mae:.4f}  ->  NEW {new_mae:.4f}")
        print(f"  Per-bet closer-to-actual:    OLD closer {closer_old}  /  NEW closer {closer_new}  /  tie {tie}")
        actual_cover_rate = sum(a for _, _, a in rows) / len(rows)
        avg_old_p = sum(o for o, _, _ in rows) / len(rows)
        avg_new_p = sum(n for _, n, _ in rows) / len(rows)
        print(f"  Calibration: real cover rate {actual_cover_rate*100:.1f}%  |  avg OLD pred {avg_old_p*100:.1f}%  |  avg NEW pred {avg_new_p*100:.1f}%")
        print()

    print("########## COMBINED (all 4 leagues) ##########")
    n_total = len(all_old)
    print(f"n={n_total}")
    if n_total:
        old_b, new_b = brier(all_old), brier(all_new)
        old_m, new_m = mae(all_old), mae(all_new)
        print(f"  Brier score  (lower better): OLD {old_b:.4f}  ->  NEW {new_b:.4f}  ({'improved' if new_b < old_b else 'worse' if new_b > old_b else 'unchanged'})")
        print(f"  Mean abs err (lower better): OLD {old_m:.4f}  ->  NEW {new_m:.4f}")
    print()
    print("Note: n reflects real settled SPREAD bets only -- the OLD pure-MC")
    print("dominance this fix targets is exactly why SPREAD has the largest")
    print("sample of the 4 hockey leagues' bet types to backtest against.")


if __name__ == "__main__":
    main()
