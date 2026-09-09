#!/usr/bin/env python3
from __future__ import annotations
"""
content_generator.py — Clairvoyance Daily Content Engine (v3)

Generates platform-ready content from data.json — no external API required.

5 daily posting slots (Mountain Time):
  10am   — Morning Preview     → post ~10:00 AM MT
  2pm    — Midday Adjustments  → post ~2:00 PM MT
  445pm  — Pre-Game Window     → post ~4:45 PM MT
  7pm    — Live + Late Slate   → post ~7:00 PM MT
  10pm   — Day Recap           → post ~10:00 PM MT

Output: ~/Desktop/Clairvoyance/YYYY-MM-DD/
  Files: {date}_{Platform}_{time}_{label}.txt / .png
  Example: 2026-05-20_X_10am_morning-preview.txt

Usage:
  python3 scripts/content_generator.py              # auto-detect slot
  python3 scripts/content_generator.py --slot 10am
  python3 scripts/content_generator.py --slot all
  python3 scripts/content_generator.py --print
"""

import argparse, json, os, subprocess, sys
from datetime import datetime, timedelta
from pathlib import Path

# ── .env loader ───────────────────────────────────────────────────────────────
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _v = _v.strip().strip('"').strip("'")
            if _v:
                os.environ.setdefault(_k.strip(), _v)

ROOT        = Path(__file__).parent.parent
FE_DATA     = ROOT / "frontend" / "data.json"
FE_SOCIAL   = ROOT / "frontend" / "social_copy.json"
DC_SOCIAL   = ROOT / "docs"     / "social_copy.json"
DESKTOP_DIR = Path.home() / "Desktop" / "Clairvoyance"
CARD_SCRIPT = Path(__file__).parent / "generate_card.py"

# ── Time (system locale = Mountain Time) ─────────────────────────────────────
NOW_MT       = datetime.now()
HOUR_MT      = NOW_MT.hour
DATE_MT      = NOW_MT.date()
DATE_DISPLAY = NOW_MT.strftime("%B %d, %Y")
DATE_SHORT   = NOW_MT.strftime("%m/%d")
DATE_SLUG    = NOW_MT.strftime("%Y-%m-%d")

# ── Slot system ───────────────────────────────────────────────────────────────
SLOT_NAMES = ["10am", "2pm", "445pm", "7pm", "10pm"]
SLOT_LABELS = {
    "10am":  "Morning Preview",
    "2pm":   "Midday Adjustments",
    "445pm": "Pre-Game Window",
    "7pm":   "Live + Late Slate",
    "10pm":  "Day Recap",
}
SLOT_POST_TIMES = {
    "10am":  "10:00 AM Mountain Time",
    "2pm":   "2:00 PM Mountain Time",
    "445pm": "4:45 PM Mountain Time",
    "7pm":   "7:00 PM Mountain Time",
    "10pm":  "10:00 PM Mountain Time",
}
SLOT_SLUGS = {
    "10am":  "morning-preview",
    "2pm":   "midday",
    "445pm": "pregame",
    "7pm":   "live-update",
    "10pm":  "recap",
}

def detect_slot() -> str:
    if  9 <= HOUR_MT < 13: return "10am"
    if 13 <= HOUR_MT < 16: return "2pm"
    if 16 <= HOUR_MT < 18: return "445pm"
    if 18 <= HOUR_MT < 22: return "7pm"
    return "10pm"

def _file_stem(slot: str, platform_slug: str) -> str:
    return f"{DATE_SLUG}_{platform_slug}_{slot}_{SLOT_SLUGS[slot]}"


# ── EV helpers ────────────────────────────────────────────────────────────────
def _ev_grade(edge_pct: float) -> str:
    if edge_pct >= 12: return "A+"
    if edge_pct >= 8:  return "A"
    if edge_pct >= 4:  return "B"
    if edge_pct >= 1:  return "C"
    return "D"


# ═══════════════════════════════════════════════════════════════════════════════
# LOCAL CONTENT GENERATOR — data-driven, no API required
# ═══════════════════════════════════════════════════════════════════════════════

# ── Data extraction helpers ───────────────────────────────────────────────────

def _games_by_sport(data: dict, state: str | None = None) -> dict[str, list]:
    """Return today's games per sport, optionally filtered by state."""
    out = {}
    for key, sport in [("nba", "NBA"), ("nhl", "NHL")]:
        games = data.get(key, {}).get("today", [])
        if state:
            games = [g for g in games if g.get("state") == state]
        if games:
            out[sport] = games
    return out

def _slate_str(data: dict) -> str:
    counts = {
        sp: len(gs)
        for sp, gs in _games_by_sport(data).items()
    }
    return " · ".join(f"{v} {k}" for k, v in counts.items()) or "No games scheduled"

def _live_games(data: dict) -> list[dict]:
    out = []
    for key, sport in [("nba", "NBA"), ("nhl", "NHL")]:
        for g in data.get(key, {}).get("today", []):
            if g.get("state") == "in":
                out.append({**g, "_sport": sport})
    return out

def _final_games(data: dict) -> list[dict]:
    out = []
    for key, sport in [("nba", "NBA"), ("nhl", "NHL")]:
        for g in data.get(key, {}).get("today", []):
            if g.get("state") == "post":
                out.append({**g, "_sport": sport})
    return out

def _upcoming_games(data: dict) -> list[dict]:
    out = []
    for key, sport in [("nba", "NBA"), ("nhl", "NHL")]:
        for g in data.get(key, {}).get("today", []):
            if g.get("state") not in ("in", "post"):
                out.append({**g, "_sport": sport})
    return out

def _top_pick(data: dict) -> dict | None:
    bets = [b for b in data.get("bestBets", []) if isinstance(b, dict)]
    if not bets:
        return None
    return max(bets, key=lambda b: b.get("edge", 0))

