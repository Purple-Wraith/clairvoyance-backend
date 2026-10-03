"""ESPN-sourced NBA team ratings + player-prop inputs (stdlib only; HTTP is injected, so every function is testable offline).

WHY (2026-10-03): the NBA model's team ratings (data.json nba.teamAdv / nba.teamRatings / nba.fourFactors) came from
Basketball-Reference, which sits behind Cloudflare and can block GitHub Actions IPs.  ESPN's public JSON is already used for
standings / rosters / injuries and is reachable from CI, so ESPN is now the PRIMARY source and Basketball-Reference only a
fallback for teams ESPN could not supply (see clairvoyance_update.collect_nba_season_data).

ESPN endpoints used (all plain unauthenticated GETs, query string omitted here -- see the *_URL / *_PARAMS constants):
  byteam    site.web.api.espn.com/apis/common/v3/sports/basketball/nba/statistics/byteam      ONE request -> all 30 teams, season totals
            for the team ("Own ...", splitId 0) AND its opponents ("Opponent ...", splitId 900): FGM/FGA/3PM/FTM/FTA/OREB/DREB/TOV/PTS.
            That is everything needed for possessions, offensive/defensive rating, pace and the four factors.
  byathlete site.web.api.espn.com/apis/common/v3/sports/basketball/nba/statistics/byathlete    per-player season per-game averages, paged
  gamelog   site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{id}/gamelog  one player's per-game box-score line, whole season
  injuries  site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries                       availability

POSSESSIONS.  The textbook estimate FGA + 0.44*FTA - OREB + TOV overstates possessions by ~2.5% against Basketball-Reference's
numbers (pace +2.5, ortg -2.2 for all 30 teams in 2025-26), so this module uses Basketball-Reference's own formula
    poss = 0.5 * (tm + opp),   tm = FGA + 0.4*FTA - 1.07*(ORB/(ORB + oppDRB))*(FGA - FGM) + TOV
Two ESPN quirks are corrected by constants fitted on 2024-25 and 2025-26 (both seasons agree to ~0.1):
  * ESPN's team `turnovers` excludes team turnovers (shot-clock violations etc.) that Basketball-Reference's TOV includes: ~0.75/game.
  * Basketball-Reference's pace divides by minutes played (overtime games have >48), ESPN's totals carry no minutes: x0.995.
ESPN does not publish SRS/SOS, so rows carry srs=None and the rating model falls back to MOV (see clairvoyance_update._nba_mov_prior).

ESPN publishes NO player prop lines (no points/rebounds/assists over-under numbers anywhere in the public feeds); the app's "lines"
are model-generated from the season averages built here.
"""
from __future__ import annotations

import statistics
import time
from typing import Any, Callable

# ── endpoints ────────────────────────────────────────────────────────────────
BYTEAM_URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/statistics/byteam"
BYATHLETE_URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/statistics/byathlete"
GAMELOG_URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{id}/gamelog"
INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
_COMMON = {"region": "us", "lang": "en", "contentorigin": "espn"}

# ── calibration (fitted against Basketball-Reference 2024-25 and 2025-26, all 30 teams) ─────────────
NBA_ESPN_TEAM_TOV_PER_GAME = 0.75     # team turnovers ESPN's `turnovers` leaves out
NBA_ESPN_OT_PACE_FACTOR = 0.995       # pace is per 48 min; overtime minutes make per-game possessions ~0.5% too high

NBA_TEAM_ABBRS = frozenset(
    "ATL BOS BKN BRK CHA CHO CHI CLE DAL DEN DET GS GSW HOU IND LAC LAL MEM MIA MIL MIN NO NOP NY NYK OKC ORL PHI PHX PHO POR "
    "SA SAS SAC TOR UTAH UTA WSH WAS".split())

PROPS_MAX_PLAYERS = 150
PROPS_MIN_GP = 5                      # a player needs this many games to be in the feed
PROPS_GAMELOG_BUDGET_S = 150.0        # stop fetching game logs after this long; the rest keep cached form (or none)
PROPS_PACE_S = 0.15                   # pause between per-player requests
FORM_N = 5


def _f(x, default=None):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default


