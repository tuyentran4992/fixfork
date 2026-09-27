"""Turn model output into validated, divergent hypotheses."""

from __future__ import annotations

import json

from .model_router import ModelRouter
from .models import Edit, Hypothesis


class HypothesisError(RuntimeError):
    """Raised when a model reply cannot be turned into ``n`` valid hypotheses."""


PROMPT_TEMPLATE = """You are FixFork's diagnosis model.

A repository has a failing test. Read the failure log below and propose {n}
DIVERGENT root-cause hypotheses: each must be a different kind of explanation
(not three variations of the same guess) and must come with concrete edits.

Reply with ONLY a JSON array, no prose, no markdown fences. Each element:
{{"title": "...", "rationale": "...", "edits": [{{"file": "path/relative/to/repo", "find": "exact existing text", "replace": "replacement text"}}]}}

Repository: {repo}
Failing test command: {test_command}

Failure log (tail):
{log}
"""


def build_prompt(repo: str, test_command: str, log: str, n: int = 3) -> str:
    return PROMPT_TEMPLATE.format(n=n, repo=repo, test_command=test_command, log=log)


def extract_json_array(text: str) -> str:
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise HypothesisError("no JSON array found in model reply")
    return text[start : end + 1]


def parse_hypotheses(text: str, n: int = 3) -> list[Hypothesis]:
    raw = extract_json_array(text)
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HypothesisError(f"hypothesis reply is not valid JSON: {exc}") from exc
    if not isinstance(items, list) or len(items) != n:
        got = len(items) if isinstance(items, list) else type(items).__name__
        raise HypothesisError(f"expected {n} hypotheses, got {got}")

    hypotheses: list[Hypothesis] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise HypothesisError(f"hypothesis #{index} is not an object")
        title = str(item.get("title", "")).strip()
        rationale = str(item.get("rationale", "")).strip()
        raw_edits = item.get("edits")
        if not title or not rationale:
            raise HypothesisError(f"hypothesis #{index} needs a title and a rationale")
        if not isinstance(raw_edits, list) or not raw_edits:
            raise HypothesisError(f"hypothesis #{index} needs at least one edit")
        edits: list[Edit] = []
        for raw_edit in raw_edits:
            if not isinstance(raw_edit, dict):
                raise HypothesisError(f"hypothesis #{index} has a non-object edit")
            edit = Edit(
                file=str(raw_edit.get("file", "")).strip(),
                find=str(raw_edit.get("find", "")),
                replace=str(raw_edit.get("replace", "")),
            )
            if not edit.file or not edit.find:
                raise HypothesisError(f"hypothesis #{index} has an edit without file/find")
            if edit.find == edit.replace:
                raise HypothesisError(f"hypothesis #{index} has a no-op edit")
            edits.append(edit)
        hypotheses.append(Hypothesis(id=index, title=title, rationale=rationale, edits=edits))

    assert_divergent(hypotheses)
    return hypotheses


def _edit_keys(hypothesis: Hypothesis) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted((edit.file, edit.find, edit.replace) for edit in hypothesis.edits))


def assert_divergent(hypotheses: list[Hypothesis]) -> None:
    """Reject exact duplicates: distinct titles and distinct full edit sets.

    Two hypotheses may target the same line with different replacements (that is
    divergence, not duplication); identical (file, find, replace) sets are not.
    """
    titles = [h.title.lower() for h in hypotheses]
    if len(set(titles)) != len(titles):
        raise HypothesisError("hypotheses must have distinct titles")
    for i in range(len(hypotheses)):
        for j in range(i + 1, len(hypotheses)):
            if _edit_keys(hypotheses[i]) == _edit_keys(hypotheses[j]):
                raise HypothesisError(
                    f"hypotheses {hypotheses[i].id} and {hypotheses[j].id} "
                    "propose the exact same edits"
                )


def generate_hypotheses(
    router: ModelRouter,
    repo: str,
    test_command: str,
    log: str,
    n: int = 3,
) -> tuple[list[Hypothesis], int]:
    reply = router.complete("reason", build_prompt(repo, test_command, log, n=n))
    return parse_hypotheses(reply.text, n=n), reply.tokens_used
