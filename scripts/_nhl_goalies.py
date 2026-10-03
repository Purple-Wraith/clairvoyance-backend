"""NHL starting goalies for docs/nhl_schedule.json (written by scripts/fetch_nhl.py).  Pure functions + one opt-in network fetch.

WHAT IS ATTACHED (per game, only when a source named a goalie):

    "goalies": {
      "home": {"name": "Jake Oettinger", "status": "confirmed"},
      "away": {"name": "Joel Hofer",     "status": "projected"},
      "at":   "2026-10-02T17:12Z",          # when this value (name+status per side) was FIRST observed; unchanged runs keep it
      "src":  "espn"                         # sources behind the sides: "espn" | "moneypuck" | "espn+moneypuck"
    }

`status` is deliberately only two values so a card can be honest:
    "confirmed"  a source says the starter is announced (ESPN probables type "confirmed"; MoneyPuck: a reporter tweet it parsed)
    "projected"  a source's best guess (ESPN probables type "expected"; MoneyPuck start_predictions, highest probability)
A side with no information at all is simply ABSENT (the card shows nothing / "TBD").  Nothing here ever guesses.

SOURCES (verified live 2026-10-02, see the task report):
  1. ESPN scoreboard `competitors[].probables[name=probableStartingGoalie]` -- the SAME response fetch_nhl.py already downloads for the
     schedule, keyed by the same ESPN game id as the odds.  Zero extra requests, no new terms.  Carries both confirmed and expected
     and keeps the value after puck drop.  This is ON in every NHL scrape.
  2. MoneyPuck (opt-in, `fetch_nhl.py --goalies-moneypuck` or env NHL_GOALIES_MONEYPUCK=true).  MoneyPuck's own pages read three
     static files (found by watching preview.htm's network calls; there is no JSON API):
         moneypuck/OldSeasonScheduleJson/SeasonSchedule-20262027.json   [{a,h,est(ET),id}]   game id -> teams/start
         moneypuck/tweets/starting_goalies/{gameId}{H|A}.csv     CONFIRMED starter (reporter tweet), 404 until one is found
         moneypuck/start_predictions/{gameId}{H|A}.csv           PROJECTED starters + probabilities, 404 when not published
     MoneyPuck's terms (https://moneypuck.com/data.htm) allow its data for NON-COMMERCIAL use and say non-approved scraping of pages
     "not listed on this page" will be blocked and to ask first -- these three files are not on that list, so this path ships DISABLED
     until the owner has approval.  When enabled it is tiny (1 schedule fetch + <=2 requests per game side that ESPN has not already
     confirmed, 0.4s apart, hard cap MP_MAX_REQUESTS) and fully fail-open.

Merge rule between runs (merge_with_previous): the fresh value wins; a side the fresh run did not return is CARRIED from the previous
file untouched (a transient fetch failure, or ESPN dropping the probables after puck drop, never erases the starter a locked pick used).
"""
from __future__ import annotations

import csv
import io
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_ISO = "%Y-%m-%dT%H:%MZ"

# ESPN probables `status.type` -> our two honest states.  Anything else is skipped (and logged by the caller), never guessed.
ESPN_STATUS = {"confirmed": "confirmed", "expected": "projected", "probable": "projected", "likely": "projected"}

# MoneyPuck / ESPN team codes -> this app's NHL const keys (docs/app.html).  Explicit and exhaustive for the 32 clubs plus the
# known alternates; a code not in here is SKIPPED and reported, never guessed.
APP_ABBR = {
    "ANA": "ANA", "BOS": "BOS", "BUF": "BUF", "CGY": "CGY", "CAR": "CAR", "CHI": "CHI", "COL": "COL", "CBJ": "CBJ", "DAL": "DAL",
    "DET": "DET", "EDM": "EDM", "FLA": "FLA", "LAK": "LAK", "MIN": "MIN", "MTL": "MTL", "NSH": "NSH", "NJD": "NJD", "NYI": "NYI",
    "NYR": "NYR", "OTT": "OTT", "PHI": "PHI", "PIT": "PIT", "SJS": "SJS", "SEA": "SEA", "STL": "STL", "TBL": "TBL", "TOR": "TOR",
    "UTA": "UTA", "VAN": "VAN", "VGK": "VGK", "WSH": "WSH", "WPG": "WPG",
    # alternates seen in ESPN / older MoneyPuck / NHL feeds
    "LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL", "UTAH": "UTA", "VEG": "VGK", "WAS": "WSH", "MON": "MTL", "CLS": "CBJ",
}

