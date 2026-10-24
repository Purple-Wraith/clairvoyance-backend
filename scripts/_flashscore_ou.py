"""Over/Under records from Flashscore's league standings "OVER/UNDER" tab, shared by fetch_nhl_ou.py and the four European hockey scrapers
(fetch_liiga.py / fetch_shl.py / fetch_nla.py / fetch_extraliga.py).  Added 2026-10-24 (owner: "make use of 4.5, 5.5, 6.5 and 7.5", and the home / away split where relevant).

Flashscore has one page per goal line; each row is a team: games played, how many of them finished OVER the line, how many UNDER, goals for:against and goals per game
(MP, O, U, F:A, G/M).  We keep the four lines 4.5 / 5.5 / 6.5 / 7.5 for the team's OVERALL, HOME and AWAY games.  The counts are regulation + overtime goals (the shootout-winning
goal is not counted, as sportsbooks settle totals).

Record shape (one team):  {"overall": {"gp": 12, "ou": {"4.5": [11, 1], "5.5": [10, 2], "6.5": [8, 4], "7.5": [5, 7]}}, "home": {...}, "away": {...}}   # ou[line] = [over, under]

Accuracy over speed (2026-10-24): every (scope, line) page is loaded fresh.  An earlier version clicked between the tab links to save time and was caught reading a STALE table for some
home / away lines (UTA home 6.5 read 4/4 over; the real page says 3/4), so that path was removed.  Checks on every scrape: over + under == games on every line, over counts only fall as the
line rises, no two lines show an identical table, and HOME + AWAY == OVERALL for every team and line.  A team that fails the last check is re-read once (a game can finish between the
overall and the home / away page) and, if it still fails, keeps its overall record only.  Images / fonts / media are blocked on the scraping page only.
"""
from __future__ import annotations

import re
from typing import Callable

LINES = (4.5, 5.5, 6.5, 7.5)
SCOPES = ("overall", "home", "away")
TEAM_HREF_RE = re.compile(r"/team/([^/]+)/([A-Za-z0-9]+)/")
_ROW = "[class*='ui-table__row']"


def ou_base(url: str) -> str:
    """'.../standings/ID/over_under/overall/6.5/' (or any deeper / shallower form of it) -> '.../standings/ID/over_under'."""
    return re.sub(r"(/over_under)(/.*)?$", r"\1", url.rstrip("/"))


def parse_rows(rows) -> dict:
    """Rows of one Over/Under standings page -> {fsTeamId: {name, gp, over, under, gf, ga, gm}}.  Pure DOM reading, no network."""
    out: dict = {}
    for row in rows:
        link = row.query_selector("a.tableCellParticipant__name") or row.query_selector(".table__cell--participant a")
        if not link:
            continue
        m = TEAM_HREF_RE.search(link.get_attribute("href") or "")
        if not m:
            continue
        over_el, under_el = row.query_selector(".table__cell--over"), row.query_selector(".table__cell--under")
        cells = [c.inner_text().strip() for c in row.query_selector_all(".table__cell--value")]
        if not over_el or not under_el or len(cells) < 5:
            continue
        try:
            over, under = int(over_el.inner_text().strip()), int(under_el.inner_text().strip())
            gp = int(cells[0])
            gm = float(cells[-1])
        except ValueError:
            continue
        gf = ga = None
        sm = re.match(r"^(\d+)\s*:\s*(\d+)$", cells[3])
        if sm:
            gf, ga = int(sm.group(1)), int(sm.group(2))
        out[m.group(2)] = {"name": (link.inner_text() or "").strip(), "gp": gp, "over": over, "under": under, "gf": gf, "ga": ga, "gm": gm}
    return out


def clean_scope(per_line: dict) -> bool:
    """per_line = {line: {gp, over, under}} for one scope: over + under must equal the games played at every line, and OVER counts can only fall as the line rises."""
    if not per_line:
        return False
    gps = {v["gp"] for v in per_line.values()}
    if len(gps) != 1:
        return False
    gp = gps.pop()
    last = None
    for line in sorted(per_line):
        o, u = per_line[line]["over"], per_line[line]["under"]
        if o < 0 or u < 0 or o + u != gp:
            return False
        if last is not None and o > last:
            return False
        last = o
    return True


