#!/usr/bin/env python3
from __future__ import annotations
"""
live_tracker.py — Clairvoyance Constant Live Stats Engine
Polls live scores every 45 seconds during game windows and writes live_data.json.
Calculates in-game win probability for every locked bet in real time.

Run modes:
  python3 scripts/live_tracker.py            # run once and exit
  python3 scripts/live_tracker.py --loop     # run every 45s until stopped (daemon mode)
  python3 scripts/live_tracker.py --verbose  # debug logging

The frontend polls live_data.json every 45s and updates:
  - Live score display for every active game
  - Win probability meter for every locked bet
  - NRFI live status
  - In-game O/U likelihood
  - Auto-settle triggers when games go FINAL
"""

import argparse
import json
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "requests"], check=True)
    import requests

ROOT    = Path(__file__).parent.parent
FE_LIVE = ROOT / "frontend" / "live_data.json"
DC_LIVE = ROOT / "docs"    / "live_data.json"
DATA    = ROOT / "data"

# ESPN's site.api.espn.com now 403s any request carrying a custom
# User-Agent (verified directly against the live endpoint) -- dropped so
# requests falls back to its own default UA, which passes through fine.
HEADERS = {
    "Accept": "application/json, */*",
}
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
NHL_API   = "https://api-web.nhle.com/v1"

_verbose = False
_running = True

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)

def vlog(msg: str) -> None:
    if _verbose:
        log(msg, "DEBUG")

def _get(url: str, timeout: int = 8) -> dict | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        vlog(f"GET {url[:60]}: {e}")
        return None

# ─── IN-GAME WIN PROBABILITY ──────────────────────────────────────────────────
# MLB: score differential × inning leverage (historical RE288 approximation)
_MLB_INNING_FACTOR = {1:0.18,2:0.26,3:0.34,4:0.42,5:0.50,6:0.60,7:0.72,8:0.86,9:1.0}

def mlb_win_prob(score_diff: int, inning: int, top_half: bool) -> float:
    """Approx win prob for the leading team based on score diff and inning."""
    if score_diff == 0:
        return 0.50
    f = _MLB_INNING_FACTOR.get(min(inning, 9), 1.0)
    if top_half and inning >= 9:
        f = 0.93
    # logistic: each run ≈ 0.25 logit units at inning factor
    logit = score_diff * 0.9 * f
    return 1 / (1 + math.exp(-logit))

def nba_win_prob(score_diff: int, seconds_remaining: int) -> float:
    """In-game NBA win probability. Uses Pythagorean-style model."""
    if seconds_remaining <= 0:
        return 1.0 if score_diff > 0 else (0.5 if score_diff == 0 else 0.0)
    # Standard deviation of score at any point scales with sqrt(time remaining)
    # ~0.45 pts/sec pace assumption; σ ≈ 0.45 * sqrt(seconds_remaining)
    sigma = 0.45 * math.sqrt(max(seconds_remaining, 1))
    z = score_diff / sigma
    # Approximate normal CDF
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))

def nhl_win_prob(score_diff: int, seconds_remaining: int, strength: str = "5v5") -> float:
    """In-game NHL regulation win probability."""
    if seconds_remaining <= 0:
        return 1.0 if score_diff > 0 else 0.5
    # ~0.035 goals/min rate in regulation; lower in overtime
    minutes = seconds_remaining / 60
    # Expected goals remaining ≈ 0.06 per minute (both teams combined)
    lambda_remaining = 0.06 * minutes
    # Poisson approximation: prob trailing team ties/surpasses
    if score_diff == 0:
        return 0.50
    elif score_diff > 0:
        # Leading: prob they hold ≈ Poisson CDF
        # P(opposing scores ≤ diff-1 in remaining time)
        prob_hold = sum(
            math.exp(-lambda_remaining/2) * (lambda_remaining/2)**k / math.factorial(min(k,20))
            for k in range(score_diff)
        )
        return max(0.50, min(0.98, 0.50 + prob_hold * 0.48))
    else:
        return 1 - nhl_win_prob(-score_diff, seconds_remaining, strength)