def _all_picks(data: dict) -> list[dict]:
    return [b for b in data.get("bestBets", []) if isinstance(b, dict) and b.get("edge", 0) > 0]

def _top_prop(data: dict) -> dict | None:
    lm = data.get("linemateForm", {})
    best, best_hr = None, -1.0
    for sk in ["nba", "nhl"]:
        for p in lm.get(sk, []):
            if not isinstance(p, dict):
                continue
            try:
                hr = float(str(p.get("hitRate", "0")).rstrip("%"))
            except Exception:
                hr = 0.0
            if hr > best_hr:
                best_hr, best = hr, {**p, "_sport": sk.upper()}
    return best

def _all_props(data: dict) -> list[dict]:
    out = []
    lm = data.get("linemateForm", {})
    for sk in ["nba", "nhl"]:
        for p in lm.get(sk, []):
            if isinstance(p, dict):
                out.append({**p, "_sport": sk.upper()})
    return out

def _record_str(data: dict) -> str | None:
    settled = data.get("settled", [])
    if not settled:
        return None
    wins   = sum(1 for s in settled if s.get("result") == "win")
    losses = sum(1 for s in settled if s.get("result") == "loss")
    pushes = sum(1 for s in settled if s.get("result") == "push")
    units  = sum(s.get("units", 0) for s in settled)
    rec = f"{wins}W-{losses}L"
    if pushes:
        rec += f"-{pushes}P"
    return f"{rec} ({units:+.1f}u)"

def _today_record(data: dict) -> str | None:
    """Record only from today's settled bets."""
    today_str = DATE_MT.strftime("%Y-%m-%d")
    settled = [
        s for s in data.get("settled", [])
        if s.get("date", "").startswith(today_str)
    ]
    if not settled:
        return None
    wins   = sum(1 for s in settled if s.get("result") == "win")
    losses = sum(1 for s in settled if s.get("result") == "loss")
    pushes = sum(1 for s in settled if s.get("result") == "push")
    units  = sum(s.get("units", 0) for s in settled)
    rec = f"{wins}W-{losses}L"
    if pushes:
        rec += f"-{pushes}P"
    return f"{rec} ({units:+.1f}u)"

def _top_weather(data: dict) -> tuple[str, dict] | None:
    weather = data.get("weather", {})
    windy = [
        (k, v) for k, v in weather.items()
        if not v.get("indoor") and (v.get("wind") or 0) >= 12
    ]
    if not windy:
        return None
    return max(windy, key=lambda x: x[1].get("wind", 0))

def _series_notes(data: dict) -> list[str]:
    notes = []
    for key in ["nba", "nhl"]:
        for g in data.get(key, {}).get("today", []):
            s = g.get("seriesNote", "")
            if s:
                notes.append(f"{g.get('away','')} @ {g.get('home','')} — {s}")
    return notes

def _nhl_leader(data: dict) -> str | None:
    mp = data.get("mp", {}).get("teams", {})
    if not mp:
        return None
    teams = [(k, v) for k, v in mp.items() if isinstance(v, dict)]
    if not teams:
        return None
    leader = max(teams, key=lambda x: x[1].get("5on5", {}).get("xgfPct", 0))
    pct = leader[1].get("5on5", {}).get("xgfPct", 0)
    return f"{leader[0]} ({pct:.3f} xGF%)"

# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_pick(pick: dict, short: bool = False) -> str:
    game  = pick.get("game", "")
    p     = pick.get("pick", "")
    mdl   = pick.get("modelProb", pick.get("prob", 0))
    impl  = pick.get("impliedProb", pick.get("implied", 0))
    edge  = pick.get("edge", round((mdl - impl) * 100, 1) if mdl and impl else 0)
    grade = _ev_grade(edge)
    if short:
        return f"{game} → {p} | EV {grade} ({edge:+.1f}%)"
    return (
        f"{game} → {p}\n"
        f"Model: {mdl*100:.1f}% | Market: {impl*100:.1f}% | Edge: {edge:+.1f}% | EV {grade}"
    )

def _fmt_prop(prop: dict) -> str:
    name  = prop.get("player", "")
    cat   = prop.get("category", "")
    line  = prop.get("line", "")
    hr    = prop.get("hitRate", "")
    trend = prop.get("trend", "")
    sport = prop.get("_sport", "")
    ev    = prop.get("ev", "")
    return f"{sport} — {name}: {cat} {line} | Hit {hr} | Trend: {trend}{' | '+ev if ev else ''}"

def _fmt_score(g: dict) -> str:
    away  = g.get("away", "")
    home  = g.get("home", "")
    a_s   = g.get("awayScore", 0)
    h_s   = g.get("homeScore", 0)
    state = g.get("state", "pre")
    if state == "post":
        return f"{away} {a_s} — {home} {h_s}  FINAL"
    if state == "in":
        per = g.get("period", "")
        clk = g.get("displayClock", "")
        return f"{away} {a_s} — {home} {h_s}  {per} {clk}".rstrip()
    return f"{away} @ {home}"

def _trunc(text: str, limit: int = 280) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"

_IG_TAGS = ["foryou", "sportsbetting", "bettingtips", "bettingpicks"]


# ── Slot builders ─────────────────────────────────────────────────────────────

