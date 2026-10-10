"""Confirmed lineups at the lock pass: NHL starting goalies, NBA and NFL game-day Out lists.  Fail-open, no Supabase, no app.html edits.

WHAT IT DOES
  The pre-kickoff sweep (scheduler/worker.js sweepsDue -> lock-watchdog.yml -> auto_lock_settle.py --watchdog --auto-lock) and every other lock
  pass call `assess_leg` (through auto_lock_settle.build_qualifying) for each qualifying GAME leg whose game starts within LINEUP_WINDOW_MIN.
  The leg comes back as one of
      none     nothing changed (lineup unknown / same lineup the model assumed / change is in the leg's favour / immaterial);
      reprice  the lineup is worse than the model assumed, the leg STILL qualifies at the adjusted probability: it is locked with the adjusted
               winProb (and the original in `lineup.adj.pBefore`);
      hold     the leg no longer qualifies at the adjusted probability (or a hard rule such as "QB out" fired): it is NOT locked this pass.
  Every checked leg gets an additive `lineup` object that rides lockPick's extraMeta onto the pick record:
      {"status": "confirmed" | "unknown", "notes": [...], "checkedAt": ISO, "src": "espn", "action": "none"|"reprice", "adj": {...}}
  "unknown" (any fetch / parse / match failure, or a not-yet-confirmed lineup) NEVER changes a leg: behaviour is exactly the pre-existing one.

SOURCES (all free, no key; verified live from the build machine 2026-10-10, see the commit message)
  NHL  ESPN scoreboard `competitors[].probables[name=probableStartingGoalie]` (type confirmed | expected).  The SAME field scripts/_nhl_goalies.py
       already stores in docs/nhl_schedule.json, but fetched FRESH here because that file is only refreshed 2-3x a day.
  NBA  ESPN `summary?event=ID` -> `injuries` (the game's own availability list: Out / Day-To-Day / ...).  The official NBA injury report is a PDF
       and is not parsed; ESPN mirrors it.
  NFL  ESPN `summary?event=ID` -> `injuries` (Out / Doubtful / Questionable).  ESPN publishes no separate "inactives" list before kickoff
       (checked: the pre-game summary has no inactive field; the core roster endpoint 404s before the game and its `active` flag is
       meaningless after it), so the game-day inactive list is NOT directly available: an `Out` designation is what we can act on, and a key
       player still Questionable/Doubtful leaves the lineup "unknown".

WHAT "THE MODEL ASSUMED" MEANS (never invent numbers: everything below mirrors docs/app.html and uses data the repo already has)
  NHL  nhlMC/nhlEns rate each team on its PRIMARY goalie = MoneyPuck's highest-games-played `situation=all` goalie (docs/data.json mp.goalies,
       _nhlApplyGoalieStats).  goalieComposite = .7*gsax + .18*(sv-.91)*100 + .12*(hdsv-.90)*100, scaled by (1 - severity) when that goalie is
       Out/IR in the injury feed (so a primary goalie the model already treats as out is NOT counted again).  The opponent's scoring rate is
       multiplied by clamp(1 - composite/80, .87, 1.13).  The ensemble adds .19*(home gsax - away gsax)/100 and weights the Monte Carlo at .64.
       A replacement with no MoneyPuck row is rated league average (composite 0) -- the model's own neutral value for an Out goalie.
  NBA  computeInjuryImpact: per-player win-probability penalty by rating (PREMIUM .070 / OPTIMAL .045 / GOOD .025 / other .012), summed and capped
       at .12, from docs/data.json injuries.nba (Out, Doubtful, Questionable, Day-To-Day).  Only the part NOT already in that snapshot is added.
  NFL  nflMC: QB1 2.5 pts, WR1/WR2/RB1/TE1 0.4 pts each (skill capped 1.0, team capped 3.5), from docs/nfl_injuries.json x depth chart in
       docs/nfl_player_stats.json.  Only the part not already in that snapshot is added.  Offensive line has no model input: the repo's own generic
       injury table (computeInjuryImpact baseW, 0.022 per lineman) is used as the estimate.
"""
from __future__ import annotations

import json
import math
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _nhl_goalies as NG  # noqa: E402  (pure ESPN probables parser + app team-abbreviation map)

ROOT = Path(__file__).resolve().parent.parent

# ── SWITCHES ────────────────────────────────────────────────────────────────────────────────────────────────────────────
LINEUP_GATE_ENABLED = True       # MASTER SWITCH.  False: no fetch, no `lineup` field, no hold, no reprice -- the lock passes behave exactly as before.
LINEUP_NHL_ENABLED = True        # per-sport switches (only consulted when the master switch is on)
LINEUP_NBA_ENABLED = True
LINEUP_NFL_ENABLED = True
LINEUP_ENV_OFF = "LINEUP_GATE"   # environment kill switch with no code push: LINEUP_GATE=off|0|false disables everything (a workflow `env:` or repo variable can set it)

# ── WHEN AND HOW MUCH WE LOOK ───────────────────────────────────────────────────────────────────────────────────────────
LINEUP_WINDOW_MIN = 150          # only games starting within this many minutes are checked (same as auto_lock_settle.AUTO_LOCK_WINDOW_MIN): lineups are
                                 # announced well inside it (NHL morning skate ~ 3-8 h out, NFL inactives 90 min), and the sweep runs 40-50 min out.
LINEUP_HTTP_TIMEOUT_S = 12       # per request
LINEUP_TOTAL_BUDGET_S = 75       # all lineup requests of one run together; past this every remaining lookup is "unknown" (never blocks a lock)
LINEUP_MAX_REQUESTS = 40         # hard ceiling per run (1 scoreboard per sport-day + 1 summary per NBA/NFL game in the window)
LINEUP_MATCH_TOLERANCE_MIN = 180 # an ESPN event matches a leg when home+away agree and the start times are within this many minutes

