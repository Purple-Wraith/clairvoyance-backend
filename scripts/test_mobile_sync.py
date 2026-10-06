#!/usr/bin/env python3
"""The mobile site must be a COMPLETE mirror of the backend's docs/ (scripts/mobile_sync.py, .github/workflows/mobile-sync.yml): every data file the app fetches, byte-identical, with only
app.html/index.html transformed and CNAME never copied. (The old workflow copied 8 files by name; the mobile site 404'd on ~64 data files.)

    python3 scripts/test_mobile_sync.py
"""
import filecmp, re, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import mobile_sync as M  # noqa: E402


def docs_files():
    return {str(p.relative_to(ROOT / "docs")) for p in (ROOT / "docs").rglob("*") if p.is_file()}


class Mirror(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / "docs").mkdir()
        (cls.tmp / "docs" / "stale_leftover.json").write_text("{}")       # a file the backend no longer has
        (cls.tmp / "docs" / "nested").mkdir()
        (cls.tmp / "docs" / "nested" / "old.txt").write_text("x")
        cls.first = M.sync(ROOT, cls.tmp)

    def test_every_backend_file_is_mirrored_byte_for_byte(self):
        for rel in sorted(docs_files() - M.TRANSFORMED - M.NEVER_COPY):
            self.assertTrue((self.tmp / "docs" / rel).exists(), f"missing on mobile: {rel}")
            self.assertTrue(filecmp.cmp(ROOT / "docs" / rel, self.tmp / "docs" / rel, shallow=False), f"differs: {rel}")

    def test_the_files_the_app_fetches_are_all_there(self):
        app = (ROOT / "docs" / "app.html").read_text(encoding="utf-8")
        names = {m.group(1) for m in re.finditer(r"""['"`]([a-z_0-9]+\.json)['"`]""", app)}
        existing = {n for n in names if (ROOT / "docs" / n).exists()}
        self.assertGreater(len(existing), 30)
        for n in existing:
            self.assertTrue((self.tmp / "docs" / n).exists(), f"app fetches {n} but the mobile mirror lacks it")

    def test_cname_never_reaches_the_mobile_repo(self):
        self.assertFalse((self.tmp / "docs" / "CNAME").exists())

    def test_app_is_the_transformed_copy_and_index_matches(self):
        out = self.tmp / "expected.html"
        subprocess.run([sys.executable, str(ROOT / "scripts" / "mobile_transform.py"), str(ROOT / "docs" / "app.html"), str(out)], check=True, capture_output=True)
        self.assertEqual((self.tmp / "docs" / "app.html").read_bytes(), out.read_bytes())
        self.assertEqual((self.tmp / "docs" / "index.html").read_bytes(), out.read_bytes())
        self.assertIn("user-scalable=no", (self.tmp / "docs" / "app.html").read_text(encoding="utf-8"))
        self.assertNotEqual((self.tmp / "docs" / "app.html").read_bytes(), (ROOT / "docs" / "app.html").read_bytes())

    def test_stale_files_are_removed_and_empty_dirs_dropped(self):
        self.assertEqual(sorted(self.first["removed"]), ["nested/old.txt", "stale_leftover.json"])
        self.assertFalse((self.tmp / "docs" / "stale_leftover.json").exists())
        self.assertFalse((self.tmp / "docs" / "nested").exists())

    def test_second_run_changes_nothing(self):
        again = M.sync(ROOT, self.tmp)
        self.assertEqual(again["copied"], 0)
        self.assertEqual(again["removed"], [])

    def test_workflow_uses_the_full_mirror_and_triggers_on_every_docs_change(self):
        wf = (ROOT / ".github" / "workflows" / "mobile-sync.yml").read_text()
        self.assertIn("scripts/mobile_sync.py", wf)
        self.assertIn("'docs/**'", wf)
        self.assertNotIn("cp source/docs/data.json", wf)                    # the old hand-picked list is gone
        self.assertNotIn("CNAME", [l for l in wf.splitlines() if l.strip().startswith("cp ")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