def _build_morning(data: dict) -> dict:
    slate   = _slate_str(data)
    pick    = _top_pick(data)
    picks   = _all_picks(data)
    prop    = _top_prop(data)
    record  = _record_str(data)
    weather = _top_weather(data)
    series  = _series_notes(data)

    # ── X post ────────────────────────────────────────────────────────────────
    if pick:
        mdl   = pick.get("modelProb", pick.get("prob", 0))
        impl  = pick.get("impliedProb", pick.get("implied", 0))
        edge  = pick.get("edge", round((mdl - impl) * 100, 1) if mdl and impl else 0)
        grade = _ev_grade(edge)
        game  = pick.get("game", "")
        p     = pick.get("pick", "")
        x_post = _trunc(
            f"Morning model — {DATE_SHORT} | {slate}\n"
            f"Top edge: {game} → {p} | {mdl*100:.0f}% model / {impl*100:.0f}% market"
            f" / {edge:+.1f}% edge | EV {grade}"
        )
    else:
        x_post = _trunc(
            f"Morning model — {DATE_SHORT} | {slate}\n"
            f"No edges above threshold at open. Monitoring line movement through the morning."
        )

    # ── Thread ────────────────────────────────────────────────────────────────
    n_picks = len(picks)
    tw1 = f"Morning model sweep — {DATE_DISPLAY}\n\n{slate}"
    if record:
        tw1 += f"\nSeason: {record}"
    tw1 += f"\n\n{n_picks} edge{'s' if n_picks != 1 else ''} flagged above market implied ↓"

    if pick:
        mdl   = pick.get("modelProb", pick.get("prob", 0))
        impl  = pick.get("impliedProb", pick.get("implied", 0))
        edge  = pick.get("edge", round((mdl - impl) * 100, 1) if mdl and impl else 0)
        grade = _ev_grade(edge)
        conf  = pick.get("confidence", "")
        tw2 = (
            f"Top read: {_fmt_pick(pick)}"
            + (f"\n{conf}" if conf else "")
        )
        if len(picks) > 1:
            p2 = picks[1]
            e2 = p2.get("edge", 0)
            g2 = _ev_grade(e2)
            tw2 += f"\n\nAlso tracking: {p2.get('game','')} → {p2.get('pick','')} | EV {g2} ({e2:+.1f}%)"
    else:
        tw2 = (
            f"No model edges above confidence threshold at open.\n\n"
            f"Slate: {slate}\n\nLines and injury updates tracking through 2 PM midday post."
        )

    if prop:
        tw3 = f"Prop spotlight:\n{_fmt_prop(prop)}"
        all_p = _all_props(data)
        if len(all_p) > 1:
            tw3 += f"\n\nAlso flagged: {_fmt_prop(all_p[1])}"
    elif weather:
        team, w = weather
        tw3 = (
            f"Weather factor — {team}: {w.get('wind')} mph, {w.get('temp')}°F\n\n"
            f"Wind direction and speed affect outdoor totals. "
            f"Model accounts for park + conditions in O/U projections."
        )
    else:
        tw3 = (
            f"Prop data refreshing with today's matchups.\n\n"
            f"Model tracks hit rate trends, line movement direction, and closing value "
            f"across NBA and NHL prop markets."
        )

    if series:
        tw4 = "Playoff context:\n\n" + "\n".join(f"• {s}" for s in series[:4])
        tw4 += "\n\nNext: Midday adjustments at 2 PM MT."
    elif weather:
        team, w = weather
        tw4 = (
            f"Weather: {team} park — {w.get('wind')} mph, {w.get('temp')}°F\n\n"
            f"Condition: {w.get('condition','')}\n\n"
            f"Next update: 2 PM MT midday read."
        )
    else:
        nhl = _nhl_leader(data)
        tw4 = "Model data refreshes 6x daily. Next: Midday adjustments at 2 PM MT."
        if nhl:
            tw4 = f"NHL 5v5 xGF% leader: {nhl}\n\nExpected goals differential is the strongest team-level predictor in model. Next: 2 PM MT."

    # ── Instagram ─────────────────────────────────────────────────────────────
    if pick:
        mdl   = pick.get("modelProb", pick.get("prob", 0))
        impl  = pick.get("impliedProb", pick.get("implied", 0))
        edge  = pick.get("edge", round((mdl - impl) * 100, 1) if mdl and impl else 0)
        grade = _ev_grade(edge)
        ig = (
            f"Morning model sweep for {DATE_DISPLAY}. {slate} across professional sports — "
            f"the engine flagged {n_picks} edge{'s' if n_picks != 1 else ''} above market implied. "
            f"Top read: {pick.get('game','')} → {pick.get('pick','')}, "
            f"where the model projects {mdl*100:.1f}% probability against a market-implied {impl*100:.1f}%, "
            f"a {edge:+.1f}% edge rated EV {grade}. "
            f"All projections update with line movement throughout the day."
        )
    else:
        ig = (
            f"Morning model sweep for {DATE_DISPLAY}. {slate} across professional sports today. "
            f"No edges above the confidence threshold at open — the engine is monitoring line movement "
            f"and injury reports. Next read at 2 PM Mountain."
        )

    # ── Story bullets ─────────────────────────────────────────────────────────
    bullets = []
    if pick:
        edge = pick.get("edge", 0)
        bullets.append(f"Top edge: {pick.get('game','')[:22]} {edge:+.0f}%")
    bullets.append(slate)
    if record:
        bullets.append(f"Season record: {record}")
    elif weather:
        team, w = weather
        bullets.append(f"Wind alert: {w.get('wind')} mph at {team}")
    bullets.append("Next update: 2 PM Mountain Time")
    bullets = [b[:50] for b in bullets[:3]]

    return {
        "x_post":              x_post,
        "x_thread":            [tw1, tw2, tw3, tw4],
        "instagram_caption":   ig,
        "instagram_hashtags":  _IG_TAGS,
        "story_bullets":       bullets,
        "content_theme":       "morning_preview",
        "highlight_game":      pick.get("game") if pick else None,
    }


