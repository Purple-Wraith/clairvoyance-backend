#!/usr/bin/env python3
"""WATCH-ONLY calibration alerts (2026-10-10, owner decision (c): alert, never block).

WHAT IT DOES
    For every market key -- league x market type (CFB:OU, NHL:SPREAD, ...) plus two pooled families, EUROHKY (SHL/LIIGA/NLA/EXTRALIGA) and SOCCER (PL/LIGA/CL/SERIEA) -- it replays the
    settled picks in time order through a one-sided overconfidence SPRT (a reflected LLR CUSUM):
        H0: the stated probability p is right            H1: the true probability is p - 0.10
        win : LLR += ln((p - d) / p)                      loss: LLR += ln((1 - p + d) / (1 - p))              L = max(0, L + LLR)
    A key enters WATCH when L >= ln(1/0.10) = 2.303 and it has at least 15 settled picks; it leaves WATCH when L falls below half the boundary or the last 50 picks (at least 20) are no longer overconfident (gap >= -5pp; the CUSUM then restarts at 0). It reports n, mean stated p,
    win rate, gap, z (all picks) and the same for the last 50. Output: docs/calibration_watch.json.

WHAT IT NEVER DOES
    Block, quarantine, hold, re-price or shadow a pick. The auto-quarantine / probation build was explicitly NOT approved. Nothing in the lock pipeline imports this file.

EVIDENCE USED
    settled picks (win / loss; pushes excluded) of ACTIVE leagues only (retired MLB / WNBA / MLS / BUND / NCAAH / WTA ... are ignored), pre-start locks only (the shared lock_timing
    classifier: known-late manual locks are excluded, unknown timing kept -- the same basis as every published figure). Source: docs/picks_backup.json (committed; no network).
    Alt-line picks stay in: their stated probability is the one the pick is sold on. NOTE the CUSUM replays the WHOLE history, so a market that was overconfident before a fix keeps
    WATCH until enough honest picks have settled for L to decay -- that is the point (the fix has to prove itself).

DAILY INTEGRATION (scripts/daily_health_check.py, full pass only)
    update_watch() recomputes + rewrites docs/calibration_watch.json, keeps each key's `notified` date, and returns the keys that are NEW in WATCH (never notified) -> one alert-level
    problem line each, which the existing once-per-day deduped owner email sends; after a successful send mark_notified() stamps them so the next day does not repeat them. Keys that were
    already known are listed as watch notes (and as one 'still watching' line in the weekly digest). A key that leaves WATCH and re-enters gets a new `since` and a new email.

    python3 scripts/calibration_watch.py            print the table
    python3 scripts/calibration_watch.py --write    also write docs/calibration_watch.json (keeps notified flags)
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lock_timing as lt  # noqa: E402

MT = ZoneInfo("America/Denver")
WATCH_PATH = ROOT / "docs" / "calibration_watch.json"

# ── parameters (owner spec) ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
DELTA = 0.10                      # H1: true probability = stated - DELTA
ALPHA = 0.10                      # WATCH boundary A = ln(1/ALPHA)
BOUNDARY = math.log(1 / ALPHA)
MIN_N = 15                        # fewest settled picks before a key can enter WATCH
WINDOW = 50                       # last-N window reported beside the all-time numbers
RELEASE_FRAC = 0.5                # leaves WATCH when L < RELEASE_FRAC * BOUNDARY (stops a flapping alert at the boundary)
RELEASE_WINDOW_MIN = 20           # ... or when the last WINDOW picks (at least this many) are no longer overconfident: gap >= -DELTA/2. The CUSUM is then restarted at 0 (else it would
                                  # re-trip on the very next pick: a reflected CUSUM remembers early losses forever, and a market that has recovered should not stay flagged by them)
P_CLAMP = (0.02, 0.98)

# Active leagues = the five paid products (PRODUCT_SPORTS in scripts/auto_lock_settle.py, SOC_ prefix dropped); test_calibration_watch.py checks this stays in step.
ACTIVE_LEAGUES = frozenset({"NFL", "CFB", "NBA", "NHL", "SHL", "LIIGA", "NLA", "EXTRALIGA", "PL", "LIGA", "CL", "SERIEA"})
FAMILY = {"SHL": "EUROHKY", "LIIGA": "EUROHKY", "NLA": "EUROHKY", "EXTRALIGA": "EUROHKY", "PL": "SOCCER", "LIGA": "SOCCER", "CL": "SOCCER", "SERIEA": "SOCCER"}
MARKETS = ("ML", "SPREAD", "OU", "PROP")
_MKT_MAP = {"ML": "ML", "SPREAD": "SPREAD", "RL": "SPREAD", "PL": "SPREAD", "OU": "OU", "PROP": "PROP"}


# ── pure functions ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def llr_increment(p: float, win: bool, delta: float = DELTA) -> float:
    """Log-likelihood ratio of H1 (true prob p - delta) against H0 (true prob p) for one settled pick."""
    p0 = min(max(p, P_CLAMP[0]), P_CLAMP[1])
    q = min(max(p - delta, P_CLAMP[0]), P_CLAMP[1])
    return math.log(q / p0) if win else math.log((1 - q) / (1 - p0))


def market_of(pick: dict) -> str | None:
    return _MKT_MAP.get(str(pick.get("betType") or "").upper())


def keys_for(league: str, market: str) -> list[str]:
    """['CFB:OU'] or ['SHL:SPREAD', 'EUROHKY:SPREAD']; [] for a retired / unknown league or market."""
    if league not in ACTIVE_LEAGUES or market not in MARKETS:
        return []
    out = [f"{league}:{market}"]
    if league in FAMILY:
        out.append(f"{FAMILY[league]}:{market}")
    return out


def settle_ms(pick: dict) -> float:
    """When the pick settled: settledAt if stored, else noon MT the day after the game (the same fallback the exploration used)."""
    s = pick.get("settledAt")
    try:
        if s:
            return float(s)
    except (TypeError, ValueError):
        pass
    try:
        d = datetime.fromisoformat(str(pick.get("date"))).replace(hour=12, tzinfo=MT)
        return d.timestamp() * 1000 + 86400000
    except Exception:
        return 0.0


def eligible_picks(picks: list[dict], idx: dict | None = None) -> list[dict]:
    """Settled (win/loss), non-parlay, stated-probability picks of active leagues, pre-start locks only. -> [{id, league, market, p, y, t(ms), date}], unsorted."""
    idx = idx if idx is not None else {}
    out = []
    for p in picks:
        if p.get("outcome") not in ("win", "loss") or p.get("winProb") is None or lt.is_parlay(p):
            continue
        mkt = market_of(p)
        league = lt.norm_sport(p)
        if mkt is None or league not in ACTIVE_LEAGUES:
            continue
        if lt.classify(p, idx) in lt.LATE_CLASSES:       # known-late manual locks are not predictions
            continue
        try:
            wp = float(p["winProb"])
        except (TypeError, ValueError):
            continue
        if not (0.0 < wp < 1.0):
            continue
        out.append({"id": p.get("id"), "league": league, "market": mkt, "p": wp, "y": 1 if p["outcome"] == "win" else 0, "t": settle_ms(p), "date": p.get("date")})
    return out


def _metrics(rows: list[tuple]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "mean_p": None, "win_rate": None, "gap_pp": None, "z": None}
    sp = sum(r[0] for r in rows)
    sy = sum(r[1] for r in rows)
    var = sum(r[0] * (1 - r[0]) for r in rows)
    return {"n": n, "mean_p": round(sp / n, 4), "win_rate": round(sy / n, 4), "gap_pp": round((sy - sp) / n * 100, 1), "z": round((sy - sp) / math.sqrt(var), 2) if var > 0 else 0.0}


def replay(stream: list[tuple], delta: float = DELTA, boundary: float = BOUNDARY, min_n: int = MIN_N, release_frac: float = RELEASE_FRAC) -> dict:
    """stream: time-ordered [(p, y, date_str)]. -> {status, since, n, llr, tripped_at_n, first_trip_n, ...metrics, window}. Pure: same stream, same answer."""
    L, status, since, entered_n, first_trip_n = 0.0, "ok", None, None, None
    win: list[tuple] = []
    for i, (p, y, d) in enumerate(stream, 1):
        L = max(0.0, L + llr_increment(p, bool(y), delta))
        win.append((p, y))
        if len(win) > WINDOW:
            win.pop(0)
        if status == "ok" and i >= min_n and L >= boundary:
            status, since, entered_n = "watch", d, i
            if first_trip_n is None:
                first_trip_n = i
        elif status == "watch":
            recovered = len(win) >= RELEASE_WINDOW_MIN and (sum(y_ for _p, y_ in win) - sum(p_ for p_, _y in win)) / len(win) >= -delta / 2
            if L < release_frac * boundary or recovered:
                status, since, entered_n = "ok", None, None
                if recovered:
                    L = 0.0
    rows = [(p, y) for p, y, _d in stream]
    m = _metrics(rows)
    m.update({"status": status, "since": since, "llr": round(L, 3), "entered_at_n": entered_n, "first_trip_n": first_trip_n,
              "window": _metrics(rows[-WINDOW:]), "last_date": stream[-1][2] if stream else None})
    return m


def build_streams(rows: list[dict]) -> dict[str, list[tuple]]:
    """{key: [(p, y, date)]} in settlement order, per league x market and per pooled family x market."""
    streams: dict[str, list[tuple]] = {}
    for r in sorted(rows, key=lambda r: (r["t"], str(r["id"]))):
        for k in keys_for(r["league"], r["market"]):
            streams.setdefault(k, []).append((r["p"], r["y"], r["date"]))
    return streams


def build_document(rows: list[dict], previous: dict | None = None, now: datetime | None = None) -> dict:
    """The docs/calibration_watch.json document. `previous` (the file as committed last time) supplies first_seen / notified so they survive a recompute."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(MT).strftime("%Y-%m-%d")
    prev_keys = (previous or {}).get("keys") or {}
    keys = {}
    for k, stream in sorted(build_streams(rows).items()):
        res = replay(stream)
        old = prev_keys.get(k) or {}
        league, market = k.split(":")
        entry = {"league": league, "market": market, "pooled": league in ("EUROHKY", "SOCCER"), **{f: res[f] for f in
                 ("status", "since", "n", "mean_p", "win_rate", "gap_pp", "z", "llr", "first_trip_n", "window", "last_date")}}
        entry["first_seen"] = old.get("first_seen") or today
        # `notified`: the Mountain date the owner was emailed about THIS episode. A new episode (ok -> watch, or a different `since`) starts un-notified.
        same_episode = res["status"] == "watch" and old.get("status") == "watch" and old.get("since") == res["since"]
        entry["notified"] = old.get("notified") if same_episode else None
        keys[k] = entry
    return {
        "generated_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "basis": "settled win/loss picks of active leagues, pre-start locks only (known-late excluded, pushes excluded); alert-only, nothing is blocked",
        "params": {"delta": DELTA, "alpha": ALPHA, "boundary": round(BOUNDARY, 4), "min_n": MIN_N, "window": WINDOW, "release_frac": RELEASE_FRAC},
        "watch": sorted(k for k, v in keys.items() if v["status"] == "watch"),
        "keys": keys,
    }


