#!/usr/bin/env python3
from __future__ import annotations
"""
generate_card.py — Clairvoyance Engine Model Card Generator

1080×1350 dark-mode PNG matching the Clairvoyance brand:
  • Carbon fiber background
  • Neon eye icon, purple/cyan palette
  • League-filtered sections (MLB / NBA / NHL)
  • Original pick → score result → WIN / LOSS badge

Output: frontend/card.png + docs/card.png
"""

import argparse, json, math, subprocess, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "Pillow"], check=True)
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT      = Path(__file__).parent.parent
FE_DATA   = ROOT / "frontend" / "data.json"
FE_SOCIAL = ROOT / "frontend" / "social_copy.json"
FE_CARD   = ROOT / "frontend" / "card.png"
DC_CARD   = ROOT / "docs"     / "card.png"

W, H = 1080, 1350

X_HANDLE  = "@ClairvoyanceEng"
IG_HANDLE = "@clairvoyanceengine"

# ── Brand palette (Clairvoyance logo) ─────────────────────────────────────────
BG       = (14,  14,  14)
PURPLE   = (192,  48, 240)
PURPLE_D = ( 80,  18, 110)
CYAN     = ( 48, 208, 240)
CYAN_D   = ( 20,  80, 110)
TEXT     = (218, 212, 232)
MUTED    = ( 76,  66, 108)
SEP      = ( 36,  28,  64)
WIN_BG   = ( 14,  56,  32)
WIN_FG   = ( 56, 224, 120)
LOSS_BG  = ( 64,  14,  22)
LOSS_FG  = (240,  60,  80)
PUSH_BG  = ( 36,  34,  52)
PUSH_FG  = (140, 128, 160)
LIVE_FG  = (240, 178,  40)
LIVE_BG  = ( 60,  36,   8)

# ── Font cache ─────────────────────────────────────────────────────────────────
_FONT_PATHS = [
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
]
_font_cache: dict[tuple, ImageFont.FreeTypeFont] = {}

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _font_cache:
        for path in _FONT_PATHS:
            p = Path(path)
            if not p.exists():
                continue
            try:
                idx = 1 if bold and path.endswith(".ttc") else 0
                _font_cache[key] = ImageFont.truetype(str(p), size, index=idx)
                break
            except Exception:
                try:
                    _font_cache[key] = ImageFont.truetype(str(p), size)
                    break
                except Exception:
                    continue
        if key not in _font_cache:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]

def _tw(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0]