def _build_midday(data: dict) -> dict:
    slate   = _slate_str(data)
    pick    = _top_pick(data)
    picks   = _all_picks(data)
    prop    = _top_prop(data)
    live    = _live_games(data)
    finals  = _final_games(data)
    record  = _record_str(data)
    weather = _top_weather(data)

    # ── X post ────────────────────────────────────────────────────────────────
    if live:
        g = live[0]
        x_post = _trunc(
            f"Midday — {DATE_SHORT} | {len(live)} game{'s' if len(live)>1 else ''} live\n"
            f"{_fmt_score(g)}"
            + (f" · {_fmt_score(live[1])}" if len(live) > 1 else "")
            + (f"\nModel still tracking {len(picks)} edge{'s' if len(picks)!=1 else ''} for tonight." if picks else "")
        )
    elif pick:
        mdl  = pick.get("modelProb", pick.get("prob", 0))
        impl = pick.get("impliedProb", pick.get("implied", 0))
        edge = pick.get("edge", round((mdl - impl) * 100, 1) if mdl and impl else 0)
        grade = _ev_grade(edge)
        x_post = _trunc(
            f"Midday read — {DATE_SHORT}\n"
            f"Top edge holding: {pick.get('game','')} → {pick.get('pick','')} | "
            f"{mdl*100:.0f}% model / {impl*100:.0f}% market | EV {grade}"
        )
    else:
        x_post = _trunc(
            f"Midday update — {DATE_SHORT} | {slate}\n"
            f"No new edges above threshold. Monitoring lines ahead of tonight's slate."
        )

    # ── Thread ────────────────────────────────────────────────────────────────
    n_final = len(finals)
    tw1 = f"Midday check — {DATE_DISPLAY}\n\n{slate}"
    if n_final:
        tw1 += f"\n{n_final} game{'s' if n_final>1 else ''} final"
    if live:
        tw1 += f" · {len(live)} live"
    if record:
        tw1 += f"\n\nSeason: {record}"

    if finals:
        final_lines = [_fmt_score(g) for g in finals[:4]]
        tw2 = "Results so far:\n\n" + "\n".join(final_lines)
        if len(finals) > 4:
            tw2 += f"\n+ {len(finals)-4} more"
    elif live:
        live_lines = [_fmt_score(g) for g in live[:4]]
        tw2 = "Live right now:\n\n" + "\n".join(live_lines)
    else:
        tw2 = "No results yet — games begin this afternoon/evening.\n\nModel is watching for any line movement that closes the gap on flagged edges."

    if pick:
        mdl   = pick.get("modelProb", pick.get("prob", 0))
        impl  = pick.get("impliedProb", pick.get("implied", 0))
        edge  = pick.get("edge", round((mdl - impl) * 100, 1) if mdl and impl else 0)
        grade = _ev_grade(edge)
        tw3 = (
            f"Model still tracking:\n{_fmt_pick(pick)}\n\n"
            f"Line movement direction: model edge {'widening' if edge > 8 else 'stable' if edge > 3 else 'narrowing — watch closing line'}."
        )
    else:
        tw3 = "No edges above confidence threshold midday.\n\nNext read at 4:45 PM — pre-game final model outputs before tonight's action."

    if prop:
        tw4 = (
            f"Prop update:\n{_fmt_prop(prop)}\n\n"
            f"Check closing line at 4:45 PM for final value assessment."
        )
    elif weather:
        team, w = weather
        tw4 = (
            f"Weather update — {team}: {w.get('wind')} mph, {w.get('temp')}°F\n"
            f"Condition: {w.get('condition','')}\n\n"
            f"Model adjusts totals projections for wind direction. Next: 4:45 PM pre-game."
        )
    else:
        tw4 = "Pre-game post at 4:45 PM MT — final model reads before tonight's slate. Accountability recap at 10 PM."

    # ── Instagram ─────────────────────────────────────────────────────────────
    if finals:
        ig = (
            f"Midday check for {DATE_DISPLAY}. "
            f"{n_final} game{'s' if n_final>1 else ''} final so far with {slate} on today's slate. "
        )
    else:
        ig = f"Midday model check for {DATE_DISPLAY}. {slate} on today's card. "

    if pick:
        mdl   = pick.get("modelProb", pick.get("prob", 0))
        edge  = pick.get("edge", 0)
        grade = _ev_grade(edge)
        ig += (
            f"Top edge remains: {pick.get('game','')} → {pick.get('pick','')}, "
            f"model at {mdl*100:.1f}% against market implied — EV {grade}. "
            f"Pre-game final read drops at 4:45 PM."
        )
    else:
        ig += "No edges above threshold at midday. Final model outputs before tonight's games at 4:45 PM Mountain."

    bullets = []
    if live:
        bullets.append(f"{len(live)} game{'s' if len(live)>1 else ''} live now")
    elif finals:
        bullets.append(f"{n_final} game{'s' if n_final>1 else ''} final")
    if pick:
        edge = pick.get("edge", 0)
        bullets.append(f"Top edge: EV {_ev_grade(edge)} ({edge:+.0f}%)")
    bullets.append(slate)
    bullets.append("Pre-game post: 4:45 PM Mountain")
    bullets = [b[:50] for b in bullets[:3]]

    return {
        "x_post":             x_post,
        "x_thread":           [tw1, tw2, tw3, tw4],
        "instagram_caption":  ig,
        "instagram_hashtags": _IG_TAGS,
        "story_bullets":      bullets,
        "content_theme":      "midday_adjustment",
        "highlight_game":     pick.get("game") if pick else None,
    }


