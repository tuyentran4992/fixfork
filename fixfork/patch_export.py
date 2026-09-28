"""Export a race winner as a git-apply-able patch.

The sandbox snapshots are ``{relative_path: text}`` trees; this module turns a
(base, winner) pair into what ``git diff`` would print, so the artifact can be
applied directly: ``git apply fix.patch``.

A report says "branch 1 won" - a patch is what a human actually applies. It
also keeps FixFork honest: the diff must survive ``git apply`` on a pristine
checkout or it is not a fix, just a claim.
"""

from __future__ import annotations

import difflib


def _terminate(lines: list[str]) -> list[str]:
    """git patches need newline-terminated lines; mark the rest like git does."""
    out: list[str] = []
    for line in lines:
        if line.endswith("\n"):
            out.append(line)
        else:
            out.append(line + "\n")
            out.append("\\ No newline at end of file\n")
    return out


def build_patch(base_files: dict[str, str], winner_files: dict[str, str]) -> str:
    """Return a unified patch (git apply compatible) from base to winner tree."""
    chunks: list[str] = []
    for rel in sorted(set(base_files) | set(winner_files)):
        before = base_files.get(rel)
        after = winner_files.get(rel)
        if before == after:
            continue
        header = [f"diff --git a/{rel} b/{rel}\n"]
        if before is None:
            header.append("new file mode 100644\n")
        elif after is None:
            header.append("deleted file mode 100644\n")
        diff = difflib.unified_diff(
            (before or "").splitlines(keepends=True),
            (after or "").splitlines(keepends=True),
            fromfile="/dev/null" if before is None else f"a/{rel}",
            tofile="/dev/null" if after is None else f"b/{rel}",
        )
        chunks.append("".join(_terminate(header + list(diff))))
    return "".join(chunks)