def _parse_clock(clock_str: str) -> int:
    """Parse 'MM:SS' clock string to seconds remaining in period."""
    try:
        parts = clock_str.strip().split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        pass
    return 0

# ─── LIVE SCOREBOARD FETCHERS ─────────────────────────────────────────────────
def fetch_mlb_live() -> list[dict]:
    today = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y%m%d")
    data = _get(f"{ESPN_BASE}/baseball/mlb/scoreboard?dates={today}&limit=20")
    if not data:
        return []
    games = []
    for ev in data.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        comps = comp.get("competitors") or []
        home = next((c for c in comps if c.get("homeAway") == "home"), {})
        away = next((c for c in comps if c.get("homeAway") == "away"), {})
        status = ev.get("status") or {}
        state  = (status.get("type") or {}).get("state", "pre")
        hs = int(home.get("score") or 0) if state != "pre" else 0
        as_ = int(away.get("score") or 0) if state != "pre" else 0
        inning = status.get("period") or 1
        situation = (comp.get("situation") or {})
        top_half = situation.get("isTopHalf", True)

        g = {
            "id":     ev.get("id"),
            "sport":  "MLB",
            "home":   (home.get("team") or {}).get("abbreviation", ""),
            "away":   (away.get("team") or {}).get("abbreviation", ""),
            "homeScore": hs,
            "awayScore": as_,
            "state":  state,  # pre | in | post
            "inning": inning,
            "topHalf": top_half,
            "outs":   situation.get("outs", 0),
            "onBase": situation.get("onBase", ""),
            "note":   (status.get("type") or {}).get("shortDetail", ""),
            "displayClock": status.get("displayClock", ""),
            "venue":  (comp.get("venue") or {}).get("fullName", ""),
            "network": ((comp.get("broadcasts") or [{}])[0].get("names") or [""])[0],
        }

        # NRFI: first inning runs
        g["nrfiSafe"] = (inning > 1) or (state == "post")
        g["firstInningRuns"] = 0   # would need play-by-play for accuracy

        # Win probability for home team
        if state == "in":
            diff = hs - as_
            g["homeWinProb"] = round(mlb_win_prob(diff, inning, top_half), 3)
        elif state == "post":
            g["homeWinProb"] = 1.0 if hs > as_ else (0.5 if hs == as_ else 0.0)
        else:
            g["homeWinProb"] = 0.5

        # Current O/U pace
        if state == "in" and inning > 0:
            innings_played = inning - (1 if top_half else 0)
            if innings_played > 0:
                pace = (hs + as_) * 9 / innings_played
                g["ouPace"] = round(pace, 1)

        games.append(g)
    return games

def fetch_nba_live() -> list[dict]:
    today = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y%m%d")
    data = _get(f"{ESPN_BASE}/basketball/nba/scoreboard?dates={today}&limit=15")
    if not data:
        return []
    games = []
    for ev in data.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        comps = comp.get("competitors") or []
        home = next((c for c in comps if c.get("homeAway") == "home"), {})
        away = next((c for c in comps if c.get("homeAway") == "away"), {})
        status = ev.get("status") or {}
        state  = (status.get("type") or {}).get("state", "pre")
        hs = int(home.get("score") or 0) if state != "pre" else 0
        as_ = int(away.get("score") or 0) if state != "pre" else 0
        quarter = status.get("period") or 1
        clock   = status.get("displayClock", "0:00")

        # Seconds remaining in game
        clock_sec = _parse_clock(clock)
        periods_left = max(0, 4 - quarter)
        total_sec_remaining = periods_left * 720 + clock_sec

        g = {
            "id":       ev.get("id"),
            "sport":    "NBA",
            "home":     (home.get("team") or {}).get("abbreviation", ""),
            "away":     (away.get("team") or {}).get("abbreviation", ""),
            "homeScore": hs,
            "awayScore": as_,
            "state":    state,
            "quarter":  quarter,
            "displayClock": clock,
            "secondsRemaining": total_sec_remaining,
            "note":     (status.get("type") or {}).get("shortDetail", ""),
            "network":  ((comp.get("broadcasts") or [{}])[0].get("names") or [""])[0],
        }

        if state == "in":
            diff = hs - as_
            g["homeWinProb"] = round(nba_win_prob(diff, total_sec_remaining), 3)
            # O/U pace
            secs_played = max(1, 4 * 720 - total_sec_remaining)
            pace = (hs + as_) * (4 * 720) / secs_played
            g["ouPace"] = round(pace, 1)
        elif state == "post":
            g["homeWinProb"] = 1.0 if hs > as_ else (0.5 if hs == as_ else 0.0)
        else:
            g["homeWinProb"] = 0.5

        games.append(g)
    return games

