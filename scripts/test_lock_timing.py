#!/usr/bin/env python3
"""Tests for scripts/lock_timing.py (the shared known-late classifier).

    python3 scripts/test_lock_timing.py              # unit tests (no network, no browser)
    python3 scripts/test_lock_timing.py --js-parity  # also: every pick in docs/picks_backup.json classified by the JS mirror
                                                     # (_pickTiming in docs/app.html, headless Chromium on localhost, all
                                                     # non-local requests aborted) must match Python exactly
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lock_timing as lt  # noqa: E402

MIN = 60_000.0
START = 1_790_000_000_000.0  # an arbitrary start (ms)


def pick(lead_min, sport="NHL", **kw):
    """A pick locked `lead_min` minutes BEFORE START (negative = after)."""
    p = {"id": f"p{lead_min}", "sport": sport, "league": sport, "hA": "A", "awA": "B", "date": "2026-10-01",
         "startMs": START, "lockedAt": START - lead_min * MIN, "outcome": "win"}
    p.update(kw)
    return p


class Classify(unittest.TestCase):
    def test_pre_boundary(self):
        self.assertEqual(lt.classify(pick(60), {}), "pre")
        self.assertEqual(lt.classify(pick(0.001), {}), "pre")
        self.assertEqual(lt.classify(pick(0), {}), "during")  # locked AT the start = not before it

    def test_during_after_cutoffs_per_league(self):
        for lg, dur in lt.DUR_MIN.items():
            self.assertEqual(lt.classify(pick(-(dur - 1), lg), {}), "during", lg)
            self.assertEqual(lt.classify(pick(-dur, lg), {}), "after", lg)  # lead == -dur is already "after" (lead > -dur is during)
            self.assertEqual(lt.classify(pick(-(dur + 1), lg), {}), "after", lg)

    def test_unlisted_league_uses_default(self):
        self.assertEqual(lt.classify(pick(-(lt.DEFAULT_DUR_MIN - 1), "WNBA"), {}), "during")
        self.assertEqual(lt.classify(pick(-(lt.DEFAULT_DUR_MIN + 1), "WNBA"), {}), "after")

    def test_unknown_when_no_start_or_no_lock_time(self):
        p = pick(10)
        del p["startMs"]
        self.assertEqual(lt.classify(p, {}), "unknown")
        self.assertEqual(lt.classify(pick(10, lockedAt=None), {}), "unknown")
        self.assertEqual(lt.classify(pick(10, lockedAt=0), {}), "unknown")
        self.assertFalse(lt.is_known_late(p, {}))  # unknown stays INCLUDED

    def test_garbage_start_ignored(self):
        p = pick(10, startMs="soon")
        self.assertEqual(lt.classify(p, {}), "unknown")
        p = pick(10, startMs=5)  # epoch seconds-looking value is rejected, not guessed
        self.assertEqual(lt.classify(p, {}), "unknown")

    def test_late_manual_stamp_without_start_is_known_late(self):
        p = pick(10, lockTiming="late-manual")
        del p["startMs"]
        self.assertEqual(lt.classify(p, {}), "during")
        self.assertTrue(lt.is_known_late(p, {}))

    def test_stored_start_wins_over_index(self):
        key = lt.game_key("2026-10-01", "A", "B")
        idx = {key: START + 600 * MIN}  # index says the game is much later
        self.assertEqual(lt.classify(pick(-30), idx), "during")  # stored startMs (30 min ago) decides

    def test_index_match_either_orientation_and_mt_date(self):
        idx = {lt.game_key("2026-10-01", "Home FC", "Away FC"): START}
        p = {"sport": "SOC", "league": "PL", "hA": "Away FC", "awA": "Home FC", "date": "2026-10-01", "lockedAt": START - 5 * MIN}
        self.assertEqual(lt.classify(p, idx), "pre")
        p["date"] = "2026-10-02"  # different Mountain date -> no match
        self.assertEqual(lt.classify(p, idx), "unknown")
        # a game starting 02:00Z belongs to the PREVIOUS Mountain date (America/Denver)
        g = {"date": "2026-10-02T02:00Z", "home": "X", "away": "Y"}
        i2: dict = {}
        lt.add_game(i2, g, "home", "away")
        self.assertIn(lt.game_key("2026-10-01", "X", "Y"), i2)

    def test_history_is_fallback_behind_live_schedule(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            docs = Path(d) / "docs"
            docs.mkdir()
            (docs / lt.HISTORY_FILE).write_text(json.dumps({"games": {"2026-10-01|A|B": 111_000_000_000_000.0, "2026-10-01|C|D": 5e12}}))
            (docs / "nhl_schedule.json").write_text(json.dumps({"games": [{"date": "2026-10-01T19:00Z", "home": "A", "away": "B"}]}))
            idx = lt.load_index(Path(d))
            self.assertEqual(idx["2026-10-01|A|B"], lt.iso_ms("2026-10-01T19:00Z"))  # live wins
            self.assertEqual(idx["2026-10-01|C|D"], 5e12)  # history fills the gap
            self.assertNotIn("2026-10-01|C|D", lt.load_index(Path(d), include_history=False))

    def test_missing_files_do_not_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(lt.load_index(Path(d)), {})


class NormSport(unittest.TestCase):
    def test_broad_tag_defers_to_league(self):
        self.assertEqual(lt.norm_sport({"sport": "FOOTBALL", "league": "CFB"}), "CFB")
        self.assertEqual(lt.norm_sport({"sport": "FOOTBALL"}), "NFL")
        self.assertEqual(lt.norm_sport({"sport": "SOCCER", "league": "PL"}), "PL")
        self.assertEqual(lt.norm_sport({"sport": "HOCKEY", "league": "SHL"}), "SHL")
        self.assertEqual(lt.norm_sport({"sport": "NHL"}), "NHL")
        self.assertEqual(lt.norm_sport({"sport": "BL"}), "BUND")
        self.assertEqual(lt.norm_sport({}), "")


class Aggregates(unittest.TestCase):
    def setUp(self):
        self.picks = [pick(30, id="pre1"), pick(-10, id="late1"), pick(-200, id="late2"),
                      {"id": "unk", "sport": "NBA", "outcome": "loss", "lockedAt": 1.0, "hA": "x", "awA": "y", "date": "2026-01-01"},
                      pick(-10, id="pend", outcome="pending")]

    def test_split_and_ids(self):
        kept, late = lt.split_picks(self.picks, {})
        self.assertEqual({p["id"] for p in late}, {"late1", "late2", "pend"})
        self.assertEqual({p["id"] for p in kept}, {"pre1", "unk"})
        self.assertEqual(lt.late_ids(self.picks, {}), {"late1", "late2", "pend"})

    def test_summarize_counts_settled_only(self):
        s = lt.summarize(self.picks, {})
        self.assertEqual(s["settled_pre_start"], 1)
        self.assertEqual(s["settled_excluded_late"], 2)
        self.assertEqual(s["settled_excluded_in_progress"], 1)
        self.assertEqual(s["settled_excluded_after_end"], 1)
        self.assertEqual(s["settled_unknown_timing_included"], 1)
        self.assertEqual(s["unknown_timing_by_league"], {"NBA": 1})
        self.assertEqual(s["basis"], lt.BASIS)

    def test_basis_fields_machine_readable(self):
        f = lt.basis_fields(self.picks, {})
        self.assertEqual(f["basis"], lt.BASIS)
        self.assertIn("basis_detail", f)


class RealLedger(unittest.TestCase):
    """Sanity on the committed ledger backup: the classifier's totals must stay consistent with the audit script."""

    def test_backup_classifies_without_error(self):
        p = lt.ROOT / "docs" / "picks_backup.json"
        if not p.exists():
            self.skipTest("no backup")
        picks = json.loads(p.read_text())
        idx = lt.load_index()
        s = lt.summarize(picks, idx)
        total = s["settled_pre_start"] + s["settled_excluded_late"] + s["settled_unknown_timing_included"]
        self.assertEqual(total, sum(1 for x in picks if lt.settled(x)))
        self.assertTrue(all(x.get("id") for x in picks if lt.is_known_late(x, idx)))  # every excluded pick is excludable by id


