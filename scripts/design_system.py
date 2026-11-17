"""
Clairvoyance Engine — Design System
====================================
Single source of truth for all social cards and logo assets.
Import this in any generator script.

FONTS
  Orbitron-wght900.ttf  — ~/Library/Fonts/Orbitron-wght900.ttf  (headings, labels, UI)
  SFNSMono.ttf          — /System/Library/Fonts/SFNSMono.ttf     (body, descriptions)

COLORS
  MAG  = (240,   0, 255)   neon magenta  — primary brand, titles, borders
  CYN  = (  0, 240, 255)   neon cyan     — subtitles, secondary accent
  GOLD = (255, 213,   0)   gold          — INSIDER tier, LAUNCHING text
  RED  = (255,  32,  80)   neon red      — SKIP grade
  WHT  = (235, 238, 252)   near-white    — body text
  DIM  = (120, 115, 150)   dim purple    — disclaimer, muted text
  T2   = (192, 192, 192)   light grey    — descriptions
  T3   = (120, 120, 120)   mid grey      — labels, metadata
  BG   = (  8,  10,  11)   near-black    — canvas background
  CARD = ( 15,   5,  28)   dark purple   — card fill

BACKGROUND
  make_background()        — standard: void-black BG, diamond grid centered,
                              rotated 45°, 30 px spacing, teal (20,90,90) α=153,
                              teal radial glow at (CX, H*0.4) r≈52% of width,
                              edge vignette (black, gaussian blur)
  make_background_covers() — alternate: #222233 fill, tight 45° crosshatch
                              (spacing max(8, w/64), teal (78,166,176) α=128),
                              lighter edge vignette. Matches covers_card4.png /
                              _cfBg() in docs/app.html and glitch_reveal.html's
                              canvas background. Used by CorrectPinnedCard6.0
                              and the Reddit ad card.

GLOW SYSTEM
  Screen-blend multi-layer glow via ImageChops.screen.
  Oversized canvas prevents bloom clipping.
  Presets (radius, alpha):
    TITLE_GLOW  = [(3,255),(8,255),(22,190),(50,100),(90,40)]
    SUB_GLOW    = [(2,255),(7,255),(18,190),(40,100),(70,40)]
    HDR_GLOW    = [(2,255),(6,210),(14,150),(30,80)]
    SOCIAL_GLOW = [(2,255),(6,180),(12,90)]

FOOTER BAR  (all cards)
  Height: 70 px, fill (8,8,10)
  4-step magenta accent line at top edge: alpha [60,120,200,255]
  Social handles centered with neon-dot separators (orb 13px)
  Disclaimer centered below: mono 13px, DIM color

CARD SCRIPTS -- repo-tracked (scripts/), all copy (leagues / pricing / props wording) from ad_coverage.py, which
scripts/test_ad_coverage.py pins to the real engine.  The older Tron-Disc-only gen_*_v2.py copies are SUPERSEDED
(they still listed Bundesliga/MLS) -- use these:
  gen_pinned_card.py          → scripts/assets/covers_card.png (the 5-day "What We Cover" rotation card, void bg)
                                 + --out-dir: CorrectPinnedCard2.0.png (void bg) + CorrectPinnedCard6.0.png (covers bg)
  gen_reddit_ad_v1.py         → ~/Desktop/ClairvoyanceRedditAd.png (covers bg) + Tron Disc Advertising/ClairvoyanceRedditAd.png (void bg)
  gen_subscription_card.py    → --out-dir: Clairvoyance_Subscription_Card_Black.png (void bg) + _Grey.png (covers bg)
  refresh_pinned_card_images.py → repaints the coverage block of docs/pinned_card_{carbon,purplecarbon,whitecarbon,plaid}.png
Tron Disc (/Volumes/Tron Disc/Clairvoyance/Python Scripts/) still holds:
  gen_grading_card.py         → ~/Desktop/Clairvoyance-Grading-Card.png
  (gen_launch_card_v2.py retired 2026-09-09 -- pre-launch teaser card, obsolete once the
   product was actually live; do not resurrect without explicit request)

LOGO SCRIPTS (Tron Disc)
  rebg_logos.py               → applies background to all 7 logo PNGs, which live at
                                 Tron Disc "Clairvoyance Logos/Style 3/" (NOT Desktop)
    bannerlogo1.png   1500×500
    bannerlogo2.png   1500×500
    TextLogo.png       500×500
    StandardLogo.png  1080×1080
    StandardLogo2.png 1080×1080
    SymbolLogo.png     500×500
    SymbolLogo2.png    500×500

MASTER REGENERATION
  gen_all.py  — runs all four card scripts then rebg_logos.py
"""

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageChops
import os, math

