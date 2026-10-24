"""Per-pick subscriber-email tracking + the catch-up rules (2026-10-10 follow-up audit, owner decision: NO MISSING PICK EMAILS).

Pure logic only -- no git, no network, no mail, no browser. scripts/auto_lock_settle.py owns every side effect (reading/writing
data/emailed_picks.jsonl through git, sending the emails, calling the ledger in the headless page) and passes plain dicts in here, so
everything below is unit-testable with fakes (scripts/test_email_delta_20261010.py).

THE STATE FILE  data/emailed_picks.jsonl  (public repo: pick ids only, never an address)  -- APPEND-ONLY, one JSON record per line:
      {"k":"cutoff","ms":1791700000000,"iso":"2026-10-11T03:06:40Z"}                       # stamped by the FIRST live run after the deploy (see SEED)
      {"k":"email","id":"<ledger pick id>","p":"hockey","d":"2026-10-11","t":"<iso>","v":"MAIN HOCKEY"}   # this pick went out in a subscriber email
      {"k":"zero","p":"hockey","d":"2026-10-11","t":"<iso>","v":"MAIN HOCKEY"}              # the one 'no picks' email for that product + slate date
  Append-only on purpose: .gitattributes marks the file `merge=union`, so two workflows that record sends at the same moment merge
  by keeping BOTH sides' lines (a rewritten JSON object conflicted in a real two-clone test and silently lost the second record).
  A record is appended by the code path that sends (send_locks_email), only after the send reached recipients; a send that reached
  nobody records nothing (so the next pass retries it), a partial send records the ids (a retry would double-deliver).

SEED / MIGRATION  Picks locked before the deploy were handled by the old code (emailed, or deliberately not). The first LIVE
process after the deploy finds no cutoff and records cutoffMs = its own start time; from then on a pick whose lockedAt is BEFORE
cutoffMs is treated as already emailed, forever. Nothing is guessed from ledger contents. If two first runs race and both append a
cutoff record, the EARLIEST wins. To move the cutoff by hand, edit the file (delete the cutoff line and append a new one).

CATCH-UP  (catchup_candidates) -- which ledger picks a pass must still email, by these rules (all must hold):
  1. the pick belongs to a sold product (nfl / cfb / nba / hockey = NHL+SHL+LIIGA+NLA+EXTRALIGA / soccer = PL+LIGA+SERIEA+CL). Every
     other league (MLS, Bundesliga, MLB, WNBA, NCAAH, KHL, tennis, ...) maps to no product and is never emailed. Props, parlays and
     removed picks are never caught up.
  2. outcome is still 'pending' and no final score is stored: a settled / final game is never emailed.
  3. lockedAt >= cutoffMs and the id is not in "emailed".
  4. the pick's game date (MT) is yesterday, today or tomorrow.
  5. it is NOT a known-late lock: lock_timing.classify() says 'during' / 'after' (locked at or after the scheduled start), or the
     pick carries lockTiming 'late-manual'. Those picks are excluded from every published figure, so they are never sold as picks.
  6. timing window: the game has not started, or began at most CATCHUP_STARTED_MAX_H hours ago (and not longer than the sport's game
     length). A pick locked BEFORE kickoff is a real, record-counting pick, so it is still emailed shortly after kickoff, with a note.
     A start time that cannot be determined is accepted only for a game dated after today (it cannot have begun).
  7. CATCHUP_MANUAL_ONLY_QUALIFIED (default False): when True an in-app manual lock is emailed only if its stored grade is a value
     grade (PREMIUM / OPTIMAL / HIGH PROB / ADJUSTED LINE) or a 75%+ moneyline.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import lock_timing

MT = ZoneInfo("America/Denver")

STATE_REL = "data/emailed_picks.jsonl"
EMAILED_KEEP_DAYS = 14                 # records older than this are ignored on load and dropped when the file is compacted (the timing window makes a pick that old ineligible anyway)
COMPACT_AT_LINES = 3000                # init rewrites the file without expired records once it grows past this

# ── tuning knobs ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
CATCHUP_ENABLED = True                 # kill switch: False = the end-of-run catch-up sends nothing (the passes' own emails and the per-pick tracking are unaffected)
CATCHUP_STARTED_MAX_H = 2.0            # a pre-start lock whose game began at most this long ago is still emailed (with a note)
CATCHUP_MAX_PICKS_PER_PRODUCT = 30     # sanity brake: more than this for ONE product in one pass means something is wrong -> no send, red run
CATCHUP_MANUAL_ONLY_QUALIFIED = False  # owner decision pending: True = in-app manual locks are emailed only when graded as a value pick
CATCHUP_DATE_BACK_DAYS = 1             # pick.date may be yesterday (a game that began just before midnight MT) ...
CATCHUP_DATE_FWD_DAYS = 1              # ... through tomorrow

# ── product map ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# Keyed by lock_timing.norm_sport(pick) -- the ledger's own league codes. Must equal auto_lock_settle.PRODUCT_SPORTS (a test pins that).
PRODUCT_OF_CODE = {
    "NFL": "nfl", "CFB": "cfb", "NBA": "nba",
    "NHL": "hockey", "SHL": "hockey", "LIIGA": "hockey", "NLA": "hockey", "EXTRALIGA": "hockey",
    "PL": "soccer", "LIGA": "soccer", "SERIEA": "soccer", "CL": "soccer",
}
# ledger league code -> the sport tag the lock emails use (SPORT_DISPLAY_NAME keys)
TAG_OF_CODE = {"PL": "SOC_PL", "LIGA": "SOC_LIGA", "SERIEA": "SOC_ITA", "CL": "SOC_CL"}


def product_of_pick(p: dict) -> str | None:
    """The paid product a ledger pick belongs to, or None (retired league, personal-only league, props, parlays, no sport tag)."""
    if not isinstance(p, dict) or lock_timing.is_parlay(p):
        return None
    if str(p.get("betType") or "").upper() == "PROP" or p.get("player"):
        return None
    if (p.get("altLine") or {}).get("bestOfGame"):          # the owner's own "best adjusted line of every game" locks (auto_lock_settle.LOCK_BEST_ADJUSTED): never part of a paid product
        return None
    return PRODUCT_OF_CODE.get(lock_timing.norm_sport(p))


# ── state ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def empty_state() -> dict:
    return {"cutoffMs": None, "cutoffIso": None, "emailed": {}, "zero": {}}


def normalize(d) -> dict:
    """Any parsed JSON (or None) -> a well-formed state dict. Never raises: a damaged file reads as an empty one."""
    s = empty_state()
    if not isinstance(d, dict):
        return s
    c = d.get("cutoffMs")
    if isinstance(c, (int, float)) and not isinstance(c, bool) and c > 946684800000:
        s["cutoffMs"] = int(c) if c == int(c) else float(c)
        s["cutoffIso"] = d.get("cutoffIso") if isinstance(d.get("cutoffIso"), str) else iso_of_ms(c)
    if isinstance(d.get("emailed"), dict):
        s["emailed"] = {str(k): dict(v) if isinstance(v, dict) else {} for k, v in d["emailed"].items()}
    if isinstance(d.get("zero"), dict):
        s["zero"] = {str(k): dict(v) if isinstance(v, dict) else {} for k, v in d["zero"].items()}
    return s


def iso_of_ms(ms) -> str:
    return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def merge(a: dict, b: dict) -> dict:
    """Union of two states: never loses an 'emailed' / 'zero' entry; the cutoff already recorded in `a` wins (it is set once and never moves)."""
    out = normalize(a)
    other = normalize(b)
    if out["cutoffMs"] is None and other["cutoffMs"] is not None:
        out["cutoffMs"], out["cutoffIso"] = other["cutoffMs"], other["cutoffIso"]
    for k, v in other["emailed"].items():
        out["emailed"].setdefault(k, v)
    for k, v in other["zero"].items():
        out["zero"].setdefault(k, v)
    return out


def prune(state: dict, now_ms: float) -> dict:
    cut = iso_of_ms(now_ms - EMAILED_KEEP_DAYS * 86400000)
    state["emailed"] = {k: v for k, v in state["emailed"].items() if str(v.get("t") or "9999") >= cut}
    state["zero"] = {k: v for k, v in state["zero"].items() if str(v.get("t") or "9999") >= cut}
    return state


def parse_lines(text, now_ms: float | None = None) -> dict:
    """The state file's text -> a state dict. A damaged line is skipped (never fatal); several cutoff records (two first runs racing) -> the EARLIEST wins; records
    older than EMAILED_KEEP_DAYS are dropped when now_ms is given."""
    s = empty_state()
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        k = r.get("k")
        if k == "cutoff":
            ms = _num(r.get("ms"))
            if ms is not None and ms > 946684800000 and (s["cutoffMs"] is None or ms < s["cutoffMs"]):
                s["cutoffMs"], s["cutoffIso"] = int(ms) if ms == int(ms) else ms, (r.get("iso") if isinstance(r.get("iso"), str) else iso_of_ms(ms))
        elif k == "email" and r.get("id"):
            s["emailed"].setdefault(str(r["id"]), {"p": r.get("p"), "d": r.get("d"), "t": r.get("t"), "v": r.get("v")})
        elif k == "zero" and r.get("p") and r.get("d"):
            s["zero"].setdefault(zero_key(str(r["p"]), str(r["d"])), {"t": r.get("t"), "v": r.get("v")})
    return prune(s, now_ms) if now_ms is not None else s


def snapshot(state: dict) -> tuple:
    return (state.get("cutoffMs"), frozenset(state["emailed"]), frozenset(state["zero"]))


def records_since(snap: tuple, state: dict) -> list[dict]:
    """The records `state` gained since `snapshot()` -- exactly what must be appended to the file."""
    cutoff, emailed, zero = snap
    out: list[dict] = []
    if cutoff is None and state.get("cutoffMs") is not None:
        out.append({"k": "cutoff", "ms": state["cutoffMs"], "iso": state.get("cutoffIso") or iso_of_ms(state["cutoffMs"])})
    for pid, v in state["emailed"].items():
        if pid not in emailed:
            out.append({"k": "email", "id": pid, "p": v.get("p"), "d": v.get("d"), "t": v.get("t"), "v": v.get("v")})
    for key, v in state["zero"].items():
        if key not in zero:
            product, _, date = key.partition("|")
            out.append({"k": "zero", "p": product, "d": date, "t": v.get("t"), "v": v.get("v")})
    return out


def render(records) -> str:
    return "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records)


def compact_text(state: dict, now_ms: float) -> str:
    """The whole state as file text, without expired records (used when the file has grown past COMPACT_AT_LINES)."""
    return render(records_since((None, frozenset(), frozenset()), prune(normalize(state), now_ms)))


def is_emailed(state: dict, pick_id) -> bool:
    return bool(pick_id) and pick_id in state["emailed"]


def needs_email(state: dict, pick_id, locked_ms) -> bool:
    """True when a pick is still owed to subscribers: not recorded as emailed AND locked at/after the recorded cutoff (anything locked
    before the deploy was handled by the old code). No cutoff recorded yet -> nothing is owed (the caller seeds it first)."""
    cutoff = state.get("cutoffMs")
    lm = _num(locked_ms)
    if not pick_id or cutoff is None or pick_id in state["emailed"]:
        return False
    return lm is None or lm >= cutoff        # an unknown lock time is treated as owed: the failure direction must be a duplicate, never a missing email


def mark_emailed(state: dict, pick_ids, product: str, via: str, now_ms: float, dates: dict | None = None) -> int:
    """Records ids as emailed. `dates` maps id -> the pick's game date (used by the zero-pick rule). Returns how many were new."""
    n = 0
    ts = iso_of_ms(now_ms)
    for pid in pick_ids:
        if not pid or pid in state["emailed"]:
            continue
        state["emailed"][pid] = {"p": product, "d": (dates or {}).get(pid), "t": ts, "v": via}
        n += 1
    return n