def _build_pregame(data: dict) -> dict:
    picks    = _all_picks(data)
    pick     = _top_pick(data)
    prop     = _top_prop(data)
    upcoming = _upcoming_games(data)
    live     = _live_games(data)
    weather  = _top_weather(data)
    record   = _record_str(data)
    series   = _series_notes(data)

    n_tonight = len(upcoming)
    tonight_str = f"{n_tonight} game{'s' if n_tonight != 1 else ''} tonight"
    if live:
        tonight_str += f" · {len(live)} live"

    # ── X post ────────────────────────────────────────────────────────────────
    if pick:
        mdl   = pick.get("modelProb", pick.get("prob", 0))
        impl  = pick.get("impliedProb", pick.get("implied", 0))
        edge  = pick.get("edge", round((mdl - impl) * 100, 1) if mdl and impl else 0)
        grade = _ev_grade(edge)
        x_post = _trunc(
            f"Pre-game — {DATE_SHORT} | {tonight_str}\n"
            f"Final read: {pick.get('game','')} → {pick.get('pick','')} | "
            f"{mdl*100:.0f}% model / {impl*100:.0f}% market / EV {grade}"
        )
    else:
        x_post = _trunc(
            f"Pre-game — {DATE_SHORT} | {tonight_str}\n"
            f"No edges above threshold at close. Model tracking {n_tonight} upcoming game{'s' if n_tonight!=1 else ''}."
        )

    # ── Thread ────────────────────────────────────────────────────────────────
    tw1 = f"Pre-game model — {DATE_DISPLAY}\n\n{tonight_str}"
    if record:
        tw1 += f"\nSeason: {record}"
    if upcoming:
        game_lines = [f"• {g.get('away','')} @ {g.get('home','')} ({g.get('_sport','')})" for g in upcoming[:5]]
        tw1 += "\n\n" + "\n".join(game_lines)

    if pick:
        mdl   = pick.get("modelProb", pick.get("prob", 0))
        impl  = pick.get("impliedProb", pick.get("implied", 0))
        edge  = pick.get("edge", round((mdl - impl) * 100, 1) if mdl and impl else 0)
        grade = _ev_grade(edge)
        conf  = pick.get("confidence", "")
        tw2 = f"Final model read:\n{_fmt_pick(pick)}"
        if conf:
            tw2 += f"\n{conf}"
        if len(picks) > 1:
            p2    = picks[1]
            e2    = p2.get("edge", 0)
            g2    = _ev_grade(e2)
            tw2 += f"\n\nAlso tracking: {p2.get('game','')} → {p2.get('pick','')} | EV {g2}"
    else:
        tw2 = (
            f"No edges flagged above confidence threshold at closing lines.\n\n"
            f"{n_tonight} games on tonight's slate. Model will monitor live."
        )

    if prop:
        tw3 = f"Prop spotlight — closing line value:\n{_fmt_prop(prop)}"
    elif weather:
        team, w = weather
        tw3 = (
            f"Weather factor at game time:\n"
            f"{team}: {w.get('wind')} mph, {w.get('temp')}°F — {w.get('condition','')}\n\n"
            f"Outdoor park wind affects total projection. Model adjusted."
        )
    else:
        tw3 = "No high-confidence props at closing line.\n\nModel tracks live win probability through game — live update at 7 PM MT."

    if series:
        tw4 = "Tonight's playoff context:\n\n" + "\n".join(f"• {s}" for s in series[:4])
        tw4 += "\n\nLive update: 7 PM MT."
    else:
        tw4 = "Live + late slate post at 7 PM MT. Full day recap at 10 PM.\n\nAll projections are probabilistic — results tracked transparently."

    # ── Instagram ─────────────────────────────────────────────────────────────
    if pick:
        mdl   = pick.get("modelProb", pick.get("prob", 0))
        impl  = pick.get("impliedProb", pick.get("implied", 0))
        edge  = pick.get("edge", round((mdl - impl) * 100, 1) if mdl and impl else 0)
        grade = _ev_grade(edge)
        ig = (
            f"Pre-game model output for {DATE_DISPLAY}. {tonight_str} remaining on the slate. "
            f"The engine's final read before tip-off: {pick.get('game','')} → {pick.get('pick','')} — "
            f"model {mdl*100:.1f}% vs. market implied {impl*100:.1f}%, an edge of {edge:+.1f}% rated EV {grade}. "
            f"Live update at 7 PM Mountain as the action unfolds."
        )
    else:
        ig = (
            f"Pre-game model output for {DATE_DISPLAY}. {tonight_str} on the card. "
            f"No edges above the confidence threshold at closing lines — the model will track live win probability "
            f"as games are in progress. Live update at 7 PM Mountain."
        )

    bullets = []
    if pick:
        edge = pick.get("edge", 0)
        bullets.append(f"Final edge: EV {_ev_grade(edge)} ({edge:+.0f}%)")
    bullets.append(f"{tonight_str}")
    if series:
        bullets.append(series[0][:45])
    elif weather:
        team, w = weather
        bullets.append(f"{w.get('wind')} mph at {team}")
    bullets.append("Live update: 7 PM Mountain")
    bullets = [b[:50] for b in bullets[:3]]

    return {
        "x_post":             x_post,
        "x_thread":           [tw1, tw2, tw3, tw4],
        "instagram_caption":  ig,
        "instagram_hashtags": _IG_TAGS,
        "story_bullets":      bullets,
        "content_theme":      "pregame",
        "highlight_game":     pick.get("game") if pick else None,
    }


