"""Shared lock-timing classifier -- the ONE place that decides whether a pick is "known late".

Why this exists (2026-10-02 audit): picks locked AFTER their game started or finished (mostly the owner's deliberate manual locks
made when the automated pre-game pass landed late, plus a few pipeline passes) win ~93% vs ~66% for picks locked before the start,
because the result was largely known. They are not predictions, so every PUBLIC / subscriber-facing figure (landing-page JSONs,
social cards and their captions, the published data bundle, digests that state a record) is computed on pre-start picks only.
Every producer imports this module so the figures cannot diverge. docs/app.html has a line-for-line JS mirror (_pickTiming) for
the owner's in-app views; test_lock_timing.py's parity check keeps the two agreeing.

Classes (same as _pickTiming in docs/app.html and scripts/audit_lock_timing.py):
  pre      locked before the scheduled start
  during   locked 0..DUR_MIN minutes after the start (game in progress)       } KNOWN LATE  -> EXCLUDED from public figures
  after    locked more than DUR_MIN minutes after the start (game finished)   }
  unknown  no start time could be determined -> INCLUDED (never guessed late). See UNKNOWN_BY_DESIGN below.

Start time, in order of trust:
  1. pick.startMs  -- stamped by lockPick()/lock_game_leg() for every pick locked since 2026-10-02 (epoch ms)
  2. pick.lockTiming == 'late-manual' with no startMs -> known late (class 'during'); stored stamp, no start needed
  3. the pick's game matched in the schedule files (SHL/LIIGA/NLA/EXTRALIGA/NHL/CFB/NFL: docs/<x>_schedule.json) or in
     docs/game_start_history.json (soccer + older games recovered from the committed git history of the schedule snapshots by
     scripts/build_game_start_history.py). Match key = the game's America/Denver date + both team names, either orientation.
What stays unknown today: NBA history (no schedule data was ever published for it), pre-October soccer games that no schedule
snapshot caught, picks with no lockedAt. Those stay INCLUDED in every public figure, and every figure's JSON says how many.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
MT = ZoneInfo("America/Denver")

# Game-length cutoffs (minutes after start). SAME numbers as _TB_DUR_MIN in docs/app.html (verify_hockey_rules.py checks the
# literal table is present in both). Locked more than this long after the start = the game was over.
DUR_MIN = {"NHL": 150, "SHL": 150, "LIIGA": 150, "NLA": 150, "EXTRALIGA": 150, "NBA": 150, "NFL": 195, "CFB": 210,
           "PL": 120, "LIGA": 120, "CL": 120, "SERIEA": 120, "BUND": 120, "MLS": 120}
DEFAULT_DUR_MIN = 150

BASIS = "pre-start locks (known-late manual locks excluded)"
BASIS_NOTE = ("Figures count only picks locked before the game started. Picks known to have been locked after the start "
              "(manual late locks) are excluded; picks whose start time cannot be determined are included.")
FOOTNOTE_SHORT = "Pre-start locks only"

LATE_CLASSES = ("during", "after")

# Schedule files and the keys of the two team fields. (file, kind, home key, away key)
_FLAT = (("shl_schedule.json", "homeName", "awayName"), ("liiga_schedule.json", "homeName", "awayName"),
         ("nla_schedule.json", "homeName", "awayName"), ("extraliga_schedule.json", "homeName", "awayName"),
         ("nhl_schedule.json", "home", "away"))
_WEEKS = (("cfb_schedule.json", "home", "away"), ("nfl_schedule.json", "home", "away"))
HISTORY_FILE = "game_start_history.json"


# ── sport normalisation (port of _normSport's explicit-tag branch; the team-abbreviation guesser is not needed for
# classification: it only runs when a pick has no sport/league tag at all) ───────────────────────────────────────────────
_BROAD_AMBIGUOUS = {"FOOTBALL", "BASKETBALL", "HOCKEY", "SOCCER", "BASEBALL"}
_ALIASES = {
    "MLB": "MLB", "BASEBALL": "MLB", "NHL": "NHL", "HOCKEY": "NHL", "ICE HOCKEY": "NHL", "WNBA": "WNBA", "NBA": "NBA",
    "BASKETBALL": "NBA", "CBB": "CBB", "NCAAB": "CBB", "COLLEGE BASKETBALL": "CBB", "NFL": "NFL", "FOOTBALL": "NFL",
    "CFB": "CFB", "COLLEGE FOOTBALL": "CFB", "NCAAF": "CFB", "KHL": "KHL", "SHL": "SHL", "LIIGA": "LIIGA", "NLA": "NLA",
    "EXTRALIGA": "EXTRALIGA", "NCAAH": "NCAAH", "COLLEGE HOCKEY": "NCAAH", "WC": "WC", "WORLDCUP": "WC", "WORLD_CUP": "WC",
    "WORLD CUP": "WC", "SOC": "WC", "PL": "PL", "PREMIER LEAGUE": "PL", "LIGA": "LIGA", "LA LIGA": "LIGA", "BUND": "BUND",
    "BL": "BUND", "BUNDESLIGA": "BUND", "MLS": "MLS", "SERIEA": "SERIEA", "SERIE A": "SERIEA", "CL": "CL", "CH": "CL",
    "CHAMPIONS LEAGUE": "CL",
}


def norm_sport(p: dict) -> str:
    """Python port of _normSport(p) (docs/app.html) for picks that carry a sport/league tag. Returns '' when untagged."""
    sport_up = (p.get("sport") or "").upper().strip()
    raw = ((p.get("league") if (sport_up in _BROAD_AMBIGUOUS and p.get("league")) else (p.get("sport") or p.get("league") or ""))
           or "").upper().strip()
    return _ALIASES.get(raw, raw)


# Scope sets the public figures use (mirror _broadSportOf's non-null tags and get_sport_performance's leagueMap codes).
BROAD_SPORT_CODES = frozenset({"NBA", "NFL", "CFB", "NHL", "KHL", "SHL", "LIIGA", "NLA", "EXTRALIGA", "NCAAH",
                               "PL", "LIGA", "BUND", "BL", "MLS", "SERIEA", "CL", "CH"})
# Figure scope = BROAD minus the retired soccer leagues (MLS, Bundesliga) -- mirrors _cvScoped() in docs/app.html (owner decision 2026-10-03). Use this for every published/record figure.
RETIRED_SOCCER_CODES = frozenset({"MLS", "BUND", "BL"})
IN_SCOPE_CODES = BROAD_SPORT_CODES - RETIRED_SOCCER_CODES


def is_parlay(p: dict) -> bool:
    """Parlays are out of the engine (owner decision 2026-10-04): they never count toward a published figure. Mirrors _isParlay() in docs/app.html."""
    bt = str(p.get("betType") or "").upper()
    return bt in ("PARLAY", "PL_PARLAY") or p.get("hA") in ("PARLAY", "NBA-PARLAY")
LEAGUE_MAP_CODES = frozenset({"NFL", "CFB", "NHL", "NCAAH", "SHL", "LIIGA", "NLA", "EXTRALIGA", "KHL", "NBA", "CL", "PL",
                              "LIGA", "SERIEA"})


# ── start-time index ─────────────────────────────────────────────────────────────────────────────────────────────────────
def iso_ms(s) -> float | None:
    """'2026-09-19T13:15Z' (schedule files) -> epoch ms; None if unparseable."""
    if not isinstance(s, str) or len(s) < 11:
        return None
    try:
        t = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    ms = t.timestamp() * 1000.0
    return ms if ms > 946684800000 else None


def mt_date(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000.0, MT).strftime("%Y-%m-%d")


def game_key(date: str, a: str, b: str) -> str:
    """Same key shape as _tbKey() in docs/app.html: 'date|teamA|teamB' with the two names sorted (orientation-free)."""
    return date + "|" + "|".join(sorted([a, b]))


def add_game(idx: dict, g: dict, hk: str, ak: str) -> None:
    if not g or not g.get("date") or not g.get(hk) or not g.get(ak):
        return
    ms = iso_ms(g["date"])
    if ms is None:
        return
    idx[game_key(mt_date(ms), g[hk], g[ak])] = ms


def load_schedule_index(root: Path = ROOT) -> dict:
    """Live schedule files only (what _tbLoad builds in the browser)."""
    idx: dict = {}
    docs = Path(root) / "docs"
    for fname, hk, ak in _FLAT:
        try:
            for g in json.loads((docs / fname).read_text()).get("games", []):
                add_game(idx, g, hk, ak)
        except (OSError, ValueError):
            continue
    for fname, hk, ak in _WEEKS:
        try:
            for wk in json.loads((docs / fname).read_text()).get("weeks", {}).values():
                for g in wk or []:
                    add_game(idx, g, hk, ak)
        except (OSError, ValueError):
            continue
    return idx


def load_history_index(root: Path = ROOT) -> dict:
    try:
        d = json.loads((Path(root) / "docs" / HISTORY_FILE).read_text())
        return {k: float(v) for k, v in (d.get("games") or {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def load_index(root: Path = ROOT, include_history: bool = True) -> dict:
    """History (older/soccer games) first, live schedule files override it -- same precedence as _tbLoad."""
    idx = load_history_index(root) if include_history else {}
    idx.update(load_schedule_index(root))
    return idx


# ── classification ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _num(v):
    try:
        t = float(v)
    except (TypeError, ValueError):
        return None
    return t if t == t else None


def pick_start_ms(p: dict, idx: dict | None) -> float | None:
    st = _num(p.get("startMs"))
    if st is not None and st > 946684800000:
        return st
    if idx and p.get("date") and p.get("hA") and p.get("awA"):
        v = idx.get(game_key(p["date"], p["hA"], p["awA"]))
        return float(v) if v is not None else None
    return None


def classify(p: dict, idx: dict | None) -> str:
    """-> 'pre' | 'during' | 'after' | 'unknown'. Mirrors _pickTiming() in docs/app.html."""
    st = pick_start_ms(p, idx)
    locked = _num(p.get("lockedAt"))
    if st is None or not locked:
        # stored stamp from lockPick(): a late manual lock with no recoverable start is still known late
        return "during" if p.get("lockTiming") == "late-manual" else "unknown"
    lead = (st - locked) / 60000.0
    if lead > 0:
        return "pre"
    dur = DUR_MIN.get(norm_sport(p), DEFAULT_DUR_MIN)
    return "during" if lead > -dur else "after"


def is_known_late(p: dict, idx: dict | None) -> bool:
    return classify(p, idx) in LATE_CLASSES


def split_picks(picks, idx: dict | None = None) -> tuple[list, list]:
    """-> (kept, late). kept = everything NOT known-late (pre + unknown); late = known-late picks."""
    kept, late = [], []
    for p in picks:
        (late if is_known_late(p, idx) else kept).append(p)
    return kept, late


def late_ids(picks, idx: dict | None = None) -> set:
    """ids of known-late picks (ids are unique in the ledger). Picks without an id cannot be excluded by id; callers that
    need a total filter should use split_picks() instead."""
    return {p.get("id") for p in picks if p.get("id") and is_known_late(p, idx)}


def settled(p: dict) -> bool:
    return p.get("outcome") in ("win", "loss")


def summarize(picks, idx: dict | None = None) -> dict:
    """Audit summary for a pick list (settled picks only): counts per class, and settled-unknown broken down by league so a
    JSON can say exactly which figures cannot be filtered. Written into every published figure under 'basis_detail'."""
    cls = {"pre": 0, "during": 0, "after": 0, "unknown": 0}
    unk: dict = {}
    for p in picks:
        if not settled(p):
            continue
        c = classify(p, idx)
        cls[c] += 1
        if c == "unknown":
            lg = norm_sport(p) or "?"
            unk[lg] = unk.get(lg, 0) + 1
    return {
        "basis": BASIS,
        "settled_pre_start": cls["pre"],
        "settled_excluded_late": cls["during"] + cls["after"],
        "settled_excluded_in_progress": cls["during"],
        "settled_excluded_after_end": cls["after"],
        "settled_unknown_timing_included": cls["unknown"],
        "unknown_timing_by_league": dict(sorted(unk.items(), key=lambda kv: -kv[1])),
    }


def basis_fields(picks_all, idx: dict | None = None) -> dict:
    """Top-level fields every published figure JSON carries (machine-readable basis + how many picks it dropped)."""
    s = summarize(picks_all, idx)
    return {"basis": BASIS, "basis_note": BASIS_NOTE, "classifier": "scripts/lock_timing.py", "basis_detail": s}
