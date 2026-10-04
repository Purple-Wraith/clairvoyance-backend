#!/usr/bin/env python3
"""
weekly_health_digest.py — a real, always-sent weekly summary, distinct
from daily_health_check.py's "silent unless something's broken" alert
philosophy. Explicit request following the 2026-09-13 Supabase egress-
quota lockout: that outage ran ~20 hours before it was even noticed
(via a direct manual check), because nothing was actively watching for
it. This digest exists specifically so a repeat gets caught within a
week instead of needing to ask a session to check.

Four sections, in priority order (headline finding first):
  1. Supabase reachability -- a direct probe against the real REST API,
     the exact signal that would have caught the 2026-09-13 outage
     immediately instead of ~20 hours in. If this fails, every other
     section that depends on a real ledger pull is skipped and clearly
     labeled unavailable, rather than silently showing stale/wrong data.
  2. Workflow success rate over the last 7 days, for the same key
     workflows daily_health_check.py already tracks (reused for
     consistency, not re-invented) -- a percentage, not just "broken or
     not", since a digest is about trend, not just alerting.
  3. Model calibration -- the exact same predicted-vs-actual-by-
     probability-bucket check now live on the Overall Dashboard (see
     docs/app.html's renderOverall), computed server-side from the same
     real ledger so it shows up here even for someone who never opens
     the Dashboard tab that week.
  4. Stuck-pending anomalies -- any pending bet whose own real-world
     game date is more than 3 days old, the exact symptom class this
     session spent real time investigating and fixing for NFL props.

Deliberately lightweight: no Playwright/browser needed at all --
SUPABASE_URL/SUPABASE_KEY are public anon-key constants already sitting
in plain text in docs/app.html (the same ones the live app itself uses
client-side), so a simple regex extraction + requests.get() is enough
for a read-only health probe and ledger pull. Every other Supabase-
touching script in this repo needs a real headless browser because it
also needs the app's own JS (settlement logic, the qualifying pipeline)
-- this one only ever reads.
"""
from __future__ import annotations
import html as _html
import os
import re
import sys
import urllib.request
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _gmail_email import send_email  # noqa: E402
import lock_timing  # noqa: E402  -- shared known-late classifier (pre-start-locks-only basis)

ROOT = Path(__file__).resolve().parent.parent
REPO = "Purple-Wraith/clairvoyance-backend"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ALERT_TO = os.environ.get("SOCIAL_CARD_EMAIL_TO", "") or os.environ.get("LOCKS_EMAIL_TO", "")

# Same set daily_health_check.py's own MONITORED list tracks -- reused
# rather than re-invented, so both scripts agree on "what counts as a
# key workflow" instead of drifting into two different opinions.
MONITORED = [
    ("auto-lock-settle.yml", "Main Auto-Lock + Settle"),
    ("european-lock-early.yml", "European Early Lock (Soccer + SHL/Liiga)"),
    ("cfb-lock-early.yml", "CFB Early Lock"),
    ("cfb-lock-evening.yml", "CFB Evening Lock"),
    ("soccer-lock-evening.yml", "Soccer Evening Lock"),
    ("hockey-lock-evening.yml", "Hockey Evening Lock — SHL/Liiga/NLA/Extraliga"),
    ("send-expiry-reminders.yml", "Expiry Reminders"),
    ("social-cards-daily.yml", "Social Cards Daily"),
    ("pick-of-day-social-daily.yml", "Pick-of-Day Social"),
]
# The data REFRESH workflows (added 2026-10-03: they were unmonitored). Same list the daily check watches, so the weekly success rate
# shows a refresh job that "mostly" works but keeps dropping runs.
try:
    from daily_health_check import REFRESH_MONITORED as _REFRESH_MONITORED  # noqa: E402
    MONITORED += [(f, l) for f, l, *_rest in _REFRESH_MONITORED]
except Exception:  # fail-open: the digest still goes out with the original list
    pass

# Same scope renderOverall()'s _broadSportOf() enforces client-side --
# retired/personal-only sports (MLB, WNBA, tennis, World Cup, CBB)
# excluded so this digest's calibration/stuck-pending numbers match
# what the Dashboard itself would show, not a broader/different set.
# Real bug, found auditing this file 2026-10-02: NLA and EXTRALIGA were
# added to docs/app.html's own _broadSportOf() HOCKEY group back on
# 2026-09-23 (same day they were promoted into auto_lock_settle.py's
# PRODUCT_SPORTS["hockey"] -- real paid-product leagues since) but this
# copy of the set was never updated to match. Any real settled NLA/
# Extraliga bet silently fell out of is_active_sport() -- excluded from
# both calibration_buckets() (so this digest's calibration numbers
# quietly omitted two live paid-product leagues) and stuck_pending() (so
# a stuck NLA/Extraliga pick older than 3 days would never be flagged
# here, even though it's real paying-subscriber inventory).
# Retired leagues (MLS, Bundesliga) and parlays are out of scope (owner decisions 2026-10-03 / 2026-10-04) -- same set the Dashboard's _cvScoped()
# and every published figure use, via the shared lock_timing constants, so this digest's calibration / stuck-pending counts match the app.
ACTIVE_TAGS = set(lock_timing.IN_SCOPE_CODES)


