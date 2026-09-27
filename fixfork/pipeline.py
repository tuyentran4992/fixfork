"""The FixFork pipeline: baseline -> hypotheses -> fork & race -> verdict."""

from __future__ import annotations

import difflib
import json
from pathlib import Path

from .hypotheses import HypothesisError, build_prompt, parse_hypotheses, render_files
from .judge import pick_winner
from .model_router import ModelRouter, RouterError
from .models import BranchResult, BranchStatus, Edit, RunReport
from .sandbox_runner import SandboxError, SandboxRunner
from .testparse import parse_unittest_output

LOOP_PROMPT_TEMPLATE = """You are FixFork's iteration model inside one branch.

The edit below was applied and the tests STILL fail. Propose a small follow-up
edit, or reply with an empty list if you have no further idea.

Reply with ONLY a JSON object: {{"edits": [{{"file": "...", "find": "exact existing text", "replace": "..."}}]}}

The `find` text MUST be copied EXACTLY from the branch files below (the branch
already contains the earlier edit).

Applied the branch hypothesis: {title}
Test command: {test_command}

Branch files:
{files}

Failure log (tail):
{log}
"""


def _parse_loop_edits(text: str) -> list[Edit]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise HypothesisError("no JSON object found in loop reply")
    data = json.loads(text[start : end + 1])
    edits = data.get("edits")
    if not isinstance(edits, list):
        raise HypothesisError("loop reply has no 'edits' list")
    parsed: list[Edit] = []
    for raw in edits:
        if not isinstance(raw, dict):
            raise HypothesisError("loop reply has a non-object edit")
        edit = Edit(
            file=str(raw.get("file", "")).strip(),
            find=str(raw.get("find", "")),
            replace=str(raw.get("replace", "")),
        )
        if not edit.file or not edit.find or edit.find == edit.replace:
            raise HypothesisError("loop reply has an invalid edit")
        parsed.append(edit)
    return parsed


def _diff(base_files: dict[str, str], winner_files: dict[str, str]) -> str:
    chunks: list[str] = []
    for rel in sorted(set(base_files) | set(winner_files)):
        before = base_files.get(rel, "").splitlines(keepends=True)
        after = winner_files.get(rel, "").splitlines(keepends=True)
        if before == after:
            continue
        chunks.extend(
            difflib.unified_diff(before, after, fromfile=f"a/{rel}", tofile=f"b/{rel}")
        )
    return "".join(chunks)


def run_pipeline(
    repo: str | Path,
    test_command: str,
    router: ModelRouter,
    sandbox: SandboxRunner,
    branches: int = 3,
    max_rounds: int = 2,
    log_tail: int = 2000,
) -> RunReport:
    repo_path = Path(repo)
    report = RunReport(repo=str(repo_path), test_command=test_command)

    baseline_sid = sandbox.create(repo_path, "baseline")
    baseline_exec = sandbox.run(baseline_sid, test_command)
    report.baseline = parse_unittest_output(baseline_exec.output, baseline_exec.returncode)
    if report.baseline.ok:
        report.notes.append("baseline test run already passes - nothing to fix")
        return report

    base_files = sandbox.read_tree(baseline_sid)
    base_snapshot = sandbox.checkpoint(baseline_sid)
    log = baseline_exec.output[-log_tail:] if baseline_exec.output else "(no output)"

    try:
        reply = router.complete(
            "reason",
            build_prompt(report.repo, test_command, log, n=branches, files=base_files),
        )
    except RouterError as exc:
        report.notes.append(f"hypothesis call failed: {exc}")
        return report
    # keep the raw reply (and its cost) even if parsing fails - it is the only
    # way to debug what the model actually said
    report.diagnosis_raw = reply.text
    report.diagnosis_tokens = reply.tokens_used
    report.diagnosis_cost_usd = reply.cost_usd
    try:
        hypotheses = parse_hypotheses(reply.text, n=branches)
    except HypothesisError as exc:
        report.notes.append(f"hypothesis reply did not parse: {exc}")
        return report
    report.hypotheses = hypotheses
    report.notes.append(
        f"diagnosis model proposed {len(hypotheses)} hypotheses "
        f"({reply.tokens_used} tokens, ${reply.cost_usd:.4f})"
    )

    for hypothesis in hypotheses:
        sid = sandbox.fork(baseline_sid, base_snapshot, f"branch-{hypothesis.id}")
        result = BranchResult(hypothesis_id=hypothesis.id)
        try:
            result.lines_changed = sandbox.apply_edits(sid, hypothesis.edits)
            exec_result = sandbox.run(sid, test_command)
            outcome = parse_unittest_output(exec_result.output, exec_result.returncode)
            rounds = 1
            while not outcome.ok and rounds < max_rounds:
                try:
                    loop_reply = router.complete(
                        "loop",
                        LOOP_PROMPT_TEMPLATE.format(
                            title=hypothesis.title,
                            test_command=test_command,
                            files=render_files(sandbox.read_tree(sid)),
                            log=exec_result.output[-log_tail:],
                        ),
                    )
                    extra_edits = _parse_loop_edits(loop_reply.text)
                except (RouterError, HypothesisError):
                    break
                result.tokens_used += loop_reply.tokens_used
                result.cost_usd += loop_reply.cost_usd
                if not extra_edits:
                    break
                result.lines_changed += sandbox.apply_edits(sid, extra_edits)
                exec_result = sandbox.run(sid, test_command)
                outcome = parse_unittest_output(exec_result.output, exec_result.returncode)
                rounds += 1
            result.status = BranchStatus.GREEN if outcome.ok else BranchStatus.RED
            result.outcome = outcome
            result.rounds = rounds
            result.log_tail = exec_result.output[-log_tail:]
        except SandboxError as exc:
            result.status = BranchStatus.ERROR
            result.log_tail = f"sandbox error: {exc}"
        report.branches.append(result)

    winner_id, reason = pick_winner(report.branches)
    report.winner_id = winner_id
    report.winner_reason = reason

    if winner_id is not None:
        winner_sid = f"branch-{winner_id}"
        for branch in report.branches:
            if branch.hypothesis_id != winner_id:
                sandbox.rollback(f"branch-{branch.hypothesis_id}", base_snapshot)
        if any(b.status is BranchStatus.GREEN for b in report.branches if b.hypothesis_id == winner_id):
            report.winner_diff = _diff(base_files, sandbox.read_tree(winner_sid))

    return report
