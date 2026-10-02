#!/usr/bin/env python3
"""Fails (exit 1) if the hockey qualification constants in scripts/auto_lock_settle.py (which only DESCRIBE the rules in the
subscriber email legend) differ from the ones docs/app.html actually grades and qualifies with (the "HOCKEY QUALIFICATION
CUTOFFS" block). Run it after retuning either side:  python3 scripts/verify_hockey_rules.py"""
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
html = (ROOT / "docs" / "app.html").read_text()


def js_num(name):
    m = re.search(r"const\s+" + name + r"\s*=\s*(-?[0-9.]+)", html)
    if m:
        return float(m.group(1))
    m = re.search(r"const [^;\n]*?\b" + name + r"\s*=\s*(-?[0-9.]+)", html)  # `const A=.65,B=.65,C=-.06;` style
    if not m:
        raise SystemExit(f"constant {name} not found in docs/app.html")
    return float(m.group(1))


def js_obj(name):
    m = re.search(r"const\s+" + name + r"\s*=\s*\{([^}]*)\}", html)
    if not m:
        raise SystemExit(f"object {name} not found in docs/app.html")
    return {k: float(v) for k, v in re.findall(r"(\w+)\s*:\s*(-?[0-9.]+)", m.group(1))}


def js_str(name):
    m = re.search(r"const\s+" + name + r"\s*=\s*'([^']*)'", html)
    if not m:
        raise SystemExit(f"string {name} not found in docs/app.html")
    return m.group(1)


import auto_lock_settle as A  # noqa: E402

checks = [
    ("HOCKEY_TIER_PROB", js_obj("HOCKEY_TIER_PROB"), A.HOCKEY_TIER_PROB),
    ("HOCKEY_TIER_EV", js_obj("HOCKEY_TIER_EV"), A.HOCKEY_TIER_EV),
    ("HOCKEY_LANE_ML_P", js_num("HOCKEY_LANE_ML_P"), A.HOCKEY_LANE_ML_P),
    ("HOCKEY_LANE_PLDOG_P", js_num("HOCKEY_LANE_PLDOG_P"), A.HOCKEY_LANE_PLDOG_P),
    ("HOCKEY_LANE_EV_MIN", js_num("HOCKEY_LANE_EV_MIN"), A.HOCKEY_LANE_EV_MIN),
    ("HOCKEY_ODDS_MAX_AGE_H", js_num("HOCKEY_ODDS_MAX_AGE_H"), A.HOCKEY_ODDS_MAX_AGE_H),
    ("HOCKEY_LANE_LABEL", js_str("HOCKEY_LANE_LABEL"), A.HOCKEY_LANE_LABEL),
    ("LOCK_START_MARGIN_MIN", js_num("LOCK_START_MARGIN_MIN"), A.LOCK_START_MARGIN_MIN),
    ("HOCKEY_REQUIRE_REAL_PRICE", "true" in re.search(r"const HOCKEY_REQUIRE_REAL_PRICE=(\w+)", html).group(1), A.HOCKEY_REQUIRE_REAL_PRICE),
]
bad = 0
for name, js, py in checks:
    ok = (js == py) if not isinstance(js, float) else abs(js - py) < 1e-9
    print(("OK   " if ok else "DIFF ") + f"{name}: app.html={js}  auto_lock_settle.py={py}")
    bad += 0 if ok else 1
sys.exit(1 if bad else 0)