# ══════════════════════════════════════════════════════════════════════════════
# HTTP: retry + backoff + per-source health.  `getter(url, params=None, timeout=N)` -> object with .status_code/.json()
# ══════════════════════════════════════════════════════════════════════════════
class Ctx:
    """Injected environment: HTTP getter, logger, sleep, clock.  Collects per-endpoint timing/health."""

    def __init__(self, getter: Callable, log: Callable[[str, str], None] | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, backoff: tuple = (1.0, 2.0, 4.0), tries: int = 3, timeout: int = 20):
        self.getter, self.sleep, self.clock = getter, sleep, clock
        self.backoff, self.tries, self.timeout = backoff, tries, timeout
        self._log = log or (lambda m, lvl="INFO": print(f"[{lvl}] {m}"))
        self.health: dict[str, dict] = {}

    def log(self, msg: str, level: str = "INFO") -> None:
        self._log(msg, level)

    def _h(self, label: str) -> dict:
        return self.health.setdefault(label, {"requests": 0, "retries": 0, "failures": 0, "seconds": 0.0, "last": ""})

    def get_json(self, url: str, params: dict | None, label: str) -> tuple[Any, str]:
        """GET + parse JSON with up to `tries` attempts (exponential backoff).  Returns (data, status):
             "ok"          data is the parsed JSON
             "not_found"   HTTP 404, or ESPN's 500 {"code":2404} (a season/stat that does not exist yet) -- permanent, not retried
             "failed: ..." every attempt failed (data is None) -- always logged as a WARN with the reason
        Never raises."""
        h = self._h(label)
        reason = "no attempt"
        for attempt in range(self.tries):
            t0 = self.clock()
            h["requests"] += 1
            transient = True
            try:
                r = self.getter(url, params=params, timeout=self.timeout)
                sc = getattr(r, "status_code", 0)
                if sc == 200:
                    try:
                        data = r.json()
                    except Exception as exc:               # HTML error page served with 200
                        reason = f"invalid JSON ({type(exc).__name__})"
                    else:
                        h["seconds"] += self.clock() - t0
                        h["last"] = "ok"
                        return data, "ok"
                else:
                    body_code = None
                    try:
                        body = r.json()
                        body_code = body.get("code") if isinstance(body, dict) else None
                    except Exception:
                        pass
                    if sc == 404 or body_code == 2404:
                        h["seconds"] += self.clock() - t0
                        h["last"] = "not_found"
                        return None, "not_found"
                    reason = f"HTTP {sc}"
                    if sc in (400, 401, 403):
                        transient = False
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
            h["seconds"] += self.clock() - t0
            if not transient or attempt == self.tries - 1:
                break
            h["retries"] += 1
            self.log(f"{label}: {reason} -- retry {attempt + 1}/{self.tries - 1} after {self.backoff[min(attempt, len(self.backoff) - 1)]:g}s", "WARN")
            self.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
        h["failures"] += 1
        h["last"] = f"failed: {reason}"
        self.log(f"{label}: FAILED after {h['requests']} request(s) total -- {reason} ({url})", "WARN")
        return None, f"failed: {reason}"

    def totals(self) -> dict:
        return {"requests": sum(v["requests"] for v in self.health.values()),
                "retries": sum(v["retries"] for v in self.health.values()),
                "failures": sum(v["failures"] for v in self.health.values()),
                "seconds": round(sum(v["seconds"] for v in self.health.values()), 1)}

    def summary(self) -> dict:
        """Compact per-endpoint health for data.json nba.health.endpoints."""
        return {k: {"req": v["requests"], "retries": v["retries"], "sec": round(v["seconds"], 1), "last": v["last"]}
                for k, v in self.health.items()}


