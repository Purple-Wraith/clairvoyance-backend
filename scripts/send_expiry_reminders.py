#!/usr/bin/env python3
"""
send_expiry_reminders.py — warns subscribers a few days before their
30-day access window lapses, so losing access isn't a surprise and they
have a chance to renew first. Each (email, product) pair only ever gets
warned once per window -- reminder_sent resets on every add/renew (see
_subscribers.add_subscriber()), so a later lapse-and-resubscribe cycle
gets its own fresh reminder too, not silently skipped forever.

Grouped by email: someone with multiple products expiring around the
same time gets ONE email listing all of them, not one per product.

Also (owner 2026-11-24): once a window has actually ENDED -- a paid month or a free / promo month -- the person gets ONE "access ended" email
(ended_sent flag, once per window) saying it has ended and how to start again; a free month's email also gives the price, for someone who liked
the sport and wants to subscribe.  Only windows that ended in the last few days are considered, so old lapses are never emailed.

Sent directly to the subscriber (matches _subscribers.send_receipt_email()
's precedent -- no owner CC there either). Originally routed to the
owner's own inbox instead as MVP scaffolding while zero real paying
subscribers existed; wired to the real recipient once that stopped being
true, per explicit request (2026-09-02 engine audit).

Usage:
  python3 scripts/send_expiry_reminders.py [--days-before 3]
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datetime import datetime  # noqa: E402
import _subscribers  # noqa: E402
from _subscribers import (subscribers_needing_reminder, mark_reminder_sent, subscribers_needing_ended_notice, mark_ended_sent,  # noqa: E402
                          PRICING_BY_COUNT, EMAIL_BANNER_URL, fmt_mt_date)
from auto_lock_settle import PRODUCT_LABEL  # noqa: E402
from _gmail_email import send_email, EMAIL_WRAP_OPEN, EMAIL_WRAP_CLOSE_SUBSCRIBER  # noqa: E402


def _build_email(products_and_days: list[tuple[str, int]], expires: dict[str, str] | None = None) -> tuple[str, str]:
    """Returns (subject, html_body). products_and_days is a list of
    (product, days_left) for this one subscriber; expires maps product -> ISO expiry
    (shown as a Mountain-time date). Closes through EMAIL_WRAP_CLOSE_SUBSCRIBER: the
    disclaimer every subscriber email carries plus the reply-to-stop footer."""
    expires = expires or {}
    names = [PRODUCT_LABEL[p] for p, _ in products_and_days]
    soonest = min(d for _, d in products_and_days)

    if len(products_and_days) == 1:
        product, days = products_and_days[0]
        subject = f"Clairvoyance — {PRODUCT_LABEL[product]} access expires in {days} day{'s' if days != 1 else ''}"
    else:
        subject = f"Clairvoyance — {len(products_and_days)} subscriptions expiring soon"

    rows = "".join(
        f'<div style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,.12);font-size:15px;color:#eee">'
        f'<strong style="color:#fff">{PRODUCT_LABEL[p]}</strong> — '
        f'<span style="color:{"#ff3b5c" if d <= 1 else "#ffdd00"}">{d} day{"s" if d != 1 else ""} left</span>'
        + (f' <span style="color:#bbb;font-size:13px">(through {fmt_mt_date(datetime.fromisoformat(expires[p]))})</span>' if expires.get(p) else "") +
        f'</div>'
        for p, d in sorted(products_and_days, key=lambda x: x[1])
    )
    in_days = f"in {soonest} day{'s' if soonest != 1 else ''}"
    headline = (f"Your access to {names[0]} expires {in_days}." if len(names) == 1
                else f"{len(names)} of your subscriptions are about to expire — the first {in_days}.")
    # Same hosted banner every other subscriber email uses (see
    # EMAIL_BANNER_URL in _subscribers.py) -- outside EMAIL_WRAP_OPEN's
    # padding so it bleeds edge-to-edge across the full 640px card width.
    banner_html = (
        f'<div style="max-width:640px;margin:0 auto"><img src="{EMAIL_BANNER_URL}" '
        f'alt="Clairvoyance Engine" width="640" '
        f'style="display:block;width:100%;max-width:640px;height:auto;border:0;'
        f'font-family:-apple-system,sans-serif;color:#999" /></div>'
    )
    body = (
        banner_html +
        EMAIL_WRAP_OPEN +
        f'<div style="font-size:16px;color:#1a1a2e;margin-bottom:14px">'
        f'{headline}</div>'
        f'<div style="background:#14001f;border-radius:6px;padding:4px 14px;margin-bottom:16px">{rows}</div>'
        f'<div style="font-size:14px;color:#444;line-height:1.6">'
        f'To keep it going with no gap in your daily picks, reply to this email or Venmo the usual '
        f'amount. Renewal gives you 30 days from the day we record your payment, so renewing early '
        f'does not add to the time you have left. '
        f'No action needed if you\'re fine letting it lapse.</div>' +
        EMAIL_WRAP_CLOSE_SUBSCRIBER
    )
    return subject, body


def _price_line() -> str:
    """The standard price in words, from the live ladder (so a repricing never leaves this email stale): $20 for one sport, +$15 for each extra."""
    one = PRICING_BY_COUNT[1]
    extra = PRICING_BY_COUNT[2] - PRICING_BY_COUNT[1]
    return f"${one} for 30 days of one sport, plus ${extra} for each extra sport (${PRICING_BY_COUNT[2]} for two, ${PRICING_BY_COUNT[3]} for three)"


def _build_ended_email(rows: list[dict]) -> tuple[str, str]:
    """(subject, html) for ONE subscriber whose window(s) just ended.  rows: [{product, ended (ISO), promo}].  A free month says so and gives the price;
    a paid one points to the usual renewal.  Closes through EMAIL_WRAP_CLOSE_SUBSCRIBER like every subscriber email."""
    rows = sorted(rows, key=lambda r: r["ended"])
    names = [PRODUCT_LABEL[r["product"]] for r in rows]
    any_promo = any(r["promo"] for r in rows)
    all_promo = all(r["promo"] for r in rows)
    date = lambda r: fmt_mt_date(datetime.fromisoformat(r["ended"]))     # noqa: E731
    if len(rows) == 1:
        r = rows[0]
        subject = (f"Clairvoyance — your free month of {names[0]} has ended" if r["promo"] else f"Clairvoyance — your {names[0]} access has ended")
        headline = (f"Your free month of {names[0]} ended on {date(r)}." if r["promo"] else f"Your access to {names[0]} ended on {date(r)}.")
    else:
        subject = ("Clairvoyance — your free months have ended" if all_promo else f"Clairvoyance — {len(rows)} subscriptions have ended")
        headline = f"{len(rows)} of your subscriptions have ended, so those picks have stopped:"
    list_rows = "".join(
        f'<div style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,.12);font-size:15px;color:#eee">'
        f'<strong style="color:#fff">{PRODUCT_LABEL[r["product"]]}</strong> — <span style="color:#bbb">ended {date(r)}'
        + (' · free month' if r["promo"] else '') + '</span></div>'
        for r in rows)
    paras = [f'You will not get {names[0] if len(rows) == 1 else "those"} picks in your daily emails from here on.']
    if any_promo:
        paras.append(f'If you liked the picks and want to keep them coming, a subscription is {_price_line()}. '
                     f'Reply to this email or Venmo that amount and we will turn it back on. Access runs 30 days from the day we record your payment.')
    if not all_promo:
        paras.append('To pick it back up, reply to this email or Venmo the usual amount. Renewal gives you 30 days from the day we record your payment.')
    paras.append('No action needed if you are done. Thanks for trying Clairvoyance.' if any_promo else 'No action needed if you are done.')
    banner_html = (
        f'<div style="max-width:640px;margin:0 auto"><img src="{EMAIL_BANNER_URL}" '
        f'alt="Clairvoyance Engine" width="640" '
        f'style="display:block;width:100%;max-width:640px;height:auto;border:0;'
        f'font-family:-apple-system,sans-serif;color:#999" /></div>'
    )
    body = (banner_html + EMAIL_WRAP_OPEN +
            f'<div style="font-size:16px;color:#1a1a2e;margin-bottom:14px">{headline}</div>'
            f'<div style="background:#14001f;border-radius:6px;padding:4px 14px;margin-bottom:16px">{list_rows}</div>'
            + "".join(f'<div style="font-size:14px;color:#444;line-height:1.6;margin-bottom:10px">{p}</div>' for p in paras)
            + EMAIL_WRAP_CLOSE_SUBSCRIBER)
    return subject, body


def _send_reminders(days_before: int, dry_run: bool, needing: list[dict]) -> int:
    """The 'access ends soon' emails.  Returns the number that failed."""
    if not needing:
        print("No subscribers need a reminder today.")
        return 0
    by_email: dict[str, list[tuple[str, int]]] = {}
    expires: dict[str, dict[str, str]] = {}
    for row in needing:
        by_email.setdefault(row["email"], []).append((row["product"], row["days_left"]))
        expires.setdefault(row["email"], {})[row["product"]] = row["expires"]

    failures = 0
    for email, products_and_days in by_email.items():
        subject, body = _build_email(products_and_days, expires[email])
        if dry_run:
            print(f"[DRY RUN] would email {email}: {subject}")
            continue
        ok, msg = send_email(subject, email, body)
        if ok:
            for product, _days in products_and_days:
                mark_reminder_sent(product, email)
            print(f"Reminder sent to {email} for {[p for p, _ in products_and_days]}")
        else:
            failures += 1
            print(f"FAILED to email {email}: {msg}")
    return failures


def _send_ended_notices(dry_run: bool, ended: list[dict]) -> int:
    """The 'access has ended' emails (one per person, all their ended products together).  Returns the number that failed; a failed one is not marked, so tomorrow retries
    (inside the few-day window)."""
    if not ended:
        print("No access-ended notices to send today.")
        return 0
    by_email: dict[str, list[dict]] = {}
    for row in ended:
        by_email.setdefault(row["email"], []).append(row)
    failures = 0
    for email, rows in by_email.items():
        subject, body = _build_ended_email(rows)
        if dry_run:
            print(f"[DRY RUN] would email {email}: {subject}")
            continue
        ok, msg = send_email(subject, email, body)
        if ok:
            for r in rows:
                mark_ended_sent(r["product"], email)
            print(f"Access-ended notice sent to {email} for {[r['product'] for r in rows]}")
        else:
            failures += 1
            print(f"FAILED to email {email}: {msg}")
    return failures


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-before", type=int, default=3,
                     help="Send a reminder once a subscription has this many days (or fewer) left")
    ap.add_argument("--dry-run", action="store_true",
                     help="Log what would be sent without actually sending or marking reminder_sent")
    args = ap.parse_args()

    needing = subscribers_needing_reminder(days_before=args.days_before)
    ended = subscribers_needing_ended_notice()
    if _subscribers.DATA_ERRORS:
        raise SystemExit(1)       # the list could not be read: "nobody needs a reminder" would be a lie (already printed as ::error::)

    failures = _send_reminders(args.days_before, args.dry_run, needing)
    failures += _send_ended_notices(args.dry_run, ended)
    if failures:
        # Non-zero so the run goes red (the failure alert/health check can see it) instead of a green
        # run where a subscriber silently never got their warning. reminder_sent / ended_sent was NOT set for them,
        # so tomorrow's run retries; the ones that did send are already marked.
        print(f"{failures} email(s) failed", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