# ── NHL RULES ───────────────────────────────────────────────────────────────────────────────────────────────────────────
NHL_MC_ENSEMBLE_WEIGHT = 0.64    # nhlEns: mc.hwP*(.64/.50)*NHL_ENS.mc at the default NHL_ENS (.50): share of the ensemble that is the goal-rate simulation
NHL_GSAX_ENSEMBLE_WEIGHT = 0.19  # nhlEns: edgeRaw = gE*.19 ..., gE = (home gsax - away gsax)/100 enters the ensemble at weight 1 (edgeRaw*(1/.30)*.30)
NHL_LG_GOALS = 2.9               # app.html _NHL_LG_GF60: scoring rate used only when the pick carries no "MC PROJ" line to size the sensitivity
NHL_REPLACEMENT_MIN_SHOTS = 60   # a replacement goalie needs at least this many shots faced (about two starts) before his own season rows are used: the model's composite
                                 # reads a goalie with 9 shots faced and 0-for-1 on high-danger chances as -10.9 (hdsv term), which is noise.  Below it he is rated league
                                 # average, the same neutral value the model gives an injured starter.  (The goalie the model ALREADY rates is mirrored as-is.)
NHL_MIN_REPRICE = 0.002         # an estimated adverse swing below 0.2 points of probability is immaterial: leave the leg exactly as it is
NHL_STALE_MARKET_ASSUME = False  # False: a market-blended leg moves by only (1 - blendAlpha) of the model-level swing, because the odds are refreshed minutes
                                 # before the sweep and a confirmed goalie is priced by the books within minutes.  True: assume the posted price does NOT yet
                                 # know the goalie news (apply the full model-level swing).  See the impact note: with alpha ~ .93 this choice is the whole story.
LINEUP_UNVERIFIED_HOLD_SWING = 0.01   # when the leg's ORIGINAL qualification cannot be reproduced here (the app's own calibration lifted it) the margin is unknown:
                                      # hold it if the estimated adverse swing is at least this much, otherwise just reprice

# ── NBA RULES ───────────────────────────────────────────────────────────────────────────────────────────────────────────
NBA_TOP_ROTATION_MPG = 28.0      # minutes per game (docs/data.json nba.playerProps.players, last season) at or above which a player counts as top rotation
NBA_PENALTY_BY_RATING = {"PREMIUM": 0.070, "OPTIMAL": 0.045, "GOOD": 0.025}   # computeInjuryImpact baseW (nba); every other player 0.012
NBA_PENALTY_DEFAULT = 0.012
NBA_PENALTY_CAP = 0.12           # computeInjuryImpact: total capped at 12 points
NBA_MIN_REPRICE = 0.005          # a newly-out player must be worth at least half a point of win probability to matter (an unrated bench player is 1.2 points but
                                 # is not "top rotation" -- see NBA_TOP_ROTATION_MPG; only top-rotation / rated players are counted at all)

# ── NFL RULES ───────────────────────────────────────────────────────────────────────────────────────────────────────────
NFL_INJ_QB_PTS = 2.5             # app.html NFL_INJ_QB_PTS .. NFL_INJ_STALE_H: nflMC's own injury inputs (points of margin)
NFL_INJ_SKILL_PTS = 0.4
NFL_INJ_SKILL_CAP = 1.0
NFL_INJ_TEAM_CAP = 3.5
NFL_INJ_STALE_H = 60             # the model ignores an injury file older than this, so then NOTHING in it was "already priced"
NFL_WIN_PROB_PER_POINT = 0.03    # one point of margin ~ 3 points of win/cover probability at an NFL spread (game sd ~13.5 points -> density ~ .0295)
NFL_QB_OUT_HARD_GATE = True      # QB1 newly Out on the side the leg backs (or on either side for an OVER): hold the lock regardless of margin
NFL_OL_WIN_PROB_EACH = 0.022     # computeInjuryImpact baseW for nfl OL (the NFL model itself has no OL input); counted per lineman listed Out
NFL_OL_CAP = 0.05                # ESPN does not say who starts, so a pile of listed-Out backups must not swing a leg: total OL effect capped at 5 points
NFL_MIN_REPRICE = 0.005
NFL_OL_POSITIONS = frozenset({"OT", "OG", "OL", "T", "G", "C", "LT", "RT", "LG", "RG"})

ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports"
SPORT_PATH = {"NHL": "hockey/nhl", "NBA": "basketball/nba", "NFL": "football/nfl"}
_ET = ZoneInfo("America/New_York")
# site.api.espn.com answers 403 to a custom User-Agent carrying a URL (checked 2026-10-10; fetch_nhl.py documents the same): send requests' default UA.
_HEADERS: dict = {}
# ESPN abbreviation -> canonical key.  Applied to BOTH the leg and the ESPN row, so either spelling matches.
_TEAM_ALIASES = {
    "NBA": {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "WAS": "WSH", "PHO": "PHX", "NOH": "NOP"},
    "NFL": {"WAS": "WSH", "LA": "LAR", "JAC": "JAX", "ARZ": "ARI"},
}


def _log(msg: str) -> None:
    print(msg, flush=True)


def gate_enabled(sport: str | None = None) -> bool:
    """Master switch + environment kill switch + (optionally) the per-sport switch."""
    import os
    if str(os.environ.get(LINEUP_ENV_OFF, "")).strip().lower() in ("off", "0", "false", "no", "disabled"):
        return False
    if not LINEUP_GATE_ENABLED:
        return False
    if sport is None:
        return True
    return {"NHL": LINEUP_NHL_ENABLED, "NBA": LINEUP_NBA_ENABLED, "NFL": LINEUP_NFL_ENABLED}.get(sport, False)


# ───────────────────────────── small helpers ─────────────────────────────

def canon_team(sport: str, abbr: str | None) -> str:
    a = (abbr or "").strip().upper()
    if sport == "NHL":
        return NG.app_abbr(a) or a
    return _TEAM_ALIASES.get(sport, {}).get(a, a)