def _extract_supabase_creds() -> tuple[str, str]:
    html = (ROOT / "docs" / "app.html").read_text()
    url = re.search(r"const SUPABASE_URL='([^']+)'", html)
    key = re.search(r"const SUPABASE_KEY='([^']+)'", html)
    if not url or not key:
        raise RuntimeError("Could not find SUPABASE_URL/SUPABASE_KEY in docs/app.html")
    return url.group(1), key.group(1)


def probe_supabase(url: str, key: str) -> str | None:
    """Returns None if healthy, or a short problem description."""
    try:
        r = requests.get(
            f"{url}/rest/v1/bets",
            params={"select": "raw", "limit": 1},
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=15,
        )
    except Exception as exc:
        return f"Supabase request failed entirely: {exc}"
    if r.status_code != 200:
        detail = ""
        try:
            detail = r.json().get("message", "")
        except Exception:
            pass
        return f"Supabase returned HTTP {r.status_code}{': ' + detail if detail else ''}"
    return None


def load_ledger(url: str, key: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    page_size = 1000
    while True:
        r = requests.get(
            f"{url}/rest/v1/bets",
            params={"select": "raw", "order": "date.desc", "outcome": "neq._removed"},
            headers={
                "apikey": key, "Authorization": f"Bearer {key}",
                "Range": f"{offset}-{offset + page_size - 1}",
            },
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        rows.extend(x["raw"] for x in batch if x.get("raw"))
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def is_active_sport(p: dict) -> bool:
    if lock_timing.is_parlay(p):
        return False
    return (p.get("sport") or "").upper() in ACTIVE_TAGS or (p.get("league") or "").upper() in ACTIVE_TAGS


def _api_get(path: str) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def workflow_success_rate(filename: str, days: int = 7) -> tuple[int, int] | None:
    """Returns (successes, total) among runs in the last `days`, or None
    if the API call itself failed."""
    try:
        # Filtered by date server-side and per_page=100: a flat per_page=50 only reached back ~1.5 days for a 30-minute workflow
        # (live-tracker), so its "last 7 days" rate was really the last day and a half.
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        data = _api_get(f"/repos/{REPO}/actions/workflows/{filename}/runs?per_page=100&status=completed&created=%3E%3D{since}")
    except Exception:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    # cancelled / skipped runs (concurrency groups) are normal -- counting them as failures made healthy workflows read red
    runs = [
        r for r in (data.get("workflow_runs") or [])
        if datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) >= cutoff
        and r.get("status") == "completed" and r.get("conclusion") not in ("cancelled", "skipped", "neutral")
    ]
    if not runs:
        return (0, 0)
    successes = sum(1 for r in runs if r.get("conclusion") == "success")
    return successes, len(runs)


def calibration_buckets(settled: list[dict]) -> list[dict]:
    buckets = [
        (0, 0.55, "<55%"), (0.55, 0.60, "55-60%"), (0.60, 0.65, "60-65%"),
        (0.65, 0.70, "65-70%"), (0.70, 0.75, "70-75%"), (0.75, 1.01, "75%+"),
    ]
    out = []
    for lo, hi, lbl in buckets:
        bets = [p for p in settled if p.get("winProb") is not None and lo <= p["winProb"] < hi]
        if not bets:
            continue
        wins = sum(1 for p in bets if p.get("outcome") == "win")
        actual = wins / len(bets)
        predicted = sum(p["winProb"] for p in bets) / len(bets)
        out.append({"lbl": lbl, "n": len(bets), "actual": actual, "predicted": predicted})
    return out


def stuck_pending(all_bets: list[dict], max_age_days: int = 3) -> list[dict]:
    today_mt = datetime.now(ZoneInfo("America/Denver")).date()
    stuck = []
    for p in all_bets:
        if p.get("outcome") != "pending" or not is_active_sport(p) or not p.get("date"):
            continue
        try:
            game_date = datetime.strptime(p["date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        age = (today_mt - game_date).days
        if age > max_age_days:
            stuck.append({**p, "_ageDays": age})
    return sorted(stuck, key=lambda p: -p["_ageDays"])


def build_email_html(supabase_problem, wf_rates, cal_rows, stuck, late_excluded: int = 0) -> str:
    parts = ['<div style="font-family:monospace;font-size:14px;color:#1a1a2e;line-height:1.6">']
    parts.append("<h2>Clairvoyance — Weekly Health Digest</h2>")

    parts.append("<h3>1. Supabase</h3>")
    if supabase_problem:
        parts.append(f'<p style="color:#c00"><strong>PROBLEM:</strong> {supabase_problem}</p>')
    else:
        parts.append('<p style="color:#0a0">Reachable and responding normally.</p>')

    parts.append("<h3>2. Workflow success rate (last 7 days)</h3><ul>")
    for label, result in wf_rates:
        if result is None:
            parts.append(f"<li>{label}: couldn't check (API error)</li>")
        else:
            s, t = result
            pct = f"{s}/{t} ({s/t*100:.0f}%)" if t else "no runs found"
            color = "#c00" if t and s < t else "#1a1a2e"
            parts.append(f'<li style="color:{color}">{label}: {pct}</li>')
    parts.append("</ul>")

    parts.append("<h3>3. Model calibration</h3>")
    if late_excluded:
        parts.append(f"<p><em>Pre-start locks only: {late_excluded} settled pick(s) locked after their game started are excluded "
                     f"(same basis as every published figure and the Dashboard's calibration).</em></p>")
    if supabase_problem:
        parts.append("<p><em>Unavailable — Supabase unreachable.</em></p>")
    elif not cal_rows:
        parts.append("<p><em>Not enough settled data yet.</em></p>")
    else:
        parts.append("<table style='border-collapse:collapse'><tr><th align=left>Bucket</th><th>n</th><th>Predicted</th><th>Actual</th></tr>")
        for r in cal_rows:
            delta = r["actual"] - r["predicted"]
            color = "#0a0" if delta >= -0.02 else "#c80" if delta >= -0.08 else "#c00"
            parts.append(
                f"<tr><td>{r['lbl']}</td><td align=center>{r['n']}</td>"
                f"<td align=center>{r['predicted']*100:.1f}%</td>"
                f"<td align=center style='color:{color}'>{r['actual']*100:.1f}%</td></tr>"
            )
        parts.append("</table>")

    parts.append("<h3>4. Stuck pending (>3 days old)</h3>")
    if supabase_problem:
        parts.append("<p><em>Unavailable — Supabase unreachable.</em></p>")
    elif not stuck:
        parts.append('<p style="color:#0a0">None found.</p>')
    else:
        parts.append(f'<p style="color:#c00">{len(stuck)} found:</p><ul>')
        for p in stuck[:20]:
            parts.append(f"<li>{_html.escape(str(p.get('betOn', '?')))} — {p.get('date', '?')} ({p['_ageDays']}d old)</li>")
        if len(stuck) > 20:
            parts.append(f"<li>...and {len(stuck) - 20} more</li>")
        parts.append("</ul>")

    parts.append("</div>")
    return "".join(parts)


def main() -> int:
    url, key = _extract_supabase_creds()
    supabase_problem = probe_supabase(url, key)

    settled: list[dict] = []
    all_bets: list[dict] = []
    late_excluded = 0
    if not supabase_problem:
        try:
            try:
                all_bets = load_ledger(url, key)
            except Exception as sb_exc:
                # Supabase unreadable (quota restriction / outage): the committed backup is the next-best ledger for a weekly summary
                all_bets = json.loads((ROOT / "docs" / "picks_backup.json").read_text())
                print(f"WARNING: Supabase ledger unavailable ({sb_exc}); using docs/picks_backup.json ({len(all_bets)} bets)")
            settled = [p for p in all_bets if is_active_sport(p) and p.get("outcome") in ("win", "loss")]
            # Calibration must not learn from picks whose result was known at lock (manual late locks) -- the Dashboard's own
            # calibration already drops them (_calEligible); this digest now agrees. Shared classifier, unknown timing kept.
            settled, _late = lock_timing.split_picks(settled, lock_timing.load_index())
            late_excluded = len(_late)
        except Exception as exc:
            supabase_problem = f"Reachable, but ledger pull failed: {exc}"

    wf_rates = []
    if GITHUB_TOKEN:
        for filename, label in MONITORED:
            wf_rates.append((label, workflow_success_rate(filename)))
    else:
        print("GITHUB_TOKEN not set -- skipping workflow success-rate checks.")

    cal_rows = calibration_buckets(settled) if settled else []
    stuck = stuck_pending(all_bets) if all_bets else []

    print(f"Supabase: {'OK' if not supabase_problem else supabase_problem}")
    print(f"Workflow checks: {len(wf_rates)}")
    print(f"Calibration buckets: {len(cal_rows)}")
    print(f"Stuck pending: {len(stuck)}")

    if not ALERT_TO:
        print("::error::No recipient configured (SOCIAL_CARD_EMAIL_TO/LOCKS_EMAIL_TO unset) -- can't email this.")
        return 1

    body = build_email_html(supabase_problem, wf_rates, cal_rows, stuck, late_excluded)
    subject = "Clairvoyance — Weekly Health Digest" + (" — SUPABASE ISSUE" if supabase_problem else "")
    ok, msg = send_email(subject, ALERT_TO, body)
    print(f"Digest email sent to {ALERT_TO}" if ok else f"::error::Digest email FAILED: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