def _goto(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector(".table__cell--over", timeout=25000)
    page.wait_for_timeout(500)


def _read(page, base: str, scope: str, line: float) -> dict:
    """One page, loaded fresh -> {fsId: row}."""
    _goto(page, f"{base}/{scope}/{line}/")
    return parse_rows(page.query_selector_all(_ROW))


def _assemble(raw: dict, lines) -> dict:
    teams: dict = {}
    for fsid, by_scope in raw.items():
        ov = by_scope.get("overall") or {}
        if len(ov) != len(lines) or not clean_scope(ov):
            continue
        t = {"name": ov[lines[0]]["name"], "gm": ov[lines[0]]["gm"], "gf": ov[lines[0]]["gf"], "ga": ov[lines[0]]["ga"]}
        for scope, per_line in by_scope.items():
            if len(per_line) == len(lines) and clean_scope(per_line):
                t[scope] = {"gp": next(iter(per_line.values()))["gp"], "ou": {f"{l}": [per_line[l]["over"], per_line[l]["under"]] for l in lines}}
        teams[fsid] = t
    return teams


def split_mismatches(teams: dict, lines) -> dict:
    """{fsId: [lines]} where HOME + AWAY != OVERALL (games or overs).  Only teams that have both splits are checked."""
    bad: dict = {}
    for fsid, t in teams.items():
        if "home" not in t or "away" not in t:
            continue
        if t["home"]["gp"] + t["away"]["gp"] != t["overall"]["gp"]:
            bad[fsid] = list(lines)
            continue
        for l in lines:
            k = f"{l}"
            if t["home"]["ou"][k][0] + t["away"]["ou"][k][0] != t["overall"]["ou"][k][0]:
                bad.setdefault(fsid, []).append(l)
    return bad


def scrape_teams(context, base: str, scopes=SCOPES, lines=LINES, log: Callable = lambda m: None) -> dict:
    """-> {fsTeamId: {"name", "gm", "gf", "ga", "overall": {gp, ou}, "home": {...}, "away": {...}}} for every team whose OVERALL rows pass clean_scope().
    HOME + AWAY must equal OVERALL (see the module docstring); a team that cannot be made to agree keeps its overall record only."""
    page = context.new_page()
    try:
        page.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font") else r.continue_())
        raw: dict = {}
        sigs: dict = {}
        for scope in scopes:
            for line in lines:
                rows = _read(page, base, scope, line)
                sig = tuple(sorted((k, v["over"], v["under"]) for k, v in rows.items()))
                for (sc2, ln2), other in sigs.items():
                    if sc2 == scope and sig and sig == other:
                        raise RuntimeError(f"{scope} {line} shows the same table as {scope} {ln2} (stale page) -- refusing to use it")
                sigs[(scope, line)] = sig
                for fsid, r in rows.items():
                    raw.setdefault(fsid, {}).setdefault(scope, {})[line] = r
        teams = _assemble(raw, lines)
        bad = split_mismatches(teams, lines) if "home" in scopes and "away" in scopes else {}
        if bad:
            log(f"  home + away != overall for {len(bad)} team(s); re-reading those lines")
            for line in sorted({l for ls in bad.values() for l in ls}):
                for scope in scopes:
                    for fsid, r in _read(page, base, scope, line).items():
                        raw.setdefault(fsid, {}).setdefault(scope, {})[line] = r
            teams = _assemble(raw, lines)
            bad = split_mismatches(teams, lines)
            for fsid in bad:
                log(f"  {teams[fsid]['name']}: home/away split still disagrees with overall -- split dropped")
                teams[fsid].pop("home", None)
                teams[fsid].pop("away", None)
        log(f"  Over/Under {base.split('flashscore.com')[-1]}: {len(teams)} clean teams")
        return teams
    finally:
        try:
            page.close()
        except Exception:
            pass


def fetch_ou_pair(context, cur_url: str, prior_url: str | None, prev_doc: dict | None, log: Callable = lambda m: None) -> tuple[dict, dict]:
    """For a European league script: -> (current, prior), both keyed by Flashscore team id.
        current = {id: {"overall": {...}, "home": {...}, "away": {...}}}          (this season, all three scopes)
        prior   = {id: {"gp": 60, "ou": {...}}}                                   (last season, overall only -- re-used from the previous file once scraped)
    Never raises: a failed scrape falls back to what the previous committed file already carried for each team."""
    prev_teams = (prev_doc or {}).get("teams") or {}
    cur: dict = {}
    try:
        for fsid, t in scrape_teams(context, ou_base(cur_url), SCOPES, LINES, log).items():
            cur[fsid] = {k: t[k] for k in SCOPES if k in t}
    except Exception as e:  # noqa: BLE001 -- fail-open, the schedule refresh must never depend on this
        log(f"  Over/Under (current) failed: {str(e)[:140]}")
    for fsid, t in prev_teams.items():
        if fsid not in cur and isinstance(t, dict) and t.get("ou"):
            cur[fsid] = t["ou"]
    prior: dict = {fsid: (t.get("prevSeason") or {})["ou"] for fsid, t in prev_teams.items()
                   if isinstance(t, dict) and isinstance(t.get("prevSeason"), dict) and (t["prevSeason"].get("ou") or {}).get("ou")}
    if prior_url and len(prior) < max(4, int(0.8 * len(cur))):
        try:
            for fsid, t in scrape_teams(context, ou_base(prior_url), ("overall",), LINES, log).items():
                prior[fsid] = t["overall"]
        except Exception as e:  # noqa: BLE001
            log(f"  Over/Under (prior season) failed: {str(e)[:140]}")
    return cur, prior