def zero_key(product: str, date: str) -> str:
    return f"{product}|{date}"


def mark_zero(state: dict, product: str, dates, via: str, now_ms: float) -> None:
    ts = iso_of_ms(now_ms)
    for d in dates:
        state["zero"].setdefault(zero_key(product, d), {"t": ts, "v": via})


def emailed_pick_dates(state: dict, product: str) -> set[str]:
    """Game dates for which a PICKS email of this product has already gone out."""
    return {v.get("d") for v in state["emailed"].values() if v.get("p") == product and v.get("d")}


def zero_dates_sent(state: dict, product: str) -> set[str]:
    pre = product + "|"
    return {k[len(pre):] for k in state["zero"] if k.startswith(pre)}


# ── ledger pick -> the leg dict the lock-email template renders ───────────────────────────────────────────────────────────────
_GRADE_RE = re.compile(r"^PICK:\s.*?\s—\s([A-Z][A-Z ]*?)\s\(", re.S)
_EV_RE = re.compile(r"win prob,\s*EV\s*([+-]?\d+(?:\.\d+)?)%")
_TIER_N = {"PREMIUM": 3, "OPTIMAL": 2, "LEAN": 1, "SKIP": 0}
VALUE_GRADES = ("PREMIUM", "OPTIMAL", "HIGH PROB", "ADJUSTED LINE")


