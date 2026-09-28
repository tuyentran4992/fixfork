"""Patch export: the winner artifact must survive `git apply`."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from fixfork.patch_export import build_patch


def _git_apply(root: Path, patch: str) -> subprocess.CompletedProcess:
    (root / "fix.patch").write_text(patch, encoding="utf-8")
    return subprocess.run(
        ["git", "apply", "fix.patch"], cwd=root, capture_output=True, text=True
    )


class PatchExportTest(unittest.TestCase):
    def test_modified_file_applies_with_git(self):
        base = {"src/tax.py": "a = 1\nb = 2\n", "keep.txt": "same\n"}
        winner = {"src/tax.py": "a = 1\nb = 3\n", "keep.txt": "same\n"}
        patch = build_patch(base, winner)

        self.assertIn("diff --git a/src/tax.py b/src/tax.py", patch)
        self.assertNotIn("keep.txt", patch)  # unchanged files never appear

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "tax.py").write_text(base["src/tax.py"], encoding="utf-8")
            (root / "keep.txt").write_text("same\n", encoding="utf-8")
            proc = _git_apply(root, patch)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                (root / "src" / "tax.py").read_text(encoding="utf-8"),
                winner["src/tax.py"],
            )

    def test_no_newline_at_eof_is_marked(self):
        patch = build_patch({"f.txt": "one\ntwo"}, {"f.txt": "one\nthree"})
        self.assertIn("\\ No newline at end of file", patch)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "f.txt").write_text("one\ntwo", encoding="utf-8")
            proc = _git_apply(root, patch)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual((root / "f.txt").read_text(encoding="utf-8"), "one\nthree")

    def test_new_and_deleted_files(self):
        patch = build_patch({"old.txt": "bye\n"}, {"new.txt": "hi\n"})
        self.assertIn("new file mode 100644", patch)
        self.assertIn("deleted file mode 100644", patch)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "old.txt").write_text("bye\n", encoding="utf-8")
            proc = _git_apply(root, patch)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((root / "old.txt").exists())
            self.assertEqual((root / "new.txt").read_text(encoding="utf-8"), "hi\n")

    def test_identical_trees_produce_empty_patch(self):
        tree = {"a.txt": "x\n"}
        self.assertEqual(build_patch(tree, dict(tree)), "")


if __name__ == "__main__":
    unittest.main()
