"""The FixFork pipeline: baseline -> hypotheses -> fork & race -> verdict."""

from __future__ import annotations

import json
from pathlib import Path

from . import guard
from .events import EventSink, NullSink
from .hypotheses import (
    MAX_FILE_CHARS,
    MAX_TOTAL_FILES_CHARS,
    PARSE_FAILURE,
    HypothesisError,
    build_prompt,
    defect_scan,
    extract_refs,
    loads_json_tolerant,
    parse_edit_object,
    parse_hypotheses,
    render_files,
)
from .judge import pick_winner
from .model_router import ModelRouter, RouterError
from .models import BranchResult, BranchStatus, Edit, RunReport
from .patch_export import build_patch
from .research import ResearchClient, ResearchError, build_query, render_research_block
from .sandbox_runner import SandboxError, SandboxRunner, preflight_edits
from .testparse import parse_unittest_output

LOOP_PROMPT_TEMPLATE = """You are FixFork's iteration model inside one branch.

The edit below was applied and the tests STILL fail. Propose a small follow-up
edit, or reply with an empty list if you have no further idea.

Reply with ONLY a JSON object: {{"edits": [{{"file": "...", "find": "exact existing text", "replace": "..."}}]}}

The `find` text MUST be copied EXACTLY from the branch files below (the branch
already contains the earlier edit); keep blank lines and trailing spaces as they
are in the file.

Edits to test files, CI workflows and build/config files are OFF-LIMITS and
will be refused - fix the source code only.

Applied the branch hypothesis: {title}
Test command: {test_command}

Branch files:
{files}

Failure log (tail):
{log}
"""


def _parse_loop_edits(text: str, notes: list[str] | None = None) -> list[Edit]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise HypothesisError("no JSON object found in loop reply")
    # json.loads raises JSONDecodeError (a ValueError) and a JSON array has no
    # .get: both used to escape the loop's `except (RouterError,
    # HypothesisError)` and crash the whole run. Same class of bug as the
    # earlier AttributeError-escapes-the-except incident. Parsing goes through
    # the same bounded near-valid-JSON repair as the diagnosis reply.
    raw = text[start : end + 1]
    data, repairs = loads_json_tolerant(raw)
    if data is PARSE_FAILURE:
        try:
            json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HypothesisError(f"loop reply is not valid JSON: {exc}") from exc
        # Defensive: PARSE_FAILURE means the strict parse above already failed,
        # so this line is unreachable today (soi chéo 01/10).
        raise HypothesisError("loop reply is not valid JSON")
    if not isinstance(data, dict):
        raise HypothesisError("loop reply JSON is not an object")
    if repairs and notes is not None:
        # Same audit rule the diagnosis reply follows: a repaired reply must
        # say so, or the report claims a clean parse that never happened
        # (soi chéo 01/10, aibox: loop-path repairs were applied silently).
        notes.append(
            f"loop reply was lightly repaired ({repairs} JSON fix(es)) before parsing"
        )
    edits = data.get("edits")
    if not isinstance(edits, list):
        raise HypothesisError("loop reply has no 'edits' list")
    return [parse_edit_object(raw, "loop reply") for raw in edits]