def diff_alerts(doc: dict) -> tuple[list[str], list[str]]:
    """-> (new, known): WATCH keys the owner has not been told about yet, and WATCH keys already notified. Pure: reads only `doc`."""
    new, known = [], []
    for k in doc.get("watch") or []:
        (known if (doc["keys"].get(k) or {}).get("notified") else new).append(k)
    return new, known


def describe(key: str, e: dict) -> str:
    w = e.get("window") or {}
    return (f"{key}: stated {e['mean_p']:.1%}, won {e['win_rate']:.1%} over n={e['n']} (gap {e['gap_pp']:+.1f}pp, z={e['z']:+.1f}; last {w.get('n')}: {w.get('win_rate', 0):.1%} vs {w.get('mean_p', 0):.1%}), "
            f"in WATCH since {e.get('since')}")


def health_items(doc: dict) -> tuple[list[str], list[str]]:
    """-> (problems, notes) for daily_health_check: one alert-level line per NEW WATCH key, one note per already-known WATCH key. Alert-only wording on purpose."""
    new, known = diff_alerts(doc)
    problems = [f"CALIBRATION WATCH (new): {describe(k, doc['keys'][k])} -- alert only, nothing is blocked" for k in new]
    notes = [f"calibration watch (still watching): {describe(k, doc['keys'][k])}" for k in known]
    return problems, notes


