"""Sandbox backends.

``SandboxRunner`` is the interface the pipeline codes against. Two backends:

* ``LocalSandbox`` - development/tests only: plain temp dirs, no isolation.
  Checkpoint = in-memory snapshot of text files; fork = copy of a snapshot.
* ``NebiusSandbox`` - Token Factory Sandboxes (live, opt-in): VM-level state
  images; checkpoint/fork/rollback are content-addressed uuid pointer moves.
  Requires a Token Factory key + project id; exercised live 29/09/2026.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import Edit

_SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache"}
_SKIP_SUFFIXES = (".pyc",)


class SandboxError(RuntimeError):
    """Raised when a sandbox operation fails (bad edit target, missing file...)."""


@dataclass
class ExecResult:
    returncode: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class SandboxRunner(Protocol):
    def create(self, repo: str | Path, sid: str) -> str: ...

    def run(self, sid: str, command: str, timeout: int = ...) -> ExecResult: ...

    def apply_edits(self, sid: str, edits: list[Edit]) -> int: ...

    def checkpoint(self, sid: str) -> str: ...

    def fork(self, sid: str, snapshot: str, new_sid: str) -> str: ...

    def rollback(self, sid: str, snapshot: str) -> None: ...

    def read_tree(self, sid: str) -> dict[str, str]: ...

    def destroy(self, sid: str) -> None: ...


def _iter_text_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.name.endswith(_SKIP_SUFFIXES):
            continue
        yield path


def locate_edit(content: str, find: str) -> tuple[int, int, str] | None:
    """Locate the span ``find`` covers in ``content``; ``None`` when not found.

    Exact match is always tried first (byte-for-byte, mode ``"exact"``). Only
    when that fails, a bounded fallback (mode ``"normalised"``) is tried: the
    non-blank lines of ``find`` must equal consecutive non-blank lines of the
    file after trailing-whitespace rstrip - blank lines inside the block and
    trailing spaces on its lines may drift. That drift is a measured failure
    class on a real repository: a diagnosis model dropped exactly one blank
    line from a 299-char block and the byte-exact apply refused it. The
    fallback is accepted ONLY when it matches a single location; zero or
    several candidates return ``None`` so ambiguity is never resolved
    silently.
    """
    pos = content.find(find)
    if pos != -1:
        return pos, pos + len(find), "exact"
    find_lines = [line.rstrip() for line in find.split("\n") if line.strip()]
    if not find_lines:
        return None
    lines = content.split("\n")
    starts: list[int] = []
    keep: list[int] = []
    offset = 0
    for index, line in enumerate(lines):
        starts.append(offset)
        if line.strip():
            keep.append(index)
        offset += len(line) + 1
    span = len(find_lines)
    matches: list[tuple[int, int]] = []
    for window_start in range(len(keep) - span + 1):
        window = keep[window_start : window_start + span]
        if all(lines[target].rstrip() == want for want, target in zip(find_lines, window)):
            matches.append((window[0], window[-1]))
            if len(matches) > 1:
                return None  # ambiguous: refuse, never guess
    if len(matches) != 1:
        return None
    first, last = matches[0]
    return starts[first], starts[last] + len(lines[last]), "normalised"


class LocalSandbox:
    """Dev backend: one temp dir per branch, no isolation. For tests and demos."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="fixfork-"))
        self._dirs: dict[str, Path] = {}
        self._snapshots: dict[str, dict[str, str]] = {}
        self._snapshot_count = 0

    # -- internals ---------------------------------------------------------
    def _dir(self, sid: str) -> Path:
        try:
            return self._dirs[sid]
        except KeyError as exc:
            raise SandboxError(f"unknown sandbox {sid!r}") from exc

    # -- interface ---------------------------------------------------------
    def create(self, repo: str | Path, sid: str) -> str:
        dest = self.root / sid
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(str(repo), dest, ignore=shutil.ignore_patterns(*_SKIP_DIRS, "*.pyc"))
        self._dirs[sid] = dest
        return sid

    def path_of(self, sid: str) -> Path:
        return self._dir(sid)

    def run(self, sid: str, command: str, timeout: int = 120) -> ExecResult:
        workdir = self._dir(sid)
        # PYTHONDONTWRITEBYTECODE: a stale __pycache__ can fake test results
        # after a rollback (a .pyc stays valid when size matches and the source
        # mtime falls in the same second), so the dev backend never writes one.
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") + (exc.stderr or "")
            return ExecResult(returncode=124, output=str(out), timed_out=True)
        return ExecResult(returncode=proc.returncode, output=proc.stdout + proc.stderr)

    def apply_edits(self, sid: str, edits: list[Edit]) -> int:
        workdir = self._dir(sid)
        changed_lines = 0
        for edit in edits:
            target = workdir / edit.file
            if not target.is_file():
                raise SandboxError(f"edit target file does not exist: {edit.file!r}")
            content = target.read_text(encoding="utf-8")
            span = locate_edit(content, edit.find)
            if span is None:
                raise SandboxError(
                    f"edit target not found in {edit.file!r}: {edit.find!r}"
                )
            start, end, _mode = span
            target.write_text(
                content[:start] + edit.replace + content[end:], encoding="utf-8"
            )
            changed_lines += edit.find.count("\n") + 1
        return changed_lines

    def checkpoint(self, sid: str) -> str:
        self._snapshot_count += 1
        snapshot = f"{sid}#{self._snapshot_count}"
        self._snapshots[snapshot] = self.read_tree(sid)
        return snapshot

    def fork(self, sid: str, snapshot: str, new_sid: str) -> str:
        files = self._snapshot(snapshot)
        dest = self.root / new_sid
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        self._dirs[new_sid] = dest
        self._write_files(dest, files)
        return new_sid

    def rollback(self, sid: str, snapshot: str) -> None:
        files = self._snapshot(snapshot)
        workdir = self._dir(sid)
        # remove tracked files that are not in the snapshot, then restore contents
        for path in _iter_text_files(workdir):
            rel = path.relative_to(workdir).as_posix()
            if rel not in files:
                path.unlink()
        for cache_dir in workdir.rglob("__pycache__"):
            shutil.rmtree(cache_dir, ignore_errors=True)
        self._write_files(workdir, files)

    def read_tree(self, sid: str) -> dict[str, str]:
        workdir = self._dir(sid)
        tree: dict[str, str] = {}
        for path in _iter_text_files(workdir):
            rel = path.relative_to(workdir).as_posix()
            try:
                tree[rel] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # binary file: skipped by design (text-only snapshots)
        return tree

    def destroy(self, sid: str) -> None:
        workdir = self._dirs.pop(sid, None)
        if workdir is not None and workdir.exists():
            shutil.rmtree(workdir)

    # -- helpers -----------------------------------------------------------
    def _snapshot(self, snapshot: str) -> dict[str, str]:
        try:
            return self._snapshots[snapshot]
        except KeyError as exc:
            raise SandboxError(f"unknown snapshot {snapshot!r}") from exc

    @staticmethod
    def _write_files(root: Path, files: dict[str, str]) -> None:
        for rel, content in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")


