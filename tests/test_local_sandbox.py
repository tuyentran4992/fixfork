"""Tests for the local sandbox backend (dev/demo only, no isolation)."""

import unittest
from pathlib import Path

from fixfork.models import Edit
from fixfork.sandbox_runner import LocalSandbox, SandboxError

DEMO_REPO = Path(__file__).resolve().parent.parent / "examples" / "demo-repo"
TEST_CMD = "python3 -m unittest discover -s tests"

FIX = Edit(
    file="src/tax.py",
    find="discounted = subtotal * (1 + discount)",
    replace="discounted = subtotal * (1 - discount)",
)


class LocalSandboxTest(unittest.TestCase):
    def setUp(self):
        self.box = LocalSandbox()
        self.sid = self.box.create(DEMO_REPO, "t1")

    def test_failing_run_apply_fix_then_rollback(self):
        res = self.box.run(self.sid, TEST_CMD)
        self.assertNotEqual(res.returncode, 0)

        snapshot = self.box.checkpoint(self.sid)
        changed = self.box.apply_edits(self.sid, [FIX])
        self.assertEqual(changed, 1)

        fixed = self.box.run(self.sid, TEST_CMD)
        self.assertEqual(fixed.returncode, 0, msg=fixed.output)
        self.assertIn("OK", fixed.output)

        self.box.rollback(self.sid, snapshot)
        again = self.box.run(self.sid, TEST_CMD)
        self.assertNotEqual(again.returncode, 0)

    def test_fork_copies_state_without_sharing(self):
        self.box.apply_edits(self.sid, [FIX])
        snapshot = self.box.checkpoint(self.sid)
        forked = self.box.fork(self.sid, snapshot, "t2")
        self.assertIn("(1 - discount)", self.box.read_tree(forked)["src/tax.py"])
        # editing the fork must not leak into the origin
        self.box.apply_edits(
            forked,
            [Edit(file="src/tax.py", find="(1 - discount)", replace="(1 - discount) + 0")],
        )
        self.assertNotIn("+ 0", self.box.read_tree(self.sid)["src/tax.py"])

    def test_missing_edit_target_raises(self):
        with self.assertRaises(SandboxError):
            self.box.apply_edits(
                self.sid, [Edit(file="src/tax.py", find="not in the file", replace="x")]
            )

    def test_missing_file_raises(self):
        with self.assertRaises(SandboxError):
            self.box.apply_edits(
                self.sid, [Edit(file="src/nope.py", find="x", replace="y")]
            )

    def test_unknown_sandbox_raises(self):
        with self.assertRaises(SandboxError):
            self.box.run("missing", TEST_CMD)


if __name__ == "__main__":
    unittest.main()