def stored_grade(p: dict) -> str | None:
    """The grade the engine printed on the pick at lock time ('PREMIUM', 'HIGH PROB', 'MANUAL LINE', ...), parsed from its stored reasoning."""
    m = _GRADE_RE.match(str(p.get("reasoning") or ""))
    return m.group(1).strip() if m else None


def _num(v):
    try:
        t = float(v)
    except (TypeError, ValueError):
        return None
    return t if t == t else None


_WINPROB_TEXT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*win\s*prob|win\s*prob\w*\W{0,12}(\d{1,3}(?:\.\d+)?)\s*%", re.I)


def win_prob(p: dict) -> float | None:
    """The pick's win probability as 0-1, from the first field that has one (winProb is what lockPick stores; older / hand-made locks may only carry prob, modelProb, conf or a 'win prob NN%' line in
    the reasoning). None when nothing says -- the email then prints 'win prob n/a' instead of a made-up 0% (owner 2026-10-10: every pick in an email, SKIP included, shows its win probability)."""
    for k in ("winProb", "prob", "modelProb"):
        v = _num(p.get(k))
        if v is not None and 0 < v <= 1:
            return v
        if v is not None and 1 < v <= 100:
            return v / 100.0
    c = _num(p.get("conf"))
    if c is not None and 0 < c <= 100:
        return c / 100.0
    m = _WINPROB_TEXT_RE.search(str(p.get("reasoning") or ""))
    if m:
        v = float(m.group(1) or m.group(2))
        if 0 < v <= 100:
            return v / 100.0
    return None


