"""Light-column ledger refresh: ask Supabase for the SMALL columns first, fetch the big `raw` JSON only for rows that actually changed.

Why (2026-11-03, Supabase egress 11.6 GB in a cycle against the Free plan's 5 GB): every CI session, every scheduled data refresh and every scoped app pull re-downloaded the `raw` blob (~1.4 KB per bet, ~85% of
the bytes) for every row in its window, although nearly all of those rows are identical to the copy the caller already holds (docs/picks_backup.json in CI, getP() in the app).  Measured against the live table:
a 14-day window + pending is 1,143 rows = 297 KB decoded / 45 KB gzipped as light columns, against ~1.6 MB / ~400 KB with `raw`; the real difference between that window and the backup is ~30 rows.

    LIGHT_COLS      the select list (flat columns + the one raw field the merge cares about, `regradedNote`)
    pick_signature  what a pick WOULD upload (mirrors docs/app.html `_supabaseBetRow`) -- the thing compared against the server's row
    row_signature   the same tuple built from a light row
    plan            (tombstone ids, ids to fetch in full) for a list of light rows against what the caller already holds

A mismatch only ever costs an extra `raw` fetch; a signature that is equal while the raw differs can only be a change to a field no column carries (the caller's daily complete pull still catches those).
"""
from __future__ import annotations

LIGHT_COLS = "id,outcome,locked_at,settled_at,date,wager,dec_odds,ml,bet_on,win_prob,grade,note,ha,awa,sport,bet_type,rn:raw->>regradedNote"
RAW_CHUNK = 40          # ids per `id=in.(...)` request (URL length, not row count, is the limit)


def _or_none(v):
    """JS `v || null`"""
    return v if v else None


def _num_or(v, default):
    """JS `(v != null && !isNaN(v)) ? v : default`"""
    if v is None or isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return default if v != v else v
    try:
        f = float(v)
        return default if f != f else v
    except (TypeError, ValueError):
        return default


def _js_string(v):
    """JS String(v) for the numbers/strings an `ml` can hold"""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def pick_signature(p: dict) -> tuple:
    return (
        p.get("outcome") or "pending",
        _or_none(p.get("lockedAt")),
        _or_none(p.get("settledAt")),
        _or_none(p.get("date")),
        _num_or(p.get("wager"), 100),
        _num_or(p.get("decOdds"), None),
        None if p.get("ml") is None else _js_string(p["ml"]),
        _or_none(p.get("betOn")),
        _num_or(p.get("winProb"), None),
        _or_none(p.get("grade")),
        _or_none(p.get("note")),
        _or_none(p.get("hA")),
        _or_none(p.get("awA")),
        _or_none(p.get("sport")),
        _or_none(p.get("betType")),
        _or_none(p.get("regradedNote")),
    )


def row_signature(r: dict) -> tuple:
    return (
        r.get("outcome") or "pending", r.get("locked_at"), r.get("settled_at"), r.get("date"), r.get("wager"), r.get("dec_odds"), r.get("ml"), r.get("bet_on"), r.get("win_prob"), r.get("grade"),
        r.get("note"), r.get("ha"), r.get("awa"), r.get("sport"), r.get("bet_type"), _or_none(r.get("rn")),
    )


def plan(light_rows: list[dict], held_by_id: dict[str, dict]) -> tuple[list[str], list[str]]:
    """-> (tombstone ids, ids whose full row must be fetched).  A row we do not hold, or whose signature differs from the one we hold, needs its `raw`; a soft-deleted row never does."""
    tombs: list[str] = []
    fetch: list[str] = []
    for r in light_rows:
        rid = r.get("id")
        if not rid:
            continue
        if r.get("outcome") == "_removed":
            tombs.append(rid)
            continue
        p = held_by_id.get(rid)
        if p is None or row_signature(r) != pick_signature(p):
            fetch.append(rid)
    return tombs, fetch


def chunks(ids: list[str], n: int = RAW_CHUNK):
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def in_filter(ids: list[str]) -> str:
    """value for PostgREST `id=in.(...)`: every id double-quoted so a comma/paren inside one cannot split the list"""
    return "(" + ",".join('"' + i.replace("\\", "\\\\").replace('"', '\\"') + '"' for i in ids) + ")"


def row_from_pick(p: dict) -> dict:
    """a full `bets` row rebuilt from a held pick -- same shape docs/app.html `_supabaseBetRow` uploads (for callers that want rows, not picks)"""
    return {
        "id": p.get("id"), "ha": _or_none(p.get("hA")), "awa": _or_none(p.get("awA")), "sport": _or_none(p.get("sport")), "league": None,
        "bet_type": _or_none(p.get("betType")), "bet_on": _or_none(p.get("betOn")), "win_prob": _num_or(p.get("winProb"), None),
        "ml": None if p.get("ml") is None else _js_string(p["ml"]), "dec_odds": _num_or(p.get("decOdds"), None), "wager": _num_or(p.get("wager"), 100),
        "outcome": p.get("outcome") or "pending", "date": _or_none(p.get("date")), "locked_at": _or_none(p.get("lockedAt")), "settled_at": _or_none(p.get("settledAt")),
        "grade": _or_none(p.get("grade")), "note": _or_none(p.get("note")), "raw": p,
    }