def _build_live(data: dict) -> dict:
    live     = _live_games(data)
    finals   = _final_games(data)
    upcoming = _upcoming_games(data)
    pick     = _top_pick(data)
    picks    = _all_picks(data)
    prop     = _top_prop(data)
    record   = _record_str(data)

    # ── X post ────────────────────────────────────────────────────────────────
    if live:
        score_lines = [_fmt_score(g) for g in live[:2]]
        header = f"Live — {DATE_SHORT} | {len(live)} in progress"
        x_post = _trunc(header + "\n" + "\n".join(score_lines))
    elif finals:
        score_lines = [_fmt_score(g) for g in finals[:2]]
        x_post = _trunc(
            f"Evening — {DATE_SHORT} | {len(finals)} final"
            + (f" · {len(upcoming)} upcoming" if upcoming else "")
            + "\n" + "\n".join(score_lines)
        )
    else:
        x_post = _trunc(
            f"Games coming up — {DATE_SHORT}\n"
            + "\n".join(f"{g.get('away','')} @ {g.get('home','')} ({g.get('_sport','')})" for g in upcoming[:3])
        )

    # ── Thread ────────────────────────────────────────────────────────────────
    tw1 = f"Live model — {DATE_DISPLAY}"
    if live:
        tw1 += f"\n\n{len(live)} game{'s' if len(live)>1 else ''} in progress:"
        for g in live[:4]:
            tw1 += f"\n• {_fmt_score(g)}"
    elif finals:
        tw1 += f"\n\n{len(finals)} game{'s' if len(finals)>1 else ''} final:"
        for g in finals[:4]:
            tw1 += f"\n• {_fmt_score(g)}"
    if upcoming:
        tw1 += f"\n\n{len(upcoming)} still to come"

    # Track picks vs live results
    if pick and live:
        # Check if top pick's game is live
        pick_game = pick.get("game", "").upper().replace(" ", "")
        matching = [
            g for g in live
            if f"{g.get('away','')}@{g.get('home','')}".upper().replace(" ", "") in pick_game
            or pick_game in f"{g.get('away','')}@{g.get('home','')}".upper().replace(" ", "")
        ]
        if matching:
            g = matching[0]
            a_s, h_s = g.get("awayScore", 0), g.get("homeScore", 0)
            per = g.get("period", "")
            clk = g.get("displayClock", "")
            tw2 = (
                f"Tracked pick live:\n{_fmt_pick(pick, short=True)}\n\n"
                f"Current: {_fmt_score(g)} | {per} {clk}\n"
                f"Model tracking win probability in real time."
            )
        else:
            tw2 = f"Tracked pick:\n{_fmt_pick(pick)}"
    elif pick:
        mdl   = pick.get("modelProb", pick.get("prob", 0))
        impl  = pick.get("impliedProb", pick.get("implied", 0))
        edge  = pick.get("edge", round((mdl - impl) * 100, 1) if mdl and impl else 0)
        grade = _ev_grade(edge)
        tw2 = (
            f"Model pick pending:\n{_fmt_pick(pick)}\n\n"
            f"Game not yet live. Edge: {edge:+.1f}% | EV {grade}"
        )
    else:
        tw2 = (
            f"No model edges flagged tonight.\n\n"
            f"Watching: {len(live)} live · {len(upcoming)} upcoming · {len(finals)} final"
        )

    # Late slate preview
    if upcoming:
        late_lines = [
            f"• {g.get('away','')} @ {g.get('home','')} ({g.get('_sport','')})"
            for g in upcoming[:4]
        ]
        tw3 = f"Still to come tonight:\n\n" + "\n".join(late_lines)
        if pick and not live:
            tw3 += f"\n\nModel tracking: {pick.get('game','')} → {pick.get('pick','')}"
    elif prop:
        tw3 = f"Live prop to watch:\n{_fmt_prop(prop)}"
    else:
        tw3 = f"Full late slate underway.\n\nModel win probability updates in real time. Full results and accuracy recap at 10 PM MT."

    tw4 = "Full day recap at 10 PM MT — results, accuracy, units, and forward look.\n\nAll picks tracked transparently. Season record updated after finals."
    if record:
        tw4 = f"Season record so far: {record}\n\nFull today recap at 10 PM MT."

    # ── Instagram ─────────────────────────────────────────────────────────────
    if live:
        ig = (
            f"Live model update for {DATE_DISPLAY}. "
            f"{len(live)} game{'s' if len(live)>1 else ''} in progress across the slate. "
        )
        if pick:
            ig += f"The engine is tracking {pick.get('game','')} live — {pick.get('pick','')} was the flagged edge. "
        ig += f"Full accountability recap at 10 PM Mountain with results, accuracy, and units."
    elif upcoming:
        ig = (
            f"Evening model check — {DATE_DISPLAY}. "
            f"{len(upcoming)} game{'s' if len(upcoming)>1 else ''} remaining on tonight's card"
            + (f", {len(finals)} final" if finals else "") + ". "
        )
        if pick:
            mdl  = pick.get("modelProb", pick.get("prob", 0))
            edge = pick.get("edge", 0)
            ig += (
                f"Model edge: {pick.get('game','')} → {pick.get('pick','')} at {mdl*100:.0f}% model probability. "
            )
        ig += "Recap at 10 PM Mountain."
    else:
        ig = (
            f"Evening update for {DATE_DISPLAY}. Games wrapping up — full accountability recap at 10 PM Mountain. "
            f"Results, model accuracy, and units tracked transparently."
        )

    bullets = []
    if live:
        bullets.append(f"{len(live)} game{'s' if len(live)>1 else ''} live right now")
    if finals:
        bullets.append(f"{len(finals)} final{'s' if len(finals)>1 else ''} so far")
    if upcoming:
        bullets.append(f"{len(upcoming)} game{'s' if len(upcoming)>1 else ''} still to come")
    if record:
        bullets.append(f"Record: {record}")
    bullets.append("Full recap: 10 PM Mountain")
    bullets = [b[:50] for b in bullets[:3]]

    return {
        "x_post":             x_post,
        "x_thread":           [tw1, tw2, tw3, tw4],
        "instagram_caption":  ig,
        "instagram_hashtags": _IG_TAGS,
        "story_bullets":      bullets,
        "content_theme":      "live_update",
        "highlight_game":     pick.get("game") if pick else (live[0].get("away","") + " @ " + live[0].get("home","") if live else None),
    }


