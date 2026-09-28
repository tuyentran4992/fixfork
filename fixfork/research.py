"""Web-search grounding for the diagnosis step (Tavily, runtime, keyless).

Why: the diagnosis model proposes fix hypotheses from the repository source
and the failing-test log alone. Before that call, FixFork runs ONE web search
for the failure signature (failing test id + exception line) via the Tavily
API and injects the top results into the diagnosis prompt as a
"Known-issue research" section. The model still has to produce concrete,
exact-match edits - the web context is a hint, not a source of truth.

Access mode: keyless by default. Tavily keyless = POST /search with header
``X-Tavily-Access-Mode: keyless`` - free, no account, rate-limited; the
response schema is identical to keyed access (Tavily docs, checked
2026-09-28; a real call from this machine returned HTTP 200). If the
``TAVILY_API_KEY`` env var is set, it is used as a Bearer token instead.

Failure policy: a failed search never breaks a run. The pipeline records an
honest note ("web research skipped: ...") and continues without the section.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

DEFAULT_ENDPOINT = "https://api.tavily.com/search"
DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_RESULTS = 5
MAX_SNIPPET_CHARS = 240
MAX_BLOCK_CHARS = 1600


class ResearchError(RuntimeError):
    """Raised when a web search cannot be completed."""


@dataclass
class ResearchResult:
    """Top results of one search, as the diagnosis prompt will see them."""

    query: str
    sources: list[dict] = field(default_factory=list)  # {title, url, snippet}

    @property
    def n_sources(self) -> int:
        return len(self.sources)


class ResearchClient(Protocol):
    """Anything the pipeline can ask for failure research."""

    def search(self, query: str) -> ResearchResult: ...  # pragma: no cover


# unittest-style failure markers ("FAIL: test_x (module.Class)", "ERROR: test_y").
_TEST_ID_RE = re.compile(r"(?:FAIL|ERROR):\s*(\S+)")
_EXC_RE = re.compile(r"([\w.]*(?:Error|Exception)\b[^\n]{0,140})")


def build_query(log: str, test_command: str = "", max_len: int = 240) -> str:
    """Build a search-engine query from a failing-test log.

    Prefers the failing test identifier plus the first exception line; falls
    back to the last non-empty log line. The test command is appended as a
    toolchain hint when it fits within ``max_len``.
    """
    test_id = ""
    match = _TEST_ID_RE.search(log)
    if match:
        test_id = match.group(1).split("(")[0].strip()
    exc = ""
    match = _EXC_RE.search(log)
    if match:
        exc = match.group(1).strip()
    if not test_id and not exc:
        lines = [line.strip() for line in log.splitlines() if line.strip()]
        exc = lines[-1] if lines else ""
    query = " ".join(part for part in (test_id, exc) if part)
    query = " ".join(query.split())  # collapse runs of whitespace
    hint = " ".join(test_command.split())
    if hint:
        candidate = f"{query} {hint}".strip() if query else hint
        if len(candidate) <= max_len:
            query = candidate
        elif not query:
            query = hint  # will be truncated by the final slice
    return query[:max_len].strip()


class TavilyResearch:
    """One Tavily ``/search`` call per run. Keyless unless a key is provided.

    ``opener`` is injectable so offline tests can fake the HTTP layer; the
    default is ``urllib.request.urlopen`` (stdlib only, no dependencies).
    """

    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT,
        max_results: int = DEFAULT_MAX_RESULTS,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("TAVILY_API_KEY", "")
        self.api_key = key.strip()
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_results = max(0, int(max_results))
        self._opener = opener or urllib.request.urlopen

    def search(self, query: str) -> ResearchResult:
        query = query.strip()
        if not query:
            raise ResearchError("empty search query")
        payload = {"query": query, "max_results": self.max_results}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            headers["X-Tavily-Access-Mode"] = "keyless"
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ResearchError(f"tavily search failed: {exc}") from exc
        if not isinstance(data, dict):
            raise ResearchError("tavily response is not a JSON object")
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            raise ResearchError("tavily response has no results list")
        sources: list[dict] = []
        for item in raw_results[: self.max_results]:
            if not isinstance(item, dict):
                continue
            sources.append(
                {
                    "title": str(item.get("title") or "").strip(),
                    "url": str(item.get("url") or "").strip(),
                    "snippet": " ".join(str(item.get("content") or "").split())[
                        :MAX_SNIPPET_CHARS
                    ],
                }
            )
        return ResearchResult(query=query, sources=sources)


def render_research_block(result: ResearchResult) -> str:
    """Format search results for the diagnosis prompt, bounded in size.

    Returns "" when there is nothing to show. Entries are added until the
    block budget is spent, so the prompt never grows unbounded.
    """
    if not result.sources:
        return ""
    lines = [
        "Known-issue research (web search results for the failure signature;",
        "external context - use as hints, verify everything against the repository):",
    ]
    used = sum(len(line) + 1 for line in lines)
    added = 0
    for index, source in enumerate(result.sources, start=1):
        entry = f"{index}. {source['title']} - {source['url']}"
        if source["snippet"]:
            entry += f"\n   {source['snippet']}"
        if used + len(entry) + 1 > MAX_BLOCK_CHARS:
            break
        lines.append(entry)
        used += len(entry) + 1
        added += 1
    return "\n".join(lines) if added else ""
