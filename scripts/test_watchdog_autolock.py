#!/usr/bin/env python3
"""run_watchdog(auto_lock=True): which runner handles which sport, the kickoff window, dry-run, failure handling. Everything the watchdog calls is stubbed:
no browser, no Supabase, no email."""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_lock_settle as a  # noqa: E402

NOW = 1_800_000_000_000.0


def q(sport, label, mins, hA="HOM", awA="AWY"):
    return {"kind": "GAME", "sport": sport, "hA": hA, "awA": awA, "label": label, "side": "mlFav", "prob": .7, "tierN": 3,
            "startMs": NOW + mins * 60000, "lockDate": "2026-10-05"}


class Harness:
    def __init__(self, legs, locked_after=None, runner_error=None):
        self.legs, self.calls, self.owner, self.status = legs, [], [], []
        self.locked, self.locked_after, self.runner_error = set(), locked_after or {}, runner_error
        self.orig = {k: getattr(a, k) for k in ("gather_legs", "gather_hockey_legs_for_dates", "gather_cfb_legs_for_dates", "gather_soccer_legs_for_dates",
                                                "build_qualifying", "leg_locked_in_ledger", "run_cfb_evening_lock", "run_soccer_evening_lock",
                                                "run_hockey_evening_lock", "run_lock_segmented", "send_owner_alert", "write_automation_status", "recipients_for")}
        a.gather_legs = lambda page: {}
        a.gather_hockey_legs_for_dates = a.gather_cfb_legs_for_dates = a.gather_soccer_legs_for_dates = lambda page, dates: {}
        a.build_qualifying = lambda res, only_sports=None, now=None, **k: [l for l in self.legs if (only_sports is None or l["sport"] in only_sports)]
        a.leg_locked_in_ledger = lambda page, leg, d: leg["label"] in self.locked
        for name in ("run_cfb_evening_lock", "run_soccer_evening_lock", "run_hockey_evening_lock"):
            setattr(a, name, self._runner(name))
        a.run_lock_segmented = lambda page, live, send_email=True, only_new=False: self._runner("run_lock_segmented")(page, live)
        a.send_owner_alert = lambda subject, html: (self.owner.append(subject) or True)
        a.write_automation_status = lambda *x, **k: self.status.append(x)
        a.recipients_for = lambda product: [f"{product}@x.com"]

    def _runner(self, name):
        def run(page, live, send_email=True, to=None, now=None):
            self.calls.append(name)
            if self.runner_error:
                raise RuntimeError(self.runner_error)
            self.locked |= set(self.locked_after.get(name, []))
            return 1
        return run

    def restore(self):
        for k, v in self.orig.items():
            setattr(a, k, v)