def _th(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[3] - bb[1]


# ── Carbon fiber background ───────────────────────────────────────────────────
def _draw_carbon_fiber(img: Image.Image) -> None:
    CW, CH = 8, 16
    TILE_W, TILE_H = CW * 2, CH * 2

    tile = Image.new("RGB", (TILE_W, TILE_H))
    pix = tile.load()
    for ty in range(TILE_H):
        for tx in range(TILE_W):
            cx = tx // CW
            cy = ty // CH
            px = (tx % CW) / (CW - 1) if CW > 1 else 0
            py = (ty % CH) / (CH - 1) if CH > 1 else 0
            t = py if (cx + cy) % 2 == 0 else px   # fiber direction alternates
            r = int(16 + 26 * t)                     # neutral charcoal — no purple tint
            g = int(16 + 26 * t)
            b = int(17 + 26 * t)                     # barely-cool so fibers stay crisp
            if py < 0.12:                            # specular leading edge
                r, g, b = r + 8, g + 8, b + 8
            pix[tx, ty] = (min(r, 52), min(g, 52), min(b, 54))

    for y in range(0, H, TILE_H):
        for x in range(0, W, TILE_W):
            img.paste(tile, (x, y))


# ── Corner HUD brackets ────────────────────────────────────────────────────────
def _draw_brackets(draw: ImageDraw.ImageDraw,
                   margin: int = 32, size: int = 44, width: int = 2) -> None:
    corners = [(margin, margin), (W - margin, margin),
               (margin, H - margin), (W - margin, H - margin)]
    for bx, by in corners:
        sx = 1 if bx < W // 2 else -1
        sy = 1 if by < H // 2 else -1
        draw.line([(bx, by), (bx + sx * size, by)], fill=CYAN_D, width=width)
        draw.line([(bx, by), (bx, by + sy * size)], fill=CYAN_D, width=width)


# ── Eye icon with glow ─────────────────────────────────────────────────────────
def _draw_eye(img: Image.Image, cx: int, cy: int, size: int = 106) -> Image.Image:
    draw = ImageDraw.Draw(img)

    ow, oh = int(size * 1.10), int(size * 0.36)  # wide flat oval like logo

    # Outer orbital ellipse (cyan)
    draw.ellipse([cx - ow//2, cy - oh//2, cx + ow//2, cy + oh//2],
                 outline=CYAN, width=2)

    # HUD tick marks
    gap, tick = 7, 18
    draw.line([(cx - ow//2 - gap - tick, cy), (cx - ow//2 - gap, cy)], fill=CYAN, width=1)
    draw.line([(cx + ow//2 + gap, cy), (cx + ow//2 + gap + tick, cy)], fill=CYAN, width=1)
    draw.line([(cx, cy - oh//2 - gap - 8), (cx, cy - oh//2 - gap)], fill=CYAN, width=1)
    draw.line([(cx, cy + oh//2 + gap), (cx, cy + oh//2 + gap + 8)], fill=CYAN, width=1)

    # Iris ring (purple)
    ir = int(size * 0.42)
    draw.ellipse([cx - ir, cy - ir, cx + ir, cy + ir], outline=PURPLE, width=3)

    # Dark pupil base
    pr = int(size * 0.26)
    draw.ellipse([cx - pr, cy - pr, cx + pr, cy + pr], fill=(14, 8, 28))

    # Purple glow bloom
    gr = int(size * 0.20)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([cx - gr, cy - gr, cx + gr, cy + gr], fill=(*PURPLE, 170))
    glow = glow.filter(ImageFilter.GaussianBlur(16))
    img_rgba = Image.alpha_composite(img.convert("RGBA"), glow)
    img = img_rgba.convert("RGB")
    draw = ImageDraw.Draw(img)

    # Pupil fill
    draw.ellipse([cx - gr, cy - gr, cx + gr, cy + gr], fill=PURPLE)

    # Bright center dot
    dr = int(size * 0.052)
    draw.ellipse([cx - dr, cy - dr, cx + dr, cy + dr], fill=(220, 180, 255))

    return img


# ── Tracked letter-spaced text ─────────────────────────────────────────────────
def _tracked_width(draw: ImageDraw.ImageDraw, text: str, font, spacing: int) -> int:
    w = 0
    for ch in text:
        bb = draw.textbbox((0, 0), ch, font=font)
        w += (bb[2] - bb[0]) + spacing
    return max(w - spacing, 0)

def _tracked_text(draw: ImageDraw.ImageDraw, xy: tuple, text: str,
                  font, fill, spacing: int) -> None:
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        bb = draw.textbbox((0, 0), ch, font=font)
        x += (bb[2] - bb[0]) + spacing


# ── Neon glow text (centered) ──────────────────────────────────────────────────
def _glow_text(img: Image.Image, text: str, y: int, font,
               text_color: tuple, glow_color: tuple,
               glow_radius: int = 12, spacing: int = 0) -> Image.Image:
    tmp = ImageDraw.Draw(img)
    tw = _tracked_width(tmp, text, font, spacing) if spacing else _tw(tmp, text, font)
    x = (W - tw) // 2

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    if spacing:
        _tracked_text(gd, (x, y), text, font, (*glow_color, 210), spacing)
    else:
        gd.text((x, y), text, font=font, fill=(*glow_color, 210))
    glow = glow.filter(ImageFilter.GaussianBlur(glow_radius))

    img_rgba = Image.alpha_composite(img.convert("RGBA"), glow)
    img = img_rgba.convert("RGB")
    draw = ImageDraw.Draw(img)
    if spacing:
        _tracked_text(draw, (x, y), text, font, text_color, spacing)
    else:
        draw.text((x, y), text, font=font, fill=text_color)
    return img


# ── Pill badge ─────────────────────────────────────────────────────────────────
def _badge(draw: ImageDraw.ImageDraw, rx: int, y: int,
           label: str, bg: tuple, fg: tuple, font, pad_x: int = 9) -> int:
    """Draw a right-aligned pill at x=rx. Returns badge width."""
    tw = _tw(draw, label, font)
    bw = tw + pad_x * 2
    bh = 20
    draw.rounded_rectangle([rx - bw, y, rx, y + bh], radius=4, fill=bg)
    draw.text((rx - bw + pad_x, y + 2), label, font=font, fill=fg)
    return bw


# ── Separator with accent diamonds ────────────────────────────────────────────
def _sep_line(draw: ImageDraw.ImageDraw, y: int,
              lx: int = 60, rx: int = W - 60) -> None:
    draw.line([(lx, y), (rx, y)], fill=SEP, width=1)
    draw.rectangle([lx - 3, y - 2, lx + 3, y + 2], fill=CYAN)
    draw.rectangle([rx - 3, y - 2, rx + 3, y + 2], fill=CYAN)


# ── Section header (  ══ MLB ══  ) ────────────────────────────────────────────
def _section_header(draw: ImageDraw.ImageDraw, y: int, label: str) -> int:
    f = _font(12, bold=True)
    pad = "    "
    full = f"{pad}{label}{pad}"
    tw = _tw(draw, full, f)
    lx = (W - tw) // 2

    draw.line([(68, y + 7), (lx, y + 7)],          fill=CYAN_D, width=1)
    draw.text((lx, y), full,                         font=f, fill=CYAN)
    draw.line([(lx + tw, y + 7), (W - 68, y + 7)],  fill=CYAN_D, width=1)
    return y + 26


# ── Header ─────────────────────────────────────────────────────────────────────
def render_header(img: Image.Image) -> tuple[Image.Image, int]:
    draw = ImageDraw.Draw(img)

    # Date top-right (local system time = Mountain)
    date_str = datetime.now().strftime("%b %d, %Y").upper()
    f_date = _font(13)
    draw.text((W - 68 - _tw(draw, date_str, f_date), 46),
              date_str, font=f_date, fill=MUTED)

    # Eye icon (wider/flatter to match logo oval)
    img = _draw_eye(img, W // 2, 116, size=118)

    # CLAIRVOYANCE neon purple
    img = _glow_text(img, "CLAIRVOYANCE", 196, _font(52, bold=True),
                     PURPLE, PURPLE_D, glow_radius=16)

    # ENGINE subtitle cyan spaced
    img = _glow_text(img, "PREDICTIVE SPORTS INTELLIGENCE ENGINE", 260,
                     _font(12), CYAN, CYAN_D, glow_radius=5, spacing=4)

    # Tagline muted
    draw = ImageDraw.Draw(img)
    f_tag = _font(11)
    tag = "SEE  WHAT  OTHERS  CANNOT"
    draw.text(((W - _tw(draw, tag, f_tag)) // 2, 288), tag, font=f_tag, fill=MUTED)

    # Separator
    _sep_line(draw, 316)
    return img, 326


# ── Record bar ─────────────────────────────────────────────────────────────────
def render_record(img: Image.Image, y: int, settled: list) -> tuple[Image.Image, int]:
    draw = ImageDraw.Draw(img)
    wins   = sum(1 for s in settled if s.get("result") == "win")
    losses = sum(1 for s in settled if s.get("result") == "loss")
    pushes = sum(1 for s in settled if s.get("result") == "push")
    units  = sum(s.get("units", 0) for s in settled)
    total  = wins + losses + pushes

    f_lbl = _font(11)
    f_val = _font(22, bold=True)

    cols = [
        ("RECORD",  f"{wins}W-{losses}L" + (f"-{pushes}P" if pushes else ""),
         WIN_FG if wins >= losses else LOSS_FG),
        ("UNITS",   f"{units:+.1f}u",
         WIN_FG if units >= 0 else LOSS_FG),
        ("WIN %",   f"{wins/total*100:.1f}%" if total else "—", CYAN),
    ]
    col_w = (W - 136) // 3
    for i, (lbl, val, col) in enumerate(cols):
        x = 68 + i * col_w
        draw.text((x, y),      lbl, font=f_lbl, fill=MUTED)
        draw.text((x, y + 15), val, font=f_val, fill=col)

    y += 52
    _sep_line(draw, y)
    return img, y + 10


# ── Single game row ────────────────────────────────────────────────────────────
_ORD = {1:"1ST",2:"2ND",3:"3RD",4:"4TH",5:"5TH",6:"6TH",
        7:"7TH",8:"8TH",9:"9TH",10:"10TH"}

def _game_row(draw: ImageDraw.ImageDraw, y: int, game: dict,
              bet: dict | None, settled: dict | None) -> int:
    """Render one game row. Returns y after row."""
    f_name  = _font(15, bold=True)
    f_sm    = _font(12)
    f_badge = _font(11, bold=True)
    f_xs    = _font(10)

    away  = game.get("away", "")
    home  = game.get("home", "")
    state = game.get("state", "pre")
    rx    = W - 68      # right anchor
    row_h = 36

    draw.text((68, y), f"{away} @ {home}", font=f_name, fill=TEXT)

    if settled:
        # ── SETTLED: pick → score → WIN / LOSS ──────────────────────────────
        result = settled.get("result", "")
        units  = settled.get("units", 0.0)
        pick   = settled.get("pick", "")
        a_s    = game.get("awayScore", "?")
        h_s    = game.get("homeScore", "?")

        if result == "win":
            bg, fg, lbl = WIN_BG, WIN_FG, f"✓ WIN  {units:+.1f}u"
        elif result == "loss":
            bg, fg, lbl = LOSS_BG, LOSS_FG, f"✗ LOSS  {units:+.1f}u"
        else:
            bg, fg, lbl = PUSH_BG, PUSH_FG, "PUSH"

        bw = _badge(draw, rx, y + 2, lbl, bg, fg, f_badge)

        # Score line
        score_str = f"{away} {a_s}  —  {home} {h_s}   FINAL"
        draw.text((68, y + 19), score_str, font=f_sm, fill=MUTED)

        # Original pick label (left of badge)
        if pick:
            pk_str = f"Pick: {pick}"
            draw.text((rx - bw - _tw(draw, pk_str, f_sm) - 14, y + 4),
                      pk_str, font=f_sm, fill=TEXT)
        row_h = 42

    elif bet:
        # ── PENDING MODEL PICK ───────────────────────────────────────────────
        pick = bet.get("pick", "")
        prob = bet.get("modelProb", bet.get("prob", 0))
        impl = bet.get("impliedProb", bet.get("implied", 0))
        edge = bet.get("edge",
                       round((prob - impl) * 100, 1) if prob and impl else 0)

        _badge(draw, rx, y + 2, "◆ TRACKING", (18, 14, 44), CYAN, f_badge)

        if pick:
            detail = f"Pick: {pick}   Mdl {prob*100:.0f}%  Mkt {impl*100:.0f}%  Edge {edge:+.1f}%"
            draw.text((68, y + 19), detail, font=f_sm, fill=MUTED)
        row_h = 42

    elif state == "in":
        # ── LIVE ─────────────────────────────────────────────────────────────
        a_s = game.get("awayScore", 0)
        h_s = game.get("homeScore", 0)
        per = game.get("period", "")
        clk = game.get("displayClock", "")
        per_str = _ORD.get(per, str(per)) if isinstance(per, int) else str(per)

        _badge(draw, rx, y + 2, "● LIVE", LIVE_BG, LIVE_FG, f_badge)
        score_str = f"{away} {a_s}  –  {home} {h_s}   {per_str} {clk}".rstrip()
        draw.text((68, y + 19), score_str, font=f_sm, fill=LIVE_FG)
        row_h = 42

    elif state == "post":
        # ── FINAL ────────────────────────────────────────────────────────────
        a_s = game.get("awayScore", 0)
        h_s = game.get("homeScore", 0)
        draw.text((68, y + 19), f"{away} {a_s}  —  {home} {h_s}", font=f_sm, fill=MUTED)
        draw.text((rx - _tw(draw, "FINAL", f_xs), y + 20), "FINAL", font=f_xs, fill=MUTED)
        row_h = 38

    else:
        # ── PRE-GAME ──────────────────────────────────────────────────────────
        game_dt = game.get("date", "")
        time_str = ""
        if game_dt:
            try:
                from datetime import datetime as _dt
                gd = _dt.fromisoformat(game_dt.replace("Z", "+00:00"))
                et_time = gd - timedelta(hours=5)
                time_str = et_time.strftime("%-I:%M %p ET")
            except Exception:
                pass
        if time_str:
            draw.text((rx - _tw(draw, time_str, f_xs), y + 2),
                      time_str, font=f_xs, fill=MUTED)
        row_h = 28

    # Playoff series note
    series = game.get("seriesNote", "")
    if series:
        draw.text((68, y + row_h - 4), series, font=f_xs, fill=(58, 48, 88))
        row_h += 12

    # Row separator
    draw.line([(68, y + row_h - 1), (rx, y + row_h - 1)], fill=(22, 17, 40), width=1)
    return y + row_h


# ── League block ───────────────────────────────────────────────────────────────
def _render_league(img: Image.Image, y: int, label: str, games: list,
                   best_bets: list, settled_list: list,
                   max_games: int = 6) -> tuple[Image.Image, int]:
    if not games:
        return img, y

    draw = ImageDraw.Draw(img)
    y = _section_header(draw, y, label)

    def _gk(g: dict) -> str:
        return f"{g.get('away','')}@{g.get('home','')}".upper().replace(" ", "")

    bet_map = {b.get("game", "").upper().replace(" ", ""): b for b in best_bets}
    stl_map = {s.get("game", "").upper().replace(" ", ""): s for s in settled_list}

    # Live first, then finals, then pre-game
    priority = {"in": 0, "post": 1, "pre": 2}
    ordered = sorted(games, key=lambda g: priority.get(g.get("state", "pre"), 2))

    for game in ordered[:max_games]:
        gk  = _gk(game)
        bet = bet_map.get(gk)
        stl = stl_map.get(gk)
        y   = _game_row(draw, y, game, bet, stl)

    if len(games) > max_games:
        f_xs = _font(11)
        draw.text((68, y), f"+ {len(games) - max_games} more games",
                  font=f_xs, fill=MUTED)
        y += 18

    return img, y + 6


# ── Intel strip + tomorrow ────────────────────────────────────────────────────
def _render_intel(img: Image.Image, y: int, data: dict) -> tuple[Image.Image, int]:
    """Model stats and tomorrow's slate. Only renders if space allows."""
    footer_reserve = 94
    min_space = 60
    if y > H - footer_reserve - min_space:
        return img, y

    draw = ImageDraw.Draw(img)
    f_lbl = _font(11, bold=True)
    f_val = _font(12)
    f_xs  = _font(10)

    y += 4
    _sep_line(draw, y)
    y += 12

    # Build intel items
    items: list[tuple[str, str]] = []

    # NHL MoneyPuck xGF% leader
    mp = data.get("mp", {}).get("teams", {})
    if mp:
        leader = max(mp.items(), key=lambda kv: kv[1].get("5on5", {}).get("xgfPct", 0)
                     if isinstance(kv[1], dict) else 0, default=(None, {}))
        if leader[0]:
            pct = leader[1].get("5on5", {}).get("xgfPct", 0)
            items.append(("NHL xGF%", f"{leader[0]}  {pct:.3f}  (5v5 leader)"))

    # Weather factor
    weather = data.get("weather", {})
    windy = [(k, v) for k, v in weather.items()
             if not v.get("indoor") and (v.get("wind") or 0) >= 15]
    if windy:
        t, w = windy[0]
        items.append(("WIND", f"{t}  {w.get('wind')} mph  {w.get('temp','')}°F"))

    # Draw intel items two-column
    col_w = (W - 136) // 2
    for i, (lbl, val) in enumerate(items[:4]):
        row = i // 2
        col = i % 2
        ix = 68 + col * col_w
        iy = y + row * 34
        draw.text((ix, iy),      lbl, font=f_lbl, fill=MUTED)
        draw.text((ix, iy + 14), val, font=f_val,  fill=TEXT)

    if items:
        rows = math.ceil(len(items) / 2)
        y += rows * 34 + 8

    # Tomorrow's slate (MLB only — most predictable)
    mlb_tom = data.get("mlb", {}).get("tomorrow", [])
    if mlb_tom and y < H - footer_reserve - 80:
        _sep_line(draw, y)
        y += 12
        draw = ImageDraw.Draw(img)
        draw.text((68, y), "TOMORROW  —  MLB", font=f_lbl, fill=MUTED)
        y += 18
        max_tom = min(4, (H - footer_reserve - y) // 22)
        for g in mlb_tom[:max_tom]:
            away, home = g.get("away", ""), g.get("home", "")
            game_dt = g.get("date", "")
            time_str = ""
            if game_dt:
                try:
                    from datetime import datetime as _dt2
                    gd = _dt2.fromisoformat(game_dt.replace("Z", "+00:00"))
                    time_str = (gd - timedelta(hours=5)).strftime("%-I:%M %p")
                except Exception:
                    pass
            draw.text((68, y), f"{away} @ {home}", font=f_val, fill=(160, 148, 192))
            if time_str:
                draw.text((W - 68 - _tw(draw, time_str, f_xs), y + 2),
                          time_str, font=f_xs, fill=MUTED)
            y += 22
        if len(mlb_tom) > max_tom:
            draw.text((68, y), f"+ {len(mlb_tom) - max_tom} more", font=f_xs, fill=MUTED)
            y += 16

    return img, y


# ── Footer ──────────────────────────────────────────────────────────────────────
def _render_footer(draw: ImageDraw.ImageDraw, platform: str = "instagram") -> None:
    f_sm  = _font(13)
    f_hnd = _font(12)
    f_xs  = _font(10)

    _sep_line(draw, H - 86)

    # Brand name — left
    draw.text((68, H - 70), "CLAIRVOYANCE ENGINE", font=f_sm, fill=PURPLE)

    # Both handles — right, platform one highlighted in CYAN the other muted
    x_col  = CYAN if platform == "x"         else (90, 78, 128)
    ig_col = CYAN if platform == "instagram" else (90, 78, 128)
    dot    = "  ·  "

    x_w   = _tw(draw, X_HANDLE,  f_hnd)
    dot_w = _tw(draw, dot,        f_hnd)
    ig_w  = _tw(draw, IG_HANDLE, f_hnd)
    total = x_w + dot_w + ig_w
    hx    = W - 68 - total

    draw.text((hx,               H - 70), X_HANDLE,  font=f_hnd, fill=x_col)
    draw.text((hx + x_w,         H - 70), dot,        font=f_hnd, fill=MUTED)
    draw.text((hx + x_w + dot_w, H - 70), IG_HANDLE, font=f_hnd, fill=ig_col)

    draw.text(
        (68, H - 42),
        "Model outputs are probabilistic projections, not financial advice.",
        font=f_xs, fill=MUTED,
    )


# ── Compose full card ──────────────────────────────────────────────────────────
def generate_card(data: dict, social: dict, platform: str = "instagram") -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    _draw_carbon_fiber(img)
    _draw_brackets(ImageDraw.Draw(img))

    img, y = render_header(img)
    draw = ImageDraw.Draw(img)

    settled   = data.get("settled",  [])
    best_bets = data.get("bestBets", [])

    # Record or tracking notice
    if settled:
        img, y = render_record(img, y, settled)
        draw = ImageDraw.Draw(img)
    else:
        f_xs = _font(11)
        draw.text((68, y), "RECORD TRACKING ACTIVE — sample accumulating",
                  font=f_xs, fill=MUTED)
        y += 22
        _sep_line(draw, y)
        y += 10

    # Dynamic space budget across leagues
    footer_reserve = 94
    available      = H - y - footer_reserve
    mlb = data.get("mlb", {}).get("today", [])
    nba = data.get("nba", {}).get("today", [])
    nhl = data.get("nhl", {}).get("today", [])
    active = sum(1 for g in [mlb, nba, nhl] if g)
    row_px = 44
    per_league = max(2, (available // row_px) // max(active, 1))

    img, y = _render_league(img, y, "MLB", mlb, best_bets, settled,
                             max_games=min(per_league, 7))
    img, y = _render_league(img, y, "NBA", nba, best_bets, settled,
                             max_games=min(per_league, 4))
    img, y = _render_league(img, y, "NHL", nhl, best_bets, settled,
                             max_games=min(per_league, 4))

    # Fill remaining vertical space with intel + tomorrow
    img, y = _render_intel(img, y, data)

    _render_footer(ImageDraw.Draw(img), platform=platform)
    return img


# ── CLI ────────────────────────────────────────────────────────────────────────
def main() -> None:
    p = argparse.ArgumentParser(description="Clairvoyance Card Generator")
    p.add_argument("--open",     action="store_true", help="Open preview after saving")
    p.add_argument("--platform", choices=["x", "instagram"], default="instagram",
                   help="Card variant: 'x' highlights @ClairvoyanceEng, 'instagram' highlights @clairvoyanceengine")
    p.add_argument("--output",   type=str, default=None,
                   help="Additional output path (PNG). Always also saves to frontend/ + docs/")
    args = p.parse_args()

    if not FE_DATA.exists():
        print("[ERROR] data.json not found — run clairvoyance_update.py first",
              file=sys.stderr)
        sys.exit(1)

    data   = json.loads(FE_DATA.read_text())
    social = json.loads(FE_SOCIAL.read_text()) if FE_SOCIAL.exists() else {}

    img = generate_card(data, social, platform=args.platform)

    img.save(str(FE_CARD), "PNG", optimize=True)
    img.save(str(DC_CARD), "PNG", optimize=True)
    kb = FE_CARD.stat().st_size // 1024
    print(f"[INFO] card.png written ({kb} KB) → frontend/ + docs/")

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out), "PNG", optimize=True)
        kb2 = out.stat().st_size // 1024
        print(f"[INFO] card.png written ({kb2} KB) → {out}")

    if args.open:
        subprocess.run(["open", str(FE_CARD)])


if __name__ == "__main__":
    main()
