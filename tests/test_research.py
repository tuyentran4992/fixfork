"""Web-search grounding: query builder, Tavily client, prompt wiring (offline)."""

import json
import unittest
import urllib.error
from pathlib import Path

from fixfork.events import human_line
from fixfork.fakes import FakeResearch, FakeRouter
from fixfork.pipeline import run_pipeline
from fixfork.research import (
    MAX_BLOCK_CHARS,
    MAX_SNIPPET_CHARS,
    ResearchError,
    ResearchResult,
    TavilyResearch,
    build_query,
    render_research_block,
)
from fixfork.sandbox_runner import LocalSandbox

DEMO_REPO = Path(__file__).resolve().parent.parent / "examples" / "demo-repo"
TEST_CMD = "python3 -m unittest discover -s tests"

FAIL_LOG = """running tests
FAIL: test_discount (tests.test_calc.TestCalc)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_calc.py", line 14, in test_discount
    self.assertEqual(apply_discount(100, 0.2), 80)
AssertionError: 120.0 != 80.0

FAILED (failures=2)
"""


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeOpener:
    """Records requests and replays one canned payload (or raises one error)."""

    def __init__(self, payload: bytes | None = None, error: Exception | None = None) -> None:
        self.payload = payload if payload is not None else b"{}"
        self.error = error
        self.requests: list[object] = []

    def __call__(self, request, timeout=None):  # noqa: ANN001 - mimics urlopen
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.payload)


def _tavily_payload(results: list[dict]) -> bytes:
    return json.dumps({"results": results}).encode("utf-8")


class BuildQueryTest(unittest.TestCase):
    def test_uses_test_id_and_exception_line(self):
        query = build_query(FAIL_LOG, TEST_CMD)
        self.assertIn("test_discount", query)
        self.assertIn("AssertionError: 120.0 != 80.0", query)
        self.assertIn(TEST_CMD, query)
        self.assertLessEqual(len(query), 240)

    def test_falls_back_to_last_line_and_collapses_whitespace(self):
        query = build_query("some noise\n\n  final   line\t here  \n", "")
        self.assertEqual(query, "final line here")

    def test_truncates_to_max_len(self):
        query = build_query("AssertionError: " + "x" * 400, "", max_len=50)
        self.assertEqual(len(query), 50)

    def test_hint_alone_fits_exactly(self):
        hint = "h" * 50
        self.assertEqual(build_query("", hint, max_len=50), hint)

    def test_long_hint_without_signal_is_truncated(self):
        self.assertEqual(build_query("", "h" * 300, max_len=50), "h" * 50)


class RenderBlockTest(unittest.TestCase):
    def test_empty_sources_render_nothing(self):
        self.assertEqual(render_research_block(ResearchResult(query="q", sources=[])), "")

    def test_block_contains_entries_and_stays_bounded(self):
        sources = [
            {"title": f"Title {i}", "url": f"https://example.com/{i}", "snippet": "s" * 200}
            for i in range(30)
        ]
        block = render_research_block(ResearchResult(query="q", sources=sources))
        self.assertIn("Known-issue research", block)
        self.assertIn("Title 0", block)
        self.assertIn("https://example.com/0", block)
        self.assertLessEqual(len(block), MAX_BLOCK_CHARS)


