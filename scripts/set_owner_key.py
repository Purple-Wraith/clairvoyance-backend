#!/usr/bin/env python3
"""Reset the owner key: the one key for LOCK NOW, SETTLE NOW and the profit tab's SYNC NOW.

    python3 scripts/set_owner_key.py

You type the new key (hidden, twice). It is handed to Cloudflare (`wrangler secret put TRIGGER_KEY`) on standard input and
is never printed, written to a file or passed on a command line. The old key stops working at once, on every device.
"""
from __future__ import annotations

import getpass, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

SCHEDULER = Path(__file__).resolve().parent.parent / "scheduler"
CHECK_URL = "https://clairvoyance-scheduler.clairvoyance-reese.workers.dev/profit"
MIN_LEN = 16   # the Worker only slows wrong guesses, so a short key is not enough
# --yes: if npx had to ask before installing wrangler, it would read the key from standard input as the answer
PUT = ["npx", "--yes", "wrangler", "secret", "put", "TRIGGER_KEY"]


def ask(read=getpass.getpass, say=print) -> str:
    say(f"New owner key: at least {MIN_LEN} characters. Four or more unrelated words are easy to type on a phone.")
    while True:
        first = read("New key (hidden): ").strip()
        if len(first) < MIN_LEN:
            say(f"Too short ({len(first)} characters). Try again.")
        elif not (first.isascii() and first.isprintable()):
            say("Use plain keyboard characters only (a phone can turn quotes and dashes into others). Try again.")
        elif read("Same key again: ").strip() != first:
            say("The two did not match. Try again.")
        else:
            return first


def put(key: str, run=subprocess.run) -> bool:
    return run(PUT, cwd=SCHEDULER, input=key, text=True).returncode == 0


def works(key: str, opener=urllib.request.urlopen, sleep=time.sleep, tries: int = 6) -> bool:
    """Ask the Worker with the new key (a read; the reply is not looked at). A new secret can take a few seconds to apply."""
    req = urllib.request.Request(CHECK_URL, headers={"x-owner-key": key, "user-agent": "clairvoyance-key-check"})
    for _ in range(tries):
        try:
            with opener(req, timeout=15) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        sleep(5)
    return False


def main() -> int:
    try:
        key = ask()
    except (KeyboardInterrupt, EOFError):
        print("\nNothing changed.")
        return 1
    if not put(key):
        print("Cloudflare did not take the new key (see above). If it says you are not logged in: cd scheduler && npx wrangler login")
        return 1
    print("Saved on Cloudflare. Checking it...")
    if works(key):
        print("The new key works. Save it in your password app now, then enter it on each device the next time the app asks.")
        return 0
    print("Saved, but the check did not pass yet. Wait a minute, then tap SYNC NOW in the app with the new key.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