# ══════════════════════════════════════════════════════════════════════════════
# Team ratings from ESPN byteam
# ══════════════════════════════════════════════════════════════════════════════
def parse_byteam(data: dict | None, season: int | None = None) -> tuple[dict, str]:
    """ESPN `statistics/byteam` JSON -> ({abbr: {"id","name","own":{stat: total}, "opp":{stat: total}}}, diagnostic).
    diagnostic is "" on success.  A response whose requestedSeason differs from `season` is rejected (ESPN silently serves
    the current season for some bad parameter combinations)."""
    if not isinstance(data, dict) or not data.get("teams"):
        return {}, "no `teams` in response"
    if season is not None:
        got = ((data.get("requestedSeason") or {}).get("year"))
        if got is not None and int(got) != int(season):
            return {}, f"ESPN returned season {got}, asked for {season}"
    names = {c["name"]: c["names"] for c in data.get("categories") or [] if c.get("names")}
    if not names:
        return {}, "no stat-name table (`categories[].names`) in response"
    out: dict = {}
    for t in data["teams"]:
        tm = t.get("team") or {}
        abbr = tm.get("abbreviation")
        if not abbr:
            continue
        sides: dict = {"own": {}, "opp": {}}
        for c in t.get("categories") or []:
            nm = c.get("name")
            if nm not in names:
                continue
            opp = str(c.get("splitId")) == "900" or str(c.get("displayName", "")).lower().startswith("opponent")
            sides["opp" if opp else "own"].update(zip(names[nm], c.get("values") or []))
        if sides["own"] and sides["opp"]:
            out[abbr] = {"id": str(tm.get("id") or ""), "name": tm.get("displayName") or abbr, **sides}
    if not out:
        return {}, "teams present but none had own+opponent stat splits"
    return out, ""


def _poss(tm: dict, opp: dict, gp: float, tov_adj: float) -> float:
    fga, fgm = tm["fieldGoalsAttempted"], tm["fieldGoalsMade"]
    orb = tm["offensiveRebounds"]
    opp_drb = opp.get("defensiveRebounds")
    if opp_drb is None:
        opp_drb = opp["rebounds"] - opp["offensiveRebounds"]
    orb_share = orb / (orb + opp_drb) if (orb + opp_drb) else 0.0
    return fga + 0.4 * tm["freeThrowsAttempted"] - 1.07 * orb_share * (fga - fgm) + tm["turnovers"] + tov_adj * gp


def row_from_raw(raw: dict, tov_adj: float = NBA_ESPN_TEAM_TOV_PER_GAME, ot_factor: float = NBA_ESPN_OT_PACE_FACTOR) -> dict:
    """One team's byteam totals -> a row in the SAME schema as clairvoyance_update.parse_nba_advanced_team (so
    select_nba_team_stats / build_nba_team_ratings take it unchanged).  w/l are 0 here; attach_records() fills them from standings.
    A team with 0 games gets ortg/drtg/pace None (like BBRef's empty preseason table)."""
    own, opp = raw["own"], raw["opp"]
    gp = int(_f(own.get("gamesPlayed"), 0) or 0)
    row = {"name": raw.get("name"), "w": 0, "l": 0, "gp": gp, "mov": None, "sos": None, "srs": None,
           "ortg": None, "drtg": None, "net_rtg": None, "pace": None, "ts_pct": None, "efg_pct": None, "tov_pct": None,
           "orb_pct": None, "ft_rate": None, "opp_efg_pct": None, "opp_tov_pct": None, "drb_pct": None, "opp_ft_rate": None}
    if gp <= 0:
        return row
    try:
        p_tm, p_opp = _poss(own, opp, gp, tov_adj), _poss(opp, own, gp, tov_adj)
        poss = 0.5 * (p_tm + p_opp)
        if poss <= 0:
            return row
        pts, opts = own["points"], opp["points"]
        row["mov"] = round((pts - opts) / gp, 2)
        row["ortg"] = round(100 * pts / poss, 1)
        row["drtg"] = round(100 * opts / poss, 1)
        row["net_rtg"] = round(row["ortg"] - row["drtg"], 1)
        row["pace"] = round(ot_factor * poss / gp, 1)

        def four(a, b):
            fga, fta, tov = a["fieldGoalsAttempted"], a["freeThrowsAttempted"], a["turnovers"] + tov_adj * gp
            orb, drb_b = a["offensiveRebounds"], b.get("defensiveRebounds")
            if drb_b is None:
                drb_b = b["rebounds"] - b["offensiveRebounds"]
            return {"efg": (a["fieldGoalsMade"] + 0.5 * a["threePointFieldGoalsMade"]) / fga,
                    "ts": a["points"] / (2 * (fga + 0.44 * fta)),
                    "tov": 100 * tov / (fga + 0.44 * fta + tov),
                    "orb": 100 * orb / (orb + drb_b),
                    "ftr": a["freeThrowsMade"] / fga}
        a, b = four(own, opp), four(opp, own)
        row.update(efg_pct=round(a["efg"], 3), ts_pct=round(a["ts"], 3), tov_pct=round(a["tov"], 1),
                   orb_pct=round(a["orb"], 1), ft_rate=round(a["ftr"], 3),
                   opp_efg_pct=round(b["efg"], 3), opp_tov_pct=round(b["tov"], 1), opp_ft_rate=round(b["ftr"], 3),
                   drb_pct=round(100 - b["orb"], 1))
    except (KeyError, TypeError, ZeroDivisionError):
        # a stat column vanished: keep the row but with no ratings (callers treat ortg None as "no data" and fall back)
        row.update(ortg=None, drtg=None, net_rtg=None, pace=None)
    return row