def weekly_lines(doc: dict | None) -> list[str]:
    """Short 'still watching' lines for the weekly digest (empty when nothing is in WATCH or the file is missing)."""
    if not doc or not doc.get("watch"):
        return []
    return [f"{k}: stated {doc['keys'][k]['mean_p']:.0%}, won {doc['keys'][k]['win_rate']:.0%}, n={doc['keys'][k]['n']}, since {doc['keys'][k].get('since')}" for k in doc["watch"]]


# ── file / ledger plumbing ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def load_document(path: Path = WATCH_PATH) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def compute(root: Path = ROOT, previous: dict | None = None, now: datetime | None = None) -> dict:
    picks = json.loads((root / "docs" / "picks_backup.json").read_text())
    idx = lt.load_index(root)
    return build_document(eligible_picks(picks, idx), previous, now)


def write_document(doc: dict, path: Path = WATCH_PATH) -> None:
    Path(path).write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n")


def update_watch(root: Path = ROOT, now: datetime | None = None, write: bool = True) -> tuple[dict, list[str], list[str], list[str]]:
    """Recompute, optionally write docs/calibration_watch.json, and return (doc, problems, notes, new_keys). Never raises into the caller's alert path (daily_health_check wraps it fail-open)."""
    path = root / "docs" / "calibration_watch.json"
    doc = compute(root, load_document(path), now)
    if write:
        write_document(doc, path)
    problems, notes = health_items(doc)
    new, _known = diff_alerts(doc)
    return doc, problems, notes, new


