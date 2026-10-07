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
        a.run_lock_segmented = lambda page, live, send_email=True: self._runner("run_lock_segmented")(page, live)
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
