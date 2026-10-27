"""Slightly softer neon-magenta glow for the Reddit ad + league scorecard (design_system's presets are shared with the locked pinned/
subscription cards, so the softening lives here, not there).  The tight core layer is kept; every wider halo is scaled by K."""
from design_system import TITLE_GLOW, SOCIAL_GLOW, HDR_GLOW

K = 0.75


def soft(layers, k: float = K):
    return [layers[0]] + [(r, round(a * k)) for r, a in layers[1:]]


MAG_TITLE_GLOW = soft(TITLE_GLOW)
MAG_SOCIAL_GLOW = soft(SOCIAL_GLOW)
MAG_LABEL_GLOW = soft([(1, 255), (3, 110)])
MAG_HDR_GLOW = soft(HDR_GLOW)