def norm_name(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


def parse_iso_ms(v) -> Optional[float]:
    if not v or not isinstance(v, str):
        return None
    try:
        s = v.strip().replace("Z", "+00:00")
        if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d\+00:00", s):
            s = s.replace("+00:00", ":00+00:00")
        return datetime.fromisoformat(s).timestamp() * 1000.0
    except ValueError:
        return None


def parse_stamp_ms(v) -> Optional[float]:
    """'2026-10-09 20:20 UTC' (the repo's generated_at style) or ISO -> epoch ms."""
    if not isinstance(v, str):
        return None
    t = parse_iso_ms(v)
    if t is not None:
        return t
    try:
        return datetime.strptime(v.replace(" UTC", "").strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp() * 1000.0
    except ValueError:
        return None


def et_yyyymmdd(start_ms: float) -> str:
    return datetime.fromtimestamp(start_ms / 1000.0, timezone.utc).astimezone(_ET).strftime("%Y%m%d")


def iso_z(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _inv_logit(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def inj_sev(status: str | None) -> float:
    """app.html _injSev: probability the player misses the game.  Out / IR / suspension 1, Doubtful .75, Questionable / Day-To-Day .30."""
    s = str(status or "").lower().strip()
    if not s or s == "active":
        return 0.0
    if re.search(r"\bout\b|injured reserve|\bir\b|-il\b|suspen", s):
        return 1.0
    if "doubtful" in s:
        return 0.75
    if "questionable" in s or "day-to-day" in s:
        return 0.30
    return 0.0


def nba_model_sev(status: str | None) -> float:
    """computeInjuryImpact's own (looser) severity for NBA: 'out' / '-il' / ' ir' / '-day' (Day-To-Day!) all count 1.0."""
    sl = (status or "").lower()
    if "out" in sl or "-il" in sl or " ir" in sl or "-day" in sl:
        return 1.0
    if "doubtful" in sl:
        return 0.75
    if "questionable" in sl:
        return 0.30
    return 0.0


# ───────────────────────────── ESPN payload parsers (pure) ─────────────────────────────

def parse_scoreboard(payload: dict | None, sport: str) -> list[dict]:
    """ESPN scoreboard JSON -> [{id, startMs, home, away, state, goalies?}] (team keys canonicalised).  NHL events also carry `goalies`
    = {"home": {name, status}, "away": {...}} from the probableStartingGoalie entries (status 'confirmed' | 'projected')."""
    out: list[dict] = []
    for ev in (payload or {}).get("events") or []:
        try:
            comp = (ev.get("competitions") or [{}])[0]
            cs = comp.get("competitors") or []
            home = next(c for c in cs if c.get("homeAway") == "home")
            away = next(c for c in cs if c.get("homeAway") == "away")
            rec = {"id": str(ev.get("id")), "startMs": parse_iso_ms(ev.get("date") or comp.get("date")),
                   "home": canon_team(sport, (home.get("team") or {}).get("abbreviation")),
                   "away": canon_team(sport, (away.get("team") or {}).get("abbreviation")),
                   "state": (((comp.get("status") or ev.get("status") or {}).get("type")) or {}).get("state")}
            if sport == "NHL":
                rec["goalies"] = NG.espn_probables(cs)
            if rec["startMs"] is None or not rec["home"] or not rec["away"]:
                continue
            out.append(rec)
        except Exception:
            continue
    return out


def parse_summary_injuries(payload: dict | None, sport: str) -> dict[str, list[dict]]:
    """ESPN summary JSON -> {team: [{name, id, pos, status, detail}]} from the top-level `injuries` list (the game's own availability list)."""
    out: dict[str, list[dict]] = {}
    for t in (payload or {}).get("injuries") or []:
        try:
            team = canon_team(sport, (t.get("team") or {}).get("abbreviation"))
            rows = []
            for i in t.get("injuries") or []:
                ath = i.get("athlete") or {}
                name = ath.get("displayName") or ath.get("fullName") or ""
                if not name:
                    continue
                det = i.get("details") or {}
                rows.append({"name": name, "id": str(ath["id"]) if ath.get("id") is not None else None,
                             "pos": ((ath.get("position") or {}).get("abbreviation") or "").upper(),
                             "status": i.get("status") or ((i.get("type") or {}).get("description") or ""),
                             "detail": det.get("type") or ""})
            out.setdefault(team, []).extend(rows)
        except Exception:
            continue
    return out


# ───────────────────────────── network (injectable, cached, budgeted, fail-open) ─────────────────────────────

def default_http_get(url: str):
    """-> parsed JSON dict, or None on ANY problem (HTTP error, timeout, bad JSON).  Never raises."""
    try:
        import requests
        r = requests.get(url, headers=_HEADERS, timeout=LINEUP_HTTP_TIMEOUT_S)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


class LineupSource:
    """Per-run cache of ESPN lookups.  `http_get(url) -> dict | None` is injectable (tests pass fixtures).  Every method returns None when the
    answer is unknown; none of them raises."""

    def __init__(self, http_get: Callable[[str], Optional[dict]] | None = None, total_budget_s: float = LINEUP_TOTAL_BUDGET_S,
                 max_requests: int = LINEUP_MAX_REQUESTS, clock: Callable[[], float] = time.monotonic):
        self._http_get = http_get or default_http_get
        self._cache: dict[str, Optional[dict]] = {}
        self._budget, self._max, self._clock = total_budget_s, max_requests, clock
        self._t0: Optional[float] = None
        self.requests = 0
        self.failures = 0

    def _get(self, url: str) -> Optional[dict]:
        if url in self._cache:
            return self._cache[url]
        if self._t0 is None:
            self._t0 = self._clock()
        res = None
        if self.requests < self._max and (self._clock() - self._t0) < self._budget:
            self.requests += 1
            try:
                res = self._http_get(url)
            except Exception:
                res = None
            if not isinstance(res, dict):
                res = None
                self.failures += 1
        self._cache[url] = res
        return res

    def scoreboard(self, sport: str, start_ms: float) -> Optional[list[dict]]:
        payload = self._get(f"{ESPN_SITE}/{SPORT_PATH[sport]}/scoreboard?dates={et_yyyymmdd(start_ms)}&limit=300")
        return None if payload is None else parse_scoreboard(payload, sport)

    def event_for(self, sport: str, home: str, away: str, start_ms: float) -> Optional[dict]:
        evs = self.scoreboard(sport, start_ms)
        if not evs:
            return None
        h, a = canon_team(sport, home), canon_team(sport, away)
        tol = LINEUP_MATCH_TOLERANCE_MIN * 60000.0
        c = [e for e in evs if e["home"] == h and e["away"] == a and abs(e["startMs"] - start_ms) <= tol]
        if len(c) != 1:      # none, or a doubleheader we cannot tell apart: unknown
            return None
        return c[0]

    def nhl_goalies(self, home: str, away: str, start_ms: float) -> Optional[dict]:
        ev = self.event_for("NHL", home, away, start_ms)
        return None if ev is None else {"eventId": ev["id"], "goalies": ev.get("goalies") or {}}

    def injuries(self, sport: str, home: str, away: str, start_ms: float) -> Optional[dict]:
        ev = self.event_for(sport, home, away, start_ms)
        if ev is None:
            return None
        payload = self._get(f"{ESPN_SITE}/{SPORT_PATH[sport]}/summary?event={ev['id']}")
        if payload is None:
            return None
        return {"eventId": ev["id"], "teams": parse_summary_injuries(payload, sport)}


# ───────────────────────────── what the model assumed (repo data) ─────────────────────────────

class ModelData:
    """The repo data the model rates a game on.  Built from dicts (tests) or `ModelData.load(docs_dir)`.  Every accessor tolerates missing data."""

    def __init__(self, data: dict | None = None, nfl_injuries: dict | None = None, nfl_players: dict | None = None):
        self.data = data or {}
        self.nfl_injuries = nfl_injuries or {}
        self.nfl_players = nfl_players or {}

    @classmethod
    def load(cls, docs_dir: Path | str | None = None) -> "ModelData":
        d = Path(docs_dir) if docs_dir else ROOT / "docs"

        def rd(name):
            try:
                return json.loads((d / name).read_text())
            except Exception:
                return {}
        return cls(rd("data.json"), rd("nfl_injuries.json"), rd("nfl_player_stats.json"))

    # ---- shared
    def _data_age_ok(self, hours: float, now_ms: float) -> bool:
        t = parse_stamp_ms(self.data.get("generated"))
        return t is not None and (now_ms - t) <= hours * 3600 * 1000

    # ---- NHL
    def nhl_goalie_rows(self) -> list[dict]:
        return [g for g in ((self.data.get("mp") or {}).get("goalies") or []) if g.get("situation") == "all"]

    def nhl_primary(self, team: str) -> Optional[dict]:
        """_nhlApplyGoalieStats: the team's highest-GP 'all' row (first wins a tie), skipping rows with no shots faced."""
        best = None
        for g in self.nhl_goalie_rows():
            if g.get("team") != team or not g.get("shots"):
                continue
            if best is None or (g.get("gp") or 0) > (best.get("gp") or 0):
                best = g
        return best

    def nhl_row_for(self, name: str, team: str) -> Optional[dict]:
        k = norm_name(name)
        m = [g for g in self.nhl_goalie_rows() if norm_name(g.get("name")) == k and g.get("shots")]
        if not m:
            return None
        return next((g for g in m if g.get("team") == team), m[0])

    def nhl_goalie_injury_sev(self, team: str, goalie_name: str, now_ms: float) -> float:
        """_nhlInjAdj: severity applied to the primary goalie's rating (0 when the injury feed is missing / older than 72 h / does not list him)."""
        if not self._data_age_ok(72, now_ms):
            return 0.0
        k = norm_name(goalie_name)
        sev = 0.0
        for r in ((self.data.get("injuries") or {}).get("nhl") or []):
            if r.get("team") != team or str(r.get("pos") or "").upper() != "G" or norm_name(r.get("name")) != k:
                continue
            s = inj_sev(r.get("status"))
            if s >= 1 and r.get("return"):
                t = parse_iso_ms(str(r["return"]) + "T00:00:00Z") if len(str(r["return"])) == 10 else parse_iso_ms(str(r["return"]))
                if t is not None and t <= now_ms:
                    s = 0.5
            sev = max(sev, s)
        return sev

    # ---- NBA
    def nba_player(self, name: str, team: str) -> dict:
        """-> {rating, mpg, team} for a roster player (empty dict when unknown)."""
        k = (name or "").lower().strip()
        roster = (self.data.get("nba") or {}).get("roster") or {}
        r = roster.get(k) or next((v for rk, v in roster.items() if norm_name(rk) == norm_name(name) and canon_team("NBA", v.get("team")) == canon_team("NBA", team)), None) or {}
        if r and canon_team("NBA", r.get("team")) != canon_team("NBA", team):
            return {}
        mpg = None
        nk = norm_name(name)
        for p in (((self.data.get("nba") or {}).get("playerProps") or {}).get("players") or []):
            if norm_name(p.get("name")) == nk and canon_team("NBA", p.get("team")) == canon_team("NBA", team):
                mpg = p.get("mpg")
                break
        out = dict(r)
        if mpg is not None:
            out["mpg"] = mpg
        return out

    def nba_snapshot_rows(self, team: str) -> list[dict]:
        return [r for r in ((self.data.get("injuries") or {}).get("nba") or []) if canon_team("NBA", r.get("team")) == canon_team("NBA", team)]

    # ---- NFL
    def nfl_snapshot_fresh(self, now_ms: float) -> bool:
        t = parse_stamp_ms(self.nfl_injuries.get("generated_at"))
        return t is not None and (now_ms - t) <= NFL_INJ_STALE_H * 3600 * 1000

    def nfl_snapshot_rows(self, team: str) -> list[dict]:
        return list(((self.nfl_injuries.get("teams") or {}).get(team)) or [])

    def nfl_key_players(self, team: str) -> list[dict]:
        return list(((self.nfl_players.get("teams") or {}).get(team)) or [])


# ───────────────────────────── Poisson sensitivity (NHL) ─────────────────────────────

def _pmf(mu: float, n: int = 25) -> list[float]:
    out, p = [], math.exp(-mu)
    for k in range(n):
        out.append(p)
        p *= mu / (k + 1)
    return out


def leg_poisson_prob(kind: str, team_is_home: bool | None, line: float | None, direction: str | None, lam_h: float, lam_a: float) -> float:
    """Probability the leg wins under independent Poisson goals.  kind: ML | PL (puck line, `line` is the team's own +/-1.5) | OU."""
    ph, pa = _pmf(lam_h), _pmf(lam_a)
    n = len(ph)
    p = push = 0.0
    for i in range(n):
        for j in range(n):
            w = ph[i] * pa[j]
            if kind == "OU":
                tot = i + j
                if tot > line:
                    p += w if direction == "OVER" else 0.0
                elif tot < line:
                    p += w if direction == "UNDER" else 0.0
                else:
                    push += w
                continue
            d = (i - j) if team_is_home else (j - i)
            if kind == "ML":
                p += w if d > 0 else (0.5 * w if d == 0 else 0.0)
            elif kind == "PL":
                if d + line > 0:
                    p += w
    if kind == "OU":
        return p / (1.0 - push) if push < 1.0 else 0.5
    return p


def goalie_composite(row: dict | None, sev: float = 0.0) -> float:
    """nhlMC goalieComposite for one goalie row (MoneyPuck 'all'): .7*gsax + .18*(sv-.91)*100 + .12*(hdsv-.90)*100, scaled by (1 - sev) like the injury rule."""
    if not row:
        return 0.0
    gsax = row.get("gsaa") or 0.0
    c = gsax * 0.7 * (1 - sev)
    if row.get("savePct") is not None:
        c += (row["savePct"] - 0.91) * 100 * 0.18 * (1 - sev)
    if row.get("hdSavePct") is not None:
        c += (row["hdSavePct"] - 0.90) * 100 * 0.12 * (1 - sev)
    return c


def goalie_glv(composite: float) -> float:
    return max(0.87, min(1.13, 1 - composite / 80.0))


# ───────────────────────────── leg parsing ─────────────────────────────

_MC_PROJ = re.compile(r"MC PROJ:\s*([A-Z]{2,4})\s+([0-9.]+)\s*[–-]\s*([A-Z]{2,4})\s+([0-9.]+)")


def parse_mc_proj(text: str | None, home: str, away: str) -> Optional[tuple[float, float]]:
    """'MC PROJ: PHI 2.7 – BOS 3.1 (Total 5.8)' -> (home goals, away goals) or None."""
    m = _MC_PROJ.search(text or "")
    if not m:
        return None
    t1, v1, t2, v2 = m.group(1), float(m.group(2)), m.group(3), float(m.group(4))
    if t1 == away and t2 == home:
        return v2, v1
    if t1 == home and t2 == away:
        return v1, v2
    return None


def parse_leg(q: dict) -> dict:
    """-> {kind: ML|SPREAD|OU, team, line, direction}.  team is the abbreviation the leg backs (None for a total)."""
    label = str(q.get("label") or "").strip()
    up = label.upper()
    side = q.get("side") or ""
    nums = re.findall(r"[+-]?\d+(?:\.\d+)?", label)
    line = float(nums[-1]) if nums else None
    if side in ("over", "under") or up.startswith(("OVER", "UNDER")):
        return {"kind": "OU", "team": None, "line": line, "direction": "OVER" if up.startswith("OVER") or side == "over" else "UNDER"}
    team = up.split()[0] if up else None
    if team in (str(q.get("hA") or "").upper(), str(q.get("awA") or "").upper()):
        pass
    else:
        team = None
    if re.search(r"[+-]\d+(\.\d+)?\s*$", label):
        return {"kind": "SPREAD", "team": team, "line": line, "direction": None}
    return {"kind": "ML", "team": team, "line": None, "direction": None}


# ───────────────────────────── decisions ─────────────────────────────
# A decision is a dict:  {"action": "none"|"reprice"|"hold", "lineup": {...}, "prob": p, "evVal": ev, "tierN": n, "lane": bool, "reason": str|None}

def _lineup_obj(status: str, notes: list[str], now_ms: float, src: str, action: str = "none", adj: dict | None = None) -> dict:
    o = {"status": status, "notes": notes[:8], "checkedAt": iso_z(now_ms), "src": src, "action": action}
    if adj:
        o["adj"] = adj
    return o


def _unknown(notes: list[str], now_ms: float, src: str = "espn") -> dict:
    return {"action": "none", "lineup": _lineup_obj("unknown", notes, now_ms, src), "reason": None}


def _ev_after(q: dict, p_new: float) -> Optional[float]:
    dec = q.get("dec")
    try:
        if dec is not None and float(dec) > 1.0:
            return round(p_new * float(dec) - 1.0, 4)
    except (TypeError, ValueError):
        pass
    ev, p = q.get("evVal"), q.get("prob")
    if ev is not None and p:
        return round(p_new * (ev + 1.0) / p - 1.0, 4)
    return None


def _settle(q: dict, adverse: float, notes: list[str], now_ms: float, requalify: Callable, status: str, src: str,
            min_reprice: float, desc: str, hard_hold: str | None = None) -> dict:
    """Common tail: turn an estimated adverse probability swing into none / reprice / hold."""
    p0 = float(q.get("prob") or 0.0)
    if hard_hold:
        return {"action": "hold", "reason": hard_hold,
                "lineup": _lineup_obj(status, notes + [hard_hold], now_ms, src, "hold", {"pBefore": round(p0, 4), "rule": "hard"})}
    if adverse < min_reprice:
        if adverse > 0:
            notes = notes + [f"{desc}: est. {adverse * 100:.2f} pts, immaterial -> unchanged"]
        return {"action": "none", "lineup": _lineup_obj(status, notes, now_ms, src)}
    p1 = max(0.0, p0 - adverse)
    ev1 = _ev_after(q, p1)
    base_ok, _t0, _l0 = requalify(q, p0, q.get("evVal"))
    ok, tier1, lane1 = requalify(q, p1, ev1)
    adj = {"pBefore": round(p0, 4), "pAfter": round(p1, 4), "swing": round(-adverse, 4)}
    if ok:
        notes = notes + [f"{desc}: est. -{adverse * 100:.1f} pts ({p0 * 100:.1f}% -> {p1 * 100:.1f}%), still qualifies"]
        d = {"action": "reprice", "prob": p1, "evVal": ev1, "tierN": tier1, "lane": lane1, "reason": None,
             "lineup": _lineup_obj(status, notes, now_ms, src, "reprice", adj)}
        return d
    if not base_ok and adverse < LINEUP_UNVERIFIED_HOLD_SWING:
        notes = notes + [f"{desc}: est. -{adverse * 100:.1f} pts; original qualification not reproducible here, swing below the {LINEUP_UNVERIFIED_HOLD_SWING * 100:.0f}-pt hold line -> repriced"]
        return {"action": "reprice", "prob": p1, "evVal": ev1, "tierN": q.get("tierN"), "lane": bool(q.get("lane")), "reason": None,
                "lineup": _lineup_obj(status, notes, now_ms, src, "reprice", adj)}
    reason = f"{desc}: est. -{adverse * 100:.1f} pts takes {p0 * 100:.1f}% to {p1 * 100:.1f}%, below the qualifying cutoff"
    return {"action": "hold", "reason": reason, "lineup": _lineup_obj(status, notes + [reason], now_ms, src, "hold", adj)}


# ---- NHL --------------------------------------------------------------------------------------------------------------

def assess_nhl(q: dict, source: LineupSource, model: ModelData, now_ms: float, requalify: Callable) -> dict:
    hA, awA = q.get("hA"), q.get("awA")
    start = q.get("_startMs")
    rec = source.nhl_goalies(hA, awA, start)
    if rec is None:
        return _unknown(["NHL goalie source unavailable or game not matched"], now_ms)
    sides = rec["goalies"]
    notes: list[str] = []
    all_conf = True
    new_row: dict[str, Optional[dict]] = {}
    old_row: dict[str, Optional[dict]] = {}
    comp_old: dict[str, float] = {}
    comp_new: dict[str, float] = {}
    gsax_old: dict[str, float] = {}
    gsax_new: dict[str, float] = {}
    for side, team in (("home", hA), ("away", awA)):
        info = sides.get(side)
        primary = model.nhl_primary(team)
        sev = model.nhl_goalie_injury_sev(team, primary.get("name"), now_ms) if primary else 0.0
        old_row[side] = primary
        comp_old[side] = goalie_composite(primary, sev)
        gsax_old[side] = (primary.get("gsaa") or 0.0) * (1 - sev) if primary else 0.0
        comp_new[side], gsax_new[side] = comp_old[side], gsax_old[side]
        if not info:
            all_conf = False
            notes.append(f"{team}: no starter listed")
            continue
        if info.get("status") != "confirmed":
            all_conf = False
            notes.append(f"{team}: {info['name']} projected, not confirmed (not acted on)")
            continue
        if primary is None:
            notes.append(f"{team}: {info['name']} confirmed; the model's own goalie rating is missing -> not compared")
            continue
        row = model.nhl_row_for(info["name"], team)
        new_row[side] = row
        if row is None or (row.get("shots") or 0) < NHL_REPLACEMENT_MIN_SHOTS:
            comp_new[side], gsax_new[side] = 0.0, 0.0
            rated = ("no season stats" if row is None else f"only {int(row.get('shots') or 0)} shots faced") + ", rated league average like an injured starter"
        else:
            comp_new[side], gsax_new[side] = goalie_composite(row), row.get("gsaa") or 0.0
            rated = f"gsax {gsax_new[side]:+.1f}"
        same = norm_name(info["name"]) == norm_name(primary.get("name"))
        notes.append(f"{team}: {info['name']} confirmed ({rated})" + ("" if same else f"; model rates {primary.get('name')} (gsax {gsax_old[side]:+.1f})"))
    status = "confirmed" if all_conf else "unknown"
    leg = parse_leg(q)
    if leg["kind"] in ("ML", "SPREAD") and leg["team"] is None:
        return {"action": "none", "lineup": _lineup_obj(status, notes + ["leg team not recognised -> not assessed"], now_ms, "espn")}
    if leg["kind"] in ("SPREAD", "OU") and leg["line"] is None:
        return {"action": "none", "lineup": _lineup_obj(status, notes + ["leg line not parsed -> not assessed"], now_ms, "espn")}
    if all(abs(comp_new[s] - comp_old[s]) < 1e-9 and abs(gsax_new[s] - gsax_old[s]) < 1e-9 for s in ("home", "away")):
        return {"action": "none", "lineup": _lineup_obj(status, notes, now_ms, "espn")}
    # scoring rates: the pick's own MC projection, else the league rate
    proj = parse_mc_proj(q.get("mcSummary"), hA, awA)
    lam_h, lam_a = proj if proj else (NHL_LG_GOALS, NHL_LG_GOALS)
    if not proj:
        notes.append(f"no MC PROJ line on the pick: sensitivity sized at the league rate {NHL_LG_GOALS}")
    # the home side scores against the AWAY goalie and vice versa
    f_h = goalie_glv(comp_new["away"]) / goalie_glv(comp_old["away"])
    f_a = goalie_glv(comp_new["home"]) / goalie_glv(comp_old["home"])
    if leg["kind"] == "ML":
        k, is_home, ln, dr = "ML", leg["team"] == hA, None, None
    elif leg["kind"] == "SPREAD":
        k, is_home, ln, dr = "PL", leg["team"] == hA, leg["line"], None
    else:
        k, is_home, ln, dr = "OU", None, leg["line"], leg["direction"]
    p_old = leg_poisson_prob(k, is_home, ln, dr, lam_h, lam_a)
    p_new = leg_poisson_prob(k, is_home, ln, dr, lam_h * f_h, lam_a * f_a)
    swing = p_new - p_old
    if k == "ML":
        d_ge = ((gsax_new["home"] - gsax_old["home"]) - (gsax_new["away"] - gsax_old["away"])) / 100.0
        swing = NHL_MC_ENSEMBLE_WEIGHT * swing + NHL_GSAX_ENSEMBLE_WEIGHT * d_ge * (1 if is_home else -1)
    desc = f"{hA} v {awA} goalie change (opp. scoring x{f_h:.3f}/x{f_a:.3f})"
    if swing >= 0:
        return {"action": "none", "lineup": _lineup_obj(status, notes + [f"{desc}: in the leg's favour -> unchanged"], now_ms, "espn")}
    adverse_model = -swing
    # the model-level swing reaches the pick only through the (1 - alpha) model share of the market blend
    alpha, pm, pf = q.get("blendAlpha"), q.get("modelProb"), q.get("prob")
    if NHL_STALE_MARKET_ASSUME or alpha is None:
        adverse = adverse_model
    elif pm is not None and pf and 0 < pm < 1 and 0 < pf < 1:
        d_logit_model = adverse_model / (pm * (1 - pm))
        adverse = (1 - alpha) * d_logit_model * pf * (1 - pf)
        notes.append(f"market blend alpha {alpha:.2f}: model-level swing -{adverse_model * 100:.1f} pts reaches the pick as -{adverse * 100:.2f}")
    else:
        adverse = (1 - alpha) * adverse_model
        notes.append(f"market blend alpha {alpha:.2f}: model-level swing -{adverse_model * 100:.1f} pts reaches the pick as -{adverse * 100:.2f}")
    return _settle(q, adverse, notes, now_ms, requalify, status, "espn", NHL_MIN_REPRICE, desc)


# ---- NBA --------------------------------------------------------------------------------------------------------------

def _nba_weight(player: dict) -> float:
    return NBA_PENALTY_BY_RATING.get(player.get("rating"), NBA_PENALTY_DEFAULT)


def _nba_is_top(player: dict) -> bool:
    return player.get("rating") in ("PREMIUM", "OPTIMAL") or (player.get("mpg") or 0) >= NBA_TOP_ROTATION_MPG


def assess_nba(q: dict, source: LineupSource, model: ModelData, now_ms: float, requalify: Callable) -> dict:
    hA, awA = q.get("hA"), q.get("awA")
    rec = source.injuries("NBA", hA, awA, q.get("_startMs"))
    if rec is None:
        return _unknown(["NBA game injury list unavailable or game not matched"], now_ms)
    teams = rec["teams"]
    if not any(teams.values()):
        return _unknown(["NBA game injury list is empty (not treated as confirmed)"], now_ms)
    leg = parse_leg(q)
    notes: list[str] = []
    unresolved = False
    new_pen: dict[str, float] = {}
    for team in (hA, awA):
        tk = canon_team("NBA", team)
        rows = teams.get(tk, [])
        # penalty the model already has (snapshot rows matched to the roster, computeInjuryImpact)
        old_total = 0.0
        snap_sev: dict[str, float] = {}
        for r in model.nba_snapshot_rows(team):
            pl = model.nba_player(r.get("name"), team)
            if not pl:
                continue
            s = nba_model_sev(r.get("status"))
            snap_sev[norm_name(r.get("name"))] = s
            old_total += _nba_weight(pl) * s
        old_total = min(NBA_PENALTY_CAP, old_total)
        add = 0.0
        for r in rows:
            pl = model.nba_player(r["name"], team)
            if not pl or not _nba_is_top(pl):
                continue
            sev_now = inj_sev(r["status"])
            if sev_now >= 1:
                already = snap_sev.get(norm_name(r["name"]), 0.0)
                delta = _nba_weight(pl) * max(0.0, 1.0 - already)
                if delta > 0:
                    add += delta
                    notes.append(f"{team}: {r['name']} OUT ({pl.get('rating') or 'rotation'}, {pl.get('mpg') or '?'} mpg)"
                                 + ("" if already <= 0 else f", model already counts {already:.2f}"))
                else:
                    notes.append(f"{team}: {r['name']} OUT (already in the model's injury input)")
            elif sev_now > 0:
                unresolved = True
                notes.append(f"{team}: {r['name']} {r['status']} (open)")
        new_pen[team] = min(NBA_PENALTY_CAP, old_total + add) - old_total
    status = "unknown" if unresolved else "confirmed"
    if leg["kind"] == "OU" or leg["team"] is None:
        return {"action": "none", "lineup": _lineup_obj(status, notes + (["total: no injury term in the model -> unchanged"] if leg["kind"] == "OU" else ["leg team not recognised"]), now_ms, "espn")}
    adverse = new_pen.get(leg["team"], 0.0)
    return _settle(q, adverse, notes, now_ms, requalify, status, "espn", NBA_MIN_REPRICE, f"{leg['team']} newly-out rotation player(s)")


# ---- NFL --------------------------------------------------------------------------------------------------------------

def _same_player(id_a, name_a, id_b, name_b) -> bool:
    """app.html's identity rule: the ESPN athlete id when BOTH sides carry one (an id mismatch is never overridden by a name match), else the normalised name."""
    if id_a and id_b:
        return str(id_a) == str(id_b)
    return bool(norm_name(name_a)) and norm_name(name_a) == norm_name(name_b)


def _nfl_snapshot_sev(model: ModelData, team: str, pid: str | None, name: str, now_ms: float) -> float:
    if not model.nfl_snapshot_fresh(now_ms):
        return 0.0
    for r in model.nfl_snapshot_rows(team):
        if _same_player(r.get("playerId"), r.get("player"), pid, name):
            s = inj_sev(r.get("status"))
            if s >= 1 and r.get("estimatedReturn"):
                t = parse_iso_ms(str(r["estimatedReturn"]) + "T00:00:00Z") if len(str(r["estimatedReturn"])) == 10 else parse_iso_ms(str(r["estimatedReturn"]))
                if t is not None and t <= now_ms:
                    s = 0.5
            return s
    return 0.0


def assess_nfl(q: dict, source: LineupSource, model: ModelData, now_ms: float, requalify: Callable) -> dict:
    hA, awA = q.get("hA"), q.get("awA")
    rec = source.injuries("NFL", hA, awA, q.get("_startMs"))
    if rec is None:
        return _unknown(["NFL game injury list unavailable or game not matched"], now_ms)
    teams = rec["teams"]
    if not any(teams.values()):
        return _unknown(["NFL game injury list is empty (not treated as confirmed)"], now_ms)
    leg = parse_leg(q)
    notes: list[str] = []
    unresolved = False
    qb_new: dict[str, str] = {}      # team -> QB1 name newly Out
    pts_new: dict[str, float] = {}   # team -> additional model points lost (QB + capped skill, on top of the snapshot)
    ol_new: dict[str, float] = {}    # team -> additional win-probability from lineman listed Out
    for team in (hA, awA):
        rows = teams.get(canon_team("NFL", team), [])
        key = model.nfl_key_players(team)
        qb_old = qb_cur = 0.0
        sk_old = sk_cur = 0.0
        ol_cnt = 0.0
        for r in rows:
            pos = (r.get("pos") or "").upper()
            sev_now = inj_sev(r["status"])
            if pos in NFL_OL_POSITIONS and sev_now >= 1:
                if _nfl_snapshot_sev(model, team, r.get("id"), r["name"], now_ms) < 1:
                    ol_cnt += 1
                    notes.append(f"{team}: {pos} {r['name']} OUT")
                continue
            if pos not in ("QB", "WR", "RB", "TE"):
                continue
            kp = next((k for k in key if (k.get("position") or "").upper() == pos and _same_player(k.get("id"), k.get("name"), r.get("id"), r["name"])), None)
            if kp is None:
                continue                      # not one of the model's key players: the model gives it no weight
            snap = _nfl_snapshot_sev(model, team, r.get("id"), r["name"], now_ms)
            if pos == "QB":
                qb_old = max(qb_old, NFL_INJ_QB_PTS * snap)
                qb_cur = max(qb_cur, NFL_INJ_QB_PTS * sev_now)
            else:
                sk_old += NFL_INJ_SKILL_PTS * snap
                sk_cur += NFL_INJ_SKILL_PTS * sev_now
            if sev_now >= 1 and snap < 1:
                notes.append(f"{team}: {pos} {r['name']} OUT" + ("" if snap <= 0 else f" (model counts {snap:.2f})"))
                if pos == "QB":
                    qb_new[team] = r["name"]
            elif 0 < sev_now < 1:
                unresolved = True
                notes.append(f"{team}: {pos} {r['name']} {r['status']} (open)")
        old_pts = min(NFL_INJ_TEAM_CAP, qb_old + min(NFL_INJ_SKILL_CAP, sk_old))
        cur_pts = min(NFL_INJ_TEAM_CAP, qb_cur + min(NFL_INJ_SKILL_CAP, sk_cur))
        pts_new[team] = max(0.0, cur_pts - old_pts)
        ol_new[team] = min(NFL_OL_CAP, ol_cnt * NFL_OL_WIN_PROB_EACH)
    status = "unknown" if unresolved else "confirmed"
    if leg["kind"] == "OU":
        if NFL_QB_OUT_HARD_GATE and qb_new and leg["direction"] == "OVER":
            who = ", ".join(f"{t} {n}" for t, n in qb_new.items())
            return _settle(q, 0.0, notes, now_ms, requalify, status, "espn", NFL_MIN_REPRICE, "total", hard_hold=f"QB out ({who}) and the leg is an OVER")
        return {"action": "none", "lineup": _lineup_obj(status, notes + ["total: no other lineup rule -> unchanged"], now_ms, "espn")}
    if leg["team"] is None:
        return {"action": "none", "lineup": _lineup_obj(status, notes + ["leg team not recognised"], now_ms, "espn")}
    t = leg["team"]
    if NFL_QB_OUT_HARD_GATE and t in qb_new:
        return _settle(q, 0.0, notes, now_ms, requalify, status, "espn", NFL_MIN_REPRICE, "qb", hard_hold=f"QB out ({t} {qb_new[t]}) on the side the leg backs")
    adverse = pts_new.get(t, 0.0) * NFL_WIN_PROB_PER_POINT + ol_new.get(t, 0.0)
    return _settle(q, adverse, notes, now_ms, requalify, status, "espn", NFL_MIN_REPRICE,
                   f"{t} newly-out skill/line player(s) ({pts_new.get(t, 0.0):.1f} model pts + {ol_new.get(t, 0.0) * 100:.1f} line pts)")


# ───────────────────────────── entry point ─────────────────────────────

class LineupContext:
    """Everything assess_leg needs.  `requalify(q, p, ev) -> (qualifies, tierN, laneOnly)` is supplied by auto_lock_settle (it owns the cutoffs)."""

    def __init__(self, source: LineupSource, model: ModelData, requalify: Callable, window_min: float = LINEUP_WINDOW_MIN):
        self.source, self.model, self.requalify, self.window_min = source, model, requalify, window_min
        self.holds: list[dict] = []


_ASSESSORS = {"NHL": assess_nhl, "NBA": assess_nba, "NFL": assess_nfl}


def assess_leg(q: dict, ctx: LineupContext, now_ms: float) -> Optional[dict]:
    """None = this leg is out of scope (not a game leg of a covered sport, outside the window, or the sport is switched off): no field, no change.
    Otherwise a decision dict (see above).  Never raises: any internal error is reported as an 'unknown' lineup."""
    sport = q.get("sport")
    if q.get("kind") != "GAME" or sport not in _ASSESSORS or not gate_enabled(sport):
        return None
    start = q.get("startMs")
    try:
        start = float(start)
    except (TypeError, ValueError):
        return None
    mins = (start - now_ms) / 60000.0
    if not (0 < mins <= ctx.window_min):
        return None
    q = dict(q, _startMs=start)
    try:
        d = _ASSESSORS[sport](q, ctx.source, ctx.model, now_ms, ctx.requalify)
    except Exception as exc:       # fail-open
        d = _unknown([f"lineup check error: {type(exc).__name__}"], now_ms)
    d["minsToStart"] = round(mins, 1)
    return d


def apply_decision(q: dict, d: dict) -> dict:
    """The leg as it should continue through build_qualifying: stamped with `lineup`, and repriced when the decision says so."""
    out = dict(q)
    out["lineup"] = d["lineup"]
    if d["action"] == "reprice":
        out["lineupPreProb"] = q.get("prob")
        out["prob"] = d["prob"]
        if d.get("evVal") is not None:
            out["evVal"] = d["evVal"]
        if d.get("tierN") is not None:
            out["tierN"] = d["tierN"]
        if "lane" in d:
            out["lane"] = bool(d["lane"])
        adj = d["lineup"].get("adj") or {}
        tail = f" LINEUP: {'; '.join(d['lineup']['notes'][:2])} -- win probability {adj.get('pBefore', 0) * 100:.1f}% -> {adj.get('pAfter', 0) * 100:.1f}%."
        out["reasoning"] = ((q.get("reasoning") or "").rstrip() + tail).strip() if q.get("reasoning") else tail.strip()
    return out


def default_context(requalify: Callable, docs_dir: Path | str | None = None) -> LineupContext:
    return LineupContext(LineupSource(), ModelData.load(docs_dir), requalify)


# ───────────────────────────── CLI: look at what the sources say right now ─────────────────────────────

def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Print the lineup information the lock pass would see (live ESPN).")
    ap.add_argument("sport", choices=sorted(SPORT_PATH))
    ap.add_argument("--date", help="YYYYMMDD (ESPN/Eastern day); default today")
    a = ap.parse_args()
    src = LineupSource()
    day = a.date or datetime.now(_ET).strftime("%Y%m%d")
    start_ms = datetime.strptime(day, "%Y%m%d").replace(hour=12, tzinfo=_ET).timestamp() * 1000.0
    evs = src.scoreboard(a.sport, start_ms)
    if evs is None:
        print("scoreboard unavailable")
        return 1
    for e in evs:
        line = f"{e['away']} @ {e['home']}  {iso_z(e['startMs'])}  {e['state']}"
        if a.sport == "NHL":
            g = e.get("goalies") or {}
            line += "  G: " + " / ".join(f"{s}={g[s]['name']}({g[s]['status']})" for s in ("away", "home") if g.get(s))
        else:
            inj = src.injuries(a.sport, e["home"], e["away"], e["startMs"]) or {"teams": {}}
            out = [f"{t}:{r['name']}({r['pos']})" for t, rows in inj["teams"].items() for r in rows if inj_sev(r["status"]) >= 1]
            line += "  OUT: " + (", ".join(out) or "-")
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
