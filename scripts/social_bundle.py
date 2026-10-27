#!/usr/bin/env python3
"""social_bundle.py -- ONE "Social content" email a day (owner request 2026-10-27: too many emails).

Until then the owner got the daily cards, the weekly / monthly / yearly / event / rotation cards and two pick-of-day posts (AM + PM) as separate emails.  Now generate_social_cards.py and
generate_pick_of_day_social.py run with --bundle-dir: instead of sending, each calls add_section() (a small JSON file naming the section's HTML and its attachment files), and
send_social_bundle.py sends the whole directory as one email via send_bundle().  Nothing is dropped: every section keeps its own heading, captions and attachments.

Gmail refuses a message over ~25 MB after base64 (x1.37), so a bundle whose attachments exceed MAX_ATTACH_BYTES is split into "part i of n" emails rather than failing -- a section is never cut.
Pure logic + file IO; the actual mail function is injected so the tests never send anything.
"""
from __future__ import annotations

import html as _html
import json
import os
from pathlib import Path

MAX_ATTACH_BYTES = 17 * 1024 * 1024
PICKS_ORDER = 0        # pick-of-day posts go first: they are the time-sensitive ones (games start), the stats cards can wait


def add_section(bundle_dir: Path | str, title: str, html: str, attachments: list[Path], order: int | None = None) -> Path:
    """Queue one section.  Files are numbered so the default order is creation order; `order` overrides it (lower first)."""
    d = Path(bundle_dir)
    d.mkdir(parents=True, exist_ok=True)
    n = len(list(d.glob("*.json")))
    key = f"{(order if order is not None else 50):03d}-{n:03d}"
    path = d / f"{key}.json"
    path.write_text(json.dumps({"title": title, "html": html, "attachments": [str(Path(a).resolve()) for a in attachments]}, indent=1))
    return path


def load_sections(bundle_dir: Path | str) -> list[dict]:
    d = Path(bundle_dir)
    out = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        s = json.loads(p.read_text())
        s["attachments"] = [Path(a) for a in s.get("attachments", []) if Path(a).exists()]
        out.append(s)
    return out


def _size(paths: list[Path]) -> int:
    return sum(p.stat().st_size for p in paths if p.exists())


def split_parts(sections: list[dict], limit: int = MAX_ATTACH_BYTES) -> list[list[dict]]:
    """Greedy, order-preserving. A section larger than the limit on its own still goes out (alone) -- never dropped."""
    parts: list[list[dict]] = []
    cur: list[dict] = []
    cur_size = 0
    for s in sections:
        sz = _size(s["attachments"])
        if cur and cur_size + sz > limit:
            parts.append(cur)
            cur, cur_size = [], 0
        cur.append(s)
        cur_size += sz
    if cur:
        parts.append(cur)
    return parts


def build_email(parts_sections: list[dict], date_label: str, part: int = 1, of: int = 1) -> tuple[str, str, list[Path]]:
    from _gmail_email import EMAIL_WRAP_OPEN, EMAIL_WRAP_CLOSE
    suffix = f" (part {part} of {of})" if of > 1 else ""
    subject = f"Clairvoyance — Social Content for {date_label}{suffix}"
    toc = "".join(f"<li>{_html.escape(s['title'])}</li>" for s in parts_sections)
    body = [EMAIL_WRAP_OPEN, f"<p>Everything to post today, in one email:</p><ol>{toc}</ol>"]
    atts: list[Path] = []
    for s in parts_sections:
        body.append(f"<hr style='border:none;border-top:1px solid #ddd;margin:22px 0'><h2 style='margin:0 0 6px;font-size:18px'>{_html.escape(s['title'])}</h2>{s['html']}")
        atts.extend(s["attachments"])
    body.append(EMAIL_WRAP_CLOSE)
    return subject, "".join(body), atts


def send_bundle(bundle_dir: Path | str, date_label: str, to: str, send=None, limit: int = MAX_ATTACH_BYTES) -> tuple[str, list[str]]:
    """Returns (outcome, details): outcome in sent | empty | failed.  `send(subject, to, html, attachments)` -> (ok, msg)."""
    sections = load_sections(bundle_dir)
    if not sections:
        return "empty", []
    if not to:
        return "failed", ["no recipient (SOCIAL_CARD_EMAIL_TO)"]
    if send is None:
        from _gmail_email import send_email as send
    parts = split_parts(sections, limit)
    sent, errs = [], []
    for i, ps in enumerate(parts, start=1):
        subject, html, atts = build_email(ps, date_label, i, len(parts))
        ok, msg = send(subject, to, html, attachments=atts)
        (sent if ok else errs).append(subject if ok else f"{subject}: {msg}")
    return ("failed" if errs else "sent"), (errs or sent)


if __name__ == "__main__":
    import argparse
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle_dir")
    a = ap.parse_args()
    label = datetime.now(ZoneInfo("America/Denver")).strftime("%B %-d, %Y")
    outcome, details = send_bundle(a.bundle_dir, label, os.environ.get("SOCIAL_CARD_EMAIL_TO", ""))
    print(f"[social_bundle] {outcome}: {details}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"outcome={outcome}\n")
    raise SystemExit(1 if outcome == "failed" else 0)