class AutoLock(unittest.TestCase):
    def run_wd(self, legs, live=True, auto=True, **kw):
        h = Harness(legs, **kw)
        failures = len(a.EMAIL_FAILURES)
        try:
            left = a.run_watchdog(None, live, now=NOW, auto_lock=auto)
        finally:
            h.restore()
        return h, left, a.EMAIL_FAILURES[failures:]

    def test_each_sport_goes_to_its_own_runner_and_locked_legs_leave_the_alert(self):
        legs = [q("CFB", "CFB A", 60), q("SOC_PL", "PL A", 90), q("SHL", "SHL A", 45), q("NFL", "NFL A", 100)]
        after = {"run_cfb_evening_lock": ["CFB A"], "run_soccer_evening_lock": ["PL A"], "run_hockey_evening_lock": ["SHL A"], "run_lock_segmented": ["NFL A"]}
        h, left, fails = self.run_wd(legs, locked_after=after)
        self.assertEqual(sorted(h.calls), ["run_cfb_evening_lock", "run_hockey_evening_lock", "run_lock_segmented", "run_soccer_evening_lock"])
        self.assertEqual(left, [])
        self.assertTrue(any("auto-locked 4 pick" in s for s in h.owner))
        self.assertEqual(fails, [])

    def test_legs_outside_the_window_or_too_close_are_alerted_not_locked(self):
        legs = [q("CFB", "far", 200), q("NFL", "too close", 5)]                      # beyond 150 min / inside the 10-minute start margin
        h, left, _ = self.run_wd(legs)
        self.assertEqual(h.calls, [])
        self.assertEqual(len(left), 2)
        self.assertTrue(any("WATCHDOG" in s and "unlocked" in s for s in h.owner))

    def test_partial_success_still_alerts_on_what_remained(self):
        legs = [q("CFB", "CFB A", 60), q("CFB", "CFB B", 70)]
        h, left, _ = self.run_wd(legs, locked_after={"run_cfb_evening_lock": ["CFB A"]})
        self.assertEqual([u["leg"] for u in left], ["CFB B"])

    def test_off_by_default_and_dry_run_never_lock(self):
        legs = [q("CFB", "CFB A", 60)]
        h, left, _ = self.run_wd(legs, auto=False)
        self.assertEqual(h.calls, [])
        h, left, _ = self.run_wd(legs, live=False)
        self.assertEqual(h.calls, [])
        self.assertEqual(len(left), 1)

    def test_runner_crash_fails_the_run_and_keeps_the_alert(self):
        h, left, fails = self.run_wd([q("CFB", "CFB A", 60)], runner_error="boom")
        self.assertEqual(len(left), 1)
        self.assertEqual(len(fails), 1)
        self.assertIn("boom", fails[0])
        a.EMAIL_FAILURES.clear()

    # ── owner 2026-11-02: the watchdog only emails what the lock passes would really lock ──
    def run_split(self, seen_legs, auth_legs, holds=(), **kw):
        """seen_legs = what the watchdog sees (build_qualifying now=0); auth_legs = what the passes' own run (real clock, start guard, lineup gate) qualifies; holds = (sport, 'AWY @ HOM', label)."""
        h = Harness(seen_legs, **kw)
        orig = a.build_qualifying

        def build(res, only_sports=None, now=None, guard_stats=None, **k):
            if now == 0 or (not isinstance(now, bool) and now is not None and float(now) == 0):
                return [l for l in seen_legs if only_sports is None or l["sport"] in only_sports]
            if guard_stats is not None and holds:
                guard_stats.setdefault("lineupHolds", []).extend({"sport": sp, "game": g, "leg": lb} for sp, g, lb in holds)
            return [l for l in auth_legs if only_sports is None or l["sport"] in only_sports]
        a.build_qualifying = build
        failures = len(a.EMAIL_FAILURES)
        try:
            left = a.run_watchdog(None, True, now=NOW, auto_lock=True)
        finally:
            a.build_qualifying = orig
            h.restore()
        return h, left, a.EMAIL_FAILURES[failures:]

    def test_a_game_that_already_started_is_logged_not_emailed(self):
        legs = [q("NFL", "CIN ML", -33)]                       # the real TEN @ CIN case: held for a newly-out player, then flagged 'started 33 min ago' for 3 hours
        h, left, _ = self.run_split(legs, auth_legs=[])
        self.assertEqual(left, [])
        self.assertEqual(h.owner, [])
        self.assertEqual(h.calls, [])

    def test_a_leg_the_lock_pass_does_not_qualify_is_not_emailed_and_starts_no_pass(self):
        legs = [q("NBA", "ATL ML", 85)]                        # qualifies in the observe-only view, not in the pass's own run (real ATL @ NO case)
        h, left, _ = self.run_split(legs, auth_legs=[])
        self.assertEqual((left, h.owner, h.calls), ([], [], []))

    def test_a_leg_the_passes_hold_through_the_lineup_gate_is_not_emailed(self):
        legs = [q("NFL", "KC ML", 61, hA="DEN", awA="KC")]
        h, left, _ = self.run_split(legs, auth_legs=[], holds=[("NFL", "KC @ DEN", "KC ML")])
        self.assertEqual((left, h.owner, h.calls), ([], [], []))

    def test_a_leg_the_pass_qualifies_but_could_not_lock_still_alerts(self):
        legs = [q("NFL", "JAX ML", 95)]
        h, left, _ = self.run_split(legs, auth_legs=legs)      # the pass attempts it and it stays unlocked: a real failure
        self.assertEqual(h.calls, ["run_lock_segmented"])
        self.assertEqual([u["leg"] for u in left], ["JAX ML"])
        self.assertTrue(any("unlocked" in s for s in h.owner))

    def test_a_pass_qualified_leg_that_gets_locked_alerts_nobody_but_the_auto_lock_note(self):
        legs = [q("NFL", "JAX ML", 95)]
        h, left, _ = self.run_split(legs, auth_legs=legs, locked_after={"run_lock_segmented": ["JAX ML"]})
        self.assertEqual(left, [])
        self.assertTrue(any("auto-locked 1 pick" in s for s in h.owner))

    def test_a_leg_inside_the_start_guard_margin_the_pass_cannot_lock_still_alerts(self):
        legs = [q("NBA", "ATL ML", 5)]                         # 5 min out: the passes' start guard refuses it, but nothing holds it -- the owner can still act
        h, left, _ = self.run_split(legs, auth_legs=[])
        self.assertEqual([u["leg"] for u in left], ["ATL ML"])

    def test_classification_table(self):
        mk = lambda mins, **kw: dict({"key": ("S", "H", "A", "L"), "startMs": NOW + mins * 60000}, **kw)
        keys = {("S", "H", "A", "L")}
        no = lambda u: False
        cls = lambda u, auth=frozenset(), holds=frozenset(), hold_now=no: a.watchdog_classify(u, set(auth), set(holds), NOW, hold_now)[0]
        self.assertTrue(cls(dict(mk(60), startMs=None)))                               # unknown start: cannot classify, tell the owner
        self.assertFalse(cls(mk(60), auth=keys, holds=keys))                           # held beats qualifying
        self.assertFalse(cls(mk(60, lineupHold=True), auth=keys))
        self.assertFalse(cls(mk(-1), auth=keys))                                       # started
        self.assertTrue(cls(mk(60), auth=keys))
        self.assertTrue(cls(mk(200), auth=keys))                                       # early warning stays: the passes would lock it and have not
        self.assertFalse(cls(mk(60)))                                                  # pre-start, the passes do not qualify it
        self.assertTrue(cls(mk(5)))                                                    # inside the margin, not held
        self.assertFalse(cls(mk(5), hold_now=lambda u: True))                          # inside the margin, held by the gate

    def test_workflow_passes_the_flag_only_for_live_runs_with_a_kill_switch(self):
        wf = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "lock-watchdog.yml").read_text()
        self.assertIn("steps.gate.outputs.live_flag != '' && vars.WATCHDOG_AUTOLOCK != 'false' && '--auto-lock'", wf)
        self.assertIn("uses: ./.github/actions/private-data", wf)         # recipients_for() needs the subscriber list

    def test_evening_slots_exist_and_a_gated_dispatch_behaves_like_a_scheduled_slot(self):
        wf = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "lock-watchdog.yml").read_text()
        for cron in ("'30 21 * * *'", "'30 23 * * *'", "'30 1 * * *'"):
            self.assertIn(f"cron: {cron}", wf)                                   # GitHub fallback slots for the evening
        self.assertIn('"${{ github.event_name }}" = "workflow_dispatch" ] && [ "${{ inputs.gated }}" != "true"', wf)   # manual dispatch = old dry-run path; gated = scheduled path
        self.assertIn("gated:", wf)


if __name__ == "__main__":
    unittest.main(verbosity=2)
