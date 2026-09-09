"""
_subscribers.py — shared subscriber-list loader for segmented per-sport
locks emails, with 30-day access windows.

MVP: manually-edited data/subscribers.json, no payment automation yet.
Venmo is the payment method for now -- when someone pays, add them with
manage_subscribers.py (or call add_subscriber() directly) and commit the
resulting file. No webhook, no database; this is intentionally the
simplest thing that works for a manually-tracked MVP.

Schema: {"nfl": [{"email": "a@x.com", "added": "2026-08-23T00:00:00+00:00"},
...], ...} -- one list of timestamped entries per paid product. "added" is
the access-window start; recipients_for() only includes entries less than
EXPIRY_DAYS old, so a subscriber silently drops off the list 30 days after
they were added (or last renewed) without anyone having to remember to
remove them by hand. Re-adding an email that's already on a product's list
renews it -- resets "added" to now rather than creating a duplicate entry.

The account owner's own address is always included for every product
regardless of subscriber list state, so the personal daily reference
this was originally built for keeps working with zero paying subscribers.

5 paid products (confirmed structure, 2026-09-08): nfl, cfb, nba,
hockey (NHL only), soccer (all 6 leagues bundled -- the 5 European
leagues + MLS -- as one purchase, not sold separately). Explicit
decision 2026-09-03: tennis (ATP/WTA), WNBA, KHL, SHL, and LIIGA are
real, live engine features kept for personal use only -- never sold to
subscribers, so "tennis" and "wnba" were removed from PRODUCTS entirely
and KHL/SHL/LIIGA were dropped from the hockey product's sport set (see
PRODUCT_SPORTS in auto_lock_settle.py). Their qualifying legs route to
the owner-only "other" locks email instead of a subscriber email.
Explicit decision 2026-09-08: MLB, CBB, and World Cup were removed from
the engine entirely (accuracy/scope reasons), dropping "mlb" from
PRODUCTS too -- 6 products became 5.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _gmail_email import send_email as _send_gmail  # noqa: E402

SUBSCRIBERS_FILE = Path(__file__).resolve().parent.parent / "data" / "subscribers.json"
EVENTS_FILE = Path(__file__).resolve().parent.parent / "data" / "subscriber_events.json"
PRODUCTS = ("nfl", "cfb", "nba", "hockey", "soccer")
OWNER_EMAIL = os.environ.get("LOCKS_EMAIL_TO", "") or os.environ.get("SOCIAL_CARD_EMAIL_TO", "")
EXPIRY_DAYS = 30

# 30-day access price by number of simultaneous products, per the pricing
# table worked out for this MVP. Used only to ESTIMATE current recurring
# revenue from active subscription counts -- this is not a real payment
# ledger (Venmo payments aren't tracked here at all), just what someone
# following the standard pricing would owe for their current product count.
# Capped at 5 (len(PRODUCTS)) -- there is no 6th+ product to bundle anymore.
# Repriced 2026-09-09: flat $15/sport marginal cost from the 2nd product
# onward (was a $10/sport marginal, decreasing avg/sport as more were
# added) -- explicit decision not to engineer a bulk-bundle discount,
# each additional sport is charged for the real added value (more picks,
# more opportunity), not discounted to push full-package signups.
PRICING_BY_COUNT = {1: 20, 2: 30, 3: 45, 4: 60, 5: 75}

# Hosted (not inline-attached) so it actually renders across mail clients --
# many strip inline/CID images or require an extra click, while a plain
# HTTPS <img src> is the standard transactional-email approach. Same
# GitHub Pages origin every other live asset (app.html, sport_
# performance.json) is already served from. docs/email_banner.jpg is a
# resized+recompressed copy of Desktop/bannerlogo2.png (1280px wide @
# JPEG q78 -- the source PNG was 843KB uncompressed at that width, too
# heavy for an email header; JPEG compresses its soft glow/gradient
# background far better than PNG does).
EMAIL_BANNER_URL = "https://purple-wraith.github.io/clairvoyance-backend/email_banner.jpg"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _load_events() -> list[dict]:
    try:
        return json.loads(EVENTS_FILE.read_text())
    except Exception:
        return []


def _log_event(action: str, product: str, email: str) -> None:
    """Append-only history of add/renew/remove actions, the only source
    of real trend/churn data -- current-state files alone (subscribers.json)
    can't answer "how many joined this week" or "who lapsed", since a
    removal or expiry just deletes/filters an entry with no trace. Analytics
    that need history only see it from the point this logging started
    (event_history_since in analytics_summary()), not retroactively."""
    events = _load_events()
    events.append({"ts": _now().isoformat(), "action": action, "product": product, "email": email})
    EVENTS_FILE.write_text(json.dumps(events, indent=2) + "\n")


def load_subscribers() -> dict[str, list[dict]]:
    """Raw file contents -- every product's full entry list, expired or
    not. Callers that care about active-only access should use
    recipients_for()/active_subscribers() instead."""
    try:
        return json.loads(SUBSCRIBERS_FILE.read_text())
    except Exception:
        return {}


def save_subscribers(data: dict[str, list[dict]]) -> None:
    SUBSCRIBERS_FILE.write_text(json.dumps(data, indent=2) + "\n")


REPO_ROOT = SUBSCRIBERS_FILE.parent.parent


def sync_subscribers_to_git(message: str) -> tuple[bool, str]:
    """Commits + pushes data/subscribers.json (and data/subscriber_events.json,
    the add/remove/renew history add_subscriber()/remove_subscriber() also
    write locally) so an add/remove made locally (CLI or the web admin
    tool) actually reaches the GitHub Actions runs without a separate
    manual step -- this used to be a "don't forget to commit and push"
    step every caller had to remember by hand. The events file was left
    out of the first version of this function, which meant it silently
    accumulated uncommitted local history forever even after
    subscribers.json itself started auto-syncing -- caught during a
    session audit. Mirrors the same pull --rebase-then-push pattern
    send-expiry-reminders.yml already uses safely against subscribers.json
    (it flips reminder_sent daily), since that file is the only other
    writer either of these can collide with. GIT_TERMINAL_PROMPT=0 so a
    credential/auth problem fails fast instead of hanging this script
    waiting for input nobody will provide."""
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

    rels = ["data/subscribers.json", "data/subscriber_events.json"]
    run("add", *rels)
    diff = run("diff", "--cached", "--quiet", "--", *rels)
    if diff.returncode == 0:
        return True, "no changes to sync"

    commit = run("commit", "-m", message)
    if commit.returncode != 0:
        return False, f"commit failed: {commit.stderr.strip() or commit.stdout.strip()}"

    # --autostash: this repo almost always has *something* else dirty
    # (in-progress edits, or automation's own docs/version.json churn) --
    # without it, pull --rebase refuses to run at all over unrelated
    # uncommitted files, even though our commit only touched
    # subscribers.json. --autostash sets those aside and restores them
    # after, whether the rebase succeeds or gets aborted below.
    pull = run("pull", "--rebase", "--autostash", "origin", "main")
    if pull.returncode != 0:
        run("rebase", "--abort")
        return False, (
            "committed locally but couldn't sync with GitHub (pull --rebase failed) "
            f"-- resolve manually: {pull.stderr.strip()[:200]}"
        )

    push = run("push", "origin", "main")
    if push.returncode != 0:
        return False, (
            f"committed locally but push failed -- run `git -C {REPO_ROOT} push` "
            f"manually: {push.stderr.strip()[:200]}"
        )
    return True, "synced to GitHub"


def _is_active(entry: dict, now: datetime) -> bool:
    added = _parse(entry.get("added", ""))
    return added is not None and now - added < timedelta(days=EXPIRY_DAYS)


def active_subscribers(product: str) -> list[dict]:
    """Non-expired entries for this product, each with a computed
    days_left so callers (the CLI, mainly) don't have to redo the date
    math themselves."""
    now = _now()
    out = []
    for entry in load_subscribers().get(product, []):
        added = _parse(entry.get("added", ""))
        if added is None or now - added >= timedelta(days=EXPIRY_DAYS):
            continue
        days_left = EXPIRY_DAYS - (now - added).days
        out.append({
            "email": entry["email"], "added": entry["added"], "days_left": days_left,
            "reminder_sent": bool(entry.get("reminder_sent")),
        })
    return out


def recipients_for(product: str) -> list[str]:
    """Owner + every subscriber whose 30-day window hasn't expired,
    deduped, order preserved (owner first)."""
    subs = [e["email"] for e in active_subscribers(product)]
    seen: list[str] = []
    for email in [OWNER_EMAIL, *subs]:
        if email and email not in seen:
            seen.append(email)
    return seen


def add_subscriber(product: str, email: str) -> str:
    """Adds email to product with a fresh 30-day window, or renews it
    (resets "added" to now) if already present -- either way returns a
    short human-readable status string for CLI/caller use."""
    if product not in PRODUCTS:
        raise ValueError(f"Unknown product {product!r} -- must be one of {PRODUCTS}")
    email = email.strip().lower()
    data = load_subscribers()
    entries = data.setdefault(product, [])
    now_iso = _now().isoformat()
    for entry in entries:
        if entry.get("email", "").strip().lower() == email:
            entry["added"] = now_iso
            # Fresh 30-day window means a fresh reminder cycle -- otherwise
            # a renewal made mid-way through the old reminder period would
            # leave reminder_sent=True carried over and they'd never get
            # warned before the NEW window's own expiry.
            entry["reminder_sent"] = False
            save_subscribers(data)
            _log_event("renew", product, email)
            return f"renewed {email} on {product} -- 30-day window reset from today"
    entries.append({"email": email, "added": now_iso, "reminder_sent": False})
    save_subscribers(data)
    _log_event("add", product, email)
    return f"added {email} to {product} -- active for {EXPIRY_DAYS} days"


def remove_subscriber(product: str, email: str) -> str:
    if product not in PRODUCTS:
        raise ValueError(f"Unknown product {product!r} -- must be one of {PRODUCTS}")
    email = email.strip().lower()
    data = load_subscribers()
    entries = data.get(product, [])
    before = len(entries)
    data[product] = [e for e in entries if e.get("email", "").strip().lower() != email]
    if len(data[product]) == before:
        return f"{email} was not on {product}'s list -- nothing removed"
    save_subscribers(data)
    _log_event("remove", product, email)
    return f"removed {email} from {product}"


def subscribers_needing_reminder(days_before: int = 3) -> list[dict]:
    """Active (product, email) pairs at or under `days_before` days left
    that haven't been sent an expiry reminder for their CURRENT window
    yet. reminder_sent resets on every add/renew (see add_subscriber()),
    so this only ever fires once per 30-day window -- checking <= rather
    than == means a subscriber still gets warned even if the reminder
    workflow misses a day and only catches them at, say, 2 days left
    instead of exactly 3."""
    out = []
    for product in PRODUCTS:
        for entry in active_subscribers(product):
            if entry["days_left"] <= days_before and not entry["reminder_sent"]:
                out.append({"product": product, "email": entry["email"], "days_left": entry["days_left"]})
    return out


def mark_reminder_sent(product: str, email: str) -> None:
    email = email.strip().lower()
    data = load_subscribers()
    for entry in data.get(product, []):
        if entry.get("email", "").strip().lower() == email:
            entry["reminder_sent"] = True
    save_subscribers(data)


def list_subscribers(product: str | None = None) -> dict[str, list[dict]]:
    """Active subscribers (with days_left) for one product, or every
    product if none given."""
    products = [product] if product else list(PRODUCTS)
    return {p: active_subscribers(p) for p in products}


def _reconstruct_periods() -> tuple[dict[tuple[str, str], list[dict]], list[float]]:
    """Replays the event log per (email, product) pair into real access
    periods -- the only way to answer any historical question (revenue
    over time, retention, lifetime, renewal timing), since subscribers.json
    only ever holds current state with no trace of what came before it.

    A "renew" event only means add_subscriber() found this email already
    sitting in the file -- it does NOT mean they were still within their
    active window (nothing prunes a lapsed-but-never-manually-removed
    entry). So periods are reconstructed from real elapsed time between
    event timestamps, not trusted from the raw action label alone: a
    "renew" that lands after the running period's computed end is treated
    as a fresh period (a win-back), not an extension of the old one.

    Returns (periods_by_key, renewal_gap_days) where periods_by_key maps
    (email, product) -> ordered list of {start, end, cycles, removed}
    dicts, and renewal_gap_days is one signed value per real "renew"
    event: negative = renewed that many days before it would have expired
    (a true on-time renewal), positive = renewed that many days after it
    had already lapsed (a win-back, not a renewal)."""
    events = _load_events()
    by_key: dict[tuple[str, str], list[dict]] = {}
    for e in events:
        by_key.setdefault((e["email"], e["product"]), []).append(e)

    periods_by_key: dict[tuple[str, str], list[dict]] = {}
    renewal_gaps: list[float] = []
    for key, evs in by_key.items():
        periods: list[dict] = []
        current: dict | None = None
        for e in evs:
            ts = _parse(e.get("ts", ""))
            if ts is None:
                continue
            action = e.get("action")
            if action in ("add", "renew"):
                if current is not None:
                    gap_days = (ts - current["end"]).total_seconds() / 86400
                    if action == "renew":
                        renewal_gaps.append(gap_days)
                    if ts <= current["end"]:
                        current["end"] = ts + timedelta(days=EXPIRY_DAYS)
                        current["cycles"] += 1
                        continue
                    periods.append(current)
                current = {"start": ts, "end": ts + timedelta(days=EXPIRY_DAYS), "cycles": 1, "removed": False}
            elif action == "remove":
                if current is not None:
                    if ts < current["end"]:
                        current["end"] = ts
                    current["removed"] = True
                    periods.append(current)
                    current = None
        if current is not None:
            periods.append(current)
        periods_by_key[key] = periods
    return periods_by_key, renewal_gaps


def revenue_timeline(weeks: int = 8) -> list[dict]:
    """Estimated revenue at each weekly checkpoint going back up to
    `weeks` weeks, reconstructed from the event log -- shows whether
    revenue is actually growing instead of just what it is right now.
    Only covers time since event logging started; empty until then."""
    periods_by_key, _ = _reconstruct_periods()
    if not periods_by_key:
        return []
    now = _now()
    earliest = min((p["start"] for periods in periods_by_key.values() for p in periods), default=now)

    checkpoints = []
    t = now
    for _ in range(weeks):
        if t < earliest:
            break
        checkpoints.append(t)
        t -= timedelta(days=7)
    checkpoints.reverse()

    out = []
    for cp in checkpoints:
        counts_by_email: dict[str, int] = {}
        for (email, _product), periods in periods_by_key.items():
            if any(p["start"] <= cp < p["end"] for p in periods):
                counts_by_email[email] = counts_by_email.get(email, 0) + 1
        revenue = sum(PRICING_BY_COUNT.get(min(n, len(PRODUCTS)), 0) for n in counts_by_email.values())
        out.append({"week_of": cp.date().isoformat(), "active_emails": len(counts_by_email), "estimated_revenue": revenue})
    return out


def per_product_revenue() -> dict[str, float]:
    """Current active revenue attributed to each product. When an email
    is on multiple products at once, their single bundle price is split
    evenly across those products -- there's no real separate per-product
    price once bundled, so this is an allocation for comparing products
    against each other, not a second ledger."""
    active_counts_by_email: dict[str, int] = {}
    active_by_product: dict[str, list[str]] = {p: [] for p in PRODUCTS}
    for product in PRODUCTS:
        for entry in active_subscribers(product):
            active_counts_by_email[entry["email"]] = active_counts_by_email.get(entry["email"], 0) + 1
            active_by_product[product].append(entry["email"])

    out = {p: 0.0 for p in PRODUCTS}
    for product, emails in active_by_product.items():
        for email in emails:
            n = active_counts_by_email[email]
            out[product] += PRICING_BY_COUNT.get(min(n, len(PRODUCTS)), 0) / n
    return {p: round(v, 2) for p, v in out.items()}


def multi_product_adoption() -> dict:
    """% of currently active subscribers on 2+ products at once -- the
    real test of whether the bundle discount curve is pulling people to
    buy more than a single product."""
    active_counts_by_email: dict[str, int] = {}
    for product in PRODUCTS:
        for entry in active_subscribers(product):
            active_counts_by_email[entry["email"]] = active_counts_by_email.get(entry["email"], 0) + 1
    total = len(active_counts_by_email)
    multi = sum(1 for n in active_counts_by_email.values() if n >= 2)
    return {
        "active_emails": total,
        "multi_product_emails": multi,
        "pct_multi": round(100 * multi / total) if total else None,
    }


def cohort_retention(weeks: int = 8) -> list[dict]:
    """For each weekly signup cohort that's old enough to have had a full
    30-day window to renew or lapse, what % were still active (renewed
    within that window, or already on a later period) past it. A cohort
    younger than EXPIRY_DAYS + 7 days simply isn't gradeable yet and is
    left out rather than shown with a misleading 0%/100%."""
    periods_by_key, _ = _reconstruct_periods()
    now = _now()
    cohorts: dict[str, dict] = {}
    for (_email, _product), periods in periods_by_key.items():
        if not periods:
            continue
        first = periods[0]
        cohort_start = (first["start"] - timedelta(days=first["start"].weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        cohort_end = cohort_start + timedelta(days=7)
        if now < cohort_end + timedelta(days=EXPIRY_DAYS):
            continue
        key = cohort_start.date().isoformat()
        c = cohorts.setdefault(key, {"cohort_week": key, "signups": 0, "retained": 0})
        c["signups"] += 1
        if first["cycles"] > 1 or len(periods) > 1:
            c["retained"] += 1
    out = []
    for c in sorted(cohorts.values(), key=lambda x: x["cohort_week"])[-weeks:]:
        c["retention_pct"] = round(100 * c["retained"] / c["signups"]) if c["signups"] else None
        out.append(c)
    return out


def avg_customer_lifetime() -> dict:
    """Average number of consecutive 30-day cycles (and days) a
    subscription lasts, across every access period that has definitively
    ended -- either manually removed, or its window lapsed without a
    renewal. Excludes still-ongoing periods since their lifetime isn't
    over yet, which would bias the average down."""
    periods_by_key, _ = _reconstruct_periods()
    now = _now()
    closed = [p for periods in periods_by_key.values() for p in periods if p["removed"] or p["end"] < now]
    if not closed:
        return {"sample_size": 0, "avg_cycles": None, "avg_days": None}
    return {
        "sample_size": len(closed),
        "avg_cycles": round(sum(p["cycles"] for p in closed) / len(closed), 1),
        "avg_days": round(sum((p["end"] - p["start"]).total_seconds() / 86400 for p in closed) / len(closed), 1),
    }


def time_to_renewal() -> dict:
    """How early or late subscribers renew relative to their prior
    expiry. Negative avg = renews before expiring, on average (a real
    renewal habit); positive avg = typically comes back after already
    lapsing (a win-back pattern, worth a reminder nudge before expiry)."""
    _, gaps = _reconstruct_periods()
    if not gaps:
        return {"sample_size": 0, "avg_days_before_expiry": None, "on_time_count": 0, "late_count": 0}
    avg = sum(gaps) / len(gaps)
    return {
        "sample_size": len(gaps),
        "avg_days_before_expiry": round(-avg, 1),
        "on_time_count": sum(1 for g in gaps if g <= 0),
        "late_count": sum(1 for g in gaps if g > 0),
    }


def analytics_summary() -> dict:
    """Everything the admin page's Analytics card shows, computed fresh
    from current subscriber state + the event log. Two different kinds
    of number here, kept clearly separate:
      - state-derived (active counts, per-product breakdown, estimated
        revenue, expiring-soon): always accurate, no history needed.
      - event-derived (signups/renewals/removes in a window, renewal
        rate): only reflects activity SINCE event logging started
        (events_since) -- there's no way to retroactively know about
        adds/removes that happened before this file existed.
    "estimated_mrr" is exactly that -- an estimate from applying
    PRICING_BY_COUNT to current active product counts per email, not a
    real payment ledger (no actual Venmo amounts are tracked anywhere)."""
    now = _now()
    all_data = load_subscribers()

    per_product_counts: dict[str, int] = {}
    active_counts_by_email: dict[str, int] = {}
    for product in PRODUCTS:
        actives = active_subscribers(product)
        per_product_counts[product] = len(actives)
        for entry in actives:
            active_counts_by_email[entry["email"]] = active_counts_by_email.get(entry["email"], 0) + 1

    estimated_mrr = sum(PRICING_BY_COUNT.get(min(n, len(PRODUCTS)), 0) for n in active_counts_by_email.values())

    expiring_soon = []
    for product in PRODUCTS:
        for entry in active_subscribers(product):
            if entry["days_left"] <= 5:
                expiring_soon.append({"product": product, "email": entry["email"], "days_left": entry["days_left"]})
    expiring_soon.sort(key=lambda r: r["days_left"])

    events = _load_events()
    events_since = events[0]["ts"] if events else None

    def _events_in(days: int) -> list[dict]:
        cutoff = now - timedelta(days=days)
        out = []
        for e in events:
            ts = _parse(e.get("ts", ""))
            if ts is not None and ts >= cutoff:
                out.append(e)
        return out

    def _window_counts(days: int) -> dict[str, int]:
        counts = {"add": 0, "renew": 0, "remove": 0}
        for e in _events_in(days):
            if e.get("action") in counts:
                counts[e["action"]] += 1
        return counts

    win7, win30 = _window_counts(7), _window_counts(30)
    renew_denom_30 = win30["renew"] + win30["remove"]
    renewal_rate_30d = round(100 * win30["renew"] / renew_denom_30) if renew_denom_30 else None

    recent_signups = [e for e in reversed(events) if e.get("action") == "add"][:8]

    return {
        "active_emails": len(active_counts_by_email),
        "active_subscriptions": sum(per_product_counts.values()),
        "per_product_counts": per_product_counts,
        "estimated_mrr": estimated_mrr,
        "expiring_soon": expiring_soon,
        "events_since": events_since,
        "window_7d": win7,
        "window_30d": win30,
        "renewal_rate_30d": renewal_rate_30d,
        "recent_signups": recent_signups,
        "revenue_timeline": revenue_timeline(),
        "per_product_revenue": per_product_revenue(),
        "multi_product_adoption": multi_product_adoption(),
        "cohort_retention": cohort_retention(),
        "avg_customer_lifetime": avg_customer_lifetime(),
        "time_to_renewal": time_to_renewal(),
    }


def products_for_email(email: str) -> list[dict]:
    """Reverse lookup: every product this email currently has active
    access to, each with days_left -- so you can add someone, then
    immediately check what they're actually going to receive. Includes
    expired entries too (flagged, not just silently omitted) so a lapsed
    subscriber's history doesn't just disappear from view."""
    email = email.strip().lower()
    now = _now()
    out = []
    for product, entries in load_subscribers().items():
        for entry in entries:
            if entry.get("email", "").strip().lower() != email:
                continue
            added = _parse(entry.get("added", ""))
            if added is None:
                continue
            days_left = EXPIRY_DAYS - (now - added).days
            out.append({
                "product": product,
                "added": entry["added"],
                "days_left": days_left,
                "active": days_left > 0,
            })
    return out


def send_receipt_email(email: str) -> tuple[bool, str]:
    """Confirmation-of-access receipt, sent after add_subscriber() adds or
    renews a product for this email. Reflects the email's CURRENT full
    active bundle (via products_for_email()), not just the product(s)
    touched by whatever call triggered it -- Venmo is the actual payment
    rail and this system has zero visibility into what was charged, so
    this is deliberately a receipt of ACCESS (what's live right now, the
    standard PRICING_BY_COUNT reference price for that bundle size, and
    each product's expiry date), not a claimed record of a specific
    payment amount.

    Safe to call after every single add/renew, including mid-batch: each
    send is independently accurate for the state at that moment, so a
    multi-product signup done one call at a time (e.g. the web admin
    tool, which only adds one product per request) still produces a
    correct, if incremental, trail of receipts -- callers doing a known
    single-email multi-product batch in one go (manage_subscribers.py's
    CLI `add`) should instead call this once after the whole batch
    completes, so the subscriber gets one receipt showing the final
    bundle instead of N ballooning ones.

    Returns (False, ...) without sending if the email has no active
    products at all (e.g. add_subscriber() was never actually called,
    or every entry already lapsed) -- there's nothing true to receipt."""
    rows = sorted((r for r in products_for_email(email) if r["active"]), key=lambda r: r["product"])
    if not rows:
        return False, f"{email} has no active products -- nothing to receipt"

    n = len(rows)
    price = PRICING_BY_COUNT.get(min(n, len(PRODUCTS)), 0)
    now = _now()
    expiry_rows = "".join(
        f'<div style="padding:14px 0;'
        f'{"border-bottom:1px solid #ececec;" if i < len(rows) - 1 else ""}'
        f'">'
        f'<div style="font-size:14px;color:#1a1a2e;font-weight:700;letter-spacing:.3px">{r["product"].upper()}</div>'
        f'<div style="color:#1a1a2e;font-size:12px;margin-top:3px;text-transform:uppercase;letter-spacing:.5px">'
        f'active through '
        f'{(_parse(r["added"]) + timedelta(days=EXPIRY_DAYS)).strftime("%B %d, %Y")}</div>'
        f'</div>'
        for i, r in enumerate(rows)
    )
    subject = f"Clairvoyance — Receipt: {n} product{'s' if n != 1 else ''} active (${price}/mo)"
    # Custom open (not the shared EMAIL_WRAP_OPEN) so the receipt can carry
    # its own bordered card instead of going flat-white right under a neon
    # header. Kept deliberately restrained (no background texture, no glow
    # shadows, no accent bar) -- a receipt reads as more professional with
    # a plain neutral border than any colored line under it.
    receipt_wrap_open = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:640px;margin:0 auto;background:#ffffff;color:#1a1a2e;'
        'border:1px solid #ececec;padding:28px 24px">'
    )
    # Black disclaimer here, not the shared magenta EMAIL_WRAP_CLOSE_DISCLOSED
    # (picks emails keep magenta, per the standing "disclaimer stays neon
    # magenta in all emails" instruction -- explicitly overridden for the
    # receipt only). Same copy, just recolored + closes the wrap div. Same
    # font-weight (400, i.e. unset/normal) as the "Confirmed" line above it.
    receipt_close = (
        '<div style="margin-top:28px;padding-top:14px;border-top:1px solid #e5e5e5;'
        'font-size:11px;line-height:1.5;color:#1a1a2e;font-weight:400">'
        'Clairvoyance Engine outputs are probabilistic projections for informational and '
        'analytical purposes only. Model outputs do not constitute financial or betting advice. '
        'Past model performance does not guarantee future results.'
        '</div></div>'
    )
    # Every "black" text element uses the same #1a1a2e shade (the brand's
    # near-black, already used for the headline/price) instead of a mix of
    # plain #000 and #1a1a2e.
    BLACK = '#1a1a2e'
    # Item rows and this price block are plain stacked (block-flow) markup,
    # not a side-by-side flex row -- deliberately NOT conditioned behind a
    # max-width media query, since a prior attempt at that (media query +
    # classes) rendered fine in a browser preview but still cramped in the
    # real send: several mobile mail clients (Gmail's app chief among them)
    # silently strip <style> blocks that aren't inside a proper <head>,
    # which this HTML-fragment email doesn't have. A layout that's simply
    # never side-by-side needs no client CSS support at all to render
    # correctly, on any screen width.
    body = (
        # Banner sits outside the padded wrap so it bleeds edge-to-edge
        # across the full 640px card width instead of sitting inset.
        f'<div style="max-width:640px;margin:0 auto"><img src="{EMAIL_BANNER_URL}" '
        f'alt="Clairvoyance Engine" width="640" '
        f'style="display:block;width:100%;max-width:640px;height:auto;border:0;'
        f'font-family:-apple-system,sans-serif;color:#999" /></div>' +
        receipt_wrap_open +
        f'<div style="font-size:11px;letter-spacing:2.5px;color:{BLACK};text-transform:uppercase;'
        'font-weight:700;margin-bottom:14px">Payment Receipt</div>'
        f'<div style="font-size:19px;font-weight:700;color:{BLACK};margin-bottom:6px;line-height:1.3">'
        'Thanks for subscribing to Clairvoyance Engine.</div>'
        f'<div style="font-size:13px;color:{BLACK};margin-bottom:26px">Confirmed {now.strftime("%B %d, %Y")}</div>'
        f'<div style="background:#fafafa;border:1px solid #e8e8e8;border-radius:6px;'
        f'padding:4px 20px;margin-bottom:20px">{expiry_rows}</div>'
        f'<div style="background:#fafafa;border:1px solid #e8e8e8;border-radius:6px;'
        f'padding:18px 22px;margin-bottom:24px">'
        f'<div style="font-size:24px;font-weight:800;color:#f20cff">${price}<span '
        f'style="font-size:13px;font-weight:500;color:{BLACK};margin-left:6px">per month</span></div></div>'
        f'<div style="font-size:13px;color:{BLACK};line-height:1.7">'
        'This confirms the access currently live on your account -- not a record of a specific '
        'Venmo payment (payments aren\'t processed or tracked here). Each product above renews '
        f'for another {EXPIRY_DAYS} days whenever you pay again; reply to this email with any '
        'questions.</div>' +
        receipt_close
    )
    return _send_gmail(subject, email, body)
