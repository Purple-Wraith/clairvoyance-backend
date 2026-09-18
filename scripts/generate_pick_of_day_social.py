#!/usr/bin/env python3
"""
generate_pick_of_day_social.py — top-3 picks of the day as three separate
Instagram-ready posts (video + still + caption each), emailed for manual
review/posting.

Reuses the existing dormant record_free_pick_reveal()/record_free_pick_still()
templates in generate_video_reveal.py (glitch-reveal aesthetic matching the
daily stats videos) rather than a new combined "top 3 in one graphic" layout
-- a template that's already designed and battle-tested, and single-highlight
posts read better on IG than a dense list crammed into one graphic. Each of
the 3 picks gets its own video, its own still, and its own caption -- three
distinct posts, not one post with three picks in it.

Deliberately does NOT hook into auto_lock_settle.py's live run_lock_segmented()
pipeline (the actual locking pass) -- that's a sensitive, real-money-adjacent
process this script has no business touching. Instead it independently
re-runs gather_legs()/build_qualifying() (both pure read/classify functions,
no locking side effects) against the same live app, purely to get today's
qualifying list for selection. This duplicates a bit of compute but keeps
this script fully decoupled from the live lock pipeline -- a bug or crash
here can never affect real subscriber locks.

Selection: PREMIUM before OPTIMAL/props of either grade (LEAN/SKIP legs
never reach build_qualifying() at all except via the HIGH HIT % exception --
see auto_lock_settle.py), then by EV descending as the tiebreaker within a
tier. Props carry no EV in the underlying data, so they sort after
EV-bearing game legs at the same grade rather than ahead of them.

Usage:
  python3 scripts/generate_pick_of_day_social.py [--no-email] [--out-dir DIR]
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from auto_lock_settle import (  # noqa: E402
    APP_URL, load_bet_ledger, gather_legs, build_qualifying,
    SPORT_DISPLAY_NAME, TIER_LABEL, _prop_matchup_key, log,
)
from _gmail_email import send_email as _send_gmail  # noqa: E402
from _gmail_email import EMAIL_WRAP_OPEN as _EMAIL_WRAP_OPEN, EMAIL_WRAP_CLOSE as _EMAIL_WRAP_CLOSE  # noqa: E402

import os
SOCIAL_CARD_EMAIL_TO = os.environ.get("SOCIAL_CARD_EMAIL_TO", "")

_MT = ZoneInfo("America/Denver")


def _mt_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(_MT)


# Two separate daily emails, 2026-09-17 -- an AM slate and a PM slate (see
# pick-of-day-social-daily.yml's own schedule comment for why: a single
# daily run structurally favored whichever sports lock earliest and
# disregarded NFL/NBA/NHL, which often aren't locked yet by mid-morning).
# Both slates independently call select_top_picks() against whatever's
# CURRENTLY locked and not yet started -- without this file, a matchup
# still genuinely upcoming at both run times (an evening game locked
# early enough to also qualify for the AM slate) could get selected and
# posted twice. Tracks {date, keys} where each key is
# "sport|matchup|pick" for every candidate actually emailed today;
# reset automatically the first time a new date shows up.
_FEATURED_TODAY_PATH = ROOT / "data" / "pick_of_day_featured_today.json"


def _featured_today_keys(today_str: str) -> set[str]:
    try:
        data = json.loads(_FEATURED_TODAY_PATH.read_text())
    except Exception:
        return set()
    if data.get("date") != today_str:
        return set()
    return set(data.get("keys") or [])


def _record_featured_today(today_str: str, new_keys: list[str]) -> None:
    existing = _featured_today_keys(today_str)
    existing.update(new_keys)
    _FEATURED_TODAY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _FEATURED_TODAY_PATH.write_text(json.dumps({"date": today_str, "keys": sorted(existing)}, indent=2))


def _candidate_key(c: dict) -> str:
    return f"{c['sport']}|{c['matchup']}|{c['pick']}"


# Real bug, found 2026-09-17: qualifying legs never carry a kickoff time at
# all (_autoLockCapture's own entry shape has no date field, confirmed by
# reading it in docs/app.html), so this script had no way to tell "already
# started" from "still hours out" -- it just posted whatever ranked #1-3 by
# tier/EV regardless of whether that game's kickoff had already passed by
# the time this workflow actually ran. With SHL/Liiga now real candidates
# (kickoffs as early as 7:15 AM MT) alongside CFB's own early risk (10 AM MT+)
# and 5 of 6 soccer leagues (7:00-10:30 AM MT), and this workflow's own
# primary slot at 10:40 AM MT (often later -- GitHub Actions scheduled-run
# delay is a documented, recurring issue elsewhere in this repo), a real
# top-3-worthy early pick was routinely already live or over by post time.
#
# Fixed by cross-referencing each candidate's matchup against the same
# schedule JSON files the app itself already publishes (docs/*_schedule.json)
# to resolve a real kickoff datetime, then excluding anything already
# started from the candidate pool before ranking -- never just skipping the
# whole run, so a day with an early gem still gets 3 posts, just never one
# for a game already underway. A sport/matchup this can't resolve (no
# schedule file wired up, or a genuine lookup miss) is kept rather than
# dropped -- failing open on missing data, not silently hiding real picks.
_SCHEDULE_FILES: dict[str, str] = {
    "CFB": "cfb_schedule.json", "NFL": "nfl_schedule.json", "NHL": "nhl_schedule.json",
    "LIIGA": "liiga_schedule.json", "SHL": "shl_schedule.json",
}
_SOCCER_LEAGUE_KEY = {
    "SOC_CL": "cl", "SOC_PL": "pl", "SOC_LIGA": "liga", "SOC_BL": "bl",
    "SOC_ITA": "ita", "SOC_MLS": "mls",
}
_schedule_cache: dict[str, list[dict]] = {}


def _flat_games_for_sport(sport: str) -> list[dict]:
    """Returns every {home, away, date} entry available for this sport,
    regardless of each schedule file's own on-disk shape (CFB/NFL nest
    games under weeks; NHL/Liiga/SHL are a flat games list; soccer nests
    under leagues[key])."""
    if sport in _schedule_cache:
        return _schedule_cache[sport]
    games: list[dict] = []
    try:
        if sport in _SCHEDULE_FILES:
            data = json.loads((ROOT / "docs" / _SCHEDULE_FILES[sport]).read_text())
            if "weeks" in data:
                for week_games in (data.get("weeks") or {}).values():
                    games.extend(week_games or [])
            else:
                games.extend(data.get("games") or [])
        elif sport in _SOCCER_LEAGUE_KEY:
            data = json.loads((ROOT / "docs" / "soccer_schedule.json").read_text())
            games.extend((data.get("leagues") or {}).get(_SOCCER_LEAGUE_KEY[sport]) or [])
    except Exception as exc:
        log(f"kickoff-time lookup: couldn't load schedule for {sport}: {exc}")
    _schedule_cache[sport] = games
    return games


def _kickoff_time(sport: str, team_a: str, team_b: str) -> datetime | None:
    """Real kickoff datetime for this matchup, order-independent (team_a/
    team_b may be hA/awA in either order), or None if this sport has no
    wired-up schedule file or the specific matchup can't be found (fails
    open -- see the module comment above).

    Checks both {home,away} (the real identifier _autoLockCapture receives
    for CFB/NFL/NHL -- abbreviations) and {homeName,awayName} (what Liiga/
    SHL's own capture calls pass instead -- their home/away are internal
    Flashscore team ids, not display names, confirmed by reading
    _liigaMatchCard/_shlMatchCard's own _autoLockCapture calls in
    docs/app.html)."""
    wanted = {team_a, team_b}
    for g in _flat_games_for_sport(sport):
        pairs = ({g.get("home"), g.get("away")}, {g.get("homeName"), g.get("awayName")})
        if wanted in pairs and g.get("date"):
            try:
                return datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
            except Exception:
                return None
    return None


def _candidate_from_game(q: dict) -> dict:
    tier_n = q["tierN"]
    return {
        "sport": q["sport"],
        "matchup": f"{q['awA']} @ {q['hA']}",
        "pick": q["label"],
        "grade": TIER_LABEL.get(tier_n, "?"),
        "rank_tier": tier_n,
        "ev": q.get("evVal"),
        "kickoff": _kickoff_time(q["sport"], q["hA"], q["awA"]),
    }


def _candidate_from_prop(q: dict) -> dict:
    leg = q["leg"]
    direction = "UNDER" if leg.get("over") is False else "OVER"
    grade = leg.get("grade") or "?"
    rank_tier = {"PREMIUM": 3, "OPTIMAL": 2}.get(grade, 1)
    team = leg.get("team") or leg.get("hA")
    opp = leg.get("opp") or leg.get("awA")
    return {
        "sport": q["sport"],
        "matchup": _prop_matchup_key(leg),
        "pick": f"{leg.get('player')} {direction} {leg.get('line')} {leg.get('stat')}",
        "grade": grade,
        "rank_tier": rank_tier,
        "ev": None,
        "kickoff": _kickoff_time(q["sport"], team, opp) if team and opp else None,
    }


def select_top_picks(qualifying: list[dict], n: int = 3, exclude_keys: set[str] | None = None) -> list[dict]:
    """PREMIUM before OPTIMAL, then EV descending within a tier. Missing EV
    (every prop, per this file's module docstring) sorts to the bottom of
    its tier rather than the top, so an EV-bearing game leg always outranks
    a same-graded prop.

    Candidates whose real kickoff has already passed by generation time are
    excluded before ranking, not just skipped when picked -- see the
    _kickoff_time block above for why this exists. A candidate this can't
    resolve a kickoff time for (kickoff is None) is kept, not dropped.

    exclude_keys: candidates already featured in an earlier slate today
    (see _candidate_key/_featured_today_keys) -- lets the AM and PM slates
    run independently without either one re-posting the other's pick."""
    now = datetime.now(timezone.utc)
    candidates = [
        _candidate_from_game(q) if q["kind"] == "GAME" else _candidate_from_prop(q)
        for q in qualifying
    ]
    live_candidates = [c for c in candidates if c["kickoff"] is None or c["kickoff"] > now]
    skipped = len(candidates) - len(live_candidates)
    if skipped:
        log(f"Top picks: excluded {skipped} candidate(s) whose game already started")
    if exclude_keys:
        before = len(live_candidates)
        live_candidates = [c for c in live_candidates if _candidate_key(c) not in exclude_keys]
        if before != len(live_candidates):
            log(f"Top picks: excluded {before - len(live_candidates)} candidate(s) already featured in an earlier slate today")
    live_candidates.sort(key=lambda c: (-c["rank_tier"], -(c["ev"] if c["ev"] is not None else -999)))
    return live_candidates[:n]


def build_pick_caption(c: dict, date_str: str) -> str:
    sport_label = SPORT_DISPLAY_NAME.get(c["sport"], c["sport"])
    ev_line = f"Model edge: EV {c['ev']*100:+.1f}%\n\n" if c["ev"] is not None else ""
    return (
        f"🔒 {c['grade']} PICK — {date_str}\n\n"
        f"{sport_label}: {c['matchup']}\n"
        f"{c['pick']}\n\n"
        f"{ev_line}"
        "Model output, not a guarantee -- full reasoning and every graded pick inside.\n\n"
        "clairvoyanceengine.info\n"
        "IG @clairvoyanceengine | X @clairvoyanceeng\n\n"
        "#sportsbetting #bettingpicks #sportsanalytics"
    )


_SLOT_LABEL = {"am": "Morning Slate", "pm": "Afternoon & Evening Slate"}


def send_pick_posts(picks_with_captions: list[dict], out_dir: Path, date_str: str, slot: str | None = None) -> None:
    """One email, three clearly-labeled sections (not three separate
    emails) -- easier to review and post from in one pass. Each section's
    video + still are both attached; the caption is inline, ready to
    copy-paste.

    slot ('am'/'pm'/None): labeled in the subject line so two same-day
    emails (see the AM/PM slate split, module comment above) are
    distinguishable at a glance in an inbox instead of both reading
    identically."""
    if not SOCIAL_CARD_EMAIL_TO:
        log("No recipient set (SOCIAL_CARD_EMAIL_TO) — skipping send")
        return
    all_attachments: list[Path] = []
    sections = []
    for i, item in enumerate(picks_with_captions, start=1):
        all_attachments.append(item["video_path"])
        all_attachments.append(item["still_path"])
        caption_html = item["caption"].replace("\n", "<br>")
        sections.append(
            f'<h3 style="margin:24px 0 4px;color:#1a1a2e">Pick {i} of {len(picks_with_captions)} — '
            f'{item["candidate"]["grade"]} — {item["candidate"]["matchup"]}</h3>'
            f'<p style="color:#555;font-size:13px">Files: {item["video_path"].name}, {item["still_path"].name}</p>'
            f'<div style="background:#14001f;border-radius:6px;padding:12px 16px;color:#eee;'
            f'font-family:monospace;font-size:13px;white-space:pre-wrap">{caption_html}</div>'
        )
    slot_label = _SLOT_LABEL.get(slot or "", "")
    slot_suffix = f" — {slot_label}" if slot_label else ""
    body_html = (
        _EMAIL_WRAP_OPEN +
        f"<p>Top {len(picks_with_captions)} picks for {date_str}{slot_suffix} — one post each, ranked "
        "PREMIUM before OPTIMAL, then by EV.</p>" +
        "".join(sections) +
        _EMAIL_WRAP_CLOSE
    )
    subject = f"Clairvoyance — Top {len(picks_with_captions)} Picks Social Posts for {date_str}{slot_suffix}"
    ok, msg = _send_gmail(subject, SOCIAL_CARD_EMAIL_TO, body_html, attachments=all_attachments)
    if not ok:
        raise RuntimeError(f"Gmail send failed for '{subject}': {msg}")
    log(f"Email sent: {subject} ({len(all_attachments)} attachment(s))")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-email", action="store_true", help="generate video/stills only, skip sending")
    ap.add_argument("--out-dir", default="/tmp/cv_pick_of_day_social")
    ap.add_argument("--app-url", default=APP_URL)
    ap.add_argument("--slot", choices=["am", "pm"], default=None,
                     help="Which daily slate this run is (see pick-of-day-social-daily.yml) -- "
                          "labels the email subject and gates dedup against the OTHER slate's "
                          "already-featured picks today. Omit for a one-off/manual run with no dedup.")
    ap.add_argument("--top-n", type=int, default=3)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    now_mt = _mt_now()
    date_str = now_mt.strftime("%B %-d, %Y")
    today_str = now_mt.strftime("%Y-%m-%d")
    date_tag = now_mt.strftime("%Y%m%d") + (f"-{args.slot}" if args.slot else "")
    already_featured = _featured_today_keys(today_str) if args.slot else set()

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 1000})
        page = context.new_page()
        log(f"Loading {args.app_url} …")
        page.goto(args.app_url, wait_until="load", timeout=60000)
        page.wait_for_timeout(3000)

        bet_count = load_bet_ledger(page)
        log(f"Loaded {bet_count} real bets from Supabase into headless session")

        result = gather_legs(page)
        qualifying = build_qualifying(result)
        log(f"{len(qualifying)} qualifying PREMIUM/OPTIMAL(+HIGH HIT) legs found today")

        browser.close()

    if not qualifying:
        log("No qualifying legs today — nothing to post.")
        return

    top_picks = select_top_picks(qualifying, n=args.top_n, exclude_keys=already_featured)
    if not top_picks:
        log("No pre-kickoff candidates remain after excluding already-started/already-featured games — nothing to post.")
        return
    log(f"Selected top {len(top_picks)}: " +
        "; ".join(f"{c['grade']} {c['matchup']} — {c['pick']}" for c in top_picks))

    from generate_video_reveal import record_free_pick_reveal, record_free_pick_still

    picks_with_captions = []
    for i, c in enumerate(top_picks, start=1):
        sport_label = SPORT_DISPLAY_NAME.get(c["sport"], c["sport"])
        video_path = out_dir / f"cv-pick{i}-{date_tag}.mp4"
        still_path = out_dir / f"cv-pick{i}-still-{date_tag}.png"
        record_free_pick_reveal(sport_label, c["matchup"], c["pick"], c["grade"], video_path, date_str=date_str)
        record_free_pick_still(sport_label, c["matchup"], c["pick"], c["grade"], still_path, date_str=date_str)
        log(f"Pick {i} rendered: {video_path.name}, {still_path.name}")
        picks_with_captions.append({
            "candidate": c,
            "video_path": video_path,
            "still_path": still_path,
            "caption": build_pick_caption(c, date_str),
        })

    if not args.no_email:
        send_pick_posts(picks_with_captions, out_dir, date_str, slot=args.slot)
        if args.slot:
            _record_featured_today(today_str, [_candidate_key(c) for c in top_picks])


if __name__ == "__main__":
    main()
