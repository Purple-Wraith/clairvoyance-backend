#!/usr/bin/env python3
"""End-to-end simulation of a European-hockey SETTLE PASS in headless Chromium on localhost (no network, no Supabase, no email).

    python3 scripts/test_settle_pass_sim.py

What is real: docs/app.html and its real settle functions (autoSettleLiiga/Shl/Nla/Extraliga via auto_lock_settle.run_settle, the
same function every settle slot calls), the real ledger (docs/picks_backup.json, loaded with saveP instead of the Supabase pull),
the real committed 2026-10-02 schedule snapshots (scripts/fixtures/schedule_carry), served by auto_lock_settle.start_local_site
exactly like `--serve-local`.  What is simulated: the clock (Date frozen with page.clock) and the results re-scrape (the merge it
performs is applied directly with _schedule_carry.merge_results_only).  Every non-localhost request is ABORTED, so nothing can reach
Supabase/ESPN/anything; live=False so nothing is flushed; the email sender is replaced by a function that fails the test if called.

Scenarios (one fresh browser page each):
  A  noon file + carry-over (games in progress are carried as 'pre'):  only the two Extraliga games that really were final settle;
     NOTHING is graded off a carried row.
  B  the same files after the results-only refresh folded in the 4:04 PM finals:  every pending 10-02 pick settles, each with the
     outcome an independent oracle computes from the final score.
  C  B again: a second pass settles nothing more (idempotent) and the ledger size never changes (a settle pass locks nothing).
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _schedule_carry as C  # noqa: E402
import auto_lock_settle as A  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
FIX = Path(__file__).resolve().parent / "fixtures" / "schedule_carry"
SCHED = {"liiga": "liiga_schedule.json", "nla": "nla_schedule.json", "extraliga": "extraliga_schedule.json", "shl": "shl_schedule.json"}
NOON = {"liiga": "1153mt", "nla": "1155mt", "extraliga": "1156mt"}
MORNING = {"liiga": "0757mt", "nla": "0756mt", "extraliga": "0756mt"}
EVENING = {"liiga": "1604mt", "nla": "1604mt", "extraliga": "1604mt"}
TAGS = {"liiga": "LIIGA", "nla": "NLA", "extraliga": "EXTRALIGA"}


def fx(lg, tag):
    return json.loads((FIX / f"{lg}_{tag}.json").read_text())


def build_files(stage: str) -> dict:
    """{league: schedule doc} for stage 'carried' (noon scrape + carry-over) or 'refreshed' (carried + results-only merge)."""
    out = {}
    for lg in ("liiga", "nla", "extraliga"):
        noon, morning, evening = fx(lg, NOON[lg]), fx(lg, MORNING[lg]), fx(lg, EVENING[lg])
        games = copy.deepcopy(noon["games"])
        C.carry_over_missing(games, morning, C.parse_generated_at(noon["generated_at"]))
        doc = {"generated_at": noon["generated_at"], "teams": {}, "games": games}
        if stage == "refreshed":
            finals = [g for g in evening["games"] if g["state"] == "post"]
            doc, _ = C.merge_results_only(doc, finals, datetime(2026, 10, 2, 22, 5, tzinfo=timezone.utc))
        out[lg] = doc
    out["shl"] = {"generated_at": "2026-10-02 13:55 UTC", "teams": {}, "games": []}
    return out


def oracle(pick, game):
    """Independent re-statement of hockey grading (final score = incl. OT/SO)."""
    hs, as_ = game["homeScore"], game["awayScore"]
    b = pick["betOn"].strip()
    low = b.lower()
    import re
    if low.startswith("over") or low.startswith("under"):
        m = re.search(r"(\d+\.?\d*)\s*$", b)
        if m:
            line, total = float(m.group(1)), hs + as_
            if total == line:
                return "push"
            return "win" if ((low.startswith("over") and total > line) or (low.startswith("under") and total < line)) else "loss"
    m = re.match(r"^(.+?)\s+([+-]\d+\.?\d*)\s*$", b)
    if m and m.group(1).strip() in (game["homeName"], game["awayName"]):
        team, line = m.group(1).strip(), float(m.group(2))
        diff = (hs - as_) if team == game["homeName"] else (as_ - hs)
        c = diff + line
        return "win" if c > 0 else "loss" if c < 0 else "push"
    team = b[:-3] if b.endswith(" ML") else b
    if team == game["homeName"]:
        return "win" if hs > as_ else "loss"
    if team == game["awayName"]:
        return "win" if as_ > hs else "loss"
    return None


class SettlePassSim(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls._pw = sync_playwright().start()
            cls.browser = cls._pw.chromium.launch()
        except Exception as exc:  # pragma: no cover
            raise unittest.SkipTest(f"playwright/chromium unavailable: {exc}")
        cls.ledger = json.loads((DOCS / "picks_backup.json").read_text())
        cls.pending_1002 = [p for p in cls.ledger if p.get("outcome") == "pending" and p.get("date") == "2026-10-02"
                            and p.get("sport") in TAGS.values()]
        assert len(cls.pending_1002) >= 20, "fixture assumption: the real ledger backup still holds the 10-02 pending picks"

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
            cls._pw.stop()
        except Exception:
            pass

    def run_pass(self, files: dict, now_iso: str, picks=None, passes=1):
        """Serve a docs copy whose 4 schedule files are `files`, drive the real app, run the real run_settle. -> (settled per pass,
        ledger after, blocked-host list, ledger size before/after)."""
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, True)
        for entry in DOCS.iterdir():
            if entry.name in SCHED.values():
                continue
            (td / entry.name).symlink_to(entry)
        for lg, fn in SCHED.items():
            (td / fn).write_text(json.dumps(files[lg]))
        url, srv = A.start_local_site(td)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        blocked: list[str] = []
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 1000})
        self.addCleanup(ctx.close)
        page = ctx.new_page()

        def gate(route):
            u = route.request.url
            if u.startswith("http://127.0.0.1") or u.startswith("data:") or u.startswith("blob:"):
                route.continue_()
            else:
                blocked.append(u)
                route.abort()

        page.route("**/*", gate)
        page.clock.set_fixed_time(datetime.fromisoformat(now_iso.replace("Z", "+00:00")))
        page.add_init_script("window.setInterval = () => 0;")  # same as the headless pass
        page.goto(A.with_nologo(url), wait_until="load", timeout=60000)
        page.wait_for_timeout(2500)
        ledger = copy.deepcopy(picks if picks is not None else self.ledger)
        page.evaluate("(l) => saveP(l)", ledger)
        before = page.evaluate("() => getP().length")
        settled_passes = []
        with mock.patch.object(A, "_send_gmail", side_effect=AssertionError("a settle pass must never send email")):
            for _ in range(passes):
                settled_passes.append(A.run_settle(page, live=False))
        after_ledger = page.evaluate("() => getP()")
        return settled_passes, after_ledger, blocked, (before, len(after_ledger))

    def games_by_key(self, files):
        out = {}
        for lg, tag in TAGS.items():
            for g in files[lg]["games"]:
                out[(tag, g["homeName"], g["awayName"], g["date"])] = g
        return out

    def test_A_carried_rows_never_settle_anything(self):
        files = build_files("carried")
        # the files really contain carried rows and the incident's missing finals
        self.assertTrue(any(g.get("carried") for lg in ("liiga", "nla", "extraliga") for g in files[lg]["games"]))
        settled, after, blocked, (n0, n1) = self.run_pass(files, "2026-10-02T18:30:00Z")
        by_id = {p["id"]: p for p in after}
        finals = [g for lg in ("liiga", "nla", "extraliga") for g in files[lg]["games"] if g["state"] == "post"
                  and g["date"].startswith("2026-10-02")]
        final_keys = {(g["homeName"], g["awayName"]) for g in finals}
        n_settled = 0
        for p in self.pending_1002:
            now_p = by_id[p["id"]]
            on_final = (p["hA"], p["awA"]) in final_keys or (p["awA"], p["hA"]) in final_keys
            if on_final:
                game = next(g for g in finals if {g["homeName"], g["awayName"]} == {p["hA"], p["awA"]})
                self.assertEqual(now_p["outcome"], oracle(p, game), p["id"])
                n_settled += 1
            else:
                self.assertEqual(now_p["outcome"], "pending", f"{p['id']} was graded without a final score")
        self.assertGreater(n_settled, 0)
        self.assertLess(n_settled, len(self.pending_1002))  # the carried games are exactly the ones still waiting
        self.assertEqual(len(settled[0]), n_settled)
        self.assertEqual(n0, n1)

    def test_B_after_the_results_refresh_everything_settles_correctly(self):
        files = build_files("refreshed")
        for lg in ("liiga", "nla", "extraliga"):
            self.assertFalse(any(g.get("carried") for g in files[lg]["games"] if g["date"].startswith("2026-10-02")))
        settled, after, blocked, (n0, n1) = self.run_pass(files, "2026-10-02T22:20:00Z")
        by_id = {p["id"]: p for p in after}
        games = self.games_by_key(files)
        graded = 0
        for p in self.pending_1002:
            game = next(g for (t, h, a, d), g in games.items() if t == p["sport"] and {h, a} == {p["hA"], p["awA"]}
                        and d.startswith("2026-10-02"))
            exp = oracle(p, game)
            self.assertIsNotNone(exp, p["id"])
            got = by_id[p["id"]]
            self.assertEqual(got["outcome"], exp, f"{p['id']}: {game['homeName']} {game['homeScore']}-{game['awayScore']} {game['awayName']}")
            self.assertIsNotNone(got.get("settledAt"))
            graded += 1
        self.assertEqual(graded, len(self.pending_1002))
        # a settle pass touches nothing else: every other pick is byte-identical, none added or removed
        untouched = {p["id"]: p for p in self.ledger if p["id"] not in {x["id"] for x in self.pending_1002}}
        for pid, p in untouched.items():
            self.assertEqual(by_id[pid], p, pid)
        self.assertEqual(n0, n1)
        self.assertEqual(len(settled[0]), len(self.pending_1002))
        # nothing left the machine
        self.assertTrue(all(not u.startswith("http://127.0.0.1") for u in blocked))
        print(f"\n[sim B] {graded} picks graded; {len(blocked)} non-local requests aborted (supabase/espn/etc), 0 allowed")

    def test_C_second_pass_is_idempotent(self):
        files = build_files("refreshed")
        settled, after, _blocked, (n0, n1) = self.run_pass(files, "2026-10-02T22:20:00Z", passes=2)
        self.assertEqual(len(settled[0]), len(self.pending_1002))
        self.assertEqual(settled[1], [])
        self.assertEqual(n0, n1)

    def test_D_fresh_checkout_copy_is_what_the_pass_reads(self):
        # --serve-local serves the checkout dir: a file changed on disk is what the app loads (no Pages in the loop)
        files = build_files("refreshed")
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, True)
        (td / "liiga_schedule.json").write_text(json.dumps(files["liiga"]))
        (td / "app.html").write_text("<html></html>")
        url, srv = A.start_local_site(td)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        import urllib.request
        base = url.rsplit("/", 1)[0]
        body = json.loads(urllib.request.urlopen(base + "/liiga_schedule.json", timeout=5).read())
        self.assertEqual(body["games"], files["liiga"]["games"])
        self.assertEqual(urllib.request.urlopen(base + "/liiga_schedule.json", timeout=5).headers.get("Cache-Control"), "no-store")

    def test_E_post_pass_ledger_fingerprint_and_backup_refresh(self):
        # the main() addition: after a settle that changed the ledger, picks_backup.json is rewritten from the in-page ledger
        files = build_files("refreshed")
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, True)
        (td / "docs").mkdir()
        for entry in DOCS.iterdir():
            if entry.name in SCHED.values():
                continue
            (td / "docs" / entry.name).symlink_to(entry)
        (td / "docs" / "picks_backup.json").unlink()
        (td / "docs" / "picks_backup.json").write_text("[]")
        for lg, fn in SCHED.items():
            (td / "docs" / fn).write_text(json.dumps(files[lg]))
        url, srv = A.start_local_site(td / "docs")
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        ctx = self.browser.new_context()
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1") else r.abort())
        page.clock.set_fixed_time(datetime(2026, 10, 2, 22, 20, tzinfo=timezone.utc))
        page.add_init_script("window.setInterval = () => 0;")
        page.goto(A.with_nologo(url), wait_until="load", timeout=60000)
        page.wait_for_timeout(2500)
        page.evaluate("(l) => saveP(l)", copy.deepcopy(self.ledger))
        fp0 = A.ledger_fingerprint(page)
        self.assertEqual(A.ledger_fingerprint(page), fp0)  # stable when nothing changed
        with mock.patch.object(A, "ROOT", td):
            A.run_settle(page, live=False)
            fp1 = A.ledger_fingerprint(page)
            self.assertNotEqual(fp1, fp0)  # the settle changed the ledger -> main() would rewrite the backup
            n = A.write_ledger_backup(page)
        written = json.loads((td / "docs" / "picks_backup.json").read_text())
        self.assertEqual(n, len(written))
        still_pending = [p for p in written if p["outcome"] == "pending" and p["date"] == "2026-10-02"
                         and p["sport"] in TAGS.values()]
        self.assertEqual(still_pending, [])  # the next settle gate sees them as settled


if __name__ == "__main__":
    unittest.main(verbosity=1)