def run_pipeline(
    repo: str | Path,
    test_command: str,
    router: ModelRouter,
    sandbox: SandboxRunner,
    branches: int = 3,
    max_rounds: int = 2,
    log_tail: int = 2000,
    events: EventSink | None = None,
    research: ResearchClient | None = None,
    max_file_chars: int = MAX_FILE_CHARS,
    max_total_chars: int = MAX_TOTAL_FILES_CHARS,
) -> RunReport:
    sink: EventSink = events if events is not None else NullSink()
    repo_path = Path(repo)
    report = RunReport(repo=str(repo_path), test_command=test_command)
    sink.emit("run_start", repo=str(repo_path), test_command=test_command, branches=branches)

    baseline_sid = sandbox.create(repo_path, "baseline")
    baseline_exec = sandbox.run(baseline_sid, test_command)
    report.baseline = parse_unittest_output(baseline_exec.output, baseline_exec.returncode)
    sink.emit("baseline_done", ok=report.baseline.ok, summary=report.baseline.summary)
    if report.baseline.ok:
        report.notes.append("baseline test run already passes - nothing to fix")
        return report

    base_files = sandbox.read_tree(baseline_sid)
    base_snapshot = sandbox.checkpoint(baseline_sid)
    log = baseline_exec.output[-log_tail:] if baseline_exec.output else "(no output)"
    # Files the failure log points at get prompt priority (see render_files).
    refs = extract_refs(log, base_files)

    research_block = ""
    if research is not None:
        query = build_query(log, test_command)
        try:
            research_result = research.search(query)
            research_block = render_research_block(research_result)
            report.research_query = query
            report.research_sources = [
                source["url"] for source in research_result.sources if source["url"]
            ]
            report.notes.append(
                f"web research (Tavily): {research_result.n_sources} source(s) "
                f"for query: {query}"
            )
            sink.emit(
                "research_done",
                query=query,
                n_sources=research_result.n_sources,
                sources=report.research_sources[:3],
            )
        except ResearchError as exc:
            report.notes.append(f"web research skipped: {exc}")
            sink.emit("research_failed", query=query, error=str(exc)[:160])

    scan = defect_scan(log, base_files, refs)
    report.notes.extend(scan.notes)
    try:
        reply = router.complete(
            "reason",
            build_prompt(
                report.repo,
                test_command,
                log,
                n=branches,
                files=base_files,
                research_block=research_block,
                refs=refs,
                max_file_chars=max_file_chars,
                max_total_chars=max_total_chars,
                occurrences_block=scan.block,
                refs_full=scan.fallback,
            ),
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
        hypotheses = parse_hypotheses(reply.text, n=branches, notes=report.notes)
    except HypothesisError as exc:
        report.notes.append(f"hypothesis reply did not parse: {exc}")
        return report
    report.hypotheses = hypotheses
    report.notes.append(
        f"diagnosis model proposed {len(hypotheses)} hypotheses "
        f"({reply.tokens_used} tokens, ${reply.cost_usd:.4f})"
    )
    sink.emit(
        "hypotheses_ready",
        n=len(hypotheses),
        titles=[h.title for h in hypotheses],
        tokens=reply.tokens_used,
        cost=round(reply.cost_usd, 6),
    )

    forked_ids: set[int] = set()
    for hypothesis in hypotheses:
        sink.emit("branch_started", id=hypothesis.id, title=hypothesis.title)
        result = BranchResult(hypothesis_id=hypothesis.id)
        # The test suite is the referee: a branch whose edits touch test, CI or
        # build/config files is refused BEFORE any sandbox work - it is never
        # forked, never green, and never suggested as a lead.
        violations = guard.check_edits(hypothesis.edits)
        if violations:
            result.status = BranchStatus.BLOCKED
            result.blocked_reason = guard.describe_violations(violations)
            result.log_tail = f"blocked before any sandbox op: {result.blocked_reason}"
            report.branches.append(result)
            sink.emit("branch_blocked", id=hypothesis.id, reason=result.blocked_reason)
            sink.emit(
                "branch_done",
                id=hypothesis.id,
                status=result.status.value,
                summary=result.blocked_reason,
                tests_ok=False,
                lines_changed=0,
                tokens=0,
                cost=0.0,
                rounds=0,
            )
            continue
        # A branch is forked only when every BASELINE edit actually locates
        # in the tree (all-or-nothing, pre-flight). Otherwise the model's
        # block was broken and the branch would die mid-apply while still
        # being counted as one that ran - its approach was never tested.
        # Follow-up (loop) edits are a different case: they apply against
        # the branch's updated tree and are caught below without discarding
        # a measured outcome.
        problems = preflight_edits(base_files, hypothesis.edits)
        if problems:
            result.status = BranchStatus.NOT_RUN
            result.blocked_reason = "; ".join(problems)
            result.log_tail = (
                "not run: edits do not locate in the baseline tree: "
                + result.blocked_reason
            )
            report.branches.append(result)
            sink.emit("branch_not_run", id=hypothesis.id, reason=result.blocked_reason)
            sink.emit(
                "branch_done",
                id=hypothesis.id,
                status=result.status.value,
                summary=result.blocked_reason,
                tests_ok=False,
                lines_changed=0,
                tokens=0,
                cost=0.0,
                rounds=0,
            )
            continue
        sid = sandbox.fork(baseline_sid, base_snapshot, f"branch-{hypothesis.id}")
        forked_ids.add(hypothesis.id)
        try:
            result.lines_changed = sandbox.apply_edits(sid, hypothesis.edits)
            exec_result = sandbox.run(sid, test_command)
            outcome = parse_unittest_output(exec_result.output, exec_result.returncode)
            rounds = 1
            while not outcome.ok and rounds < max_rounds:
                branch_files = sandbox.read_tree(sid)
                branch_log = exec_result.output[-log_tail:] if exec_result.output else ""
                try:
                    loop_reply = router.complete(
                        "loop",
                        LOOP_PROMPT_TEMPLATE.format(
                            title=hypothesis.title,
                            test_command=test_command,
                            files=render_files(
                                branch_files,
                                max_file_chars=max_file_chars,
                                max_total_chars=max_total_chars,
                                refs=extract_refs(branch_log, branch_files),
                            ),
                            log=branch_log or "(no output)",
                        ),
                    )
                except RouterError as exc:
                    # Same shape as a parsed reply (branch_id/round/text/
                    # tokens/cost_usd) so sidecar consumers can read every
                    # entry uniformly; empty text + zero cost mark the call
                    # that errored (soi chéo 30/09: schema was non-uniform).
                    report.loop_raw.append(
                        {
                            "branch_id": hypothesis.id,
                            "round": rounds,
                            "text": "",
                            "tokens": 0,
                            "cost_usd": 0.0,
                            "error": str(exc),
                        }
                    )
                    break
                # Keep the raw follow-up reply (and its cost) even when it
                # does not parse - same rule the diagnosis call follows; a
                # real run was undebuggable because its loop replies were lost.
                result.tokens_used += loop_reply.tokens_used
                result.cost_usd += loop_reply.cost_usd
                loop_entry = {
                    "branch_id": hypothesis.id,
                    "round": rounds,  # test rounds completed when it was asked
                    "tokens": loop_reply.tokens_used,
                    "cost_usd": round(loop_reply.cost_usd, 6),
                    "text": loop_reply.text,
                }
                report.loop_raw.append(loop_entry)
                try:
                    extra_edits = _parse_loop_edits(loop_reply.text, notes=report.notes)
                except HypothesisError as exc:
                    loop_entry["parse_error"] = str(exc)
                    break
                if not extra_edits:
                    break
                loop_violations = guard.check_edits(extra_edits)
                if loop_violations:
                    result.blocked_reason = guard.describe_violations(loop_violations)
                    result.status = BranchStatus.BLOCKED
                    sink.emit("branch_blocked", id=hypothesis.id, reason=result.blocked_reason)
                    break
                try:
                    result.lines_changed += sandbox.apply_edits(sid, extra_edits)
                    exec_result = sandbox.run(sid, test_command)
                    outcome = parse_unittest_output(exec_result.output, exec_result.returncode)
                except SandboxError as exc:
                    # A follow-up edit that cannot be applied must not kill a
                    # branch that already ran and produced a measured outcome.
                    # Measured on a real repo: two loop edits died on apply and
                    # flipped their branch from red to error, discarding the
                    # result of the tests that HAD run.
                    report.notes.append(
                        f"branch {hypothesis.id}: follow-up edit failed to apply "
                        f"({exc}); keeping the last test outcome"
                    )
                    break
                rounds += 1
            if result.status is not BranchStatus.BLOCKED:
                result.status = BranchStatus.GREEN if outcome.ok else BranchStatus.RED
            result.outcome = outcome
            result.rounds = rounds
            result.log_tail = exec_result.output[-log_tail:]
        except SandboxError as exc:
            result.status = BranchStatus.ERROR
            result.log_tail = f"sandbox error: {exc}"
        report.branches.append(result)
        sink.emit(
            "branch_done",
            id=hypothesis.id,
            status=result.status.value,
            summary=(
                result.blocked_reason
                if result.status is BranchStatus.BLOCKED
                else result.outcome.summary
            ),
            tests_ok=result.outcome.ok,
            lines_changed=result.lines_changed,
            tokens=result.tokens_used,
            cost=round(result.cost_usd, 6),
            rounds=result.rounds,
        )

    winner_id, reason = pick_winner(report.branches)
    report.winner_id = winner_id
    report.winner_reason = reason
    sink.emit("winner", id=winner_id, reason=reason)

    if winner_id is not None:
        winner_sid = f"branch-{winner_id}"
        for branch in report.branches:
            # only branches that were actually forked have a state to roll back
            if branch.hypothesis_id != winner_id and branch.hypothesis_id in forked_ids:
                sandbox.rollback(f"branch-{branch.hypothesis_id}", base_snapshot)
                sink.emit("rollback", id=branch.hypothesis_id)
        if any(b.status is BranchStatus.GREEN for b in report.branches if b.hypothesis_id == winner_id):
            candidate = build_patch(base_files, sandbox.read_tree(winner_sid))
            # Defense in depth: pre-flight refusals make this unreachable on a
            # correct path; if a protected path still shows up anyway, withhold
            # the patch rather than export an edit to the referee.
            leaked = guard.diff_violations(candidate)
            if leaked:
                report.notes.append(
                    "winning patch withheld: it touches protected files ("
                    + ", ".join(leaked)
                    + ")"
                )
            else:
                report.winner_diff = candidate

    sink.emit("done", total_tokens=report.total_tokens, cost=round(report.total_cost_usd, 6))
    return report
