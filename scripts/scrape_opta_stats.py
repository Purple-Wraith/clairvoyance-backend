#!/usr/bin/env python3
"""
scrape_opta_stats.py — Daily team-stats + power-rankings refresh for
BL/PL/Serie A/La Liga/MLS.

Pulls real team totals (attacking, passing, pressing, sequences, defending)
plus Opta's own team-strength Power Rankings (a 0-100 rolling rating,
updated as the season plays out) from theanalyst.com's public soccerdata
JSON API. This script re-fetches that data on a schedule so the frontend's
_SOC_XG fallback table and _socXG/_soccerMC's live blend never go stale
the way they had (rosters still listing relegated Ipswich/Venezia/Bochum,
xG figures frozen at whatever a human typed in months ago).

tmcl (tournament/competition-season id) is per-season, not permanent —
each league's tmcl here was re-confirmed against the live 2026/27-season
page on 2026-08-21 (PL/Serie A/Bundesliga's previous tmcls were still
pinned to the completed 2025/26 season and had frozen there; La Liga
added new this session). When a season rolls over, re-derive the new
tmcl the same way: load https://theanalyst.com/competition/{slug}/stats
and grep the page for `tmcl":"..."` in the
`wp-block-sdapi-blocks-stats-page` block's embedded config — the same
tmcl also serves /power-rankings and /table for that league-season, so
one lookup covers every soccerdata resource for that competition.

Writes:
  - data/opta_reference/{league}_team_stats_2026_27.json  (full scrape:
    attacking/passing/pressing/sequences/defending/powerRankings)
  - docs/{league}_opta_stats.json — same payload, served straight to the
    frontend (loadLeagueOptaStats() in app.html)
  - Regenerates the corresponding block inside docs/app.html's _SOC_XG
    object (per-game xg/xga/gf/ga, +/-10% home-away split — same
    convention that table already used before this script existed).
    Power Rankings aren't part of _SOC_XG (that table only ever held
    xg/xga) — they're consumed live from docs/{league}_opta_stats.json
    by _optaPowerRankFactors() in app.html instead.

Usage:
  python3 scripts/scrape_opta_stats.py            # scrape + write, no push
  python3 scripts/scrape_opta_stats.py --push     # scrape + write + commit + push
  python3 scripts/scrape_opta_stats.py --dry-run  # print, no file writes
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys
from pathlib import Path
import requests

ROOT = Path(__file__).parent.parent
APP = ROOT / "docs" / "app.html"
INDEX = ROOT / "docs" / "index.html"
REF_DIR = ROOT / "data" / "opta_reference"
REF_DIR.mkdir(parents=True, exist_ok=True)

API_BASE = "https://theanalyst.com/wp-json/sdapi/v1/soccerdata"
API = f"{API_BASE}/tournamentstats"
POWER_API = f"{API_BASE}/seasonpowerrankings"

# league key -> (tmcl competition-season id, referer page + _meta_post_id
# the API checks against — requests without a matching Referer 401, since
# this endpoint is meant to be called from within the page it's embedded
# on, not hit directly), real team-name -> _SOC_XG key map, and whether to
# also pull Power Rankings for this league (MLS has its own real-xG
# first-party pipeline already and isn't one of Opta's tracked Power
# Rankings competitions the way the 4 European leagues are).
LEAGUES: dict[str, dict] = {
    "cl": {
        # tmcl confirmed live 2026-09-01 by capturing the real network
        # request theanalyst.com's own frontend makes on this page (the
        # static-HTML grep this docstring describes for the other 5
        # leagues found nothing for CL -- the block renders its config
        # client-side here rather than embedding it server-rendered).
        # meta_post_id likewise pulled from the page's own post-<id>
        # marker and confirmed against the real API.
        #
        # CL DOES have real Power Rankings (theanalyst.com/competition/
        # uefa-champions-league/power-rankings is real, not a 404 -- an
        # earlier version of this comment wrongly said otherwise, from
        # testing the wrong API. The wp-json/sdapi/v1/soccerdata/
        # seasonpowerrankings endpoint the domestic leagues use genuinely
        # 404s for CL's tmcl, but the real page doesn't call that one --
        # it calls a plain static JSON file on a different subdomain,
        # dataviz.theanalyst.com/project-data/soccer/{tmcl}/
        # power-rankings.json (found the same way as tmcl/meta_post_id
        # above: capturing the page's own real network request). No
        # session/cookie trick needed for this one -- confirmed a plain
        # requests.get() with no special headers returns 200. See
        # fetch_dataviz_power_rankings() below.
        #
        # Also confirmed while investigating this: Real Madrid's rating
        # from that CL-scoped file (94.75) exactly matches their rating
        # from the DOMESTIC La Liga-scoped fetch -- Opta's Power Ranking
        # is one single global rating system, not computed separately
        # per competition. The per-league "seasonpowerrankings" fetches
        # below are just filtered views into that same global table.
        # (This matters: an earlier attempt to compare ratings across
        # leagues for CL applied a manual per-league strength offset,
        # assuming each league's ratings were on its own separate scale
        # -- wrong assumption, reverted once this was confirmed.)
        #
        # UPDATE 2026-09-09: confirmed the season DID roll to a new tmcl,
        # exactly as the note below anticipated -- the old one
        # (2mr0u0l78k2gdsm79q56tb2fo) was still frozen on 2025/26's final
        # totals (lastUpdated 2026-06-02) as late as today, a full day
        # after the real 2026/27 league phase kicked off (Sept 8).
        # Re-captured the live network request theanalyst.com's frontend
        # makes on this same referer page and found the new id below --
        # confirmed it returns real current-season data (lastUpdated
        # 2026-09-09, 12 teams with stats so far matching the actual
        # staggered league-phase schedule -- AEK Athens/Aston Villa/
        # Dortmund/Club Brugge/Porto/Inter/LASK/Lille/Man City/Real
        # Betis/Real Madrid/Villarreal, all real 2026/27 CL participants
        # per this app's own ESPN standings, not last season's field).
        # meta_post_id is unchanged -- it identifies the referer PAGE
        # itself, not the season, and the page didn't move.
        #
        # Original note (kept for the next time this needs re-deriving):
        # tmcl (tournament/competition-season id) is per-season and WILL
        # go stale again once 2026/27 ends -- when this same "old season's
        # frozen totals" symptom reappears, re-verify by capturing the
        # live network request this same referer page makes (this file's
        # own top docstring describes the process), the same way this
        # value and the one before it were both found.
        "tmcl": "99jev9kv55deht65t6myggxlg",
        "referer": "https://theanalyst.com/competition/uefa-champions-league/stats",
        "power_referer": None,
        "meta_post_id": "194412",
        "file": "champions_league_team_stats_2026_27.json",
        "power": True,
        "power_source": "dataviz",
        # Deliberately None, same reasoning as MLS below: CL teams are
        # each already carrying a domestic-league _SOC_XG entry (their
        # season-long base rate across 30-38 games). Writing CL-specific
        # attacking/defending into those SAME shared keys would let an
        # 8-game European sample distort a team's base xG used for every
        # one of their matches in every competition, not just CL -- so
        # this stays reference-only (docs/cl_opta_stats.json), blended in
        # ONLY for CL matches specifically via _blendLeagueOpta/
        # _optaBaseXG in app.html (added to _OPTA_LEAGUES), never merged
        # into the static table.
        "name_map": None,
    },
    "pl": {
        "tmcl": "6pdwluctev9iebv00r4qqukno",
        "referer": "https://theanalyst.com/competition/premier-league/stats",
        "power_referer": "https://theanalyst.com/competition/premier-league/power-rankings",
        "meta_post_id": "135731",
        "file": "premier_league_team_stats_2026_27.json",
        "power": True,
        "name_map": {
            "Man Utd": "manchester united", "Leeds": "leeds", "Arsenal": "arsenal",
            "Newcastle": "newcastle", "Spurs": "tottenham", "Villa": "aston villa",
            "Chelsea": "chelsea", "Everton": "everton", "Liverpool": "liverpool",
            "Forest": "nottingham forest", "West Ham": "west ham", "Palace": "crystal palace",
            "Brighton": "brighton", "Wolves": "wolverhampton", "Man City": "manchester city",
            "Fulham": "fulham", "Sunderland": "sunderland", "Burnley": "burnley",
            "Bournemouth": "bournemouth", "Brentford": "brentford",
        },
    },
    "liga": {
        "tmcl": "830epggffy1nfkfyrtpqdwhlg",
        "referer": "https://theanalyst.com/competition/la-liga/stats",
        "power_referer": "https://theanalyst.com/competition/la-liga/power-rankings",
        "meta_post_id": "135739",
        "file": "la_liga_team_stats_2026_27.json",
        "power": True,
        "name_map": {
            "Barcelona": "barcelona", "Real Madrid": "real madrid", "Atlético": "atlético madrid",
            "Villarreal": "villarreal", "Betis": "real betis", "Valencia": "valencia",
            "Celta": "celta vigo", "Rayo": "rayo vallecano", "Alavés": "alavés",
            "Real Sociedad": "real sociedad", "Athletic": "athletic club", "Osasuna": "osasuna",
            "Getafe": "getafe", "Espanyol": "espanyol", "Levante": "levante", "Sevilla": "sevilla",
            "Santander": "racing santander", "Elche": "elche", "Málaga": "málaga",
            "Deportivo": "deportivo",
        },
    },
    "ita": {
        "tmcl": "60cryos85i4bp5ul34tt0brx0",
        "referer": "https://theanalyst.com/competition/serie-a/stats",
        "power_referer": "https://theanalyst.com/competition/serie-a/power-rankings",
        "meta_post_id": "135738",
        "file": "serie_a_team_stats_2026_27.json",
        "power": True,
        "name_map": {
            "Inter": "inter milan", "Napoli": "napoli", "Juventus": "juventus", "Milan": "ac milan",
            "Atalanta": "atalanta", "Roma": "roma", "Lazio": "lazio", "Fiorentina": "fiorentina",
            "Bologna": "bologna", "Torino": "torino", "Udinese": "udinese", "Genoa": "genoa",
            "Verona": "hellas verona", "Cagliari": "cagliari", "Parma": "parma", "Como": "como",
            "Lecce": "lecce", "Pisa": "pisa", "Cremonese": "cremonese", "Sassuolo": "sassuolo",
        },
    },
    "bl": {
        "tmcl": "8h5xijv2u4mlf5028gso6kw7o",
        "referer": "https://theanalyst.com/competition/bundesliga/stats",
        "power_referer": "https://theanalyst.com/competition/bundesliga/power-rankings",
        "meta_post_id": "135740",
        "file": "bundesliga_team_stats_2026_27.json",
        "power": True,
        "name_map": {
            "FC Bayern": "bayern munich", "Bayer 04": "bayer leverkusen",
            "Dortmund": "borussia dortmund", "Leipzig": "rb leipzig",
            "Frankfurt": "eintracht frankfurt", "Stuttgart": "vfb stuttgart",
            "Freiburg": "sc freiburg", "Hoffenheim": "tsg hoffenheim",
            "M'gladbach": "borussia monchengladbach", "Union Berlin": "union berlin",
            "Wolfsburg": "vfl wolfsburg", "Bremen": "werder bremen", "Augsburg": "augsburg",
            "Mainz": "mainz 05", "Heidenheim": "fc heidenheim", "Hamburger": "hamburger sv",
            "Köln": "fc koln", "St. Pauli": "st pauli",
        },
    },
    "mls": {
        "tmcl": "6i6n0jkbh9zzij6s8htfjh2j8",
        "referer": "https://theanalyst.com/competition/mls/stats",
        "power_referer": "https://theanalyst.com/competition/mls/power-rankings",
        "meta_post_id": "202451",
        "file": "mls_team_stats_2026.json",
        # MLS IS one of Opta's tracked Power Rankings competitions after
        # all (confirmed live at theanalyst.com/competition/mls/power-
        # rankings, same tmcl/meta_post_id as its stats page) — just not a
        # tournament-stats source, since mlssoccer.com's own real xG
        # already covers that. Power Rankings is a genuinely different,
        # independent signal (whole-squad Elo-style rating vs this
        # season's own goals), so it's still additive even though MLS
        # already has the best base xG of all 6 leagues.
        "power": True,
        # MLS already has a dedicated first-party stats pipeline
        # (fetch_mls_team_stats() in clairvoyance_update.py) that feeds
        # docs/data.json / _socXG's live path — this scrape is saved as
        # reference data only, not merged into _SOC_XG's static fallback,
        # so it doesn't fight with that existing real-time source.
        "name_map": None,
    },
}


def fetch_tournament_stats(page, tmcl: str, referer: str, meta_post_id: str) -> dict | None:
    """Loads the real stats page in an actual browser (the API 401s on a
    plain HTTP request — same Cloudflare-style bot-check this repo already
    documents hitting FBref with — but works fine once a genuine page/
    session context makes the call) and pulls the JSON via the page's own
    fetch(), exactly like a human loading the page would trigger it."""
    url = f"{API}?tmcl={tmcl}&_meta_post_id={meta_post_id}&_meta_subpage=stats"
    try:
        page.goto(referer, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        result = page.evaluate(f"""
            fetch({json.dumps(url)}).then(r => r.ok ? r.json() : null)
        """)
        return result
    except Exception as exc:
        print(f"[WARN] fetch failed for tmcl={tmcl}: {exc}", file=sys.stderr)
        return None


def fetch_power_rankings(page, tmcl: str, referer: str, meta_post_id: str) -> dict | None:
    """Same session-cookie-via-real-page-load trick as fetch_tournament_stats,
    against the seasonpowerrankings resource instead. Opta's Power Rankings
    are a rolling 0-100 team-strength rating (same system their own
    Supercomputer predictor uses to seed match-outcome probabilities before
    simulating a season) -- this is what changes week to week as results
    come in, unlike the season-cumulative attacking/defending totals above."""
    url = f"{POWER_API}?tmcl={tmcl}&_meta_post_id={meta_post_id}&_meta_subpage=power-rankings"
    try:
        page.goto(referer, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        result = page.evaluate(f"""
            fetch({json.dumps(url)}).then(r => r.ok ? r.json() : null)
        """)
        return result
    except Exception as exc:
        print(f"[WARN] power-rankings fetch failed for tmcl={tmcl}: {exc}", file=sys.stderr)
        return None


def slim_power_rankings(raw: dict) -> list[dict]:
    """Flattens the 'global' division ranking block down to just what the
    frontend blend (_optaPowerRankFactors in app.html) actually needs: team
    name + its current rating. Keeps rank/globalRank too since they're free
    and useful for display even though the blend itself only uses rating."""
    divisions = raw.get("division") or []
    global_div = next((d for d in divisions if d.get("type") == "global"), divisions[0] if divisions else {})
    ranking = global_div.get("ranking") or []
    return [
        {
            "team": r.get("contestantShortName") or r.get("contestantName"),
            "rating": r.get("currentRating"),
            "rank": int(r["rank"]) if r.get("rank") is not None else None,
            "globalRank": int(r["currentGlobalRank"]) if r.get("currentGlobalRank") is not None else None,
        }
        for r in ranking
    ]


DATAVIZ_POWER_API = "https://dataviz.theanalyst.com/project-data/soccer"

def fetch_dataviz_power_rankings(tmcl: str) -> dict | None:
    """CL-specific alternative to fetch_power_rankings() above -- the real
    CL Power Rankings page doesn't call the wp-json/sdapi/v1/soccerdata/
    seasonpowerrankings API the domestic leagues use (that 404s for CL's
    tmcl); it calls this plain static JSON file on a different subdomain
    instead, found by capturing the real page's own network request the
    same way tmcl/meta_post_id were found. No browser session/cookie
    trick needed here -- a plain unauthenticated GET returns 200."""
    url = f"{DATAVIZ_POWER_API}/{tmcl}/power-rankings.json"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        print(f"[WARN] dataviz power-rankings fetch failed for tmcl={tmcl}: {exc}", file=sys.stderr)
        return None


def slim_dataviz_power_rankings(raw: dict) -> list[dict]:
    """Same output shape as slim_power_rankings() above (team/rating/rank/
    globalRank) so the frontend's _optaPowerRankFactors() doesn't need a
    separate code path for CL -- just a flatter input shape to unpack
    (top-level "ranking" list, not nested under a "division" block, and
    "lastWeekGlobalRank" instead of "currentGlobalRank")."""
    ranking = raw.get("ranking") or []
    return [
        {
            "team": r.get("contestantShortName") or r.get("contestantClubName"),
            "rating": r.get("currentRating"),
            "rank": int(r["rank"]) if r.get("rank") is not None else None,
            "globalRank": int(r["lastWeekGlobalRank"]) if r.get("lastWeekGlobalRank") is not None else None,
        }
        for r in ranking
    ]


def slim_categories(team_block: dict) -> dict:
    """Extract the 5 requested categories (attacking/passing/pressing/
    sequences/defending) per team from the raw tournamentstats payload."""
    def pick(rows, fields):
        out = []
        for t in rows:
            row = {"team": t.get("contestantShortName") or t.get("team")}
            for f in fields:
                row[f] = t.get(f)
            out.append(row)
        return out

    attack = team_block.get("attack", {}).get("overall", [])
    poss = team_block.get("possession", {}).get("overall", [])
    seq = team_block.get("sequences", {}).get("overall", [])
    defend = team_block.get("defending", {}).get("overall", [])

    return {
        "attacking": pick(attack, ["played", "goals", "xg", "goals_vs_xg", "total_shots",
                                    "sot", "shot_conv", "xg_per_shot"]),
        "passing": pick(poss, ["pos_perc", "passes", "successful_pass", "accuracy",
                                "final_third_passes", "successful_final_third_passes_perc",
                                "op_crosses", "through_balls"]),
        "pressing": pick(seq, ["ppda", "high_turnovers", "shot_ending_high_turnovers",
                                "defensive_actions"]),
        "sequences": pick(seq, ["pressed_sequences", "build_ups", "build_up_goals",
                                 "direct_attacks", "direct_attack_goals", "ten_plus_passes",
                                 "direct_speed_for", "seq_time_for"]),
        "defending": pick(defend, ["goals_against", "xg_against", "goals_vs_xg_conceded",
                                    "total_shots_against", "sot_against",
                                    "shot_conv_against", "xg_per_shot_against"]),
    }


# Matches _SOC_HOME_ADV.default in docs/app.html — keep these in sync.
# MLS gets a distinctly larger boost there (0.16 vs 0.10), but MLS's
# name_map is None so it never reaches this function (only pl/liga/ita/bl
# do — see main()'s `if cfg["name_map"]:` gate), so there's no MLS case
# to handle here.
SOC_HOME_ADV = 0.10

def build_soc_xg_lines(slim: dict, name_map: dict) -> list[str]:
    """Per-game xg/xga/gf/ga with the same +/-10% home-away split
    convention _SOC_XG already used before this script existed."""
    att = {t["team"]: t for t in slim["attacking"]}
    deff = {t["team"]: t for t in slim["defending"]}
    lines = []
    for short, key in name_map.items():
        a, d = att.get(short), deff.get(short)
        if not a or not d or not a.get("played"):
            print(f"[WARN] missing data for '{short}' — skipping", file=sys.stderr)
            continue
        played = a["played"]
        xg_pg = a["xg"] / played
        xga_pg = d["xg_against"] / played
        gf_pg = a["goals"] / played
        ga_pg = d["goals_against"] / played
        hxg, axg = round(xg_pg * (1 + SOC_HOME_ADV), 3), round(xg_pg * (1 - SOC_HOME_ADV), 3)
        hxga, axga = round(xga_pg * (1 - SOC_HOME_ADV), 3), round(xga_pg * (1 + SOC_HOME_ADV), 3)
        lines.append(
            f"  '{key}':{{xg:{round(xg_pg,3)},xga:{round(xga_pg,3)},hxg:{hxg},hxga:{hxga},"
            f"axg:{axg},axga:{axga},gf:{round(gf_pg,3)},ga:{round(ga_pg,3)},mp:{played}}},"
        )
    return lines


_STATIC_TABLE_MP_RE = re.compile(r"mp:(\d+)")

def replace_league_block(html: str, name_map: dict, new_lines: list[str]) -> str:
    """Replace each existing `'key':{...},` entry for this league's teams
    in _SOC_XG in place, preserving surrounding structure/comments. Falls
    back to leaving unmatched keys untouched (roster changes — promotions/
    relegations — need a manual one-off edit like the initial migration,
    same as the docstring for fetch_mls_rosters already documents for
    similar roster-coverage gaps).

    Never lets a patch DECREASE a team's mp (games-played sample size).
    This ran daily against every team the Opta scrape found data for,
    regardless of how much bigger a sample the existing entry already
    had -- so an established club sitting on a full 38-game prior-season
    baseline got silently overwritten with a noisy n=1/n=2 current-season
    read the very first day Opta's tournamentstats endpoint returned
    anything for it, and every day after until the new season caught up.
    A newly-promoted club with no prior-season entry at all (mp effectively
    0) still updates immediately, same as before -- this only blocks a
    same-team downgrade, not first-time population.
    """
    new_by_key = {}
    for line in new_lines:
        key = line.split("'")[1]
        new_by_key[key] = line

    for key, new_line in new_by_key.items():
        # Trailing comma is optional — the last entry in a block (right
        # before the closing `};`) has none. Capture it so the
        # replacement preserves whichever the original line had, instead
        # of only ever matching entries that happen to have a comma.
        pattern = re.compile(r"^  '" + re.escape(key) + r"':\{[^}]*\}(,?)\s*$", re.MULTILINE)
        m = pattern.search(html)
        if m:
            old_mp_m = _STATIC_TABLE_MP_RE.search(m.group(0))
            new_mp_m = _STATIC_TABLE_MP_RE.search(new_line)
            old_mp = int(old_mp_m.group(1)) if old_mp_m else 0
            new_mp = int(new_mp_m.group(1)) if new_mp_m else 0
            if new_mp < old_mp:
                print(f"[INFO] '{key}': keeping existing mp={old_mp} entry over thinner "
                      f"mp={new_mp} scrape — not downgrading sample size", file=sys.stderr)
                continue
            trailing_comma = m.group(1)
            replacement = new_line.rstrip().rstrip(",") + trailing_comma
            html = pattern.sub(replacement, html, count=1)
        else:
            print(f"[WARN] key '{key}' not found in _SOC_XG — needs manual roster update "
                  f"(promoted/relegated team not yet in the table)", file=sys.stderr)
    return html


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    html = APP.read_text(encoding="utf-8")
    original_html = html
    any_changes = False
    # Tracks whether any docs/{league}_opta_stats.json was (re)written this
    # run, independent of any_changes (which only reflects the _SOC_XG
    # static-table patch below, gated on cfg["name_map"] existing). Without
    # this, a run where e.g. MLS's Opta detail legitimately updated but
    # PL/Serie A/Bundesliga's xg numbers happened not to move would write
    # the JSON files locally and then never reach the push step at all.
    any_json_written = False

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        for lg_key, cfg in LEAGUES.items():
            print(f"[INFO] {lg_key}: fetching tournament stats…")
            raw = fetch_tournament_stats(page, cfg["tmcl"], cfg["referer"], cfg["meta_post_id"])
            if not raw or "team" not in raw:
                print(f"[WARN] {lg_key}: no data returned, skipping", file=sys.stderr)
                continue
            slim = slim_categories(raw["team"])
            slim["league"] = raw["team"].get("league")
            slim["lastUpdated"] = raw["team"].get("lastUpdated")
            slim["source"] = "theanalyst.com (Opta) tournament stats API"

            if cfg.get("power") and cfg.get("power_source") == "dataviz":
                print(f"[INFO] {lg_key}: fetching power rankings (dataviz)…")
                power_raw = fetch_dataviz_power_rankings(cfg["tmcl"])
                if power_raw and power_raw.get("ranking"):
                    slim["powerRankings"] = slim_dataviz_power_rankings(power_raw)
                    slim["powerRankingsUpdated"] = power_raw.get("lastUpdated")
                    print(f"[INFO]   {len(slim['powerRankings'])} teams ranked")
                else:
                    print(f"[WARN] {lg_key}: no power-rankings data returned", file=sys.stderr)
            elif cfg.get("power"):
                print(f"[INFO] {lg_key}: fetching power rankings…")
                power_raw = fetch_power_rankings(page, cfg["tmcl"], cfg["power_referer"], cfg["meta_post_id"])
                if power_raw and power_raw.get("division"):
                    slim["powerRankings"] = slim_power_rankings(power_raw)
                    slim["powerRankingsUpdated"] = power_raw.get("lastUpdated")
                    print(f"[INFO]   {len(slim['powerRankings'])} teams ranked")
                else:
                    print(f"[WARN] {lg_key}: no power-rankings data returned", file=sys.stderr)

            out_path = REF_DIR / cfg["file"]
            if args.dry_run:
                print(f"[DRY-RUN] would write {out_path} ({len(slim['attacking'])} teams)")
            else:
                out_path.write_text(json.dumps(slim, indent=2), encoding="utf-8")
                print(f"[INFO] wrote {out_path} ({len(slim['attacking'])} teams)")
                # Every league's slim categories are also served straight to
                # the browser (docs/ is the GitHub Pages root), not just
                # MLS's — loadLeagueOptaStats(key) in app.html fetches
                # docs/{key}_opta_stats.json for the offense/defense radar
                # breakdown (attacking/passing/sequences for offense,
                # pressing/defending for defense) and, for MLS specifically,
                # also feeds _mlsStyleFactor()'s existing MC adjustment.
                # Previously only MLS's copy was synced here, so PL/Serie A/
                # Bundesliga's passing/pressing/sequences/defending detail
                # was fetched, used once to refresh _SOC_XG's xg/xga
                # fallback numbers, and then discarded — never reaching the
                # frontend at all despite being scraped every run.
                (ROOT / "docs" / f"{lg_key}_opta_stats.json").write_text(
                    json.dumps(slim, indent=2), encoding="utf-8"
                )
                print(f"[INFO] synced docs/{lg_key}_opta_stats.json")
                any_json_written = True

            if cfg["name_map"]:
                new_lines = build_soc_xg_lines(slim, cfg["name_map"])
                if not args.dry_run:
                    updated = replace_league_block(html, cfg["name_map"], new_lines)
                    if updated != html:
                        html = updated
                        any_changes = True
                else:
                    print(f"[DRY-RUN] {lg_key} _SOC_XG lines:\n" + "\n".join(new_lines))

        browser.close()

    if any_changes and not args.dry_run:
        APP.write_text(html, encoding="utf-8")
        INDEX.write_text(html, encoding="utf-8")
        print("[INFO] Wrote docs/app.html + docs/index.html")

        val = subprocess.run(["python3", str(ROOT / "scripts" / "validate.py")],
                              capture_output=True, text=True, cwd=ROOT)
        if val.returncode != 0:
            print("[ERROR] Validator FAILED after inject — reverting!", file=sys.stderr)
            APP.write_text(original_html, encoding="utf-8")
            INDEX.write_text(original_html, encoding="utf-8")
            print(val.stdout[-2000:])
            sys.exit(1)
        print("[INFO] Validator: all checks passed")

    if args.push and (any_changes or any_json_written) and not args.dry_run:
        msg = "chore: refresh BL/PL/Serie A/La Liga/MLS Opta team stats + power rankings"
        add_paths = ["docs/app.html", "docs/index.html", "data/opta_reference"]
        add_paths += [f"docs/{lg_key}_opta_stats.json" for lg_key in LEAGUES]
        subprocess.run(["git", "add", *add_paths], cwd=ROOT, check=True)
        r = subprocess.run(["git", "commit", "-m", msg], cwd=ROOT, capture_output=True, text=True)
        if r.returncode == 0:
            print("[INFO] Committed.")
            push = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT,
                                   capture_output=True, text=True)
            if push.returncode != 0:
                print(f"[WARN] Push rejected, retrying with rebase: {push.stderr.strip()[:200]}")
                pull = subprocess.run(["git", "pull", "--rebase", "origin", "main"],
                                       cwd=ROOT, capture_output=True, text=True)
                if pull.returncode == 0:
                    push2 = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT,
                                            capture_output=True, text=True)
                    if push2.returncode != 0:
                        print(f"[ERROR] Push still failed: {push2.stderr.strip()[:300]}", file=sys.stderr)
                else:
                    print(f"[ERROR] Rebase failed: {pull.stderr.strip()[:300]}", file=sys.stderr)
            else:
                print("[INFO] Pushed.")
        elif "nothing to commit" in r.stdout + r.stderr:
            print("[INFO] Nothing changed — skipping commit.")
        else:
            print(f"[WARN] Commit failed: {r.stderr}", file=sys.stderr)

    print("[INFO] Done.")


if __name__ == "__main__":
    main()