class NebiusSandbox:
    """Token Factory Sandboxes backend (live, opt-in).

    Session model: a session's filesystem is a content-addressed *state image*
    (uuid) on the Sandboxes service. Facts behind this design were verified live
    (see ``nghien-cuu/thi-nghiem-sandbox-29-09/``):

    * every SUCCESS instance operation returns ``result_image_uuid`` - the
      filesystem state after the command; an unchanged tree yields the SAME
      uuid (content-addressed);
    * a later spawn whose ``image`` is such a uuid starts from that exact state
      => checkpoint/fork/rollback are pointer moves, branches are isolated;
    * ops carry ``result.state.exit_code`` and ``resources.cost``.

    ``create`` uploads the repo via ``POST /files`` and attaches the uploaded
    blobs to the first spawn; ``apply_edits`` re-uploads changed files and
    spawns from the current state. ``read_tree`` runs one ``tar | base64``
    command and decodes it locally (the output is refused if truncated).

    Requires ``NEBIUS_API_KEY`` and a project id (``project=`` or env
    ``NEBIUS_SANDBOX_PROJECT``); the sandboxes API is Beta, so this backend
    raises rather than silently falling back to the local one.
    """

    DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/sandboxes/v1"
    # astral/uv:python3.11-alpine - Python 3.11 + uv on Alpine (verified live).
    DEFAULT_IMAGE = "3d64cfd9-f318-3755-8375-64e0f4580db1"
    REPO_DIR = "/repo"

    def __init__(
        self,
        api_key: str | None = None,
        project: str | None = None,
        base_url: str | None = None,
        image: str | None = None,
        transport=None,
        poll_interval: float = 1.5,
    ) -> None:
        self.api_key = api_key or os.environ.get("NEBIUS_API_KEY")
        self.project = project or os.environ.get("NEBIUS_SANDBOX_PROJECT")
        if not self.api_key:
            raise SandboxError("NebiusSandbox needs an API key (NEBIUS_API_KEY)")
        if not self.project:
            raise SandboxError(
                "NebiusSandbox needs a project id (project= or NEBIUS_SANDBOX_PROJECT)"
            )
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.image = image or self.DEFAULT_IMAGE
        self.poll_interval = poll_interval
        self._transport = transport or self._http
        self._sessions: dict[str, str] = {}  # sid -> current state image uuid
        self._file_ids: dict[str, str] = {}  # sha256 -> uploaded file uuid
        self.ops: list[dict] = []  # audit trail: every operation JSON (no secrets)

    # -- transport ---------------------------------------------------------
    def _http(self, method: str, path: str, body=None, raw: bytes | None = None,
              timeout: int = 60):
        """Default transport: urllib, stdlib-only. Returns (status, headers, bytes)."""
        import urllib.error
        import urllib.request

        url = path if path.startswith("http") else self.base_url + path
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Project": self.project,
            "Accept": "application/json",
        }
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def _request(self, method: str, path: str, body=None, raw: bytes | None = None,
                 timeout: int = 60):
        status, headers, data = self._transport(method, path, body=body, raw=raw,
                                                timeout=timeout)
        if status is None or status >= 400:
            preview = (data or b"")[:300].decode(errors="replace")
            raise SandboxError(f"{method} {path} -> HTTP {status}: {preview}")
        return status, headers, data

    # -- sandboxes API helpers ----------------------------------------------
    @staticmethod
    def _decode_stream(stream: dict | None) -> tuple[str, bool]:
        if not stream or not isinstance(stream, dict):
            return "", False
        value = stream.get("value") or ""
        if stream.get("encoding") == "base64":
            import base64 as _b64

            try:
                value = _b64.b64decode(value).decode(errors="replace")
            except Exception:
                pass
        return value, bool(stream.get("truncated"))

    def _upload_file(self, content: bytes) -> str:
        import hashlib

        sha = hashlib.sha256(content).hexdigest()
        if sha in self._file_ids:
            return self._file_ids[sha]
        _, _, data = self._request("POST", "/files", raw=content)
        info = json.loads(data)
        uuid = info["uuid"]
        self._file_ids[sha] = uuid
        return uuid

    def _spawn(self, image: str, command: str, files: dict | None = None,
               timeout: int = 120, disposable: bool = False) -> str:
        body = {
            "command": command,
            "image": image,
            "shell": True,
            "cwd": self.REPO_DIR,
            "timeout": timeout,
            "disposable": disposable,
        }
        if files:
            body["files"] = files
        _, headers, data = self._request("POST", "/instances", body=body)
        spawned = json.loads(data)
        op_id = spawned.get("uuid")
        location = headers.get("Location") or headers.get("location")
        if location:
            op_id = location.rstrip("/").split("/")[-1] or op_id
        if not op_id:
            raise SandboxError(f"spawn returned no operation id: {data[:200]!r}")
        return op_id

    def _wait(self, op_id: str, timeout: int) -> dict:
        import time as _time

        deadline = _time.monotonic() + timeout + 90
        while True:
            _, headers, data = self._request("GET", f"/operations/{op_id}")
            op = json.loads(data)
            status = op.get("status")
            if status in ("SUCCESS", "FAILED", "CANCELLED"):
                self.ops.append(op)
                return op
            if _time.monotonic() > deadline:
                raise SandboxError(f"operation {op_id} still {status} after {timeout + 90}s")
            retry_after = headers.get("Retry-After") or headers.get("retry-after")
            delay = float(retry_after) if retry_after else self.poll_interval
            _time.sleep(max(0.5, delay))

    def _op(self, image: str, command: str, files: dict | None = None,
            timeout: int = 120, disposable: bool = False) -> dict:
        op_id = self._spawn(image, command, files=files, timeout=timeout,
                            disposable=disposable)
        op = self._wait(op_id, timeout)
        if op.get("status") != "SUCCESS":
            raise SandboxError(
                f"operation {op_id} {op.get('status')}: {op.get('error')}"
            )
        return op

    @staticmethod
    def _result(op: dict) -> tuple[dict, dict]:
        metadata = op.get("metadata") or {}
        result = metadata.get("result") or {}
        return result, (result.get("state") or {})

    def _state(self, sid: str) -> str:
        try:
            return self._sessions[sid]
        except KeyError as exc:
            raise SandboxError(f"unknown sandbox {sid!r}") from exc

    def _advance(self, sid: str, op: dict) -> None:
        new_state = op.get("result_image_uuid")
        if new_state:
            self._sessions[sid] = new_state

    # -- interface ----------------------------------------------------------
    def create(self, repo: str | Path, sid: str) -> str:
        root = Path(repo)
        files: dict[str, dict] = {}
        for path in _iter_text_files(root):
            rel = path.relative_to(root).as_posix()
            blob = path.read_bytes()
            files[f"{self.REPO_DIR}/{rel}"] = {"uuid": self._upload_file(blob)}
        if not files:
            raise SandboxError(f"repo has no usable files: {repo!r}")
        op = self._op(self.image, "true", files=files, timeout=60)
        state = op.get("result_image_uuid")
        if not state:
            raise SandboxError("setup operation produced no state image")
        self._sessions[sid] = state
        return sid

    def run(self, sid: str, command: str, timeout: int = 120) -> ExecResult:
        op = self._op(self._state(sid), command, timeout=timeout)
        self._advance(sid, op)
        result, state = self._result(op)
        stdout, truncated = self._decode_stream(result.get("stdout"))
        stderr, _ = self._decode_stream(result.get("stderr"))
        return ExecResult(
            returncode=int(state.get("exit_code") or 0),
            output=stdout + stderr,
            timed_out=bool(state.get("timed_out")),
        )

    def apply_edits(self, sid: str, edits: list[Edit]) -> int:
        tree = self.read_tree(sid)
        pending: dict[str, str] = {}
        changed_lines = 0
        for edit in edits:
            content = pending.get(edit.file) or tree.get(edit.file)
            if content is None:
                raise SandboxError(f"edit target file does not exist: {edit.file!r}")
            span = locate_edit(content, edit.find)
            if span is None:
                raise SandboxError(f"edit target not found in {edit.file!r}: {edit.find!r}")
            start, end, _mode = span
            pending[edit.file] = content[:start] + edit.replace + content[end:]
            changed_lines += edit.find.count("\n") + 1
        files = {
            f"{self.REPO_DIR}/{rel}": {"uuid": self._upload_file(text.encode())}
            for rel, text in pending.items()
        }
        op = self._op(self._state(sid), "true", files=files, timeout=60)
        if not op.get("result_image_uuid"):
            raise SandboxError("apply_edits produced no state image")
        self._advance(sid, op)
        return changed_lines

    def checkpoint(self, sid: str) -> str:
        # states are immutable and content-addressed: the checkpoint IS the uuid
        return self._state(sid)

    def fork(self, sid: str, snapshot: str, new_sid: str) -> str:
        self._state(sid)  # guard: source session must exist
        self._sessions[new_sid] = snapshot
        return new_sid

    def rollback(self, sid: str, snapshot: str) -> None:
        self._state(sid)
        self._sessions[sid] = snapshot

    def read_tree(self, sid: str) -> dict[str, str]:
        import base64 as _b64
        import io
        import tarfile

        command = f"cd {self.REPO_DIR} && tar -cf - . 2>/dev/null | base64 | tr -d '\\n'"
        op = self._op(self._state(sid), command, timeout=120, disposable=True)
        result, _ = self._result(op)
        payload, truncated = self._decode_stream(result.get("stdout"))
        if truncated:
            raise SandboxError("read_tree output truncated (tree too large for one op)")
        if not payload:
            raise SandboxError("read_tree returned no data")
        blob = _b64.b64decode(payload)
        tree: dict[str, str] = {}
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                rel = member.name[2:] if member.name.startswith("./") else member.name
                if not rel or any(part in _SKIP_DIRS for part in rel.split("/")):
                    continue
                if rel.endswith(_SKIP_SUFFIXES):
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                try:
                    tree[rel] = extracted.read().decode("utf-8")
                except UnicodeDecodeError:
                    continue  # binary file: skipped by design (text-only snapshots)
        if not tree:
            raise SandboxError("read_tree decoded no text files")
        return tree

    def destroy(self, sid: str) -> None:
        # Drops the session pointer. The content-addressed upload cache and the
        # op audit trail are kept on purpose: the cache dedupes across sessions,
        # and ops[] is evidence for the run report (no secrets in it).
        self._sessions.pop(sid, None)