def pick_to_leg(p: dict, now_ms: float | None = None, idx: dict | None = None) -> dict:
    """A ledger pick as the 'qualifying leg' dict build_locks_email_html / _leg_html render -- the SAME template as the lock emails."""
    code = lock_timing.norm_sport(p)
    bt = str(p.get("betType") or "ML").upper()
    bet_on = str(p.get("betOn") or "")
    if bt == "OU":
        side = "under" if bet_on.upper().startswith("UNDER") else "over"
    elif bt in ("SPREAD", "PL", "RL"):
        side = "sprdFav"
    else:
        side = "mlFav"
    grade = stored_grade(p)
    ev = None
    m = _EV_RE.search(str(p.get("reasoning") or ""))
    if m and grade not in ("ADJUSTED LINE", "MANUAL LINE"):
        ev = float(m.group(1)) / 100.0
    alt = p.get("altLine") if isinstance(p.get("altLine"), dict) else None
    leg = {
        "kind": "GAME", "sport": TAG_OF_CODE.get(code, code), "hA": p.get("hA"), "awA": p.get("awA"), "side": side, "label": bet_on,
        "prob": (win_prob(p) or 0.0), "probKnown": win_prob(p) is not None, "ml": p.get("ml"), "dec": p.get("decOdds"), "tierN": _TIER_N.get(grade, 0), "evVal": ev,
        "lane": grade == "HIGH PROB", "mcSummary": None, "best": None, "altLine": alt, "startMs": p.get("startMs"),
        "lockDate": p.get("date"), "pickId": p.get("id"), "fromLedger": True,
    }
    if grade not in _TIER_N and grade != "HIGH PROB" and not (grade == "ADJUSTED LINE" and alt):
        # no value grade on record (a manual lock without reasoning, or a hand-edited line): say what it is instead of printing a made-up tier
        leg["badge"] = ("MANUAL LINE" if grade == "MANUAL LINE" else "ADJUSTED LINE" if grade == "ADJUSTED LINE" else "LOCKED PICK",
                        "#ff9f1c" if grade == "MANUAL LINE" else "#9aa0a6")
    st = lock_timing.pick_start_ms(p, idx)
    if st is not None and now_ms is not None and st <= now_ms:
        leg["note"] = ("Locked before kickoff" + (f" ({datetime.fromtimestamp(float(p['lockedAt']) / 1000.0, MT).strftime('%a %I:%M %p MT')})"
                                                  if _num(p.get("lockedAt")) else "")
                       + "; the game was already under way when this email went out.")
    return leg