def _fetch_football_live(espn_path: str, sport_tag: str) -> list[dict]:
    """Shared CFB/NFL live-score fetcher -- both are quarter-based ESPN
    scoreboards with an identical response shape, just different paths.
    No win-probability here (unlike MLB/NBA/NHL above): those 3 have a
    real if simplistic score-diff/time-remaining model already tuned for
    this app; football's isn't (down/distance/possession all matter a lot
    more than in basketball, and no such model exists in this codebase
    yet) -- rather than ship a fabricated number, this only reports what
    ESPN's scoreboard actually says: score, quarter, clock, state. Same
    home/away abbreviation convention as fetch_cfb.py/fetch_nfl.py's own
    schedule scrapes (team.abbreviation), so frontend lookups by
    home+away pair match cleanly against docs/cfb_schedule.json /
    nfl_schedule.json without any extra normalization.
    """
    today = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y%m%d")
    data = _get(f"{ESPN_BASE}/{espn_path}/scoreboard?dates={today}&limit=100")
    if not data:
        return []
    games = []
    for ev in data.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        comps = comp.get("competitors") or []
        home = next((c for c in comps if c.get("homeAway") == "home"), {})
        away = next((c for c in comps if c.get("homeAway") == "away"), {})
        status = ev.get("status") or {}
        state  = (status.get("type") or {}).get("state", "pre")
        hs = int(home.get("score") or 0) if state != "pre" else 0
        as_ = int(away.get("score") or 0) if state != "pre" else 0

        games.append({
            "id":       ev.get("id"),
            "sport":    sport_tag,
            "home":     (home.get("team") or {}).get("abbreviation", ""),
            "away":     (away.get("team") or {}).get("abbreviation", ""),
            "homeScore": hs,
            "awayScore": as_,
            "state":    state,
            "quarter":  status.get("period") or 1,
            "displayClock": status.get("displayClock", "0:00"),
            "note":     (status.get("type") or {}).get("shortDetail", ""),
            "network":  ((comp.get("broadcasts") or [{}])[0].get("names") or [""])[0],
        })
    return games

def fetch_cfb_live() -> list[dict]:
    return _fetch_football_live("football/college-football", "CFB")

def fetch_nfl_live() -> list[dict]:
    return _fetch_football_live("football/nfl", "NFL")

