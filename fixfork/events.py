"""Structured run events — one JSON object per line, written while the run happens.

Why this exists: a FixFork run takes tens of seconds and the interesting part is
the RACE (branches fork, tests pass or fail, losers roll back), not just the
final table. The event log is the audit trail of a run: the CLI prints live
progress from it, and tools (e.g. the demo-video builder) can replay a run
exactly as it happened — with real timestamps.

Design: the pipeline only knows the ``EventSink`` protocol, so it stays
print-free and testable. ``NullSink`` (default) drops everything; ``JsonlEventSink``
persists events and can echo a one-line human summary to stdout.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol


class EventSink(Protocol):
    """Anything the pipeline can emit events into."""

    def emit(self, kind: str, **data: Any) -> None: ...  # pragma: no cover


class NullSink:
    """Drops every event (default when no event log was requested)."""

    def emit(self, kind: str, **data: Any) -> None:
        return None

    def close(self) -> None:
        return None


def human_line(kind: str, data: dict) -> str | None:
    """One-line terminal rendering of an event (English, goes into captures)."""
    if kind == "research_done":
        return (
            f"web research: {data.get('n_sources')} source(s) for "
            f"\"{str(data.get('query', ''))[:60]}\""
        )
    if kind == "research_failed":
        return f"web research: failed ({data.get('error', '')}) - continuing without it"
    if kind == "baseline_done":
        return f"baseline: {data.get('summary', '?')}"
    if kind == "hypotheses_ready":
        n = data.get("n")
        tok = data.get("tokens")
        cost = data.get("cost")
        return f"diagnosis: {n} hypotheses ({tok} tokens, ${cost:.4f})"
    if kind == "branch_started":
        return f"fork branch {data.get('id')}: {data.get('title', '')}"
    if kind == "branch_blocked":
        return f"branch {data.get('id')}: BLOCKED - {data.get('reason', '')}"
    if kind == "branch_done":
        return (
            f"branch {data.get('id')}: {data.get('status')} | "
            f"{data.get('summary', '')} | {data.get('lines_changed')} line(s) changed"
        )
    if kind == "winner":
        winner = data.get("id")
        if winner is None:
            return "winner: none (no branch produced a passing fix)"
        return f"winner: branch {winner} — {data.get('reason', '')}"
    if kind == "rollback":
        return f"rollback branch {data.get('id')} (fork discarded)"
    if kind == "done":
        return f"done: {data.get('total_tokens')} tokens, ~${data.get('cost'):.4f}"
    return None


class JsonlEventSink:
    """Appends one JSON line per event, with seconds-since-start timestamps."""

    def __init__(self, path: str | Path, echo: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._t0 = time.monotonic()
        self._fh = self.path.open("w", encoding="utf-8")
        self._echo = echo

    def emit(self, kind: str, **data: Any) -> None:
        if kind == "run_start":
            self._t0 = time.monotonic()
        record = {"t": round(time.monotonic() - self._t0, 3), "event": kind, **data}
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()
        if self._echo:
            line = human_line(kind, data)
            if line is not None:
                print(line, flush=True)

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()


def read_events(path: str | Path) -> list[dict]:
    """Load an event log written by JsonlEventSink (tolerates a torn last line)."""
    events: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a killed process can leave a partial last line
    return events
