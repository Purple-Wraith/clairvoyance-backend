"""Pure (no network, stdlib only) parsers for ESPN's injuries + team-roster payloads.

Shared by scripts/clairvoyance_update.py (data.json `injuries` / `<sport>.roster`) and scripts/fetch_nfl.py (docs/nfl_injuries.json).
Kept dependency-free so scripts/test_injury_wiring.py can exercise them offline against fixtures.

WHY THIS MODULE EXISTS (two silent producer bugs found 2026-10-03, both "scraped but unusable"):

  1. ESPN's `/injuries` payload no longer carries `team.abbreviation` on the team entry -- only `team.id` + `team.displayName`.  The
     abbreviation moved to each injury's `athlete.team.abbreviation`.  The old parsers read the (now missing) top-level field, so
     every data.json injury row had team="" and fetch_nfl.fetch_injuries() skipped EVERY team (docs/nfl_injuries.json == {"teams":{}}
     even though ESPN lists ~800 NFL rows).  `team_abbr_for` reads the athlete-level field first.
  2. ESPN's NHL `/teams/{id}/roster` groups `athletes` by position ([{"position": "Centers", "items": [player, ...]}, ...]) instead of
     the flat list the NBA/WNBA endpoint returns.  The old loop treated each GROUP dict as a player, and a group's `position` is a
     plain string ("Centers"), so `(p.get("position") or {}).get("abbreviation")` raised `'str' object has no attribute 'get'` ->
     32 of 32 teams logged a warning and nhl.roster stayed {}.  `flatten_roster_athletes` accepts both shapes (and the NFL
     offense/defense/specialTeam group shape).

The athlete id is in the injury row only as a link (`.../player/_/id/4565270/drew-helleson`); the row's own `id` is the INJURY record's
id.  `athlete_id` extracts the real ESPN athlete id so matching against roster rows (whose `id` IS the athlete id) is by identity.
"""
from __future__ import annotations

import re
import unicodedata

_ID_RE = re.compile(r"/id/(\d+)")

# ESPN's NHL team abbreviations that differ from the NHL-API / app (docs/app.html NHL const) keys.  Same table as fetch_nhl.py's
# ESPN_ABBR_FIX; applied to NHL injury + roster rows so `team` lines up with NHL[abbr] lookups.  Other leagues pass no map.
NHL_ABBR_FIX = {"LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL", "UTAH": "UTA"}


def norm_name(name: str) -> str:
    """Accent/case/punctuation-insensitive person-name key ('Leevi Meriläinen' == 'leevi merilainen'; 'Jr.' kept as a token)."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


def athlete_id(athlete: dict | None) -> str | None:
    """ESPN athlete id for an injuries-feed athlete (from its player-card/stats link); None when no link carries one."""
    ath = athlete or {}
    if ath.get("id"):
        return str(ath["id"])
    for ln in ath.get("links") or []:
        m = _ID_RE.search((ln or {}).get("href") or "")
        if m:
            return m.group(1)
    return None


def team_abbr_for(entry: dict | None, athlete: dict | None, id_to_abbr: dict | None = None) -> str:
    """Team abbreviation for one injuries-feed row: athlete.team first (where ESPN now puts it), then the entry's own field (the
    pre-2026 shape), then an optional {team_id: abbr} map built from the league's teams list.  "" when none resolve."""
    a = ((athlete or {}).get("team") or {}).get("abbreviation")
    if a:
        return a
    t = (entry or {}).get("team") or {}
    if t.get("abbreviation"):
        return t["abbreviation"]
    if id_to_abbr and t.get("id") is not None:
        return id_to_abbr.get(str(t["id"]), "")
    return ""


