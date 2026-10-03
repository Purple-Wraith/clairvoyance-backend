"""Season arithmetic derived from the date, so nothing needs hand-editing when a new season starts (added 2026-10-03).

Every helper takes `today` (a date/datetime, default: now in UTC) and an optional environment override, so a run can be pinned
(`CFB_SEASON_YEAR=2026 python3 scripts/fetch_cfb.py ...`) or flipped early/late without a code change.  Pure functions, no I/O.

  football_season_year("cfb"|"nfl")   year if month >= 7 else year-1    (the 2026 season runs Aug 2026 - Feb 2027)
                                       env: CFB_SEASON_YEAR / NFL_SEASON_YEAR, then FOOTBALL_SEASON_YEAR
  hockey_season_label()               "2026-27"  (QuantHockey): start year = year if month >= 9 else year-1
                                       env: QUANTHOCKEY_SEASON (e.g. "2026-27")
  soccer_season_start_year()          year if month >= 7 else year-1  (European club season, Aug - May)
                                       env: SOCCER_SEASON_START_YEAR

The same Jul-1 flip is what football_season_year documents because FBS/NFL schedules for the coming season are published by then
and the previous season (incl. bowls / Super Bowl) is long finished; the stats scripts additionally fall back to the previous season
while the new one has no games yet (see fetch_cfb.write_stats / fetch_nfl).  clairvoyance_update.py has its own nba_season_end_year /
nhl_season_end_year in the same style (month >= 10 / month >= 9 -> year+1).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone


def _as_date(today=None) -> date:
    if today is None:
        return datetime.now(timezone.utc).date()
    return today.date() if isinstance(today, datetime) else today


def _env_year(names, environ=None) -> int | None:
    env = os.environ if environ is None else environ
    for n in names:
        v = str(env.get(n, "") or "").strip()
        if v:
            try:
                y = int(v)
            except ValueError:
                continue
            if 2000 <= y <= 2100:
                return y
    return None


def football_season_year(sport: str | None = None, today=None, environ=None) -> int:
    """CFB / NFL season year (the calendar year the season STARTS in): year if month >= 7 else year-1."""
    names = ([f"{sport.upper()}_SEASON_YEAR"] if sport else []) + ["FOOTBALL_SEASON_YEAR"]
    ov = _env_year(names, environ)
    if ov is not None:
        return ov
    d = _as_date(today)
    return d.year if d.month >= 7 else d.year - 1


def hockey_season_label(today=None, environ=None) -> str:
    """QuantHockey-style label for the European leagues' current season, e.g. '2026-27'."""
    env = os.environ if environ is None else environ
    ov = str(env.get("QUANTHOCKEY_SEASON", "") or "").strip()
    if len(ov) == 7 and ov[4] == "-" and ov[:4].isdigit() and ov[5:].isdigit():
        return ov
    d = _as_date(today)
    start = d.year if d.month >= 9 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def soccer_season_start_year(today=None, environ=None) -> int:
    ov = _env_year(["SOCCER_SEASON_START_YEAR"], environ)
    if ov is not None:
        return ov
    d = _as_date(today)
    return d.year if d.month >= 7 else d.year - 1


# ── Prior-season snapshot rollover ────────────────────────────────────────────────────────────────────────────────────────────
# docs/cfb_team_stats.json always holds the "current" season's team stats (or last season's final stats while the new season has no
# games yet -- fetch_cfb.write_stats falls back).  docs/app.html's cfbMC blends in a FIXED 15% weight of the PREVIOUS season's final
# numbers.  That previous-season file used to be a one-off hand-made snapshot (cfb_team_stats_2025.json) that needed a manual redo every
# year.  roll_prior_snapshot() derives it instead: the moment the producer is about to overwrite cfb_team_stats.json with a NEWER
# season's stats, the file it is replacing (last season's final stats) is saved as docs/cfb_team_stats_prior.json -- once, never
# overwritten mid-season, no network, no hand-editing.
def roll_prior_snapshot(stats_path, prior_path, new_season: int, now_iso: str | None = None) -> str:
    """Call right BEFORE writing `new_season` stats to `stats_path`.  Returns what it did:
       'rolled'       stats_path held season new_season-1 (and prior_path was older/missing): copied to prior_path
       'same-season'  stats_path is already new_season (a mid-season refresh): prior untouched
       'already'      prior_path already holds new_season-1 (or newer): untouched
       'no-current'   stats_path missing/unreadable/no season: nothing to roll
       'gap'          stats_path is older than new_season-1 (a skipped season): refuse to relabel it as the prior season
       Never raises."""
    import json
    from pathlib import Path
    try:
        stats_path, prior_path = Path(stats_path), Path(prior_path)
        try:
            cur = json.loads(stats_path.read_text())
            cur_season = int(cur.get("season"))
        except Exception:
            return "no-current"
        if cur_season >= new_season:
            return "same-season"
        if cur_season != new_season - 1 or not cur.get("teams"):
            return "gap"
        try:
            prior_season = int(json.loads(prior_path.read_text()).get("season"))
        except Exception:
            prior_season = None
        if prior_season is not None and prior_season >= cur_season:
            return "already"
        out = dict(cur)
        out["note"] = (f"Previous-season ({cur_season}) final team stats, rolled automatically from cfb_team_stats.json when "
                       f"{new_season} stats started (scripts/_season.py roll_prior_snapshot). Never overwritten mid-season.")
        out["rolled_at"] = now_iso or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        tmp = prior_path.with_name(prior_path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(out, indent=2))
        os.replace(tmp, prior_path)
        return "rolled"
    except Exception:
        return "no-current"