# ── Canvas ───────────────────────────────────────────────────────
W, H = 1080, 1080
CX   = W // 2

# ── Colors ───────────────────────────────────────────────────────
BG   = (  8,  10,  11)
CARD = ( 15,   5,  28)
MAG  = (240,   0, 255)
CYN  = (  0, 240, 255)
GOLD = (255, 213,   0)
RED  = (255,  32,  80)
WHT  = (235, 238, 252)
DIM  = (120, 115, 150)
T2   = (192, 192, 192)
T3   = (120, 120, 120)

# ── Fonts ────────────────────────────────────────────────────────
ORB_PATH = os.path.expanduser('~/Library/Fonts/Orbitron-wght900.ttf')
SYS_PATH = '/System/Library/Fonts/SFNSMono.ttf'

def orb(sz):  return ImageFont.truetype(ORB_PATH, sz)
def mono(sz): return ImageFont.truetype(SYS_PATH, sz)

# ── Glow presets ─────────────────────────────────────────────────
TITLE_GLOW  = [(3,255),(8,255),(22,190),(50,100),(90,40)]
SUB_GLOW    = [(2,255),(7,255),(18,190),(40,100),(70,40)]
HDR_GLOW    = [(2,255),(6,210),(14,150),(30,80)]
SOCIAL_GLOW = [(2,255),(6,180),(12,90)]

# ── Screen-blend glow ────────────────────────────────────────────
def glow(base, text, pos, font, color, layers, anchor='mm'):
    """Additive screen-blend neon glow. Safe against bloom clipping."""
    cx, cy = pos
    max_pad = max(r for r, _ in layers) * 4
    CW, CH = W + max_pad*2, H + max_pad*2
    ox, oy = cx + max_pad, cy + max_pad
    glow_big = Image.new('RGB', (CW, CH), (0,0,0))
    for radius, alpha in layers:
        layer = Image.new('RGB', (CW, CH), (0,0,0))
        c = tuple(int(ch * alpha / 255) for ch in color)
        ImageDraw.Draw(layer).text((ox, oy), text, font=font, fill=c, anchor=anchor)
        layer = layer.filter(ImageFilter.GaussianBlur(radius))
        glow_big = ImageChops.screen(glow_big, layer)
    g = glow_big.crop((max_pad, max_pad, max_pad+W, max_pad+H))
    merged = ImageChops.screen(base.convert('RGB'), g)
    base = merged.convert('RGBA')
    ImageDraw.Draw(base).text((cx, cy), text, font=font, fill=(*color,255), anchor=anchor)
    return base

# ── Background ───────────────────────────────────────────────────
def make_background(w=W, h=H):
    """Generate the standard Clairvoyance background at any size."""
    cx, cy = w // 2, h // 2
    base = Image.new('RGBA', (w, h), (*BG, 255))
    d = ImageDraw.Draw(base)
    S = 30
    half = int(max(w, h) * 0.85)
    c45, s45 = math.cos(math.pi/4), math.sin(math.pi/4)
    for i in range(-half, half + 1, S):
        d.line([(cx+i*c45-(-half)*s45, cy+i*s45+(-half)*c45),
                (cx+i*c45-half*s45,    cy+i*s45+half*c45)],   fill=(20,90,90,153), width=1)
        d.line([(cx+(-half)*c45-i*s45, cy+(-half)*s45+i*c45),
                (cx+half*c45-i*s45,    cy+half*s45+i*c45)],   fill=(20,90,90,153), width=1)
    # Teal center glow
    lg = Image.new('RGBA', (w, h), (0,0,0,0))
    r_out = int(min(w, h) * 0.52)
    for r in range(r_out, 0, -8):
        a = int(10 * (1 - r/r_out)**1.5)
        ImageDraw.Draw(lg).ellipse([cx-r, int(h*0.4)-r, cx+r, int(h*0.4)+r], fill=(0,180,180,a))
    lg = lg.filter(ImageFilter.GaussianBlur(50))
    base = Image.alpha_composite(base, lg)
    # Edge vignette
    vg = Image.new('RGBA', (w, h), (0,0,0,0))
    r_hi, r_lo = int(min(w,h)*0.75), int(min(w,h)*0.28)
    for r in range(r_hi, r_lo, -6):
        a = int(153 * (1 - (r - r_lo) / (r_hi - r_lo))**2)
        ImageDraw.Draw(vg).ellipse([cx-r, cy-r, cx+r, cy+r], fill=(0,0,0,a))
    vg = vg.filter(ImageFilter.GaussianBlur(40))
    return Image.alpha_composite(base, vg)