def _build_recap(data: dict) -> dict:
    finals    = _final_games(data)
    live      = _live_games(data)
    picks     = _all_picks(data)
    pick      = _top_pick(data)
    record    = _record_str(data)
    today_rec = _today_record(data)
    settled   = data.get("settled", [])
    nba_tom   = data.get("nba", {}).get("tomorrow", [])

    n_final = len(finals)
    n_total = n_final + len(live)

    # ── X post ────────────────────────────────────────────────────────────────
    if today_rec:
        x_post = _trunc(
            f"Day recap — {DATE_SHORT} | Today: {today_rec}"
            + (f" | Season: {record}" if record and record != today_rec else "")
            + (f" | {n_final} games final" if n_final else "")
        )
    elif finals:
        score_lines = [_fmt_score(g) for g in finals[:2]]
        x_post = _trunc(
            f"Day recap — {DATE_SHORT} | {n_final} final\n" + "\n".join(score_lines)
        )
    else:
        x_post = _trunc(
            f"Day recap — {DATE_SHORT} | Model tracking complete. Season: {record or 'Accumulating'}"
        )

    # ── Thread ────────────────────────────────────────────────────────────────
    tw1 = f"Day recap — {DATE_DISPLAY}"
    if today_rec:
        tw1 += f"\n\nToday: {today_rec}"
    if record:
        tw1 += f"\nSeason: {record}"
    tw1 += f"\n\n{n_final} game{'s' if n_final!=1 else ''} final"
    if live:
        tw1 += f" · {len(live)} still in progress"

    if finals:
        result_lines = [f"• {_fmt_score(g)}" for g in finals[:6]]
        tw2 = "Final results:\n\n" + "\n".join(result_lines)
        if len(finals) > 6:
            tw2 += f"\n+ {len(finals)-6} more"
    else:
        tw2 = "Scores still pending.\n\nModel accuracy and units will update once all games are final."

    # Model accuracy for the day
    today_str  = DATE_MT.strftime("%Y-%m-%d")
    today_bets = [s for s in settled if s.get("date", "").startswith(today_str)]
    if today_bets:
        wins   = sum(1 for s in today_bets if s.get("result") == "win")
        losses = sum(1 for s in today_bets if s.get("result") == "loss")
        units  = sum(s.get("units", 0) for s in today_bets)
        win_pct = wins / len(today_bets) * 100 if today_bets else 0
        best_today = max(today_bets, key=lambda s: s.get("units", 0), default=None)
        tw3 = (
            f"Model performance today:\n\n"
            f"{wins}W-{losses}L | {units:+.1f} units | {win_pct:.0f}% accuracy\n"
        )
        if best_today and best_today.get("result") == "win":
            tw3 += f"\nBest call: {best_today.get('game','')} → {best_today.get('pick','')} ({best_today.get('units',0):+.1f}u)"
    elif picks:
        tw3 = (
            f"Model tracked {len(picks)} edge{'s' if len(picks)!=1 else ''} today.\n\n"
            + "\n".join(_fmt_pick(p, short=True) for p in picks[:3])
            + "\n\nResults pending final scores."
        )
    else:
        tw3 = (
            f"No edges above threshold today — model correctly identified a low-confidence slate.\n\n"
            f"Discipline is part of the edge. Season: {record or 'Accumulating'}."
        )

    # Forward look
    tw4 = "Tomorrow:\n\n"
    if nba_tom:
        tw4 += f"NBA — {len(nba_tom)} games\n"
        for g in nba_tom[:3]:
            tw4 += f"• {g.get('away','')} @ {g.get('home','')}\n"
    tw4 += "\nMorning preview at 10 AM MT."

    # ── Instagram ─────────────────────────────────────────────────────────────
    if today_rec and n_final:
        ig = (
            f"Day recap for {DATE_DISPLAY}. {n_final} game{'s' if n_final!=1 else ''} final. "
            f"Today's model record: {today_rec}. "
        )
    else:
        ig = f"Day recap for {DATE_DISPLAY}. {n_final} game{'s' if n_final!=1 else ''} final. "

    if record:
        ig += f"Season: {record}. "
    ig += (
        f"The engine tracks every projection transparently — results, accuracy, and units "
        f"updated in real time. Morning preview tomorrow at 10 AM Mountain."
    )

    bullets = []
    if today_rec:
        bullets.append(f"Today: {today_rec}")
    if record:
        bullets.append(f"Season: {record}")
    bullets.append(f"{n_final} game{'s' if n_final!=1 else ''} final today")
    if nba_tom:
        bullets.append(f"Tomorrow: {len(nba_tom)} NBA games")
    bullets.append("Morning preview: 10 AM Mountain")
    bullets = [b[:50] for b in bullets[:3]]

    return {
        "x_post":             x_post,
        "x_thread":           [tw1, tw2, tw3, tw4],
        "instagram_caption":  ig,
        "instagram_hashtags": _IG_TAGS,
        "story_bullets":      bullets,
        "content_theme":      "recap",
        "highlight_game":     None,
    }


# ── Main dispatcher ───────────────────────────────────────────────────────────
_SLOT_BUILDERS = {
    "10am":  _build_morning,
    "2pm":   _build_midday,
    "445pm": _build_pregame,
    "7pm":   _build_live,
    "10pm":  _build_recap,
}