def parse_injuries(data: dict | None, sport_key: str, id_to_abbr: dict | None = None, abbr_fix: dict | None = None) -> list[dict]:
    """ESPN `/injuries` JSON -> flat rows: {team,name,pos,status,detail,return,sport,id}.  `id` is the ESPN ATHLETE id (or "" when ESPN
    gave no link).  `detail` = details.detail, else details.type (NHL/NFL carry the body part as `type`, e.g. 'Lower Body').
    `abbr_fix` optionally rewrites ESPN abbreviations to the app's (NHL_ABBR_FIX)."""
    fix = abbr_fix or {}
    items: list[dict] = []
    for entry in (data or {}).get("injuries") or []:
        for inj in entry.get("injuries") or []:
            ath = inj.get("athlete") or {}
            det = inj.get("details") or {}
            tm = team_abbr_for(entry, ath, id_to_abbr)
            items.append({
                "team":   fix.get(tm, tm),
                "name":   ath.get("displayName", ""),
                "pos":    (ath.get("position") or {}).get("abbreviation", ""),
                "status": inj.get("status", ""),
                "detail": det.get("detail") or det.get("type") or "",
                "return": det.get("returnDate", "") or "",
                "sport":  sport_key,
                "id":     athlete_id(ath) or "",
            })
    return items


def flatten_roster_athletes(roster: dict | None) -> list[dict]:
    """ESPN team-roster JSON -> flat list of player dicts, whatever the grouping:
         flat      : athletes = [player, ...]                                   (NBA, WNBA, soccer)
         by group  : athletes = [{"position": "Centers", "items": [player...]}, ...]   (NHL; NFL offense/defense/specialTeam/...)
    Anything that is not a player dict (a bare string, a group with no `items`) is skipped rather than raising."""
    out: list[dict] = []
    for a in (roster or {}).get("athletes") or []:
        if not isinstance(a, dict):
            continue
        if isinstance(a.get("items"), list):
            out.extend(p for p in a["items"] if isinstance(p, dict))
        else:
            out.append(a)
    return out


def parse_roster(roster: dict | None, abbr: str, abbr_fix: dict | None = None) -> dict:
    """ESPN team-roster JSON -> {"player name": {"team": abbr, "pos": "C", "id": "5149153"}} keyed lowercase (the codebase's existing
    name-lookup convention; docs/app.html _playerTeam reads .team/.pos).  `id` is the ESPN athlete id (additive field)."""
    abbr = (abbr_fix or {}).get(abbr, abbr)
    out: dict = {}
    for p in flatten_roster_athletes(roster):
        name = (p.get("fullName") or p.get("displayName") or "").strip()
        if not name:
            continue
        pos = p.get("position")
        pos_abbr = pos.get("abbreviation", "") if isinstance(pos, dict) else ""
        out[name.lower()] = {"team": abbr, "pos": pos_abbr, "id": str(p.get("id") or "")}
    return out


def parse_nfl_injuries(data: dict | None) -> dict:
    """ESPN NFL `/injuries` JSON -> {"teams": {ABBR: [row, ...]}} in docs/nfl_injuries.json's shape (rows: player, playerId, position, status,
    injury, estimatedReturn, comment, date).  Team comes from the athlete (see module docstring, bug 1).  "Active" rows (ESPN lists ~600
    players who are on the report but playing) are dropped: both app.html consumers already filter them, and the file is downloaded on
    every app load.  Comments are capped at 240 chars for the same reason."""
    teams: dict[str, list[dict]] = {}
    for entry in (data or {}).get("injuries") or []:
        for item in entry.get("injuries") or []:
            athlete = item.get("athlete") or {}
            details = item.get("details") or {}
            abbr = team_abbr_for(entry, athlete)
            if not abbr:
                continue
            if str(item.get("status") or "").strip().lower() == "active":
                continue
            teams.setdefault(abbr, []).append({
                "player": athlete.get("displayName"),
                "playerId": athlete_id(athlete),
                "position": (athlete.get("position") or {}).get("abbreviation"),
                "status": item.get("status"),
                "injury": details.get("type") or item.get("shortComment"),
                "estimatedReturn": details.get("returnDate"),
                "comment": ((item.get("longComment") or item.get("shortComment") or "")[:240]) or None,
                "date": item.get("date"),
            })
    return {"teams": teams}