def rows_from_byteam(data: dict | None, season: int | None = None) -> tuple[dict, str]:
    raw, diag = parse_byteam(data, season)
    if not raw:
        return {}, diag
    return {a: row_from_raw(r) for a, r in raw.items()}, ""


def attach_records(rows: dict, standings: dict | None) -> dict:
    """Fill w/l (and gp when the row has none) from ESPN standings ({abbr: {"w","l",...}}); returns rows (mutated)."""
    for a, r in rows.items():
        st = (standings or {}).get(a)
        if not st:
            continue
        w, l = _f(st.get("w")), _f(st.get("l"))
        if w is not None and l is not None:
            r["w"], r["l"] = int(w), int(l)
    return rows


def compare_rows(espn_rows: dict, ref_rows: dict, keys=("ortg", "drtg", "net_rtg", "pace", "efg_pct", "ts_pct", "tov_pct",
                                                         "orb_pct", "ft_rate", "opp_efg_pct", "opp_tov_pct", "drb_pct", "opp_ft_rate")) -> dict:
    """Mean absolute / signed difference per metric over the teams both have: {key: {"mae","bias","max","n"}}."""
    out = {}
    for k in keys:
        d = [espn_rows[a][k] - ref_rows[a][k] for a in espn_rows
             if a in ref_rows and espn_rows[a].get(k) is not None and ref_rows[a].get(k) is not None]
        if d:
            out[k] = {"mae": round(statistics.mean(abs(x) for x in d), 3), "bias": round(statistics.mean(d), 3),
                      "max": round(max(abs(x) for x in d), 3), "n": len(d)}
    return out


