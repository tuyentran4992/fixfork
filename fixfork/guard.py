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


def diff_violations(diff: str) -> list[str]:
    """Protected file paths touched by a unified diff (defense in depth).

    The pipeline refuses protected edits before they are applied, so a clean
    ``build_patch`` output should contain no protected path at all; if one
    appears anyway, the caller must withhold the patch rather than export it.
    Both sides of each file header are checked, so deletions and renames of a
    protected file cannot slip through as their non-protected target name.
    """
    touched: set[str] = set()
    for line in diff.splitlines():
        match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
        if match:
            touched.add(match.group(1))
            touched.add(match.group(2))
    return sorted(path for path in touched if protected_reason(path))
