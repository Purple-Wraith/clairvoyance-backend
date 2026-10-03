"""Pure merge + atomic-write helpers for `clairvoyance_update.py --only-nhl-core` (the lock_prep.py "data-nhl" job).

Kept in its own tiny module (no requests / bs4 / network imports) so the merge rules are unit-testable in any interpreter and the
big clairvoyance_update.py needs no new top-level code.  See run_nhl_core_refresh() there for the fetch side.
"""
from __future__ import annotations

import os
from pathlib import Path

NHL_CORE_MIN_STANDINGS = 20   # a real /standings/now has 32 teams; fewer = a partial/broken response, keep the old table


def merge_nhl_core(data: dict, standings: dict, edge: dict, mp_data: dict, skater_value: dict, injuries: list,
                   stamp_iso: str) -> tuple[dict, list[str]]:
    """Pure merge: -> (data, [names of the parts that were replaced]).  A part is replaced only when the fresh fetch looks real;
    an empty/short fetch keeps what data.json already had.  Never touches keys other than nhl.standings / nhl.edge /
    nhl.skaterValue / mp / injuries.nhl, plus the top-level `nhlCoreAt` stamp (set only when something was replaced).
    Raises ValueError when data has no 'nhl' object (the caller then leaves the file untouched)."""
    changed: list[str] = []
    nhl = data.get("nhl")
    if not isinstance(nhl, dict):
        raise ValueError("data.json has no 'nhl' object -- refusing to merge")
    if isinstance(standings, dict) and len(standings) >= NHL_CORE_MIN_STANDINGS:
        nhl["standings"] = standings
        changed.append("standings")
    if isinstance(edge, dict) and (edge.get("goalies") or edge.get("teamRates")):
        nhl["edge"] = edge
        changed.append("edge")
    if isinstance(skater_value, dict) and skater_value.get("players"):
        nhl["skaterValue"] = skater_value
        changed.append("skaterValue")
    if isinstance(mp_data, dict) and mp_data.get("teams"):
        data["mp"] = mp_data
        changed.append("mp")
    if isinstance(injuries, list) and injuries:
        inj = data.get("injuries")
        if not isinstance(inj, dict):
            inj = data["injuries"] = {}
        inj["nhl"] = injuries
        changed.append("injuries.nhl")
    if changed:
        data["nhlCoreAt"] = stamp_iso
    return data, changed


def atomic_write_text(path: Path, text: str) -> None:
    """Write next to the target then os.replace (a reader / a killed process never sees a half-written file)."""
    path = Path(path)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
