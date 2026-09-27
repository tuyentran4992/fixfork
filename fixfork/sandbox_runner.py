"""Sandbox backends.

``SandboxRunner`` is the interface the pipeline codes against. Two backends:

* ``LocalSandbox`` - development/tests only: plain temp dirs, no isolation.
  Checkpoint = in-memory snapshot of text files; fork = copy of a snapshot.
* ``NebiusSandbox`` - Token Factory Sandboxes (git-style fork/rollback at VM
  level). NOT WIRED YET: enable once an API key exists and the sandbox API has
  been exercised against the live service.
"""

from __future__ import annotations

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
            if edit.find not in content:
                raise SandboxError(
                    f"edit target not found in {edit.file!r}: {edit.find!r}"
                )
            target.write_text(
                content.replace(edit.find, edit.replace, 1), encoding="utf-8"
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
    """Token Factory Sandboxes backend - NOT WIRED YET.

    Enable once an API key exists and ``api.tokenfactory.nebius.com/sandboxes``
    has been exercised live (TIEN-DO step 4). Every method raises until then, on
    purpose: no silent fallback that would fake "it ran in a Nebius sandbox".
    """

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "NebiusSandbox backend is not wired yet: needs a Token Factory API key "
            "and a live check of the sandboxes API (still Beta)."
        )