class TavilyResearchTest(unittest.TestCase):
    def test_keyless_request_shape_and_parsing(self):
        payload = _tavily_payload(
            [
                {
                    "title": "Known issue",
                    "url": "https://example.com/a",
                    "content": "  some\n content   with\nwhitespace  " + "y" * 400,
                },
                {"title": "Second", "url": "https://example.com/b", "content": "text"},
            ]
        )
        opener = _FakeOpener(payload=payload)
        client = TavilyResearch(opener=opener, api_key="")
        result = client.search("my query")

        request = opener.requests[0]
        self.assertEqual(request.full_url, "https://api.tavily.com/search")
        self.assertEqual(request.get_header("X-tavily-access-mode"), "keyless")
        self.assertIsNone(request.get_header("Authorization"))
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["query"], "my query")
        self.assertEqual(body["max_results"], 5)

        self.assertEqual(result.n_sources, 2)
        self.assertEqual(result.sources[0]["title"], "Known issue")
        snippet = result.sources[0]["snippet"]
        self.assertNotIn("\n", snippet)
        self.assertLessEqual(len(snippet), MAX_SNIPPET_CHARS)

    def test_api_key_switches_to_bearer(self):
        opener = _FakeOpener(payload=_tavily_payload([]))
        client = TavilyResearch(opener=opener, api_key="tvly-secret")
        client.search("q")
        request = opener.requests[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer tvly-secret")
        self.assertIsNone(request.get_header("X-tavily-access-mode"))

    def test_network_error_becomes_research_error(self):
        opener = _FakeOpener(error=urllib.error.URLError("boom"))
        client = TavilyResearch(opener=opener, api_key="")
        with self.assertRaises(ResearchError):
            client.search("q")

    def test_bad_payload_becomes_research_error(self):
        client = TavilyResearch(opener=_FakeOpener(payload=b"not json"), api_key="")
        with self.assertRaises(ResearchError):
            client.search("q")

    def test_missing_results_list_becomes_research_error(self):
        client = TavilyResearch(opener=_FakeOpener(payload=b'{"answer": "hi"}'), api_key="")
        with self.assertRaises(ResearchError):
            client.search("q")

    def test_non_object_payload_becomes_research_error(self):
        client = TavilyResearch(opener=_FakeOpener(payload=b"[]"), api_key="")
        with self.assertRaises(ResearchError):
            client.search("q")

    def test_null_fields_do_not_leak_none_strings(self):
        payload = _tavily_payload([{"title": None, "url": None, "content": None}])
        result = TavilyResearch(opener=_FakeOpener(payload=payload), api_key="").search("q")
        self.assertEqual(result.sources[0]["title"], "")
        self.assertEqual(result.sources[0]["url"], "")
        self.assertEqual(result.sources[0]["snippet"], "")

    def test_empty_query_rejected(self):
        with self.assertRaises(ResearchError):
            TavilyResearch(opener=_FakeOpener(), api_key="").search("   ")

    def test_zero_max_results_returns_empty(self):
        payload = _tavily_payload([{"title": "t", "url": "u", "content": "c"}])
        client = TavilyResearch(opener=_FakeOpener(payload=payload), api_key="", max_results=0)
        self.assertEqual(client.search("q").n_sources, 0)


class PipelineResearchTest(unittest.TestCase):
    def test_research_grounds_the_diagnosis_prompt(self):
        router = FakeRouter()
        research = FakeResearch()
        report = run_pipeline(
            DEMO_REPO, TEST_CMD, router, LocalSandbox(), branches=3, research=research
        )

        # the search ran once, on the failure signature
        self.assertEqual(len(research.queries), 1)
        self.assertIn("AssertionError", research.queries[0])

        # the diagnosis prompt actually carried the web context
        reason_prompts = [p for role, p in router.calls if role == "reason"]
        self.assertIn("Known-issue research", reason_prompts[0])
        self.assertIn("https://example.com/known-issue", reason_prompts[0])

        # ...and the run outcome is unchanged: evidence still decides
        self.assertEqual(report.winner_id, 1)
        self.assertEqual(report.research_query, research.queries[0])
        self.assertEqual(report.research_sources, ["https://example.com/known-issue"])

    def test_failed_search_never_breaks_the_run(self):
        router = FakeRouter()
        report = run_pipeline(
            DEMO_REPO, TEST_CMD, router, LocalSandbox(), branches=3,
            research=FakeResearch(fail=True),
        )
        self.assertEqual(report.winner_id, 1)
        self.assertEqual(report.research_query, "")
        self.assertIn("web research skipped", " ".join(report.notes))
        reason_prompts = [p for role, p in router.calls if role == "reason"]
        self.assertNotIn("Known-issue research", reason_prompts[0])

    def test_no_research_client_means_no_prompt_change(self):
        router = FakeRouter()
        report = run_pipeline(DEMO_REPO, TEST_CMD, router, LocalSandbox(), branches=3)
        self.assertEqual(report.research_query, "")
        reason_prompts = [p for role, p in router.calls if role == "reason"]
        self.assertNotIn("Known-issue research", reason_prompts[0])


class EventLineTest(unittest.TestCase):
    def test_research_events_have_human_lines(self):
        done = human_line(
            "research_done",
            {"query": "AssertionError: x != y", "n_sources": 5, "sources": []},
        )
        self.assertIn("5 source(s)", done or "")
        failed = human_line("research_failed", {"error": "timeout"})
        self.assertIn("continuing without it", failed or "")


if __name__ == "__main__":
    unittest.main()
