"""Shared "a started game never vanishes" carry-over for the Flashscore hockey schedule files
(docs/{liiga,shl,nla,extraliga}_schedule.json, written by fetch_liiga.py / fetch_shl.py / fetch_nla.py / fetch_extraliga.py).

THE BUG THIS FIXES (verified 2026-10-02 against the committed snapshots; see scripts/test_schedule_carry.py):
each fetch script builds its `games` list as  fixtures-page rows  +  results-page rows,  keyed by match id.  A game that is
IN PROGRESS (or just ended but not yet on the results page) when the scrape runs is on neither page: Flashscore removes a
match from /fixtures/ the moment it starts and only adds it to /results/ once it is final.  So the merge silently DROPS it, and
the schedule file has no entry for that game at all until a later refresh happens to catch the final.  Real evidence: Liiga's
07:57 AM MT snapshot listed 5 games for 2026-10-02 (all 'pre'); the 11:53 AM MT snapshot (scraped 17:51Z, games started 15:30Z
and 16:30Z) listed ZERO games for that date; the 4:02 PM MT snapshot listed the same 5 ids as 'post' with scores.  Over the
committed history 2026-09-16..10-02, every game that vanished while already started later reappeared -- the file just lagged by
whole refresh cycles (and the refreshes themselves land hours late).  docs/app.html's _autoSettleFlashscoreHockey only settles
a pick against a state:'post' game that has scores, so every pick on a vanished game sat 'pending' until a later scrape.

THE FIX: after merging, re-add any game from the PREVIOUS committed file that is in neither the new fixtures nor the new
results -- but ONLY once it has started (carrying a not-yet-started fixture that merely disappeared would invent phantom
upcoming games for the lock pipeline), and only for CARRY_MAX_DAYS after its start (a game that never reappears is dropped
instead of living in the file forever).  A carried game keeps its previous fields untouched and gains:
    "carried": true
    "lastSeen": "YYYY-MM-DDTHH:MMZ"   -- the scrape time of the last run that really saw it (preserved across carries)
It is NEVER promoted: state stays whatever it was (normally 'pre'), scores stay null.  The moment a later scrape sees the game
again, the fresh row replaces it wholesale (so `carried`/`lastSeen` vanish).

Why this is safe for the rest of the pipeline (checked 2026-10-02):
  * settle: _autoSettleFlashscoreHockey requires state==='post' AND numeric scores -- a carried 'pre' row can never settle a pick,
    and a carried 'post' row only ever carries scores that a real results scrape produced.
  * lock: the automated passes read startMs from g.date and refuse any game whose start is <= now + LOCK_START_MARGIN_MIN
    (auto_lock_settle.start_guard / app.html _lockStartBlock) -- a carried row is by construction already started, so the guard
    refuses it exactly as it would a game still listed as 'pre' in the fixtures.  Carrying adds no lockable leg.
  * the "0 upcoming fixtures" transient-failure guard in the fetch scripts counts the SCRAPED rows, before carry-over is applied
    (so carried 'pre' rows cannot mask a failed fixtures scrape).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# How long after its start a vanished game is still carried.  A hockey game is over ~3h after its start and Flashscore posts the
# final within minutes-hours; the scheduled refreshes land 2-6h late, so 3 days is far beyond any real gap yet small enough that
# a game which genuinely never reappears (postponed / abandoned / removed) leaves the file quickly.
CARRY_MAX_DAYS = 3

_ISO_FMT = "%Y-%m-%dT%H:%MZ"


def _parse_iso(s):
    """'2026-10-02T15:30Z' (the schedule files' own format) -> aware UTC datetime, or None."""
    try:
        return datetime.strptime(str(s), _ISO_FMT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_generated_at(s):
    """'2026-10-02 13:55 UTC' (the files' generated_at) -> aware UTC datetime, or None."""
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _has_scores(g) -> bool:
    return all(isinstance(g.get(k), (int, float)) and not isinstance(g.get(k), bool) for k in ("homeScore", "awayScore"))


def load_previous(path) -> dict:
    """The previous committed schedule file as a dict ({} on any problem -- carry-over then simply does nothing)."""
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def carry_over_missing(games: list[dict], prev_doc: dict, now: datetime, max_days: float = CARRY_MAX_DAYS) -> dict:
    """Append, in place, every STARTED game of `prev_doc` whose id is not already in `games` (the new fixtures + results merge),
    then re-sort `games` by date.  `now` is the scrape time (aware datetime).  Returns {"carried": [ids], "expired": [ids]}
    (expired = vanished + started more than max_days ago, deliberately NOT carried; the caller can log them).

    Never raises on malformed previous rows: a row without an id or a parseable start is skipped."""
    out = {"carried": [], "expired": []}
    prev_games = (prev_doc or {}).get("games") or []
    if not isinstance(prev_games, list):
        return out
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    present = {g.get("id") for g in games}
    prev_seen = parse_generated_at((prev_doc or {}).get("generated_at"))
    prev_seen_iso = prev_seen.strftime(_ISO_FMT) if prev_seen else None
    horizon = timedelta(days=max_days)
    for pg in prev_games:
        if not isinstance(pg, dict):
            continue
        gid = pg.get("id")
        if not gid or gid in present:
            continue
        start = _parse_iso(pg.get("date"))
        if start is None:
            continue
        if start > now:
            continue  # not started yet: a fixture that merely dropped off the page -- never invent an upcoming game
        if now - start > horizon:
            out["expired"].append(gid)
            continue
        g = dict(pg)
        # never present a score-less game as final (a 'post' row with no numeric scores is demoted back to 'pre')
        if g.get("state") == "post" and not _has_scores(g):
            g["state"] = "pre"
            g["homeScore"] = None
            g["awayScore"] = None
        g["carried"] = True
        g["lastSeen"] = pg.get("lastSeen") or prev_seen_iso or pg.get("date")
        games.append(g)
        present.add(gid)
        out["carried"].append(gid)
    if out["carried"]:
        games.sort(key=lambda x: x.get("date") or "")
    return out


def merge_results_only(prev_doc: dict, results: list[dict], now: datetime) -> tuple[dict, dict]:
    """RESULTS-ONLY refresh (scripts/refresh_hockey_results.py): fold a fresh /results/ scrape into the previous committed file
    WITHOUT touching anything else -- fixtures, odds, teams/standings and every other game stay byte-identical.  Used by the settle
    passes so they can read final scores minutes old instead of waiting for the next full refresh.

    For each scraped final result:
      * the game is already in the file  -> its row becomes {**previous row, **result row}: the fresh state/scores/date win, the
        previous row's odds/ou survive (the results page carries none), and carried/lastSeen are cleared (it is seen again);
      * the game is not in the file       -> appended (a game that started and finished between two full refreshes).
    A result without numeric scores is ignored (never create a 'post' without a score).  Returns (new_doc, stats); new_doc is a
    deep-enough copy (prev_doc is never mutated) and equals prev_doc exactly when nothing changed -- the caller then skips the
    write so an idle refresh commits nothing.  stats = {"updated": [ids pre->post or score change], "added": [ids], "unchanged": n}.
    On changes `resultsRefreshedAt` is stamped; `generated_at` (the full-refresh time other code reads) is left alone."""
    doc = json.loads(json.dumps(prev_doc or {}))
    games = doc.get("games")
    if not isinstance(games, list):
        games = []
    by_id = {g.get("id"): i for i, g in enumerate(games) if isinstance(g, dict) and g.get("id")}
    stats = {"updated": [], "added": [], "unchanged": 0}
    for r in results or []:
        if not isinstance(r, dict) or not r.get("id") or r.get("state") != "post" or not _has_scores(r):
            continue
        i = by_id.get(r["id"])
        if i is None:
            games.append(dict(r))
            by_id[r["id"]] = len(games) - 1
            stats["added"].append(r["id"])
            continue
        old = games[i]
        new = {**old, **r}
        new.pop("carried", None)
        new.pop("lastSeen", None)
        if new == old:
            stats["unchanged"] += 1
            continue
        games[i] = new
        stats["updated"].append(r["id"])
    if stats["updated"] or stats["added"]:
        games.sort(key=lambda x: x.get("date") or "")
        doc["games"] = games
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        doc["resultsRefreshedAt"] = now.strftime("%Y-%m-%d %H:%M UTC")
    else:
        doc = json.loads(json.dumps(prev_doc or {}))
    return doc, stats