def generate_content(data: dict, slot: str, verbose: bool = False) -> dict:
    """Generate content from data.json — no external API required."""
    builder = _SLOT_BUILDERS.get(slot)
    if not builder:
        print(f"[ERROR] Unknown slot: {slot}", file=sys.stderr)
        return {}
    try:
        result = builder(data)
        result["generated_at"] = NOW_MT.strftime("%Y-%m-%d %H:%M MT")
        result["slot"]         = slot
        result["slot_label"]   = SLOT_LABELS[slot]
        return result
    except Exception as exc:
        print(f"[ERROR] Content generation failed for slot {slot}: {exc}", file=sys.stderr)
        if verbose:
            import traceback
            traceback.print_exc()
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT WRITERS
# ═══════════════════════════════════════════════════════════════════════════════

def _header(slot: str, platform_name: str, handle: str) -> list[str]:
    return [
        "═" * 62,
        f"  PLATFORM :  {platform_name}  ({handle})",
        f"  POST TIME:  {SLOT_POST_TIMES[slot]}",
        f"  DATE     :  {DATE_DISPLAY}",
        f"  SLOT     :  {SLOT_LABELS[slot]}",
        "═" * 62,
        "",
    ]


def _write_x_file(path: Path, content: dict) -> None:
    slot   = content.get("slot", "10am")
    x_post = content.get("x_post", "")
    thread = content.get("x_thread", [])
    lines  = _header(slot, "X (Twitter)", "@ClairvoyanceEng")
    lines += [
        f"POST  ({len(x_post)}/280 chars — copy and paste directly):",
        "─" * 62,
        x_post,
        "",
        "─" * 62,
        "THREAD  (post as reply chain under the above post):",
        "─" * 62,
    ]
    for i, tweet in enumerate(thread, 1):
        lines.append(f"[{i}]  {tweet}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_ig_file(path: Path, content: dict) -> None:
    slot     = content.get("slot", "10am")
    caption  = content.get("instagram_caption", "")
    hashtags = " ".join(f"#{t.lstrip('#')}" for t in content.get("instagram_hashtags", []))
    bullets  = content.get("story_bullets", [])
    lines    = _header(slot, "Instagram", "@clairvoyanceengine")
    lines   += [
        "CAPTION  (copy and paste):",
        "─" * 62,
        caption,
        "",
        "─" * 62,
        "HASHTAGS  (paste in first comment or end of caption):",
        "─" * 62,
        hashtags,
        "",
        "─" * 62,
        "STORY BULLETS  (use for Stories / carousel chips):",
        "─" * 62,
    ]
    for b in bullets:
        lines.append(f"  •  {b}")
    path.write_text("\n".join(lines), encoding="utf-8")


def _generate_card(out_path: Path, platform: str) -> None:
    cmd = [
        sys.executable, str(CARD_SCRIPT),
        "--platform", platform,
        "--output", str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[WARN] Card failed ({platform}): {result.stderr[:200]}", file=sys.stderr)
    elif out_path.exists():
        kb = out_path.stat().st_size // 1024
        print(f"[INFO] {out_path.name} ({kb} KB)")


def write_desktop_output(content: dict) -> None:
    if not content:
        return

    slot     = content.get("slot", "10am")
    date_dir = DESKTOP_DIR / DATE_SLUG
    date_dir.mkdir(parents=True, exist_ok=True)

    x_stem  = _file_stem(slot, "X")
    ig_stem = _file_stem(slot, "Instagram")

    _write_x_file( date_dir / f"{x_stem}.txt",  content)
    _write_ig_file(date_dir / f"{ig_stem}.txt", content)

    _generate_card(date_dir / f"{x_stem}.png",  "x")
    _generate_card(date_dir / f"{ig_stem}.png", "instagram")

    print(f"[INFO] → {date_dir}")


def write_social_json(content: dict) -> None:
    if not content:
        return
    payload = json.dumps(content, indent=2)
    FE_SOCIAL.write_text(payload)
    DC_SOCIAL.write_text(payload)


def print_content(content: dict) -> None:
    if not content:
        return
    print(f"\n{'='*62}")
    print(f"SLOT:  {content.get('slot_label','').upper()}  |  {content.get('generated_at','')}")
    print(f"THEME: {content.get('content_theme','')}")
    print(f"{'='*62}")
    x_post = content.get("x_post", "")
    print(f"\nX POST ({len(x_post)} chars):\n  {x_post}")
    print(f"\nX THREAD:")
    for i, t in enumerate(content.get("x_thread", []), 1):
        print(f"  [{i}] {t}")
    print(f"\nINSTAGRAM:\n  {content.get('instagram_caption','')}")
    print(f"  #{' #'.join(content.get('instagram_hashtags',[]))}")
    print(f"\nSTORY BULLETS:")
    for b in content.get("story_bullets", []):
        print(f"  • {b}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Clairvoyance Content Generator")
    parser.add_argument(
        "--slot", choices=SLOT_NAMES + ["all"],
        help="Slot to generate (default: auto-detect from MT time)",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--print",   action="store_true", dest="print_output")
    args = parser.parse_args()

    if not FE_DATA.exists():
        print("[ERROR] data.json not found — run clairvoyance_update.py first", file=sys.stderr)
        sys.exit(1)

    data  = json.loads(FE_DATA.read_text())
    slots = SLOT_NAMES if args.slot == "all" else [args.slot or detect_slot()]

    any_ok = False
    for slot in slots:
        print(f"[INFO] Generating: {slot} ({SLOT_LABELS[slot]})")
        content = generate_content(data, slot, verbose=args.verbose)
        if content:
            write_desktop_output(content)
            write_social_json(content)
            if args.print_output or args.verbose:
                print_content(content)
            any_ok = True
        else:
            print(f"[WARN] No content generated for: {slot}", file=sys.stderr)

    sys.exit(0 if any_ok else 1)


if __name__ == "__main__":
    main()