def fetch_nhl_live() -> list[dict]:
    today = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d")
    data = _get(f"{NHL_API}/score/{today}")
    if not data:
        return []
    games = []
    for g in data.get("games") or []:
        home = g.get("homeTeam") or {}
        away = g.get("awayTeam") or {}
        state = g.get("gameState", "FUT")  # FUT|PRE|LIVE|CRIT|FINAL|OFF
        hs = home.get("score", 0) or 0
        as_ = away.get("score", 0) or 0
        period = g.get("period") or 1
        clock  = (g.get("clock") or {}).get("timeRemaining", "20:00")
        clock_sec = _parse_clock(clock)
        periods_left = max(0, 3 - period)
        total_sec = periods_left * 1200 + clock_sec

        game = {
            "id":       g.get("id"),
            "sport":    "NHL",
            "home":     home.get("abbrev", ""),
            "away":     away.get("abbrev", ""),
            "homeScore": hs,
            "awayScore": as_,
            "state":    state,
            "period":   period,
            "displayClock": clock,
            "secondsRemaining": total_sec,
            "seriesStatus": (g.get("seriesSummary") or {}).get("seriesStatusShort", ""),
            "note":     f"P{period} {clock}",
        }

        if state in ("LIVE", "CRIT"):
            diff = hs - as_
            game["homeWinProb"] = round(nhl_win_prob(diff, total_sec), 3)
            # O/U pace
            secs_played = max(1, 3600 - total_sec)
            pace = (hs + as_) * 3600 / secs_played
            game["ouPace"] = round(pace, 2)
        elif state in ("FINAL", "OFF"):
            game["homeWinProb"] = 1.0 if hs > as_ else (0.5 if hs == as_ else 0.0)
        else:
            game["homeWinProb"] = 0.5

        # Save % and shots if available
        game["homeSOG"] = home.get("sog", 0)
        game["awaySOG"] = away.get("sog", 0)
        games.append(game)
    return games

# ─── BET PROBABILITY UPDATER ──────────────────────────────────────────────────
def update_bet_probs(
    mlb_live: list, nba_live: list, nhl_live: list, locked_bets: list
) -> list[dict]:
    """
    For each pending locked bet, find the matching live game and
    compute current win probability given game state.
    """
    # Index live games by (home, away)
    live_idx: dict = {}
    for g in mlb_live + nba_live + nhl_live:
        h, a = g.get("home",""), g.get("away","")
        live_idx[(h, a)] = g
        live_idx[(a, h)] = g

    updated = []
    for bet in locked_bets:
        if bet.get("outcome") != "pending":
            updated.append(bet)
            continue

        hA, awA = bet.get("hA",""), bet.get("awA","")
        game = live_idx.get((hA, awA))

        if not game or game.get("state") == "pre":
            updated.append(bet)
            continue

        bet_on   = bet.get("betOn", "")
        bet_type = bet.get("betType", "ML")
        hs = game.get("homeScore", 0) or 0
        as_ = game.get("awayScore", 0) or 0
        home_wp = game.get("homeWinProb", 0.5)

        # Current probability for this specific bet
        current_prob = None

        if bet_type == "ML":
            # Is bet on home or away?
            team = bet.get("team", "")
            if team == hA:
                current_prob = home_wp
            elif team == awA:
                current_prob = 1 - home_wp

        elif bet_type in ("RL", "PL"):
            # Run/puck line — need to factor in spread
            line = float(str(bet.get("line","1.5")).replace("+","").replace("−","-") or 1.5)
            team = bet.get("team","")
            # Adjust score by line for probability
            if team == hA:
                adj_diff = (hs - as_) + line
            else:
                adj_diff = (as_ - hs) + line
            sport = game.get("sport","")
            if sport == "MLB":
                current_prob = mlb_win_prob(adj_diff, game.get("inning",1), game.get("topHalf",True))
            elif sport == "NBA":
                current_prob = nba_win_prob(adj_diff * 2.5, game.get("secondsRemaining",0))
            elif sport == "NHL":
                current_prob = nhl_win_prob(int(adj_diff), game.get("secondsRemaining",0))

        elif bet_type == "OU":
            total = hs + as_
            line_val = float(bet.get("ou") or bet.get("line") or 0)
            pace = game.get("ouPace", total)
            over = "OVER" in str(bet_on).upper() or bet.get("over", True)
            # Simple: if pace is above/below line, shift probability
            if line_val > 0:
                diff_from_line = (pace - line_val) / max(line_val * 0.1, 1)
                raw = 0.5 + diff_from_line * 0.15
                current_prob = max(0.05, min(0.95, raw)) if over else max(0.05, min(0.95, 1 - raw))

        b = dict(bet)
        if current_prob is not None:
            b["liveProb"] = round(current_prob, 3)
            b["liveGame"] = {
                "homeScore": hs,
                "awayScore": as_,
                "state":     game.get("state"),
                "note":      game.get("note",""),
            }
        updated.append(b)
    return updated