def mark_notified(keys: list[str], today: str, path: Path = WATCH_PATH) -> None:
    """After the owner email went out: stamp the keys so tomorrow's pass does not repeat them."""
    doc = load_document(path)
    if not doc:
        return
    for k in keys:
        if k in doc.get("keys", {}):
            doc["keys"][k]["notified"] = today
    write_document(doc, path)


def _print_table(doc: dict) -> None:
    print(f"generated {doc['generated_at']}  boundary ln(1/{ALPHA}) = {BOUNDARY:.3f}, min n {MIN_N}, delta {DELTA}")
    print(f"{'key':18s} {'status':6s} {'n':>4s} {'mean p':>7s} {'win':>6s} {'gap pp':>7s} {'z':>6s} {'LLR':>6s}  {'last50 win/p':>14s}  since")
    for k, e in doc["keys"].items():
        w = e["window"]
        print(f"{k:18s} {e['status']:6s} {e['n']:4d} {e['mean_p']:7.3f} {e['win_rate']:6.3f} {e['gap_pp']:+7.1f} {e['z']:+6.2f} {e['llr']:6.2f}  {w['win_rate']:.3f}/{w['mean_p']:.3f} n={w['n']:<3d} {e['since'] or ''}")
    print("WATCH:", ", ".join(doc["watch"]) or "none")


if __name__ == "__main__":
    write = "--write" in sys.argv
    d = compute(ROOT, load_document(WATCH_PATH))
    if write:
        write_document(d)
    _print_table(d)
