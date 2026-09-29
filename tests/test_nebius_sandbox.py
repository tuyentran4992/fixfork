"""NebiusSandbox tests - fully offline via an injected fake transport.

These tests lock the contract with the LIVE sandboxes API as measured on
2026-09-29 (nghien-cuu/thi-nghiem-sandbox-29-09/REPORT.json):
content-addressed state uuids, spawn/poll flow, base64 stream decoding,
tar|base64 read_tree, pointer-move checkpoint/fork/rollback.
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from fixfork.models import Edit
from fixfork.sandbox_runner import NebiusSandbox, SandboxError


def _stream(text: str, truncated: bool = False) -> dict:
    return {
        "value": base64.b64encode(text.encode()).decode(),
        "encoding": "base64",
        "truncated": truncated,
    }


def _op(op_id: str = "op-1", state_uuid: str | None = "state-2", exit_code: int = 0,
        stdout: str = "", stderr: str = "", status: str = "SUCCESS",
        error: str | None = None) -> tuple:
    body = json.dumps(
        {
            "uuid": op_id,
            "status": status,
            "error": error,
            "result_image_uuid": state_uuid,
            "metadata": {
                "result": {
                    "state": {"exit_code": exit_code, "timed_out": False},
                    "stdout": _stream(stdout),
                    "stderr": _stream(stderr),
                    "resources": {"cost": 5.87e-05},
                }
            },
        }
    ).encode()
    return (200, {}, body)


class FakeTransport:
    """Scripted transport: pops queued (status, headers, body) responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, method, path, body=None, raw=None, timeout=None):
        self.calls.append((method, path, body, raw))
        if not self.responses:
            raise AssertionError(f"no queued response for {method} {path}")
        return self.responses.pop(0)


def _spawn_resp(op_id="op-1"):
    return (201, {"Location": f"/v1/operations/{op_id}"}, json.dumps({"uuid": op_id}).encode())


def _file_resp(uuid="f-1"):
    return (201, {}, json.dumps({"uuid": uuid, "sha256": "x" * 64, "size": 1}).encode())


def _tar_payload(files: dict[str, str], truncated: bool = False) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


