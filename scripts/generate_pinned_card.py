#!/usr/bin/env python3
"""
generate_pinned_card.py — Clairvoyance Engine Pinned Post Cards
Generates 4 × 1080×1080 PNGs for Instagram + X pinned posts.

Variants:
  pinned_card_carbon.png       — black/grey charcoal carbon fiber
  pinned_card_whitecarbon.png  — white/grey charcoal carbon fiber
  pinned_card_purplecarbon.png — deep purple carbon fiber (original brand)
  pinned_card_plaid.png        — black/grey/white/purple tartan plaid

Usage:
  python3 scripts/generate_pinned_card.py
"""

import math, subprocess, sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    import numpy as np
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "Pillow", "numpy"], check=True)
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    import numpy as np

ROOT = Path(__file__).parent.parent
W = H = 1080

X_HANDLE  = "@ClairvoyanceEng"
IG_HANDLE = "@clairvoyanceengine"
DOMAIN    = "clairvoyanceengine.info"

# ── Brand colors (canonical) ───────────────────────────────────────────────────
_PURPLE  = (240,   0, 255)   # neon purple  rgb(240,0,255)
_CYAN    = (  0, 240, 255)   # neon cyan    rgb(0,240,255)
_CYAN_D  = (  0, 130, 155)   # dim cyan for brackets / domain
_PURP_D  = (110,   0, 130)   # dim purple for dashed ring

# ── Theme palettes ─────────────────────────────────────────────────────────────
THEMES = {
    "carbon": dict(
        BG=(16,16,16), PURPLE=_PURPLE, PURPLE_D=_PURP_D,
        CYAN=_CYAN, CYAN_D=_CYAN_D,
        TEXT=(232,240,255), MUTED=(105,118,155), DIM=(55,50,72),
        SEP=(45,40,60), FOOTER=(8,8,8), light=False, purple_fiber=False,
    ),
    "whitecarbon": dict(
        BG=(235,235,235), PURPLE=_PURPLE, PURPLE_D=_PURP_D,
        CYAN=_CYAN, CYAN_D=(0,160,190),
        TEXT=(30,24,46), MUTED=(60,55,80), DIM=(120,115,140),
        SEP=(180,172,200), FOOTER=(200,200,204), light=True, purple_fiber=False,
    ),
    "purplecarbon": dict(
        BG=(12,8,18), PURPLE=_PURPLE, PURPLE_D=_PURP_D,
        CYAN=_CYAN, CYAN_D=_CYAN_D,
        TEXT=(232,240,255), MUTED=(105,118,155), DIM=(55,50,72),
        SEP=(45,40,60), FOOTER=(6,4,14), light=False, purple_fiber=True,
    ),
    "plaid": dict(
        BG=(10,10,12), PURPLE=(200,30,255), PURPLE_D=(90,10,130),
        CYAN=(0,210,245), CYAN_D=(0,130,160),
        TEXT=(12,10,18), MUTED=(30,24,44), DIM=(50,44,66),
        SEP=(40,36,56), FOOTER=(6,6,8), light=False, purple_fiber=False,
        plaid=True,
    ),
}

OUTPUT = {
    "carbon":       (ROOT/"frontend"/"pinned_card_carbon.png",      ROOT/"docs"/"pinned_card_carbon.png"),
    "whitecarbon":  (ROOT/"frontend"/"pinned_card_whitecarbon.png",  ROOT/"docs"/"pinned_card_whitecarbon.png"),
    "purplecarbon": (ROOT/"frontend"/"pinned_card_purplecarbon.png", ROOT/"docs"/"pinned_card_purplecarbon.png"),
    "plaid":        (ROOT/"frontend"/"pinned_card_plaid.png",        ROOT/"docs"/"pinned_card_plaid.png"),
}