def js_parity() -> int:
    """Python vs the JS mirror, pick by pick. Returns the number of mismatches."""
    import functools
    import http.server
    import socketserver
    import threading
    from playwright.sync_api import sync_playwright

    picks = json.loads((lt.ROOT / "docs" / "picks_backup.json").read_text())
    idx = lt.load_index()
    py = {p["id"]: lt.classify(p, idx) for p in picks}

    class Q(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 8793), functools.partial(Q, directory=str(lt.ROOT / "docs")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    bad = 0
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            ctx = b.new_context(timezone_id="America/Denver")
            ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1:8793/") else r.abort())
            pg = ctx.new_page()
            pg.goto("http://127.0.0.1:8793/app.html", wait_until="load", timeout=60000)
            pg.wait_for_timeout(2500)
            js = pg.evaluate("""async (picks) => { await _tbLoad(); const o = {}; picks.forEach(p => { o[p.id] = _pickTiming(p); }); return o; }""", picks)
            b.close()
    finally:
        srv.shutdown()
        srv.server_close()
    diffs = [(i, py[i], js.get(i)) for i in py if py[i] != js.get(i)]
    bad = len(diffs)
    print(f"JS parity: {len(py)} picks compared, {bad} mismatch(es)")
    for d in diffs[:15]:
        print("  MISMATCH", d)
    return bad


if __name__ == "__main__":
    want_js = "--js-parity" in sys.argv
    if want_js:
        sys.argv.remove("--js-parity")
    res = unittest.main(exit=False, verbosity=1)
    rc = 0 if res.result.wasSuccessful() else 1
    if want_js:
        rc = rc or (1 if js_parity() else 0)
    sys.exit(rc)