def make_background_covers(w=W, h=H):
    """Alternate background — matches covers_card4.png / _cfBg() in
    docs/app.html and glitch_reveal.html's canvas: solid #222233 fill,
    tight 45° crosshatch grid, lighter radial vignette. Distinct from
    make_background()'s void-black/teal-diamond style."""
    base = Image.new('RGBA', (w, h), (34, 34, 51, 255))
    d = ImageDraw.Draw(base)
    ds = max(8, round(w / 64))
    for i in range(-h, w + h + 1, ds):
        d.line([(i, 0), (i + h, h)], fill=(78, 166, 176, 128), width=1)
        d.line([(i, h), (i + h, 0)], fill=(78, 166, 176, 128), width=1)
    vg = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    r_hi, r_lo = int(w * 0.72), int(w * 0.30)
    for r in range(r_hi, r_lo, -6):
        a = int(56 * (r - r_lo) / (r_hi - r_lo))
        ImageDraw.Draw(vg).ellipse([w//2-r, h//2-r, w//2+r, h//2+r], fill=(0, 0, 0, a))
    vg = vg.filter(ImageFilter.GaussianBlur(30))
    return Image.alpha_composite(base, vg)

# 4.4 px cells on a 20 px repeat, the same twill weave the app paints behind every page (docs/app.html `body`): lighter cells #1E1E1E, darker #161616, 0.6 px gaps showing the #121212 base.
CARBON_BASE = (18, 18, 18)
CARBON_LIGHT = (30, 30, 30)
CARBON_DARK = (22, 22, 22)
CARBON_CELL = 4.4
CARBON_TILE = 20
CARBON_LIGHT_CELLS = ((0, 0), (5, 0), (0, 5), (15, 5), (10, 10), (15, 10), (5, 15), (10, 15))
CARBON_DARK_CELLS = ((10, 0), (15, 0), (5, 5), (10, 5), (0, 10), (5, 10), (0, 15), (15, 15))
# the page's two soft glows: white top-left (r 620, .11) and grey bottom-right (r 560, .07), alpha falling linearly to 0 at the radius
CARBON_GLOWS = (((0.16, 0.04), 620, (255, 255, 255), 0.11), ((0.86, 0.94), 560, (166, 166, 166), 0.07))

def _carbon_tile():
    k = 8                                   # draw big, shrink: the 4.4 px cells land on fractions of a pixel
    big = Image.new('RGBA', (CARBON_TILE * k, CARBON_TILE * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    for cells, col in ((CARBON_LIGHT_CELLS, CARBON_LIGHT), (CARBON_DARK_CELLS, CARBON_DARK)):
        for x, y in cells:
            d.rectangle([x * k, y * k, (x + CARBON_CELL) * k - 1, (y + CARBON_CELL) * k - 1], fill=(*col, 255))
    return big.resize((CARBON_TILE, CARBON_TILE), Image.LANCZOS)

def make_background_carbon(w=W, h=H):
    """The app's carbon twill weave (docs/app.html body background) -- the third design_system background, used by the Reddit/pinned/subscription cards since 2026-11-17."""
    base = Image.new('RGBA', (w, h), (*CARBON_BASE, 255))
    tile = _carbon_tile()
    for ty in range(0, h, CARBON_TILE):
        for tx in range(0, w, CARBON_TILE):
            base.alpha_composite(tile, (tx, ty))
    for (fx, fy), radius, rgb, alpha in CARBON_GLOWS:
        m = Image.radial_gradient('L').resize((radius * 2, radius * 2), Image.BILINEAR)         # 0 at the centre, 255 only at the square's CORNERS (about 180 at the inscribed circle's edge)
        m = m.point(lambda v: int(round(alpha * 255 * max(0.0, 1 - v / 180.3))))                  # linear fall to 0 at the radius, like CSS `radial-gradient(circle Rpx, rgba(..), transparent)`
        halo = Image.new('RGBA', (radius * 2, radius * 2), (*rgb, 0))
        halo.putalpha(m)                                    # straight alpha: colour stays full strength, only the opacity falls off
        glow_layer = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        glow_layer.paste(halo, (int(w * fx) - radius, int(h * fy) - radius))      # plain paste copies all four channels (a masked paste would blend the colour toward black)
        base = Image.alpha_composite(base, glow_layer)
    return base

# ── Footer bar ───────────────────────────────────────────────────
FBAR_H = 70

def make_footer_carbon(w=W, h=FBAR_H):
    """The Film Archive's "darker carbon with white edge" panel (film-archive src/app/globals.css `.side-menu`), as a footer strip: #101010 base, the carbon weave tiled from the
    strip's own top-left corner, a 35% black layer over it, and a 160deg white sheen (8% at the corner, gone by 40% of the gradient's length)."""
    strip = Image.new('RGBA', (w, h), (16, 16, 16, 255))
    tile = _carbon_tile()
    for ty in range(0, h, CARBON_TILE):
        for tx in range(0, w, CARBON_TILE):
            strip.alpha_composite(tile, (tx, ty))
    strip = Image.alpha_composite(strip, Image.new('RGBA', (w, h), (0, 0, 0, 89)))        # rgb(0 0 0 / .35)
    ang = math.radians(160)
    dx, dy = math.sin(ang), -math.cos(ang)                                                # CSS angle: 0deg = up, 90deg = right
    length = abs(w * dx) + abs(h * dy)
    sheen = Image.new('L', (w, h), 0)
    px = sheen.load()
    for y in range(h):
        for x in range(w):
            t = ((x - w / 2) * dx + (y - h / 2) * dy) / length + 0.5                      # 0 at the gradient's start corner, 1 at its end
            px[x, y] = int(round(255 * 0.08 * max(0.0, 1 - t / 0.4)))
    white = Image.new('RGBA', (w, h), (255, 255, 255, 0))
    white.putalpha(sheen)
    return Image.alpha_composite(strip, white)

def draw_footer(base, cx=CX, carbon=False):
    """Standard footer bar with social handles + disclaimer.  carbon=True paints the Film Archive's darker carbon panel instead of the flat near-black bar."""
    draw = ImageDraw.Draw(base)
    if carbon:
        base.alpha_composite(make_footer_carbon(W, FBAR_H), (0, H - FBAR_H))
        draw = ImageDraw.Draw(base)
    else:
        draw.rectangle([(0, H-FBAR_H), (W, H)], fill=(8,8,10,255))
    for i, al in enumerate([60,120,200,255]):
        draw.line([(0, H-FBAR_H+i),(W, H-FBAR_H+i)], fill=(*MAG,al), width=1)
    FY = H - FBAR_H//2
    sf = orb(13)
    mf = mono(13)
    base = _draw_footer_handles(base,
        ['X @ClairvoyanceEng','clairvoyanceengine.info','IG @clairvoyanceengine'],
        cx, FY, sf)
    base = glow(base, 'Model outputs are probabilistic projections, not financial advice',
        (cx, FY+18), mf, DIM, [(1,200),(3,120)], anchor='mm')
    return base

def _draw_footer_handles(base, parts, cx_center, ym, font, dot_r=2):
    DOT_W = 20
    total_w = sum(font.getlength(p) for p in parts) + DOT_W*(len(parts)-1)
    x = cx_center - total_w/2
    txt_pos, dot_xs = [], []
    for i, p in enumerate(parts):
        pw = font.getlength(p)
        txt_pos.append((x, p))
        x += pw
        if i < len(parts)-1:
            dot_xs.append(x + DOT_W//2)
            x += DOT_W
    for dot_x in dot_xs:
        for ro, al in [(4,80),(2,150)]:
            lay = Image.new('RGBA',(W,H),(0,0,0,0))
            ImageDraw.Draw(lay).ellipse(
                [dot_x-dot_r-ro, ym-dot_r-ro, dot_x+dot_r+ro, ym+dot_r+ro],
                fill=(*MAG,al))
            lay = lay.filter(ImageFilter.GaussianBlur(ro+1))
            base = Image.alpha_composite(base, lay)
    d = ImageDraw.Draw(base)
    for dot_x in dot_xs:
        d.ellipse([dot_x-dot_r,ym-dot_r,dot_x+dot_r,ym+dot_r], fill=(*MAG,230))
    for tx, p in txt_pos:
        d.text((tx, ym), p, font=font, fill=(*MAG,255), anchor='lm')
    return base

# ── Horizontal rule ──────────────────────────────────────────────
def hline(base, y, color=MAG, alpha=128, mx=60, w=W, h=H):
    lay = Image.new('RGBA', (w, h), (0,0,0,0))
    for x in range(mx, w-mx):
        t = (x-mx)/(w-2*mx)
        a = int(alpha * math.sin(t*math.pi))
        ImageDraw.Draw(lay).point((x, y), fill=(*color, a))
    return Image.alpha_composite(base, lay)
