#!/usr/bin/env python3
"""Tests for the single daily "Social content" email (scripts/social_bundle.py, 2026-10-27): sections queue instead of sending, everything goes out together, picks first, oversize bundles split
(never drop a section), and the workflows are wired so the AM/PM pick-of-day emails and the separate cards emails are gone.   python3 scripts/test_social_bundle.py"""
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import social_bundle as B  # noqa: E402


def mkfile(d: Path, name: str, kb: int) -> Path:
    p = d / name
    p.write_bytes(b"x" * kb * 1024)
    return p


class Bundle(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.bundle = self.d / "bundle"
        self.sent = []

    def fake(self, subject, to, html, attachments=None):
        self.sent.append((subject, to, html, list(attachments or [])))
        return True, "sent"

    def test_empty_bundle_sends_nothing(self):
        self.assertEqual(B.send_bundle(self.bundle, "October 27, 2026", "o@x.com", send=self.fake)[0], "empty")
        self.assertEqual(self.sent, [])

    def test_all_sections_go_in_one_email_with_every_attachment(self):
        a, b, c = (mkfile(self.d, n, 10) for n in ("daily.png", "pick1.mp4", "pick1.png"))
        B.add_section(self.bundle, "Daily Social Cards (October 26, 2026)", "<p>daily</p>", [a])
        B.add_section(self.bundle, "Top 1 Picks Social Posts", "<p>picks</p>", [b, c], order=B.PICKS_ORDER)
        out, det = B.send_bundle(self.bundle, "October 27, 2026", "o@x.com", send=self.fake)
        self.assertEqual(out, "sent")
        self.assertEqual(len(self.sent), 1)
        subject, to, html, atts = self.sent[0]
        self.assertEqual(subject, "Clairvoyance — Social Content for October 27, 2026")
        self.assertEqual(to, "o@x.com")
        self.assertEqual({p.name for p in atts}, {"daily.png", "pick1.mp4", "pick1.png"})
        self.assertLess(html.index("Top 1 Picks Social Posts"), html.index("Daily Social Cards"))      # picks first: they are time-sensitive
        self.assertIn("<p>daily</p>", html)
        self.assertIn("<p>picks</p>", html)

    def test_oversize_bundle_splits_into_parts_and_never_drops_a_section(self):
        for i in range(3):
            B.add_section(self.bundle, f"S{i}", f"<p>s{i}</p>", [mkfile(self.d, f"f{i}.bin", 600)])
        out, det = B.send_bundle(self.bundle, "October 27, 2026", "o@x.com", send=self.fake, limit=1000 * 1024)
        self.assertEqual(out, "sent")
        self.assertEqual(len(self.sent), 3)
        self.assertTrue(all("(part " in s[0] and " of 3)" in s[0] for s in self.sent))
        self.assertEqual(sorted(re.findall(r"<p>(s\d)</p>", "".join(s[2] for s in self.sent))), ["s0", "s1", "s2"])

    def test_single_section_larger_than_the_limit_is_still_sent(self):
        B.add_section(self.bundle, "Big", "<p>big</p>", [mkfile(self.d, "big.bin", 600)])
        out, _ = B.send_bundle(self.bundle, "October 27, 2026", "o@x.com", send=self.fake, limit=100 * 1024)
        self.assertEqual((out, len(self.sent)), ("sent", 1))

    def test_a_failed_send_is_reported_as_failed(self):
        B.add_section(self.bundle, "S", "<p>s</p>", [])
        out, det = B.send_bundle(self.bundle, "October 27, 2026", "o@x.com", send=lambda *a, **k: (False, "boom"))
        self.assertEqual(out, "failed")
        self.assertIn("boom", det[0])

    def test_no_recipient_is_a_failure_not_a_silent_skip(self):
        B.add_section(self.bundle, "S", "<p>s</p>", [])
        self.assertEqual(B.send_bundle(self.bundle, "October 27, 2026", "", send=self.fake)[0], "failed")

    def test_missing_attachment_files_are_ignored_not_fatal(self):
        B.add_section(self.bundle, "S", "<p>s</p>", [self.d / "gone.png"])
        self.assertEqual(B.send_bundle(self.bundle, "October 27, 2026", "o@x.com", send=self.fake)[0], "sent")


class Generators(unittest.TestCase):
    def test_cards_send_email_queues_instead_of_mailing_when_a_bundle_dir_is_set(self):
        import generate_social_cards as g
        d = Path(tempfile.mkdtemp())
        sent = []
        g.SOCIAL_CARD_EMAIL_TO = "o@x.com"
        g._send_gmail = lambda *a, **k: sent.append(a) or (True, "sent")
        g.BUNDLE_DIR = d / "b"
        try:
            g.send_email("Clairvoyance — Daily Social Cards (October 26, 2026)", [], {"instagram": "ig", "x": "x"}, intro="hi")
        finally:
            g.BUNDLE_DIR = None
        self.assertEqual(sent, [])
        secs = B.load_sections(d / "b")
        self.assertEqual([s["title"] for s in secs], ["Daily Social Cards (October 26, 2026)"])
        self.assertIn("Instagram caption", secs[0]["html"])

    def test_pick_posts_queue_first_when_a_bundle_dir_is_set(self):
        import generate_pick_of_day_social as p
        d = Path(tempfile.mkdtemp())
        sent = []
        p.SOCIAL_CARD_EMAIL_TO = "o@x.com"
        p._send_gmail = lambda *a, **k: sent.append(a) or (True, "sent")
        p.BUNDLE_DIR = d / "b"
        v, s = d / "v.mp4", d / "s.png"
        v.write_bytes(b"1"); s.write_bytes(b"2")
        try:
            p.send_pick_posts([{"candidate": {"grade": "PREMIUM", "matchup": "A @ B"}, "video_path": v, "still_path": s, "caption": "cap"}], d, "October 27, 2026")
        finally:
            p.BUNDLE_DIR = None
        self.assertEqual(sent, [])
        secs = B.load_sections(d / "b")
        self.assertEqual(len(secs), 1)
        self.assertTrue(Path(__file__).name and next(d.glob("b/000-*.json"), None))          # order 0 => sorts before the stats cards (050-)
        self.assertEqual({a.name for a in secs[0]["attachments"]}, {"v.mp4", "s.png"})


class Wiring(unittest.TestCase):
    def test_social_workflow_builds_one_email_from_cards_and_picks(self):
        wf = (ROOT / ".github" / "workflows" / "social-cards-daily.yml").read_text()
        self.assertIn("generate_social_cards.py", wf)
        self.assertIn("--bundle-dir /tmp/cv_social_bundle", wf)
        self.assertIn("generate_pick_of_day_social.py --bundle-dir /tmp/cv_social_bundle", wf)
        self.assertIn("social_bundle.py /tmp/cv_social_bundle", wf)
        self.assertIn("./.github/actions/publish", wf)

    def test_pick_of_day_workflow_has_no_schedule_any_more(self):
        wf = (ROOT / ".github" / "workflows" / "pick-of-day-social-daily.yml").read_text()
        self.assertNotIn("cron:", wf)
        self.assertIn("workflow_dispatch", wf)

    def test_health_checks_no_longer_watch_the_retired_pick_of_day_slates(self):
        for f in ("daily_health_check.py", "weekly_health_digest.py"):
            src = (HERE / f).read_text()
            self.assertNotIn('("pick-of-day-social-daily.yml"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