# ─── MAIN LIVE POLL ───────────────────────────────────────────────────────────
def poll_once() -> dict:
    ts = datetime.now(timezone.utc)
    et_now = ts - timedelta(hours=5)

    mlb  = fetch_mlb_live()
    nba  = fetch_nba_live()
    nhl  = fetch_nhl_live()
    cfb  = fetch_cfb_live()
    nfl  = fetch_nfl_live()

    # Load locked bets (written by frontend exportLockedProps())
    locked: list = []
    lp = DATA / "locked_props.json"
    if lp.exists():
        try:
            locked = json.loads(lp.read_text())
        except Exception:
            pass

    # Update bet probabilities
    live_bets = update_bet_probs(mlb, nba, nhl, locked)

    # Summarise live games
    active_mlb = [g for g in mlb  if g.get("state") == "in"]
    active_nba = [g for g in nba  if g.get("state") == "in"]
    active_nhl = [g for g in nhl  if g.get("state") in ("LIVE","CRIT")]
    active_cfb = [g for g in cfb  if g.get("state") == "in"]
    active_nfl = [g for g in nfl  if g.get("state") == "in"]

    payload = {
        "ts":         ts.isoformat(),
        "tsET":       et_now.strftime("%Y-%m-%d %H:%M:%S ET"),
        "mlb":        mlb,
        "nba":        nba,
        "nhl":        nhl,
        "cfb":        cfb,
        "nfl":        nfl,
        "liveBets":   live_bets,
        "activeCounts": {
            "mlb": len(active_mlb),
            "nba": len(active_nba),
            "nhl": len(active_nhl),
            "cfb": len(active_cfb),
            "nfl": len(active_nfl),
        },
        "hasLiveGames": bool(active_mlb or active_nba or active_nhl or active_cfb or active_nfl),
    }

    FE_LIVE.write_text(json.dumps(payload))
    DC_LIVE.write_text(json.dumps(payload))

    live_str = ", ".join(filter(None, [
        f"MLB:{len(active_mlb)}" if active_mlb else "",
        f"NBA:{len(active_nba)}" if active_nba else "",
        f"NHL:{len(active_nhl)}" if active_nhl else "",
        f"CFB:{len(active_cfb)}" if active_cfb else "",
        f"NFL:{len(active_nfl)}" if active_nfl else "",
    ])) or "no live games"
    log(f"Live poll: {live_str} | {len([b for b in live_bets if b.get('liveProb')])} bets tracked")
    return payload

def is_game_window() -> bool:
    """True during hours when games may be live (11am–1am ET)."""
    h = (datetime.now(timezone.utc) - timedelta(hours=5)).hour
    return h >= 11 or h == 0

def signal_handler(sig, frame):
    global _running
    log("Shutting down live tracker…")
    _running = False

# ─── ENTRY POINT ─────────────────────────────────────────────────────────────
def main() -> None:
    global _verbose, _running
    parser = argparse.ArgumentParser(description="Clairvoyance live stats tracker")
    parser.add_argument("--loop",    action="store_true", help="Run every 45s until stopped")
    parser.add_argument("--interval",type=int, default=45, help="Poll interval in seconds (default 45)")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--always",  action="store_true", help="Poll even outside game window hours")
    args = parser.parse_args()
    _verbose = args.verbose

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT,  signal_handler)

    if args.loop:
        log(f"Live tracker started — polling every {args.interval}s")
        idle_logged = False
        while _running:
            if args.always or is_game_window():
                idle_logged = False
                try:
                    poll_once()
                except Exception as e:
                    log(f"Poll error: {e}", "WARN")
            else:
                if not idle_logged:
                    log("Outside game window (11am–1am ET) — sleeping until 11am")
                    idle_logged = True
            for _ in range(args.interval):
                if not _running:
                    break
                time.sleep(1)
        log("Live tracker stopped.")
    else:
        # Single poll
        poll_once()

if __name__ == "__main__":
    main()