# ── Font loader — Orbitron primary ────────────────────────────────────────────
_FONT_PATHS = [
    "/tmp/Orbitron.ttf",                                      # canonical brand font
    "/Library/Fonts/Orbitron-Regular.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
_cache: dict = {}

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _cache:
        for path in _FONT_PATHS:
            p = Path(path)
            if not p.exists(): continue
            try:
                idx = 1 if bold and path.endswith(".ttc") else 0
                _cache[key] = ImageFont.truetype(str(p), max(6, size), index=idx); break
            except Exception:
                try: _cache[key] = ImageFont.truetype(str(p), max(6, size)); break
                except Exception: continue
        if key not in _cache:
            _cache[key] = ImageFont.load_default()
    return _cache[key]

def _tw(draw, text, font):
    bb = draw.textbbox((0,0), text, font=font); return bb[2]-bb[0]
def _cx(draw, text, font): return (W-_tw(draw,text,font))//2
def _tracked_w(draw, text, font, sp):
    return sum(_tw(draw,ch,font)+sp for ch in text)-sp
def _tracked(draw, xy, text, font, fill, sp):
    x, y = xy
    for ch in text:
        draw.text((x,y), ch, font=font, fill=fill); x += _tw(draw,ch,font)+sp
def _tracked_cx(draw, text, font, sp):
    return (W-_tracked_w(draw,text,font,sp))//2

# ── Plaid / tartan background ─────────────────────────────────────────────────
def _draw_plaid_bg(img: Image.Image) -> None:
    """
    Classic black-and-white tartan with cool-grey transitions.
    White and black are the dominant bands; grey creates the authentic
    woven mid-tones at intersections.  No purple in the background —
    purple/cyan are reserved for the text layer above.
    """
    # Palette — strictly black · grey family · off-white
    BLK  = (8,   8,   9)     # near-black base
    DG   = (32,  32,  36)    # dark grey transition
    MG   = (72,  70,  78)    # mid grey
    LG   = (130, 128, 138)   # light grey highlight
    WHT  = (215, 213, 220)   # cool off-white

    # Symmetric tartan sett — wide black & white bands, grey accents
    # One full repeat ≈ 104 px; gives ~10 visible checks across 1080 px
    # fmt: off
    SETT = [
        (BLK, 20), (DG, 5),
        (WHT, 16), (DG, 5),
        (BLK, 10), (DG, 4),
        (MG,   8), (LG, 4), (WHT, 4), (LG, 4), (MG, 8),
        (DG,   4), (BLK, 10),
        (DG,   5), (WHT, 16),
        (DG,   5), (BLK,  1),
    ]
    # fmt: on

    # Build the colour strip for one period
    strip = []
    for col, w in SETT:
        strip.extend([col] * w)
    n = len(strip)

    def avg(a, b):
        """Average blend — gives authentic grey mid-tones where b/w threads cross."""
        return tuple((a[i] + b[i]) // 2 for i in range(3))

    # Build the square tile using a 2×2 twill weave
    tile = Image.new("RGB", (n, n))
    pix  = tile.load()
    for ty in range(n):
        vc = strip[ty]
        for tx in range(n):
            hc = strip[tx]
            # Twill: alternate which thread sits on top in each 2×2 block
            if (tx // 2 + ty // 2) % 2 == 0:
                # Warp (horizontal thread) on top
                pix[tx, ty] = avg(hc, avg(hc, vc))   # 75% hc, 25% vc
            else:
                # Weft (vertical thread) on top
                pix[tx, ty] = avg(vc, avg(vc, hc))   # 75% vc, 25% hc

    # Tile across the full canvas
    for y in range(0, H, n):
        for x in range(0, W, n):
            img.paste(tile, (x, y))

# ── Carbon fiber background — proper 2×2 twill weave ─────────────────────────
# Each fiber tow = CELL×CELL px. Repeat unit = 2*CELL × 2*CELL.
# Alternating horiz/vert fiber direction in a checkerboard of cells.
# Over/under weave factor + directional bevel recreate authentic CF depth.
def _draw_bg(img: Image.Image, t: dict) -> None:
    CELL   = 16    # fiber tow width in pixels — larger = more visible weave
    GROOVE = 2     # groove border thickness (prominent dark gap between fibers)

    if t.get("light"):
        base_v, fill_v, peak_v = 162.0, 205.0, 248.0
    else:
        base_v, fill_v, peak_v = 6.0, 20.0, 64.0

    xs = np.arange(W, dtype=np.float32)
    ys = np.arange(H, dtype=np.float32)
    xg, yg = np.meshgrid(xs, ys)

    # Cell indices and local pixel position
    ci  = (xg // CELL).astype(np.int32)
    cj  = (yg // CELL).astype(np.int32)
    lx  = xg % CELL   # 0 … CELL-1
    ly  = yg % CELL

    # Normalized position within cell (offset by groove so body=0..1)
    body  = float(CELL - GROOVE)
    nx    = np.clip((lx - GROOVE) / (body - 1), 0.0, 1.0)
    ny    = np.clip((ly - GROOVE) / (body - 1), 0.0, 1.0)

    # Hard groove on leading (top + left) edges
    is_groove = (lx < GROOVE) | (ly < GROOVE)

    # 2×2 repeat — fiber direction alternates in checkerboard
    qx = ci % 2
    qy = cj % 2
    is_horiz = ((qx + qy) % 2 == 0)

    # Highlight bell-curve — sharper (squared) for more vivid specular pop
    h_horiz = (1.0 - np.abs(ny * 2.0 - 1.0)) ** 0.7   # bright band at y=0.5
    h_vert  = (1.0 - np.abs(nx * 2.0 - 1.0)) ** 0.7   # bright band at x=0.5
    highlight = np.where(is_horiz, h_horiz, h_vert)

    # Over/under weave — strong contrast so the interlocking reads clearly
    over        = ((ci + cj) % 2 == 0)
    over_factor = np.where(over, 1.0, 0.52)

    # Directional bevel — top-left light source (45 % dimming at shadow edge)
    bevel = np.where(is_horiz,
                     1.0 - ny * 0.45,
                     1.0 - nx * 0.45)

    v = np.where(
        is_groove,
        base_v,
        fill_v + (peak_v - fill_v) * highlight * over_factor * bevel
    )
    v = np.clip(v, base_v, peak_v).astype(np.uint8)

    if t.get("purple_fiber"):
        r = np.clip(v.astype(np.int32) - 2, 0, 255).astype(np.uint8)
        g = np.clip(v.astype(np.int32) - 4, 0, 255).astype(np.uint8)
        b = np.clip(v.astype(np.int32) + 9, 0, 255).astype(np.uint8)
        arr = np.stack([r, g, b], axis=-1)
    elif t.get("light"):
        arr = np.stack([v, v, v], axis=-1)
    else:
        b = np.clip(v.astype(np.int32) + 1, 0, 255).astype(np.uint8)
        arr = np.stack([v, v, b], axis=-1)

    img.paste(Image.fromarray(arr))

# ── Top-only corner HUD brackets ──────────────────────────────────────────────
def _brackets(draw: ImageDraw.ImageDraw, t: dict, m=32, s=48, w=2) -> None:
    for bx, by in [(m,m), (W-m,m)]:          # top-left and top-right only
        sx = 1 if bx < W//2 else -1
        draw.line([(bx,by),(bx+sx*s,by)], fill=t["CYAN_D"], width=w)
        draw.line([(bx,by),(bx,by+s)],    fill=t["CYAN_D"], width=w)

# ── Orbital eye ───────────────────────────────────────────────────────────────
def _draw_eye(img: Image.Image, cx: int, cy: int, t: dict, size: int=94) -> Image.Image:
    draw = ImageDraw.Draw(img)
    ow=int(size*1.10); oh=int(size*0.36)
    draw.ellipse([cx-ow//2,cy-oh//2,cx+ow//2,cy+oh//2], outline=t["CYAN"], width=2)
    gap,tick=7,16
    draw.line([(cx-ow//2-gap-tick,cy),(cx-ow//2-gap,cy)], fill=t["CYAN"], width=1)
    draw.line([(cx+ow//2+gap,cy),(cx+ow//2+gap+tick,cy)], fill=t["CYAN"], width=1)
    draw.line([(cx,cy-oh//2-gap-8),(cx,cy-oh//2-gap)],   fill=t["CYAN"], width=1)
    draw.line([(cx,cy+oh//2+gap),(cx,cy+oh//2+gap+8)],   fill=t["CYAN"], width=1)
    ir=int(size*0.42)
    for angle in range(0,360,12):
        a0=math.radians(angle); a1=math.radians(angle+7)
        x0=cx+int(ir*math.cos(a0)); y0=cy+int(ir*math.sin(a0))
        x1=cx+int(ir*math.cos(a1)); y1=cy+int(ir*math.sin(a1))
        draw.line([(x0,y0),(x1,y1)], fill=t["PURPLE_D"], width=1)
    draw.ellipse([cx-ir,cy-ir,cx+ir,cy+ir], outline=t["PURPLE"], width=3)
    gr=int(size*0.20)
    glow=Image.new("RGBA",(W,H),(0,0,0,0)); gd=ImageDraw.Draw(glow)
    gd.ellipse([cx-gr,cy-gr,cx+gr,cy+gr], fill=(*t["PURPLE"],160))
    glow=glow.filter(ImageFilter.GaussianBlur(18))
    img=Image.alpha_composite(img.convert("RGBA"),glow).convert("RGB")
    draw=ImageDraw.Draw(img)
    pr=int(size*0.26)
    draw.ellipse([cx-pr,cy-pr,cx+pr,cy+pr], fill=(14,8,28) if not t["light"] else (230,225,245))
    draw.ellipse([cx-gr,cy-gr,cx+gr,cy+gr], fill=t["PURPLE"])
    dr=int(size*0.052)
    draw.ellipse([cx-dr,cy-dr,cx+dr,cy+dr], fill=(228,190,255))
    return img

# ── Centred glow text ─────────────────────────────────────────────────────────
def _glow(img, text, y, font, color, glow_color, radius=10, spacing=0):
    tmp=ImageDraw.Draw(img)
    x=_tracked_cx(tmp,text,font,spacing) if spacing else _cx(tmp,text,font)
    gl=Image.new("RGBA",(W,H),(0,0,0,0)); gd=ImageDraw.Draw(gl)
    if spacing: _tracked(gd,(x,y),text,font,(*glow_color,200),spacing)
    else:       gd.text((x,y),text,font=font,fill=(*glow_color,200))
    gl=gl.filter(ImageFilter.GaussianBlur(radius))
    img=Image.alpha_composite(img.convert("RGBA"),gl).convert("RGB")
    draw=ImageDraw.Draw(img)
    if spacing: _tracked(draw,(x,y),text,font,color,spacing)
    else:       draw.text((x,y),text,font=font,fill=color)
    return img

def _sep(draw, y, t, mx=72):
    draw.line([(mx,y),(W-mx,y)], fill=t["SEP"], width=1)

# ── Main card generator ───────────────────────────────────────────────────────
def generate(t: dict) -> Image.Image:
    img  = Image.new("RGB",(W,H),t["BG"])
    if t.get("plaid"):
        _draw_plaid_bg(img)
    else:
        _draw_bg(img, t)
    draw = ImageDraw.Draw(img)

    # Top-only corner brackets
    _brackets(draw, t)

    # Eye logo
    img  = _draw_eye(img, W//2, 148, t, size=90)
    draw = ImageDraw.Draw(img)

    # CLAIRVOYANCE title
    title_font = _font(88, bold=True)
    img  = _glow(img,"CLAIRVOYANCE",258,title_font,
                 color=t["PURPLE"],glow_color=t["PURPLE"],radius=18,spacing=4)
    draw = ImageDraw.Draw(img)

    # Subtitle — updated wording
    sub_font = _font(24)
    img  = _glow(img,"ADVANCED SPORTS INTELLIGENCE ENGINE",362,sub_font,
                 color=t["CYAN"],glow_color=t["CYAN"],radius=6,spacing=3)
    draw = ImageDraw.Draw(img)

    # Tagline — now in PURPLE, same shade as title
    tag_font = _font(18)
    x_tag = _tracked_cx(draw,"SEE WHAT OTHERS CANNOT",tag_font,3)
    _tracked(draw,(x_tag,402),"SEE WHAT OTHERS CANNOT",tag_font,t["PURPLE"],3)

    _sep(draw,442,t)

    # Description — updated copy
    desc_font  = _font(25)
    desc_lines = [
        "An advanced sports intelligence engine built around",
        "mathematical precision, multi-layered ensemble models,",
        "and adaptive intelligence.",
    ]
    dy = 468
    for line in desc_lines:
        x = _cx(draw,line,desc_font)
        draw.text((x,dy),line,font=desc_font,fill=t["TEXT"]); dy+=36

    _sep(draw,dy+18,t); dy+=46

    # Feature rows
    label_font = _font(20,bold=True)
    val_font   = _font(20)
    rows = [
        ("SPORTS COVERED",
         "NBA  ·  NFL  ·  NHL  ·  Soccer  ·  More Coming"),
        ("EVERY PICK GRADED",
         "Advanced Analytics  ·  Confidence Scores  ·  Market Edge"),
        ("ADAPTIVE ENGINE",
         "Recalibration  ·  Simulations  ·  Cutting Edge Statistical Analysis"),
    ]
    for label, value in rows:
        lx = _tracked_cx(draw,label,label_font,2)
        _tracked(draw,(lx,dy),label,label_font,t["CYAN"],2); dy+=30
        vx = _cx(draw,value,val_font)
        draw.text((vx,dy),value,font=val_font,fill=t["MUTED"]); dy+=48

    _sep(draw,dy+4,t); dy+=28

    # Follow block
    follow_font = _font(22,bold=True)
    fol_x = _tracked_cx(draw,"FOLLOW FOR DAILY SIGNALS",follow_font,2)
    _tracked(draw,(fol_x,dy),"FOLLOW FOR DAILY SIGNALS",follow_font,t["TEXT"],2)
    dy+=40

    # Handles — both in PURPLE (same shade as CLAIRVOYANCE header)
    lbl_font = _font(17); h_font = _font(26,bold=True); gap_mid = 36
    x_lbl_w  = _tw(draw,"X ",lbl_font)
    x_h_w    = _tw(draw,X_HANDLE,h_font)
    ig_lbl_w = _tw(draw,"IG ",lbl_font)
    ig_h_w   = _tw(draw,IG_HANDLE,h_font)
    total_w  = x_lbl_w+x_h_w+gap_mid+ig_lbl_w+ig_h_w
    sx       = (W-total_w)//2

    # "X" label + X handle (purple, with glow)
    draw.text((sx,dy+5),"X ",font=lbl_font,fill=t["MUTED"]); sx+=x_lbl_w
    gl=Image.new("RGBA",(W,H),(0,0,0,0)); gd=ImageDraw.Draw(gl)
    gd.text((sx,dy),X_HANDLE,font=h_font,fill=(*t["PURPLE"],200))
    gl=gl.filter(ImageFilter.GaussianBlur(8))
    img=Image.alpha_composite(img.convert("RGBA"),gl).convert("RGB")
    draw=ImageDraw.Draw(img)
    draw.text((sx,dy),X_HANDLE,font=h_font,fill=t["PURPLE"]); sx+=x_h_w+gap_mid

    # "IG" label + IG handle (purple, with glow)
    draw.text((sx,dy+5),"IG ",font=lbl_font,fill=t["MUTED"]); sx+=ig_lbl_w
    gl=Image.new("RGBA",(W,H),(0,0,0,0)); gd=ImageDraw.Draw(gl)
    gd.text((sx,dy),IG_HANDLE,font=h_font,fill=(*t["PURPLE"],200))
    gl=gl.filter(ImageFilter.GaussianBlur(8))
    img=Image.alpha_composite(img.convert("RGBA"),gl).convert("RGB")
    draw=ImageDraw.Draw(img)
    draw.text((sx,dy),IG_HANDLE,font=h_font,fill=t["PURPLE"])

    dy+=46

    # Domain
    dom_font = _font(20)
    dx = _cx(draw,DOMAIN,dom_font)
    draw.text((dx,dy),DOMAIN,font=dom_font,fill=t["CYAN_D"])

    # Footer bar
    draw.rectangle([(0,H-60),(W,H)],fill=t["FOOTER"])
    foot_font = _font(18,bold=True)
    draw.text((72,H-38),"CLAIRVOYANCE ENGINE",font=foot_font,fill=t["MUTED"])
    disc_font = _font(16)
    disc = "Model outputs are probabilistic projections, not financial advice."
    draw.text((_cx(draw,disc,disc_font),H-22),disc,font=disc_font,fill=t["DIM"])

    return img


if __name__ == "__main__":
    for name, theme in THEMES.items():
        print(f"Generating pinned_card_{name}.png…")
        img = generate(theme)
        for path in OUTPUT[name]:
            img.save(str(path), format="PNG", optimize=True)
            print(f"  Saved → {path}")
    print("Done — all 4 variants saved.")
