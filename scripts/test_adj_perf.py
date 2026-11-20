"""Pins the adjusted-line scorecard numbers (adj_perf.py, a port of the app's ADJ. LINES table) -- grading rules, the app's own win % rule, and that the table adds up."""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ad_coverage as ac, adj_perf
import design_system as D


def pick(**kw):
    p = {"sport": "NBA", "betType": "OU", "betOn": "OVER 220.5", "hA": "AAA", "awA": "BBB", "hScore": 110, "aScore": 108, "outcome": "win",
         "altLine": {"line": 215.5, "posted": 220.5, "shift": 5}}
    p.update(kw)
    return p


class Grading(unittest.TestCase):
    def stats(self, *picks):
        s = adj_perf._blank()
        for p in picks:
            adj_perf.tally(p, s)
        return s

    def test_saved_means_lost_at_posted_won_at_locked(self):
        s = self.stats(pick())                                   # total 218: over 215.5 wins, over 220.5 loses
        self.assertEqual((s["aw"], s["al"], s["bpw"], s["bpl"], s["saved"], s["cost"]), (1, 0, 0, 1, 1, 0))

    def test_posted_line_is_skipped_when_regrading_the_locked_line_disagrees_with_the_stored_outcome(self):
        s = self.stats(pick(outcome="loss"))                     # stored loss, but 218 > 215.5 would be a win: do not trust a posted result
        self.assertEqual((s["settled"], s["al"], s["both"], s["bpw"] + s["bpl"]), (1, 1, 0, 0))

    def test_pending_picks_count_as_picks_not_results(self):
        s = self.stats(pick(outcome="pending", hScore=None, aScore=None))
        self.assertEqual((s["n"], s["pending"], s["settled"], s["aw"] + s["al"]), (1, 1, 0, 0))

    def test_win_pct_is_wins_over_wins_plus_losses_whole_number(self):
        self.assertEqual(adj_perf.pct(349, 140), "71%")
        self.assertEqual(adj_perf.pct(0, 0), "--")
        self.assertEqual(adj_perf.rec(5, 2, 1), "5-2-1")

    def test_only_real_adjusted_picks_and_never_removed_ones(self):
        self.assertIsNone(adj_perf.alt_of({"altLine": None}))
        self.assertIsNone(adj_perf.alt_of({"altLine": {"line": "x", "posted": 1}}))
        self.assertIsNotNone(adj_perf.alt_of(pick()))


class Ledger(unittest.TestCase):
    def test_table_adds_up_and_covers_only_the_12_leagues(self):
        d = adj_perf.load()
        self.assertTrue(set(n for n, _ in d["rows"]) <= set(ac.ALL_LEAGUES))
        for k in ("n", "pending", "settled", "aw", "al", "ap", "both", "bpw", "bpl", "saved", "cost"):
            self.assertEqual(sum(s[k] for _, s in d["rows"]), d["overall"][k], k)
        o = d["overall"]
        self.assertEqual(o["n"], o["pending"] + o["settled"])
        self.assertEqual(o["aw"] + o["al"] + o["ap"], o["settled"])
        self.assertGreaterEqual(o["aw"], o["saved"])             # a saved pick is a locked-line win
        self.assertLessEqual(o["saved"] + o["cost"], o["both"])      # saved / cost only exist on picks graded both ways

    def test_rows_sorted_most_picks_first(self):
        ns = [s["n"] for _, s in adj_perf.load()["rows"]]
        self.assertEqual(ns, sorted(ns, reverse=True))

    @unittest.skipUnless(Path(D.ORB_PATH).exists() and Path(D.SYS_PATH).exists(), "card text needs the Mac-only Orbitron/SF Mono fonts (CI does not have them)")
    def test_card_renders(self):
        import gen_adjusted_scorecard as g
        self.assertEqual(g.build_card().size, (1080, 1080))


if __name__ == "__main__":
    unittest.main()