# ── catch-up eligibility ───────────────────────────────────────────────────────────────────────────────────────────────────────
def window_dates(now_ms: float) -> set[str]:
    t = datetime.fromtimestamp(now_ms / 1000.0, MT)
    return {(t + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(-CATCHUP_DATE_BACK_DAYS, CATCHUP_DATE_FWD_DAYS + 1)}


def _qualifies_as_value(p: dict) -> bool:
    g = stored_grade(p)
    if g in VALUE_GRADES:
        return True
    return str(p.get("betType") or "").upper() == "ML" and (win_prob(p) or 0) >= 0.75


def catchup_candidates(picks, state: dict, now_ms: float, idx: dict | None = None):
    """-> (eligible, skipped). eligible = [(product, pick)] to email now; skipped = [(pick, reason)] worth a log line (known-late locks,
    unknown start times, over-old games). Everything else (not a product pick, settled, before the cutoff, already emailed, outside the
    date window) is dropped silently. Needs a recorded cutoff: with none, nothing is eligible (the caller seeds it first)."""
    eligible: list[tuple[str, dict]] = []
    skipped: list[tuple[dict, str]] = []
    cutoff = state.get("cutoffMs")
    if cutoff is None:
        return eligible, skipped
    dates = window_dates(now_ms)
    today = datetime.fromtimestamp(now_ms / 1000.0, MT).strftime("%Y-%m-%d")
    for p in picks or []:
        pid = p.get("id") if isinstance(p, dict) else None
        if not pid:
            continue
        product = product_of_pick(p)
        if product is None:
            continue
        if p.get("outcome") != "pending" or p.get("hScore") is not None or p.get("aScore") is not None:
            continue                                                   # settled / final: never emailed
        locked = _num(p.get("lockedAt"))
        if not locked or locked < cutoff or is_emailed(state, pid):
            continue
        if p.get("date") not in dates:
            continue
        cls = lock_timing.classify(p, idx)
        if cls in lock_timing.LATE_CLASSES or p.get("lockTiming") == "late-manual":
            skipped.append((p, "known-late lock (locked at/after the scheduled start): excluded from published figures, never emailed"))
            continue
        st = lock_timing.pick_start_ms(p, idx)
        if st is None:
            if str(p.get("date")) <= today:
                skipped.append((p, "start time unknown for a game dated today or earlier: cannot prove it was locked pre-start"))
                continue
        else:
            began_h = (now_ms - st) / 3600000.0
            dur_h = lock_timing.DUR_MIN.get(lock_timing.norm_sport(p), lock_timing.DEFAULT_DUR_MIN) / 60.0
            if began_h > min(CATCHUP_STARTED_MAX_H, dur_h):
                skipped.append((p, f"game began {began_h:.1f} h ago: too late to email (nothing is sent for a game that is over or nearly over)"))
                continue
        if (CATCHUP_MANUAL_ONLY_QUALIFIED and p.get("lockOrigin") != "auto" and not _qualifies_as_value(p)):
            skipped.append((p, "manual lock without a value grade (CATCHUP_MANUAL_ONLY_QUALIFIED)"))
            continue
        eligible.append((product, p))
    return eligible, skipped


def group_by_product(eligible) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for product, p in eligible:
        out.setdefault(product, []).append(p)
    for v in out.values():
        v.sort(key=lambda p: (_num(p.get("startMs")) or 9e15, str(p.get("id"))))
    return out