MP_BASE = "https://moneypuck.com/moneypuck"
# A normal, identifiable browser-style UA (MoneyPuck serves it identically to a stock browser UA; checked 2026-10-02).
MP_USER_AGENT = "Mozilla/5.0 (compatible; ClairvoyanceEngine/1.0; +https://clairvoyanceengine.info)"
MP_REQUEST_GAP_S = 0.4
MP_MAX_REQUESTS = 90          # hard ceiling per run (1 schedule + 2 sides x ~2 files x ~20 games is the realistic worst case)
MP_LOOKAHEAD_HOURS = 30       # starters are announced same day (a few the day before): nothing further out is worth a request
MP_PRED_PROBE_SIDES = 4       # probe start_predictions on this many sides; if every one 404s, projections are not published -- stop


def _log(msg: str) -> None:
    print(f"[nhl-goalies] {msg}", flush=True)


def norm_name(name: str) -> str:
    """Accent/case/punctuation-insensitive goalie-name key ('Leevi Meriläinen' == 'Leevi Merilainen')."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", s)).strip()


def app_abbr(code: str | None) -> str | None:
    return APP_ABBR.get((code or "").strip().upper())


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_ISO)


# ───────────────────────────── ESPN (always on) ─────────────────────────────

def espn_probables(competitors: list[dict], unknown: set | None = None) -> dict:
    """ESPN scoreboard `competitors` -> {"home": {name,status}, "away": {...}} (a side is omitted when ESPN lists no starter).
    `unknown` collects probables status types we do not recognise (those sides are skipped)."""
    out: dict = {}
    for c in competitors or []:
        side = c.get("homeAway")
        if side not in ("home", "away"):
            continue
        for p in c.get("probables") or []:
            if p.get("name") != "probableStartingGoalie":
                continue
            name = ((p.get("athlete") or {}).get("fullName") or (p.get("athlete") or {}).get("displayName") or "").strip()
            stype = ((p.get("status") or {}).get("type") or "").strip().lower()
            status = ESPN_STATUS.get(stype)
            if not name:
                continue
            if status is None:
                if unknown is not None:
                    unknown.add(stype or "(none)")
                continue
            out[side] = {"name": name, "status": status}
            break
    return out


# ───────────────────────────── merge / carry (pure) ─────────────────────────────

def _side_key(s: dict | None):
    return None if not s else (norm_name(s.get("name", "")), s.get("status"), s.get("src"), s.get("id"), s.get("by"), s.get("ts"), s.get("p"))


def _clean_side(s: dict) -> dict:
    return {k: v for k, v in s.items() if v not in (None, "")}


def build_goalies(sides: dict, prev: dict | None, now: datetime) -> dict | None:
    """Combine this run's per-side values with the previous file's `goalies` object for the same game.

    * a side present now wins; a side absent now is carried from `prev` untouched;
    * `at` keeps the previous stamp when no side changed (so an unchanged game produces no JSON diff) and is `now` otherwise;
    * returns None when there is nothing to attach."""
    prev = prev if isinstance(prev, dict) else {}
    out: dict = {}
    for side in ("home", "away"):
        cur, old = sides.get(side), prev.get(side)
        if cur:
            out[side] = _clean_side(cur)
        elif isinstance(old, dict) and old.get("name"):
            out[side] = old
    if not out:
        return None
    unchanged = all(_side_key(out.get(s)) == _side_key(prev.get(s)) for s in ("home", "away")) and prev.get("at")
    out["at"] = prev["at"] if unchanged else _iso(now)
    srcs = sorted({(v.get("src") or "espn") for v in (out.get("home"), out.get("away")) if v})
    out["src"] = "+".join(srcs) if srcs else "espn"
    return out


def merge_with_previous(games: list[dict], fresh: dict[str, dict], prev_doc: dict | None, now: datetime) -> dict:
    """Attach `goalies` to every game in `games` from `fresh` ({game id: {"home":..,"away":..}}) + the previous schedule document.
    Returns counters.  Mutates `games`.  A game with nothing fresh and nothing previous gets no key."""
    prev_by_id = {str(g.get("id")): g.get("goalies") for g in (prev_doc or {}).get("games", []) if g.get("goalies")}
    stats = {"games": len(games), "with_goalies": 0, "confirmed_sides": 0, "projected_sides": 0, "carried_sides": 0}
    for g in games:
        gid = str(g.get("id"))
        prev = prev_by_id.get(gid)
        sides = fresh.get(gid) or {}
        built = build_goalies(sides, prev, now)
        if built is None:
            g.pop("goalies", None)
            continue
        g["goalies"] = built
        stats["with_goalies"] += 1
        for s in ("home", "away"):
            v = built.get(s)
            if not v:
                continue
            stats["confirmed_sides" if v.get("status") == "confirmed" else "projected_sides"] += 1
            if s not in sides and prev and prev.get(s) == v:
                stats["carried_sides"] += 1
    return stats


def upgrade_with_moneypuck(espn_sides: dict, mp_side: dict | None) -> dict | None:
    """Pick the side value when both ESPN and MoneyPuck spoke.  MoneyPuck is only consulted for sides ESPN had not confirmed, so the
    cases are: ESPN projected/absent + MP confirmed -> MP wins; ESPN projected + MP projected -> ESPN kept (adds nothing);
    ESPN absent + MP projected -> MP."""
    if not mp_side:
        return espn_sides if espn_sides else None
    if not espn_sides:
        return mp_side
    if espn_sides.get("status") == "confirmed":
        return espn_sides
    if mp_side.get("status") == "confirmed":
        return mp_side
    return espn_sides


# ───────────────────────────── MoneyPuck parsing (pure) ─────────────────────────────

def parse_tweet_csv(text: str) -> dict | None:
    """tweets/starting_goalies/{id}{H|A}.csv -> {"name","status":"confirmed","src":"moneypuck","id":<NHL player id>,"by":<handle>,"ts":<UTC>}.
    Header: tweet_id,author_id,handle,created_at,found_at,goalie_id,goalie_name,text.  MoneyPuck's own page reads the FIRST data row;
    so do we.  created_at is US Eastern ('2026-10-02 11:35 AM').  A 404 HTML body (or anything without that header) -> None."""
    try:
        rows = list(csv.DictReader(io.StringIO((text or "").lstrip("﻿"))))
    except csv.Error:
        return None
    if not rows or "goalie_name" not in (rows[0] or {}):
        return None
    r = rows[0]
    name = (r.get("goalie_name") or "").strip()
    if not name:
        return None
    out = {"name": name, "status": "confirmed", "src": "moneypuck"}
    gid = (r.get("goalie_id") or "").strip()
    if gid.isdigit():
        out["id"] = int(gid)
    handle = (r.get("handle") or "").strip()
    if handle:
        out["by"] = handle
    for col in ("created_at", "found_at"):
        raw = (r.get(col) or "").strip()
        try:
            out["ts"] = _iso(datetime.strptime(raw, "%Y-%m-%d %I:%M %p").replace(tzinfo=_ET))
            break
        except ValueError:
            continue
    return out


def parse_prediction_csv(text: str) -> dict | None:
    """start_predictions/{id}{H|A}.csv (goalie_id,goalieName,final_start_probability) -> projected value for the MOST likely goalie
    {"name","status":"projected","src":"moneypuck","id","p":<0-1, 2dp>} or None."""
    try:
        rows = list(csv.DictReader(io.StringIO((text or "").lstrip("﻿"))))
    except csv.Error:
        return None
    best = None
    for r in rows:
        try:
            p = float(r.get("final_start_probability"))
        except (TypeError, ValueError):
            continue
        name = (r.get("goalieName") or "").strip()
        if name and (best is None or p > best[0]):
            best = (p, name, (r.get("goalie_id") or "").strip())
    if not best:
        return None
    out = {"name": best[1], "status": "projected", "src": "moneypuck", "p": round(best[0], 2)}
    if best[2].isdigit():
        out["id"] = int(best[2])
    return out


def mp_schedule_index(rows: list[dict], unmapped: set | None = None) -> dict:
    """MoneyPuck SeasonSchedule rows ([{a,h,est,id}], duplicates exist) -> {(home_abbr, away_abbr): [(start_utc, game_id), ...]}.
    `est` is US Eastern wall time 'YYYYMMDD HH:MM:SS' despite the name.  Unmapped team codes are skipped and collected."""
    idx: dict = {}
    seen = set()
    for r in rows or []:
        gid = r.get("id")
        if gid in seen:
            continue
        seen.add(gid)
        h, a = app_abbr(r.get("h")), app_abbr(r.get("a"))
        if not h or not a:
            if unmapped is not None:
                unmapped.update(c for c, m in ((r.get("h"), h), (r.get("a"), a)) if not m)
            continue
        try:
            start = datetime.strptime(r["est"], "%Y%m%d %H:%M:%S").replace(tzinfo=_ET).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        idx.setdefault((h, a), []).append((start, gid))
    return idx


def mp_game_id(idx: dict, home: str, away: str, start_utc: datetime) -> int | None:
    """The MoneyPuck game id for an ESPN game: same (home, away) and the nearest start within 3h; None if none or ambiguous."""
    cands = [(abs((s - start_utc).total_seconds()), gid) for s, gid in idx.get((home, away), []) if abs((s - start_utc).total_seconds()) <= 3 * 3600]
    if not cands:
        return None
    cands.sort()
    if len(cands) > 1 and cands[1][0] - cands[0][0] < 3600:
        return None  # two near-simultaneous games for the same pairing: refuse to guess
    return cands[0][1]


# ───────────────────────────── MoneyPuck fetch (opt-in, fail-open) ─────────────────────────────

def fetch_moneypuck(games: list[dict], espn_sides: dict[str, dict], season_id: str, now: datetime | None = None,
                    http_get: Optional[Callable] = None, sleep: Callable[[float], None] = time.sleep,
                    lookahead_hours: float = MP_LOOKAHEAD_HOURS) -> tuple[dict, dict]:
    """For upcoming games, ask MoneyPuck about every side ESPN has NOT confirmed.  Returns (fresh_sides_by_game_id, meta).
    `fresh` has the already-merged per-side value (MoneyPuck wins only when it is confirmed, see upgrade_with_moneypuck).
    Never raises; any failure just leaves ESPN's value in place."""
    now = now or datetime.now(timezone.utc)
    meta = {"enabled": True, "requests": 0, "games": 0, "confirmed": 0, "projected": 0, "unmatched": 0, "unmapped": [],
            "pred_published": None, "error": None}
    fresh: dict[str, dict] = {}
    if http_get is None:
        import requests

        def http_get(url):  # noqa: E306
            return requests.get(url, headers={"User-Agent": MP_USER_AGENT}, timeout=20)

    def get(path: str):
        if meta["requests"] >= MP_MAX_REQUESTS:
            raise RuntimeError("request cap reached")
        if meta["requests"]:
            sleep(MP_REQUEST_GAP_S)
        meta["requests"] += 1
        return http_get(f"{MP_BASE}/{path}")

    try:
        r = get(f"OldSeasonScheduleJson/SeasonSchedule-{season_id}.json")
        if r.status_code != 200:
            raise RuntimeError(f"schedule HTTP {r.status_code}")
        unmapped: set = set()
        idx = mp_schedule_index(r.json(), unmapped)
        meta["unmapped"] = sorted(unmapped)
        if unmapped:
            _log(f"WARNING unmapped MoneyPuck team codes (skipped): {sorted(unmapped)}")
        pred_probes, pred_hits = 0, 0
        for g in games:
            if g.get("state") != "pre" or not g.get("home") or not g.get("away"):
                continue
            try:
                start = datetime.strptime(g["date"], _ISO).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if not (0 <= (start - now).total_seconds() / 3600 <= lookahead_hours):
                continue
            gid = str(g.get("id"))
            mpid = mp_game_id(idx, g["home"], g["away"], start)
            if mpid is None:
                meta["unmatched"] += 1
                _log(f"no unique MoneyPuck game for {g['away']}@{g['home']} {g['date']}")
                continue
            espn = espn_sides.get(gid) or {}
            got: dict = {}
            meta["games"] += 1
            for side, letter in (("home", "H"), ("away", "A")):
                if (espn.get(side) or {}).get("status") == "confirmed":
                    continue  # ESPN already says confirmed: nothing a request could improve
                mp_val = None
                tr = get(f"tweets/starting_goalies/{mpid}{letter}.csv")
                if tr.status_code == 200:
                    mp_val = parse_tweet_csv(tr.text)
                elif tr.status_code != 404:
                    _log(f"  tweets {mpid}{letter}: HTTP {tr.status_code}")
                if mp_val is None and (pred_probes < MP_PRED_PROBE_SIDES or pred_hits):
                    pr = get(f"start_predictions/{mpid}{letter}.csv")
                    pred_probes += 1
                    if pr.status_code == 200:
                        mp_val = parse_prediction_csv(pr.text)
                        pred_hits += 1 if mp_val else 0
                if mp_val:
                    meta["confirmed" if mp_val["status"] == "confirmed" else "projected"] += 1
                chosen = upgrade_with_moneypuck(espn.get(side), mp_val)
                if chosen:
                    got[side] = chosen
            for side in ("home", "away"):  # keep ESPN's confirmed side in the same record
                if side not in got and espn.get(side):
                    got[side] = espn[side]
            if got:
                fresh[gid] = got
        meta["pred_published"] = bool(pred_hits) if pred_probes else None
    except Exception as exc:
        meta["error"] = str(exc)[:200]
        _log(f"MoneyPuck pass aborted (fail-open; ESPN values kept): {exc}")
    return fresh, meta
