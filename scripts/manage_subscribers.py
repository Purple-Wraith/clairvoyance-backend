#!/usr/bin/env python3
"""
manage_subscribers.py — easy CLI for adding/removing/listing paid mailing
list subscribers (see _subscribers.py). MVP is Venmo + this script: someone
pays, you run `add`, and it commits + pushes data/subscribers.json for you
(see sync_subscribers_to_git()) -- nothing else to remember afterward.

Usage:
  python3 scripts/manage_subscribers.py add nba someone@example.com
  python3 scripts/manage_subscribers.py add nba nfl hockey someone@example.com  # one email, multiple products -- any order, email can go anywhere
  python3 scripts/manage_subscribers.py remove nba someone@example.com
  python3 scripts/manage_subscribers.py list                                  # every product
  python3 scripts/manage_subscribers.py list nba                              # one product
  python3 scripts/manage_subscribers.py check someone@example.com             # what does this email get?
  python3 scripts/manage_subscribers.py products                             # list valid product names

For add/remove, the email is found by looking for the one argument that
contains "@" -- so `add soccer hockey someone@x.com` and
`add someone@x.com soccer hockey` both work identically. Whatever's left
after pulling out the email is treated as the product list.

Adding an email that's already active on a product renews it (resets its
30-day window to today) instead of creating a duplicate entry -- so `add`
doubles as the renewal command when someone pays again. `check` is the
reverse lookup -- add someone, then immediately confirm what they're
actually going to receive.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _subscribers import (
    PRODUCTS, add_subscriber, remove_subscriber, list_subscribers,
    products_for_email, send_receipt_email, EXPIRY_DAYS, sync_subscribers_to_git,
)


def _print_list(data: dict[str, list[dict]]) -> None:
    total = 0
    for product, entries in data.items():
        total += len(entries)
        if not entries:
            print(f"{product}: (no active subscribers)")
            continue
        print(f"{product}:")
        for e in sorted(entries, key=lambda x: x["days_left"]):
            print(f"  {e['email']:<40} {e['days_left']} day(s) left (added {e['added'][:10]})")
    print(f"\n{total} total active subscriber-product pair(s), {EXPIRY_DAYS}-day window")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    cmd = args[0]

    if cmd == "products":
        print(", ".join(PRODUCTS))
        return

    if cmd == "list":
        product = args[1] if len(args) > 1 else None
        if product and product not in PRODUCTS:
            print(f"Unknown product {product!r} -- must be one of: {', '.join(PRODUCTS)}")
            sys.exit(1)
        _print_list(list_subscribers(product))
        return

    if cmd == "add":
        if len(args) < 3:
            print("Usage: manage_subscribers.py add <product> [more products...] <email>  (any order)")
            sys.exit(1)
        # Email detected by "@" rather than a fixed position -- the old
        # strict "product, email, more products..." order silently
        # misparsed a perfectly reasonable `add soccer hockey
        # someone@x.com` (typing every product before the email, which is
        # how most people would naturally type it) as email="hockey" and
        # "someone@x.com" as an unknown product. Detecting by "@" instead
        # accepts the email in any position, so every ordering works.
        email_candidates = [a for a in args[1:] if "@" in a]
        if len(email_candidates) != 1:
            print(f"Usage: manage_subscribers.py add <product> [more products...] <email>  (any order)\n"
                  f"Expected exactly one argument containing '@' (the email) -- found {len(email_candidates)}.")
            sys.exit(1)
        email = email_candidates[0]
        products = [a for a in args[1:] if a != email]
        if not products:
            print("No product given -- need at least one, e.g.: add soccer someone@example.com")
            sys.exit(1)
        for product in products:
            if product not in PRODUCTS:
                print(f"Unknown product {product!r} -- must be one of: {', '.join(PRODUCTS)}")
                sys.exit(1)
        for product in products:
            print(add_subscriber(product, email))
        ok, msg = send_receipt_email(email)
        print(f"Receipt sent to {email}" if ok else f"Receipt NOT sent: {msg}")
        synced, sync_msg = sync_subscribers_to_git(f"Add {email} to {', '.join(products)}")
        print(sync_msg if synced else f"WARNING: {sync_msg}")
        return

    if cmd == "check":
        if len(args) < 2:
            print("Usage: manage_subscribers.py check <email>")
            sys.exit(1)
        email = args[1]
        rows = products_for_email(email)
        if not rows:
            print(f"{email} isn't on any product's list.")
            return
        active = [r for r in rows if r["active"]]
        expired = [r for r in rows if not r["active"]]
        if active:
            print(f"{email} is actively receiving:")
            for r in sorted(active, key=lambda x: x["days_left"]):
                print(f"  {r['product']:<10} {r['days_left']} day(s) left (added {r['added'][:10]})")
        else:
            print(f"{email} has no active subscriptions.")
        if expired:
            print("Expired (no longer receiving):")
            for r in expired:
                print(f"  {r['product']:<10} added {r['added'][:10]}")
        return

    if cmd == "remove":
        if len(args) < 3:
            print("Usage: manage_subscribers.py remove <product> [more products...] <email>  (any order)")
            sys.exit(1)
        # Same "@" detection as add -- see the comment there.
        email_candidates = [a for a in args[1:] if "@" in a]
        if len(email_candidates) != 1:
            print(f"Usage: manage_subscribers.py remove <product> [more products...] <email>  (any order)\n"
                  f"Expected exactly one argument containing '@' (the email) -- found {len(email_candidates)}.")
            sys.exit(1)
        email = email_candidates[0]
        products = [a for a in args[1:] if a != email]
        if not products:
            print("No product given -- need at least one, e.g.: remove soccer someone@example.com")
            sys.exit(1)
        for product in products:
            if product not in PRODUCTS:
                print(f"Unknown product {product!r} -- must be one of: {', '.join(PRODUCTS)}")
                sys.exit(1)
        for product in products:
            print(remove_subscriber(product, email))
        synced, sync_msg = sync_subscribers_to_git(f"Remove {email} from {', '.join(products)}")
        print(sync_msg if synced else f"WARNING: {sync_msg}")
        return

    print(f"Unknown command {cmd!r}\n")
    print(__doc__)
    sys.exit(1)


if __name__ == "__main__":
    main()