def fetch_team_rows(ctx: Ctx, season: int) -> tuple[dict, dict]:
    """One byteam request for `season` (regular season).  Returns (rows_by_abbr, info) where
    info = {season, status: ok|not_found|failed: ..., teams, gpMedian, hardFail, reason}.
    `hardFail` is True only for a real failure (network / 5xx / unparseable) -- "not_found" means ESPN has no table for that season
    yet (the new season before game 1) and is expected."""
    data, status = ctx.get_json(BYTEAM_URL, {**_COMMON, "season": season, "seasontype": 2}, f"espn.byteam.{season}")
    info = {"season": season, "status": status, "teams": 0, "gpMedian": 0, "hardFail": status.startswith("failed"), "reason": status}
    if status == "not_found":
        ctx.log(f"NBA ESPN team stats {season}: ESPN has no regular-season table yet (expected before game 1)", "INFO")
        return {}, info
    if data is None:
        return {}, info
    rows, diag = rows_from_byteam(data, season)
    if not rows:
        info.update(status=f"failed: {diag}", hardFail=True, reason=diag)
        ctx.log(f"NBA ESPN team stats {season}: 0 teams parsed -- {diag}", "WARN")
        return {}, info
    gps = sorted(r["gp"] for r in rows.values())
    info.update(teams=len(rows), gpMedian=gps[len(gps) // 2], reason="ok")
    if len(rows) != 30:
        ctx.log(f"NBA ESPN team stats {season}: parsed {len(rows)} teams (expected 30)", "WARN")
    return rows, info


# ══════════════════════════════════════════════════════════════════════════════
# Player props inputs
# ══════════════════════════════════════════════════════════════════════════════
def _cat_values(row: dict, cat_names: dict) -> dict:
    vals: dict = {}
    for c in row.get("categories") or []:
        for nm, v in zip(cat_names.get(c.get("name"), []), c.get("values") or []):
            vals.setdefault(nm, v)
    return vals


def parse_byathlete(data: dict | None) -> list[dict]:
    """ESPN `statistics/byathlete` JSON -> [{id,name,team,pos,gp,min_total,mpg,ppg,rpg,apg,tpm,spg,bpg}] (file order)."""
    cat_names = {c.get("name"): c.get("names") or [] for c in (data or {}).get("categories") or []}
    out = []
    for row in (data or {}).get("athletes") or []:
        ath = row.get("athlete") or {}
        v = _cat_values(row, cat_names)
        if not ath.get("id") or v.get("avgPoints") is None:
            continue
        out.append({"id": str(ath["id"]), "name": ath.get("displayName", ""), "team": ath.get("teamShortName", ""),
                    "pos": (ath.get("position") or {}).get("abbreviation", ""),
                    "gp": int(_f(v.get("gamesPlayed"), 0) or 0), "min_total": _f(v.get("minutes"), 0.0) or 0.0,
                    "mpg": round(_f(v.get("avgMinutes"), 0.0) or 0.0, 1), "ppg": round(_f(v.get("avgPoints"), 0.0) or 0.0, 1),
                    "rpg": round(_f(v.get("avgRebounds"), 0.0) or 0.0, 1), "apg": round(_f(v.get("avgAssists"), 0.0) or 0.0, 1),
                    "tpm": round(_f(v.get("avgThreePointFieldGoalsMade"), 0.0) or 0.0, 1),
                    "spg": round(_f(v.get("avgSteals"), 0.0) or 0.0, 1), "bpg": round(_f(v.get("avgBlocks"), 0.0) or 0.0, 1)})
    return out


def fetch_player_pool(ctx: Ctx, season: int, pages: int = 2, limit: int = 100) -> tuple[list[dict], str]:
    """Top players by total minutes for `season` (regular season).  Returns (players, status); status "ok" when at least one page parsed."""
    pool: list[dict] = []
    status = "ok"
    for page in range(1, pages + 1):
        data, st = ctx.get_json(BYATHLETE_URL, {**_COMMON, "isqualified": "false", "page": page, "limit": limit,
                                                "sort": "general.minutes:desc", "season": season, "seasontype": 2},
                                f"espn.byathlete.{season}")
        if data is None:
            status = st if not pool else status
            break
        got = parse_byathlete(data)
        pool.extend(got)
        if len(got) < limit * 0.5:       # last page (or season with no data yet)
            break
        ctx.sleep(0.2)
    if not pool and status == "ok":
        status = "empty"
    return pool, status


def parse_gamelog(data: dict | None) -> list[dict]:
    """ESPN athlete `gamelog` JSON -> regular-season games, newest first:
    [{date, min, pts, reb, ast, tpm, opp, home}].  Rows where the player did not play (0/blank minutes) are dropped."""
    if not isinstance(data, dict):
        return []
    names = data.get("names") or []
    ix = {n: i for i, n in enumerate(names)}
    need = ("minutes", "totalRebounds", "assists", "points", "threePointFieldGoalsMade-threePointFieldGoalsAttempted")
    if any(n not in ix for n in need):
        return []
    events = data.get("events") or {}
    games = []
    for st in data.get("seasonTypes") or []:
        if "regular" not in str(st.get("displayName", "")).lower():
            continue
        for cat in st.get("categories") or []:
            for ev in cat.get("events") or []:
                stats = ev.get("stats") or []
                if len(stats) < len(names):
                    continue
                mins = _f(stats[ix["minutes"]], 0.0)
                if not mins:
                    continue
                meta = events.get(str(ev.get("eventId"))) or {}
                opp = (meta.get("opponent") or {}).get("abbreviation", "")
                if opp and opp not in NBA_TEAM_ABBRS:
                    continue                      # the All-Star game is filed under "Regular Season" (opponent "STARS"/"WORLD"/...) -- not a real game
                made = str(stats[ix["threePointFieldGoalsMade-threePointFieldGoalsAttempted"]]).split("-")[0]
                games.append({"date": (meta.get("gameDate") or "")[:10], "min": mins,
                              "pts": _f(stats[ix["points"]], 0.0), "reb": _f(stats[ix["totalRebounds"]], 0.0),
                              "ast": _f(stats[ix["assists"]], 0.0), "tpm": _f(made, 0.0),
                              "opp": opp, "home": meta.get("atVs") == "vs"})
    games.sort(key=lambda g: g["date"], reverse=True)
    return games


def form_from_games(games: list[dict], n: int = FORM_N, min_games_for_sd: int = 10) -> dict:
    """{"last5": {...}|None, "stdev": {...}|None, "lastGame": "YYYY-MM-DD", "n": games}.  `games` newest first."""
    if not games:
        return {"last5": None, "stdev": None, "lastGame": None, "n": 0}
    last = games[:n]
    avg = lambda key, rows: round(sum(g[key] for g in rows) / len(rows), 1)
    last5 = {"n": len(last), "ppg": avg("pts", last), "rpg": avg("reb", last), "apg": avg("ast", last),
             "tpm": avg("tpm", last), "mpg": avg("min", last)}
    sd = None
    if len(games) >= min_games_for_sd:
        sd = {k: round(statistics.stdev([g[src] for g in games]), 2) for k, src in (("pts", "pts"), ("reb", "reb"), ("ast", "ast"), ("tpm", "tpm"))}
    return {"last5": last5, "stdev": sd, "lastGame": games[0]["date"] or None, "n": len(games)}


def normalize_status(raw: str | None) -> str:
    s = (raw or "").strip()
    return s if s else "active"


def injury_status_maps(rows: list[dict] | None) -> tuple[dict, dict]:
    """flat injury rows ({id,name,status,...}) -> ({athlete_id: status}, {lowercase name: status})."""
    by_id, by_name = {}, {}
    for r in rows or []:
        st = normalize_status(r.get("status"))
        if r.get("id"):
            by_id[str(r["id"])] = st
        if r.get("name"):
            by_name[r["name"].lower()] = st
    return by_id, by_name


def build_player_props(pool: list[dict], roster: dict | None, injuries: list[dict] | None, forms: dict, *, season: int,
                       generated: str, max_players: int = PROPS_MAX_PLAYERS, min_gp: int = PROPS_MIN_GP,
                       form_info: dict | None = None) -> dict:
    """Pure: assemble the `nba.playerProps` block.
      pool      parse_byathlete rows (any order)       roster  {lowercase name: {team,pos,id}} (current ESPN rosters; may be empty)
      injuries  flat injury rows                        forms   {athlete_id: form_from_games(...) dict}  (missing id -> no form)
    Team/position come from the CURRENT roster when the player is on one (a player who changed teams in the offseason is listed with
    his new team); players on no current roster are dropped when a roster is available (retired/unsigned players are not prop targets)."""
    roster = roster or {}
    by_roster_id = {str(v.get("id")): v for v in roster.values() if v.get("id")}
    inj_id, inj_name = injury_status_maps(injuries)
    ranked = sorted((p for p in pool if p["gp"] >= min_gp), key=lambda p: (-p["min_total"], p["name"]))
    players, dropped = [], 0
    for p in ranked:
        cur = by_roster_id.get(p["id"]) or roster.get(p["name"].lower())
        if roster and not cur:
            dropped += 1
            continue
        team = (cur or {}).get("team") or p["team"]
        pos = (cur or {}).get("pos") or p["pos"]
        fm = forms.get(p["id"]) or {}
        players.append({"id": p["id"], "name": p["name"], "team": team, "pos": pos, "gp": p["gp"], "mpg": p["mpg"],
                        "ppg": p["ppg"], "rpg": p["rpg"], "apg": p["apg"], "tpm": p["tpm"], "spg": p["spg"], "bpg": p["bpg"],
                        "last5": fm.get("last5"), "stdev": fm.get("stdev"), "lastGame": fm.get("lastGame"),
                        "status": inj_id.get(p["id"]) or inj_name.get(p["name"].lower()) or "active"})
        if len(players) >= max_players:
            break
    block = {"season": season, "generated": generated, "source": "espn",
             "propLines": "none -- ESPN publishes no player prop lines; engine lines are model-generated from these averages",
             "n": len(players), "droppedNoRoster": dropped,
             "withForm": sum(1 for q in players if q["last5"]), "players": players}
    if form_info:
        block["formInfo"] = form_info
    return block


def fetch_player_props(ctx: Ctx, season: int, roster: dict | None, injuries: list[dict] | None, prev_block: dict | None,
                       generated: str, max_players: int = PROPS_MAX_PLAYERS) -> tuple[dict, dict]:
    """Network orchestration for nba.playerProps.  Returns (block, info); block is {} when ESPN gave no player pool
    (caller carries the previous block forward).  Game logs are fetched only for players whose games-played changed since
    `prev_block` (cached form is reused otherwise) and stop after PROPS_GAMELOG_BUDGET_S, so a typical in-season run costs
    ~half the players' game logs and an offseason/preseason run costs none."""
    pool, status = fetch_player_pool(ctx, season)
    info = {"season": season, "poolStatus": status, "pool": len(pool), "gamelogFetched": 0, "gamelogCached": 0,
            "gamelogFailed": 0, "gamelogSkippedBudget": 0}
    if not pool:
        ctx.log(f"NBA playerProps: ESPN byathlete season={season} gave no players ({status})", "WARN")
        return {}, info
    # rank first (same rule build_player_props uses) so only the players that make the feed cost a game-log request
    prelim = build_player_props(pool, roster, injuries, {}, season=season, generated=generated, max_players=max_players)["players"]
    prev = {}
    if prev_block and prev_block.get("season") == season:
        prev = {p["id"]: p for p in prev_block.get("players") or [] if p.get("id")}
    forms: dict = {}
    t_start = ctx.clock()
    stale_from_prev = 0
    for p in prelim:
        pid, old = p["id"], prev.get(p["id"])
        if old and old.get("gp") == p["gp"] and old.get("last5") is not None:
            forms[pid] = {"last5": old["last5"], "stdev": old.get("stdev"), "lastGame": old.get("lastGame")}
            info["gamelogCached"] += 1
            continue
        if ctx.clock() - t_start > PROPS_GAMELOG_BUDGET_S:
            info["gamelogSkippedBudget"] += 1
            if old and old.get("last5") is not None:
                forms[pid] = {"last5": old["last5"], "stdev": old.get("stdev"), "lastGame": old.get("lastGame")}
                stale_from_prev += 1
            continue
        data, st = ctx.get_json(GAMELOG_URL.format(id=pid), {**_COMMON, "season": season}, "espn.gamelog")
        ctx.sleep(PROPS_PACE_S)
        if data is None:
            info["gamelogFailed"] += 1
            if old and old.get("last5") is not None:
                forms[pid] = {"last5": old["last5"], "stdev": old.get("stdev"), "lastGame": old.get("lastGame")}
                stale_from_prev += 1
            continue
        games = parse_gamelog(data)
        if games:
            forms[pid] = form_from_games(games)
            info["gamelogFetched"] += 1
        else:
            info["gamelogFailed"] += 1       # 200 but no regular-season rows / unknown layout
    info["staleFormFromPrev"] = stale_from_prev
    if info["gamelogFailed"] or info["gamelogSkippedBudget"]:
        ctx.log(f"NBA playerProps: {info['gamelogFailed']} game-log failure(s), {info['gamelogSkippedBudget']} skipped over the "
                f"{PROPS_GAMELOG_BUDGET_S:.0f}s budget; {stale_from_prev} kept stale cached form", "WARN")
    block = build_player_props(pool, roster, injuries, forms, season=season, generated=generated, max_players=max_players,
                               form_info={k: info[k] for k in ("gamelogFetched", "gamelogCached", "gamelogFailed", "gamelogSkippedBudget")})
    return block, info
