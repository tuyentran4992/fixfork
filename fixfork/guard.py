"""Referee protection: refuse edits to test, CI and build/config files.

Why this exists: FixFork's only arbiter is the repository's test suite. A branch
that rewrites the tests can make them pass without fixing anything - the
referee gets bought off. A recorded race showed exactly that: a hypothesis
"the test expectations are wrong" edited ``tests/test_tax.py`` and the branch
was scored green, losing only because another branch changed fewer lines.

The rule: a branch may fix source code, but it may never touch the referee
(test files), the build pipeline (CI workflows) or the configuration those run
with. Proposed edits to protected paths are refused *before any sandbox work*,
the branch is marked ``blocked``, and it can never win or be suggested as a
lead. ``diff_violations`` is the last-resort check: no exported patch may touch
a protected path, no matter how it got produced.

Keep this list in sync with the threat it addresses - test files, CI config,
build scripts, dependency manifests - not with any particular repository.
The list is deliberately conservative (cross-checked): shell scripts and config
files are a code-injection surface, so they are refused even when they look
like ordinary source. Refusing a legitimate edit costs a lead; accepting an
edit to the referee costs the product's only guarantee.
"""

from __future__ import annotations

import re
from typing import Iterable, Protocol


class _HasFile(Protocol):
    file: str


# (pattern over the repo-relative path, human-readable reason)
PROTECTED: list[tuple[str, str]] = [
    (r"(^|/)(tests?|testing|spec|specs)(/|$)", "edits files under a test directory"),
    (r"(^|/)test_[^/]*\.py$", "edits a test file"),
    (r"(^|/)[^/]*_test\.py$", "edits a test file"),
    (r"(^|/)conftest\.py$", "edits test configuration"),
    (
        r"(^|/)(pytest\.ini|tox\.ini|setup\.cfg|noxfile\.py|\.pre-commit-config\.yaml)$",
        "edits test/build configuration",
    ),
    (r"(^|/)\.github/", "edits CI workflow (build-time command injection point)"),
    (r"(^|/)\.gitlab-ci\.yml$|(^|/)\.circleci/", "edits CI configuration"),
    (r"(^|/)(Makefile|makefile|Dockerfile)$", "edits a build file"),
    (r"\.(sh|bash|yml|yaml|ini|cfg|toml)$", "edits a config or shell-script file"),
    (r"(^|/)requirements[^/]*\.txt$", "edits the dependency list"),
    (r"^/", "absolute path (outside the repo)"),
    (r"(^|/)\.\.(/|$)", "path escapes the repo (..)"),
]


def protected_reason(path: str) -> str | None:
    """Return why ``path`` is off-limits, or None when the path is editable."""
    if not path or not path.strip():
        return "empty path"
    for pattern, reason in PROTECTED:
        if re.search(pattern, path):
            return reason
    return None


def check_edits(edits: Iterable[_HasFile]) -> list[tuple[str, str]]:
    """List (file, reason) pairs for edits that target protected paths."""
    violations: list[tuple[str, str]] = []
    for edit in edits:
        path = getattr(edit, "file", "") or ""
        reason = protected_reason(path)
        if reason:
            violations.append((path, reason))
    return violations


def describe_violations(violations: list[tuple[str, str]]) -> str:
    """One-line human explanation of a violation list (for reports/logs)."""
    return "; ".join(f"{path} ({reason})" for path, reason in violations)


_HDR_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')

_GIT_UNESCAPE = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def _git_unquote(token: str) -> str:
    """Undo git's C-style quoting for one path token (``core.quotePath``)."""
    return re.sub(
        r"\\(.)", lambda m: _GIT_UNESCAPE.get(m.group(1), m.group(1)), token
    )


def _header_paths(line: str) -> tuple[str, str] | None:
    """The two repo paths in a ``diff --git`` header, quoted or not.

    Git writes ``diff --git a/x b/x``; a path with spaces or other unusual
    characters is C-quoted instead (``diff --git "a/x y" "b/x y"``), and the
    quoting can differ per side. Tokenizing covers both forms; a naive
    ``a/(...) b/(...)`` regex silently drops quoted headers, which would let a
    protected change slip past this last-resort check.
    """
    if not line.startswith("diff --git "):
        return None
    got: list[str] = []
    for match in _HDR_TOKEN.finditer(line[len("diff --git "):]):
        token = match.group(1)
        if token is not None:
            token = _git_unquote(token)
        else:
            token = match.group(2)
        for prefix in ("a/", "b/"):
            if token.startswith(prefix):
                token = token[len(prefix):]
                break
        got.append(token)
        if len(got) == 2:
            return got[0], got[1]
    return None


def diff_violations(diff: str) -> list[str]:
    """Protected file paths touched by a unified diff (defense in depth).

    The pipeline refuses protected edits before they are applied, so a clean
    ``build_patch`` output should contain no protected path at all; if one
    appears anyway, the caller must withhold the patch rather than export it.
    Both sides of each file header are checked - including C-quoted headers
    for paths with spaces - so deletions and renames of a protected file
    cannot slip through as their non-protected counterpart.
    """
    touched: set[str] = set()
    for line in diff.splitlines():
        paths = _header_paths(line)
        if paths:
            touched.update(paths)
    return sorted(path for path in touched if protected_reason(path))
