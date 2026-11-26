#!/usr/bin/env python3
"""scripts/set_owner_key.py: refuses weak or mistyped keys, hands the key to wrangler on stdin only, never prints it. No network, no wrangler.

    python3 scripts/test_owner_key.py
"""
from __future__ import annotations

import contextlib, io, sys, unittest, urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import set_owner_key as K  # noqa: E402

GOOD = "maple river lantern copper"


class OwnerKey(unittest.TestCase):
    def test_short_odd_and_mismatched_keys_are_asked_again(self):
        answers = iter(["short", "curly ’quote’ key here", GOOD, "typo" + GOOD, GOOD, GOOD])
        said = []
        self.assertEqual(K.ask(lambda _p: next(answers), said.append), GOOD)
        text = " ".join(said)
        self.assertIn("Too short", text); self.assertIn("plain keyboard characters", text); self.assertIn("did not match", text)
        self.assertNotIn(GOOD, text)

    def test_the_key_goes_on_stdin_never_on_the_command_line(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0))
        self.assertTrue(K.put(GOOD, run))
        args, kw = run.call_args
        self.assertEqual(args[0], ["npx", "--yes", "wrangler", "secret", "put", "TRIGGER_KEY"])
        self.assertNotIn(GOOD, " ".join(args[0]))
        self.assertEqual(kw["input"], GOOD)
        self.assertTrue((kw["cwd"] / "wrangler.toml").is_file())

    def test_the_check_sends_the_key_in_a_header_and_retries(self):
        seen = []

        def opener(req, timeout):
            seen.append(req)
            if len(seen) < 3:
                raise urllib.error.URLError("not yet")
            return contextlib.nullcontext(mock.Mock(status=200))
        self.assertTrue(K.works(GOOD, opener, lambda _s: None))
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen[0].get_header("X-owner-key"), GOOD)
        self.assertNotIn("maple", seen[0].full_url)

    def test_a_refused_put_reports_failure_and_nothing_prints_the_key(self):
        out = io.StringIO()
        with mock.patch.object(K, "ask", return_value=GOOD), mock.patch.object(K, "put", return_value=False), \
             mock.patch.object(K, "works") as works, contextlib.redirect_stdout(out):
            self.assertEqual(K.main(), 1)
        works.assert_not_called()
        self.assertNotIn(GOOD, out.getvalue())

    def test_ctrl_c_changes_nothing(self):
        with mock.patch.object(K, "ask", side_effect=KeyboardInterrupt), mock.patch.object(K, "put") as put, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(K.main(), 1)
        put.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
