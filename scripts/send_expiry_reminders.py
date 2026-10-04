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
from _subscribers import subscribers_needing_reminder, mark_reminder_sent, EMAIL_BANNER_URL, fmt_mt_date  # noqa: E402
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-before", type=int, default=3,
                     help="Send a reminder once a subscription has this many days (or fewer) left")
    ap.add_argument("--dry-run", action="store_true",
                     help="Log what would be sent without actually sending or marking reminder_sent")
    args = ap.parse_args()

    needing = subscribers_needing_reminder(days_before=args.days_before)
    if not needing:
        print("No subscribers need a reminder today.")
        return

    by_email: dict[str, list[tuple[str, int]]] = {}
    expires: dict[str, dict[str, str]] = {}
    for row in needing:
        by_email.setdefault(row["email"], []).append((row["product"], row["days_left"]))
        expires.setdefault(row["email"], {})[row["product"]] = row["expires"]

    failures = 0
    for email, products_and_days in by_email.items():
        subject, body = _build_email(products_and_days, expires[email])
        if args.dry_run:
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
    if failures:
        # Non-zero so the run goes red (the failure alert/health check can see it) instead of a green
        # run where a subscriber silently never got their warning. reminder_sent was NOT set for them,
        # so tomorrow's run retries; the ones that did send are already marked.
        print(f"{failures} reminder email(s) failed", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
