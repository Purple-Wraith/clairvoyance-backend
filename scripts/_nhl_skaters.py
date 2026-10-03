"""NHL skater-value table for the injury adjustment in docs/app.html (nhlMC): pure, stdlib only, no network.

WHAT IT PRODUCES (data.json -> nhl.skaterValue, written by clairvoyance_update.fetch_nhl_skater_value):

    {"season": "20262027", "priorSeason": "20252026", "shrinkGames": 20,
     "players": {"sebastian aho": [["F", 0.93], ["D", 0.41]], "leo carlsson": [["F", 0.84]], ...}}

  key   = _espn_injuries.norm_name(full name)  (accent/case/punctuation-insensitive -- the SAME normalizer docs/app.html _injNorm uses)
  value = list (name collisions are real: two NHL "Sebastian Aho"s) of [position group, estimated points per game]
          position group: "F" (C/L/R/W) or "D".  The app only uses an entry when exactly ONE entry has the injured player's group,
          so a collision it cannot resolve is skipped (fail-open), never guessed.

WHY POINTS PER GAME: it is the one player-quality number the free NHL stats API (api.nhle.com/stats/rest) gives for every skater in a
single request, it is stable (unlike a 4-game sample of anything finer), and the app turns it into a capped, small scoring adjustment
(see NHL_INJ_* constants in docs/app.html for the rationale and the cap).  It does NOT see defensive value, penalty killing or
faceoffs, so it UNDER-states a shutdown defenseman or a faceoff centre -- the deliberate, conservative direction.

ESTIMATE: shrink the current season toward last season's rate so a 3-game hot/cold start cannot make a role player look like a star
(or a star look like depth):

    ppg_est = (points_now + K * ppg_prior) / (games_now + K)          K = SHRINK_GAMES = 20
    ppg_prior = points_prior / games_prior   if games_prior >= MIN_PRIOR_GAMES else POS_DEFAULT_PPG[group]   (rookies/call-ups)

A player with NO current-season games (the injured star, by definition) is simply his prior-season rate.  Players below MIN_KEEP_PPG
are dropped (a fringe skater is never worth an adjustment, and it keeps the payload small).
"""
from __future__ import annotations

from _espn_injuries import norm_name

SHRINK_GAMES = 20
MIN_PRIOR_GAMES = 20
# Rate assumed for a skater with no usable prior season (rookie / call-up) = the app's REPLACEMENT level (NHL_INJ_REPL_PPG in docs/app.html),
# so an unknown contributes nothing to an injury adjustment until real production proves otherwise (conservative, fail-open direction).
POS_DEFAULT_PPG = {"F": 0.30, "D": 0.20}
MIN_KEEP_PPG = {"F": 0.35, "D": 0.25}   # below this a skater is never worth an adjustment; also keeps the payload small


def pos_group(code: str | None) -> str | None:
    c = (code or "").strip().upper()
    if c == "D":
        return "D"
    if c in ("C", "L", "R", "LW", "RW", "W", "F"):
        return "F"
    return None   # goalies and anything unknown are not skaters


def _rows_by_player(rows: list[dict]) -> dict:
    """{(norm name, group): {"gp": int, "pts": int}} -- a repeated key (mid-season split rows) sums, a missing/zero-games row is skipped."""
    out: dict = {}
    for r in rows or []:
        g = pos_group(r.get("positionCode"))
        name = norm_name(r.get("skaterFullName") or "")
        gp = r.get("gamesPlayed") or 0
        if not g or not name or gp <= 0:
            continue
        cur = out.setdefault((name, g), {"gp": 0, "pts": 0})
        cur["gp"] += gp
        cur["pts"] += r.get("points") or 0
    return out


def build_skater_value(cur_rows: list[dict], prior_rows: list[dict], shrink: int = SHRINK_GAMES) -> dict:
    """NHL stats-API skater/summary rows (current + prior season) -> {normalized name: [[group, est points/game], ...]}."""
    cur, pri = _rows_by_player(cur_rows), _rows_by_player(prior_rows)
    players: dict = {}
    for key in set(cur) | set(pri):
        name, g = key
        c, p = cur.get(key), pri.get(key)
        if p and p["gp"] >= MIN_PRIOR_GAMES:
            prior_ppg = p["pts"] / p["gp"]
        else:
            prior_ppg = POS_DEFAULT_PPG[g]
        gp_now, pts_now = (c["gp"], c["pts"]) if c else (0, 0)
        est = (pts_now + shrink * prior_ppg) / (gp_now + shrink)
        if est < MIN_KEEP_PPG[g]:
            continue
        players.setdefault(name, []).append([g, round(est, 3)])
    for v in players.values():
        v.sort(key=lambda e: (e[0], -e[1]))
    return players
