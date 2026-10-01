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


def _shq(text: str) -> str:
    """Quote one string as a POSIX shell word (busybox sh compatible)."""
    return "'" + text.replace("'", "'\\''") + "'"


def _decode_tar_payload(payload: str) -> dict[str, str]:
    """Decode a base64 tar payload into {relative path: text}.

    Directories, cache paths and bytecode are skipped; binary files are
    skipped by design (text-only snapshots).
    """
    import base64 as _b64
    import io
    import tarfile

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
    return tree


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


# Bound for the uniform leading-space offset the last-resort matcher accepts
# (see locate_edit); a larger shift means the model's block is not a shifted
# copy of the file region and must be refused, not guessed at. Measured live
# 2026-10-01 run 3: +1 on most lines, +5 on one continuation line inside
# parentheses (17 vs 12 spaces) - allow up to two indent levels of slip.
MAX_INDENT_SHIFT = 8


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

    Last resort (mode ``"indent±K"``): the model's block is byte-identical
    after lstrip on every non-blank line, shifted by ONE uniform leading-space
    offset K (measured live 2026-10-01, run 3: most lines carried one extra
    leading space and one continuation line inside parens carried five - the
    occurrence list's separator bled into the copy). The FIRST such candidate
    wins - the same first-match semantics as the byte-exact search above, and
    sequential edits re-scan the updated content, so repeated sites resolve
    in order. Callers shift the replacement by the same K
    (``corrected_replace``) so the applied block keeps the file's own
    indentation. |K| is bounded by ``MAX_INDENT_SHIFT``.
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
    if len(matches) == 1:
        first, last = matches[0]
        return starts[first], starts[last] + len(lines[last]), "normalised"
    if len(matches) > 1:
        return None  # ambiguous: refuse, never guess

    # Indent-shift pass (see docstring): uniform leading-space offset K != 0.
    find_leads = [len(l) - len(l.lstrip(" ")) for l in find_lines]
    for window_start in range(len(keep) - span + 1):
        window = keep[window_start : window_start + span]
        shifts: list[int] = []
        ok = True
        for j, target in enumerate(window):
            file_line = lines[target]
            if file_line.rstrip().lstrip(" ") != find_lines[j].lstrip(" "):
                ok = False
                break
            file_lead = len(file_line) - len(file_line.lstrip(" "))
            shifts.append(find_leads[j] - file_lead)
        if not ok:
            continue
        shift = shifts[0]
        if shift == 0 or any(s != shift for s in shifts):
            continue
        if abs(shift) > MAX_INDENT_SHIFT:
            continue
        return (
            starts[window[0]],
            starts[window[-1]] + len(lines[window[-1]]),
            f"indent{shift:+d}",
        )
    return None


def corrected_replace(replace: str, mode: str) -> str | None:
    """Replacement adjusted for the indent shift ``locate_edit`` accepted.

    ``mode == "indent+K"`` means the model's block has K more leading spaces
    per line than the file: K spaces are removed from every non-blank line of
    the replacement (negative K: K spaces are added), so the applied block
    keeps the file's own indentation. Returns ``None`` when the correction
    cannot be applied safely - a non-blank replacement line with fewer leading
    spaces than a positive shift - so callers refuse the edit instead of
    guessing.
    """
    if not mode.startswith("indent"):
        return replace
    shift = int(mode[len("indent") :])
    out: list[str] = []
    for line in replace.split("\n"):
        if not line.strip():
            out.append(line)
            continue
        if shift > 0:
            lead = len(line) - len(line.lstrip(" "))
            if lead < shift:
                return None
            out.append(line[shift:])
        else:
            out.append(" " * (-shift) + line)
    return "\n".join(out)


def preflight_edits(files: dict[str, str], edits: list[Edit]) -> list[str]:
    """All-or-nothing pre-flight: would every edit apply to ``files``?

    Walks the edits in order, applying each one to an in-memory copy with the
    exact same ``locate_edit`` the sandbox appliers use, so this check can
    never drift from apply semantics. Returns a problem list - empty means
    every edit would apply and the branch is safe to fork and race. It stops
    at the first problem: the appliers raise on the first failing edit, so a
    later problem would never be reached anyway.

    Measured failure class behind this check (2026-09-30, run #2): a model
    reply whose ``find`` text could not be located (one-character syntax
    slip) still forked a branch; the branch died mid-apply and was counted
    among the branches that "ran", hiding that its approach was never
    actually tested by the referee.

    File lookup is exact-key against the tree map (conservative on purpose):
    a path spelling that does not resolve here is refused before any sandbox
    op instead of dying mid-apply.
    """
    work = dict(files)
    for edit in edits:
        content = work.get(edit.file)
        if content is None:
            return [f"edit target file not in the baseline tree: {edit.file!r}"]
        span = locate_edit(content, edit.find)
        if span is None:
            lines = edit.find.strip().splitlines()
            snippet = lines[0][:60] if lines else ""
            return [f"edit target not found in {edit.file!r}: {snippet!r}"]
        start, end, mode = span
        replace = corrected_replace(edit.replace, mode)
        if replace is None:
            return [
                f"edit apply refused in {edit.file!r}: indent shift {mode!r} "
                "is larger than the replacement's own indentation"
            ]
        work[edit.file] = content[:start] + replace + content[end:]
    return []


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
            start, end, mode = span
            replace = corrected_replace(edit.replace, mode)
            if replace is None:
                raise SandboxError(
                    f"edit apply refused in {edit.file!r}: indent shift {mode!r} "
                    "is larger than the replacement's own indentation"
                )
            target.write_text(
                content[:start] + replace + content[end:], encoding="utf-8"
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
    blobs to the first spawn; ``apply_edits`` fetches only the files its edits
    name, re-uploads the changed ones and spawns from the current state.
    ``read_tree`` reads the tree in chunks: one ``find`` listing plus one
    ``tar | base64`` op per group of ``read_chunk_files`` files, with
    split-on-truncation - the service caps one op's stdout at exactly
    1,048,576 chars (measured 2026-09-30); cache paths never enter the tar.

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
        read_chunk_files: int = 16,
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
        self.read_chunk_files = max(1, read_chunk_files)
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
        # Fetch only the files the edits target (2026-09-30): apply_edits never
        # needs more than the edited files, while a whole-tree read costs
        # several ops on big trees.
        wanted: list[str] = []
        for edit in edits:
            if edit.file not in wanted:
                wanted.append(edit.file)
        fetched = self._fetch_paths(sid, wanted)
        pending: dict[str, str] = {}
        changed_lines = 0
        for edit in edits:
            content = pending[edit.file] if edit.file in pending else fetched.get(edit.file)
            if content is None:
                raise SandboxError(f"edit target file does not exist: {edit.file!r}")
            span = locate_edit(content, edit.find)
            if span is None:
                raise SandboxError(f"edit target not found in {edit.file!r}: {edit.find!r}")
            start, end, mode = span
            replace = corrected_replace(edit.replace, mode)
            if replace is None:
                raise SandboxError(
                    f"edit apply refused in {edit.file!r}: indent shift {mode!r} "
                    "is larger than the replacement's own indentation"
                )
            pending[edit.file] = content[:start] + replace + content[end:]
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

    def _list_files(self, sid: str) -> list[str]:
        """List text-candidate repo files (one op; busybox-safe).

        Plain ``find`` + sort only: ``find -printf`` is GNU-only and silently
        fails on the VM's busybox (a failing find behind a pipe looked like
        "no files"; measured 2026-09-30). Cache dirs and bytecode are filtered
        here so they never enter any later tar.
        """
        command = f"cd {self.REPO_DIR} && find . -type f | LC_ALL=C sort"
        op = self._op(self._state(sid), command, timeout=60, disposable=True)
        result, _ = self._result(op)
        output, _ = self._decode_stream(result.get("stdout"))
        files: list[str] = []
        for line in output.splitlines():
            rel = line.strip()
            if rel.startswith("./"):
                rel = rel[2:]
            if not rel:
                continue
            if any(part in _SKIP_DIRS for part in rel.split("/")):
                continue
            if rel.endswith(_SKIP_SUFFIXES):
                continue
            files.append(rel)
        if not files:
            raise SandboxError("read_tree: no files found")
        return files

    def _fetch_group(self, sid: str, rels: list[str]) -> dict[str, str] | None:
        """Fetch one group of files via a single ``tar | base64`` op.

        Returns the decoded files, ``{}`` when the op carried no payload, or
        ``None`` when the service truncated the output (the caller splits the
        group and refetches). A non-zero ``tar`` exit (missing path) does not
        fail the op: the absent path is simply not in the result.
        """
        paths = " ".join(_shq(f"./{rel}") for rel in rels)
        command = f"cd {self.REPO_DIR} && tar -cf - {paths} | base64 | tr -d '\\n'"
        op = self._op(self._state(sid), command, timeout=120, disposable=True)
        result, _ = self._result(op)
        payload, truncated = self._decode_stream(result.get("stdout"))
        if truncated:
            return None
        if not payload:
            return {}
        return _decode_tar_payload(payload)

    def _fetch_paths(self, sid: str, rels: list[str]) -> dict[str, str]:
        """Fetch the given repo files, chunked with split-on-truncation.

        Each group of ``read_chunk_files`` paths costs one op; a truncated
        group is split in half and refetched, so total tree size is never a
        failure mode. Erroring only when a SINGLE file exceeds the per-op
        stdout cap (exactly 1,048,576 chars - measured 2026-09-30).
        """
        tree: dict[str, str] = {}
        for start in range(0, len(rels), self.read_chunk_files):
            stack = [rels[start : start + self.read_chunk_files]]
            while stack:
                group = stack.pop()
                if not group:
                    continue
                fetched = self._fetch_group(sid, group)
                if fetched is None:
                    if len(group) == 1:
                        raise SandboxError(
                            "read_tree: single file above the 1 MiB per-op cap: "
                            f"{group[0]!r}"
                        )
                    mid = len(group) // 2
                    stack.append(group[mid:])
                    stack.append(group[:mid])
                    continue
                tree.update(fetched)
        return tree

    def read_tree(self, sid: str) -> dict[str, str]:
        """Read the repo tree (chunked; safe against the 1 MiB per-op cap).

        One ``find`` listing + one ``tar | base64`` op per group of
        ``read_chunk_files`` files, with split-on-truncation. A whole-tree
        single op exceeds the service's stdout cap on real trees (the
        pytest-generated ``__pycache__`` in tomlkit pushed it over; measured
        2026-09-30).
        """
        tree = self._fetch_paths(sid, self._list_files(sid))
        if not tree:
            raise SandboxError("read_tree decoded no text files")
        return tree

    def destroy(self, sid: str) -> None:
        # Drops the session pointer. The content-addressed upload cache and the
        # op audit trail are kept on purpose: the cache dedupes across sessions,
        # and ops[] is evidence for the run report (no secrets in it).
        self._sessions.pop(sid, None)