class NebiusSandboxTest(unittest.TestCase):
    def make_repo(self, files: dict[str, str]) -> Path:
        root = Path(tempfile.mkdtemp(prefix="nebtest-"))
        for rel, text in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return root

    def box(self, responses, **kw) -> tuple[NebiusSandbox, FakeTransport]:
        transport = FakeTransport(responses)
        box = NebiusSandbox(api_key="test-key", project="proj-1",
                            transport=transport, poll_interval=0.01, **kw)
        return box, transport

    # -- create ------------------------------------------------------------
    def test_create_uploads_files_and_captures_state(self):
        repo = self.make_repo({"src/tax.py": "x = 1\n", "README.md": "hi\n"})
        box, transport = self.box([_file_resp("f-1"), _file_resp("f-2"),
                                   _spawn_resp("op-setup"), _op("op-setup", "state-1")])
        sid = box.create(repo, "baseline")
        self.assertEqual(sid, "baseline")
        self.assertEqual(box.checkpoint("baseline"), "state-1")
        uploads = [c for c in transport.calls if c[1] == "/files"]
        self.assertEqual(len(uploads), 2)
        spawn_body = [c for c in transport.calls if c[1] == "/instances"][0][2]
        self.assertEqual(spawn_body["command"], "true")
        self.assertEqual(
            spawn_body["files"],
            {"/repo/README.md": {"uuid": "f-1"}, "/repo/src/tax.py": {"uuid": "f-2"}},
        )

    def test_create_dedupes_identical_content(self):
        repo = self.make_repo({"a.txt": "same\n", "b.txt": "same\n"})
        box, transport = self.box([_file_resp("f-1"), _spawn_resp(), _op(state_uuid="state-1")])
        box.create(repo, "s")
        uploads = [c for c in transport.calls if c[1] == "/files"]
        self.assertEqual(len(uploads), 1)  # second file served from the cache

    # -- run ---------------------------------------------------------------
    def test_run_advances_state_and_decodes_output(self):
        box, transport = self.box([_spawn_resp("op-r"), _op(
            "op-r", "state-9", exit_code=0, stdout="OK\n")])
        box._sessions["s"] = "state-1"  # simulate a created session
        result = box.run("s", "python3 -m unittest")
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "OK\n")
        self.assertEqual(box.checkpoint("s"), "state-9")
        spawn_body = [c for c in transport.calls if c[1] == "/instances"][0][2]
        self.assertEqual(spawn_body["image"], "state-1")
        self.assertEqual(spawn_body["cwd"], "/repo")

    def test_run_reports_nonzero_exit_without_raising(self):
        box, transport = self.box([_spawn_resp("op-r"), _op(
            "op-r", "state-9", exit_code=1, stdout="FAILED (1 of 2)\n")])
        box._sessions["s"] = "state-1"
        result = box.run("s", "test")
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 1)
        # a failing test still advanced the state (measurement on 29/09: SUCCESS op)
        self.assertEqual(box.checkpoint("s"), "state-9")

    def test_run_raises_when_operation_failed(self):
        box, _ = self.box([_spawn_resp("op-r"), _op("op-r", None, status="FAILED", error="boom")])
        box._sessions["s"] = "state-1"
        with self.assertRaises(SandboxError):
            box.run("s", "test")

    def test_unknown_session_raises(self):
        box, _ = self.box([])
        with self.assertRaises(SandboxError):
            box.run("nope", "test")

    # -- apply_edits ---------------------------------------------------------
    def test_apply_edits_uploads_changed_file_and_advances(self):
        repo_text = "def price(x):\n    return x + 1\n"
        box, transport = self.box([
            _spawn_resp("op-read"), _op("op-read", "state-1",
                                        stdout=_tar_payload({"src/tax.py": repo_text})),
            _file_resp("f-new"),
            _spawn_resp("op-edit"), _op("op-edit", "state-2"),
        ])
        box._sessions["s"] = "state-1"
        n = box.apply_edits("s", [Edit(file="src/tax.py", find="x + 1", replace="x - 1")])
        self.assertEqual(n, 1)
        self.assertEqual(box.checkpoint("s"), "state-2")
        edit_body = [c for c in transport.calls if c[1] == "/instances"][1][2]
        self.assertIn("/repo/src/tax.py", edit_body["files"])

    def test_apply_edits_missing_find_raises(self):
        box, _ = self.box([
            _spawn_resp("op-read"), _op("op-read", "state-1",
                                        stdout=_tar_payload({"src/tax.py": "a = 1\n"})),
        ])
        box._sessions["s"] = "state-1"
        with self.assertRaises(SandboxError):
            box.apply_edits("s", [Edit(file="src/tax.py", find="NOT THERE", replace="x")])

    def test_apply_edits_tolerates_blank_line_drift(self):
        # Same drift class as the local backend: one missing blank line inside
        # the find block must still apply (unique normalised match).
        repo_text = "def price(x):\n\n\n    return x + 1\n"
        box, _ = self.box([
            _spawn_resp("op-read"), _op("op-read", "state-1",
                                        stdout=_tar_payload({"src/tax.py": repo_text})),
            _file_resp("f-new"),
            _spawn_resp("op-edit"), _op("op-edit", "state-2"),
        ])
        box._sessions["s"] = "state-1"
        n = box.apply_edits(
            "s",
            [Edit(file="src/tax.py", find="def price(x):\n\n    return x + 1",
                  replace="def price(x):\n    return x - 1")],
        )
        self.assertEqual(n, 3)
        self.assertEqual(box.checkpoint("s"), "state-2")

    # -- checkpoint / fork / rollback ----------------------------------------
    def test_fork_and_rollback_are_pointer_moves(self):
        box, _ = self.box([])
        box._sessions["base"] = "state-1"
        box.fork("base", "state-1", "branch-a")
        box._sessions["branch-a"] = "state-2"  # branch edits advance only itself
        self.assertEqual(box.checkpoint("base"), "state-1")
        self.assertEqual(box.checkpoint("branch-a"), "state-2")
        box.rollback("branch-a", "state-1")
        self.assertEqual(box.checkpoint("branch-a"), "state-1")

    # -- read_tree -----------------------------------------------------------
    def test_read_tree_decodes_tar(self):
        box, _ = self.box([
            _spawn_resp("op-read"),
            _op("op-read", "state-1", stdout=_tar_payload({"src/tax.py": "x = 1\n",
                                                           "tests/test_tax.py": "pass\n"})),
        ])
        box._sessions["s"] = "state-1"
        tree = box.read_tree("s")
        self.assertEqual(tree, {"src/tax.py": "x = 1\n", "tests/test_tax.py": "pass\n"})

    def test_read_tree_refuses_truncated_output(self):
        box, _ = self.box([
            _spawn_resp("op-read"),
            (200, {}, json.dumps({
                "uuid": "op-read", "status": "SUCCESS", "result_image_uuid": "state-1",
                "metadata": {"result": {
                    "state": {"exit_code": 0},
                    "stdout": {"value": base64.b64encode(b"xx").decode(),
                               "encoding": "base64", "truncated": True},
                }},
            }).encode()),
        ])
        box._sessions["s"] = "state-1"
        with self.assertRaises(SandboxError):
            box.read_tree("s")


if __name__ == "__main__":
    unittest.main()
