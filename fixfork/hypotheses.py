"""Turn model output into validated, divergent hypotheses."""

from __future__ import annotations

import ast
import json
import re
from typing import NamedTuple

from . import guard
from .model_router import ModelReply, ModelRouter
from .models import Edit, Hypothesis


class HypothesisError(RuntimeError):
    """Raised when a model reply cannot be turned into ``n`` valid hypotheses."""


PROMPT_TEMPLATE = """You are FixFork's diagnosis model.

A repository has a failing test. Read the repository files and the failure log
below and propose {n} DIVERGENT root-cause hypotheses: each must be a different
kind of explanation (not three variations of the same guess) and must come with
concrete edits.

Reply with ONLY a JSON array, no prose, no markdown fences. Each element:
{{"title": "...", "rationale": "...", "edits": [{{"file": "path/relative/to/repo", "find": "exact existing text", "replace": "replacement text"}}]}}

The `find` text MUST be copied EXACTLY (character for character, whitespace
and blank lines included) from the repository files below - the edit is applied by an exact
string match, so any paraphrase will fail.

Edits to test files, CI workflows and build/config files are OFF-LIMITS:
FixFork refuses any branch that touches them - the test suite is the referee.
Fix the source code only.

Repository: {repo}
Failing test command: {test_command}

Repository files:
{files}

{occurrences_block}Failure log (tail):
{log}
"""

# Prompt-size guards: whole small repos fit, big ones get an explicit note.
MAX_FILE_CHARS = 8000
MAX_TOTAL_FILES_CHARS = 24000
# The "not shown: ..." note lists at most this many entries; the rest is
# summarized as "+N more" so the note cannot outgrow the budget it reports on.
MAX_SKIPPED_IN_NOTE = 8
MAX_NOTE_CHARS = 600


def extract_refs(log: str, files: dict[str, str] | None) -> list[str]:
    """Repo files referenced by a failure log, in order of first mention.

    Matches both traceback style (``File "/tmp/.../tests/test_items.py", line
    811``) and the short ``pkg/mod.py:123:`` style pytest prints. The log may
    carry sandbox-absolute paths, so a key counts as referenced when the token
    either equals it or ends with it at a path-component boundary; tokens that
    match no key (stdlib, site-packages) are ignored - they are not part of the
    rendered tree.
    """
    if not log or not files:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in _REF_RE.finditer(log):
        token = (match.group(1) or match.group(2) or "").replace("\\", "/")
        if token.startswith("./"):
            token = token[2:]
        # Longest match wins: for a token ``/tmp/x/src/utils.py`` with keys
        # ``utils.py`` and ``src/utils.py`` in the tree, the longer key is the
        # file the log actually means - a shorter endswith hit would be wrong.
        candidates = [
            key for key in files if token == key or token.endswith("/" + key)
        ]
        if not candidates:
            continue
        key = max(candidates, key=len)
        if key not in seen:
            seen.add(key)
            found.append(key)
    return found


# Traceback ``File "..."`` frames and short ``path.py:NN:`` references.
_REF_RE = re.compile(r'File "([^"]+)"|([A-Za-z0-9_][\w\-./]*\.py):\d+')


# --- Machine-scanned defect occurrences (JEV-p4 plan) -----------------------
# Live evidence (2026-10-01, greynoise #7778 on OpenCTI connectors): the failing
# file was 32,214 chars, over the per-file prompt cap, so the diagnosis model
# never saw it; it patched the single site quoted in the last traceback and
# every branch stayed red (2/11 tests still failing, 7 sites needed). JEV
# arbitration picked the combined plan: machine-scan the log-referenced files
# for EVERY occurrence of the failing access and put that list in the prompt;
# when no usable signature can be extracted, fall back to rendering the
# referenced files under a larger budget instead of silently dropping them.
MAX_OCCURRENCE_LINES = 80
MAX_OCCURRENCE_LINE_CHARS = 200
# Fallback per-file and total budget for log-referenced files (refs_full mode).
REFS_FALLBACK_FILE_CHARS = 48000
REFS_FALLBACK_TOTAL_CHARS = 64000

_KEYERROR_RE = re.compile(r"""KeyError:\s*['"]([^'"]+)['"]""")
_PYTEST_QUOTED_LINE_RE = re.compile(r"^>.*$", re.M)
_SUBSCRIPT_LITERAL_RE = re.compile(r"""\[['"]([^'"]+)['"]\]""")


def extract_defect_signature(log: str) -> list[str] | None:
    """Search substrings that identify the failing access, read from the log.

    First choice: the missing key named by ``KeyError: 'x'``. Fallback: the
    subscript literals inside pytest's quoted failing lines (``> ...["x"]``) -
    the LAST literal of each quoted line is the one that raised. Returns exact
    search substrings in both quote styles, or ``None`` when nothing usable.
    """
    keys: list[str] = []
    match = _KEYERROR_RE.search(log)
    if match:
        keys.append(match.group(1))
    else:
        for line_match in _PYTEST_QUOTED_LINE_RE.finditer(log):
            literals = _SUBSCRIPT_LITERAL_RE.findall(line_match.group(0))
            if literals:
                keys.append(literals[-1])
    ordered: list[str] = []
    for key in keys:
        if key not in ordered:
            ordered.append(key)
    if not ordered:
        return None
    patterns: list[str] = []
    for key in ordered:
        patterns.append(f'["{key}"]')
        patterns.append(f"['{key}']")
    return patterns


def scan_occurrences(
    files: dict[str, str] | None,
    refs: list[str] | None,
    patterns: list[str],
    max_lines: int = MAX_OCCURRENCE_LINES,
) -> tuple[list[str], int, bool]:
    """Find every occurrence of any pattern in the log-referenced files.

    Returns ``(lines, total, truncated)``: ``lines`` are ``path:NN: <line>``
    strings (capped at ``max_lines``), ``total`` is the true number of matching
    lines and ``truncated`` says whether some were left out of ``lines``. Only
    ``refs`` are scanned - files the failure log points at are the trusted set.
    """
    if not files or not refs or not patterns:
        return [], 0, False
    lines: list[str] = []
    total = 0
    for rel in refs:
        content = files.get(rel)
        if content is None:
            continue
        for lineno, line in enumerate(content.split("\n"), start=1):
            if not any(pattern in line for pattern in patterns):
                continue
            total += 1
            if len(lines) >= max_lines:
                continue
            text = line.rstrip()
            if len(text) > MAX_OCCURRENCE_LINE_CHARS:
                text = text[:MAX_OCCURRENCE_LINE_CHARS] + " ..."
            lines.append(f"{rel}:{lineno}: {text}")
    return lines, total, total > len(lines)


def render_occurrences_block(
    patterns: list[str], lines: list[str], total: int
) -> str:
    """Prompt section listing every scanned occurrence, plus the coverage rule."""
    shown = ", ".join(f"`{p}`" for p in patterns)
    out = [f"Machine-scanned occurrences of the failing access ({shown}):"]
    out.extend(f"- {line}" for line in lines)
    if total > len(lines):
        out.append(f"- ... (+{total - len(lines)} more occurrences not listed)")
    out.append(
        "EVERY hypothesis must include edits covering EVERY occurrence listed "
        "above - the suite runs all failing tests, so a fix that covers only "
        "some occurrences is red; do not split one defect's sites across "
        "hypotheses."
    )
    return "\n".join(out) + "\n\n"


class DefectScan(NamedTuple):
    """Result of the pre-prompt defect scan (JEV-p4 plan)."""

    block: str
    fallback: bool
    notes: list[str]


def defect_scan(
    log: str, files: dict[str, str] | None, refs: list[str] | None
) -> DefectScan:
    """Scan for defect occurrences and decide the prompt strategy.

    Either the machine-scanned occurrence list goes into the prompt, or - when
    no usable signature exists - the log-referenced files fall back to the
    larger rendering budget (``refs_full``). Test/CI/build files stay out of
    the scan: edits to them are refused by the guard anyway, so listing their
    lines as fix targets would only mislead the model.
    """
    patterns = extract_defect_signature(log)
    scan_refs = [
        rel for rel in (refs or []) if guard.protected_reason(rel) is None
    ]
    excluded = len(scan_refs) != len(refs or [])
    lines: list[str] = []
    total = 0
    if patterns and files and scan_refs:
        lines, total, _truncated = scan_occurrences(files, scan_refs, patterns)
    if lines and patterns:
        note = (
            f"machine scan: {total} occurrence(s) of the failing access "
            f"({', '.join(patterns)}) found in log-referenced files and sent "
            "to the diagnosis prompt"
        )
        if total > len(lines):
            note += f" (only the first {len(lines)} are listed in the prompt)"
        if excluded:
            note += "; test/CI/build files were excluded from the scan"
        return DefectScan(
            block=render_occurrences_block(patterns, lines, total),
            fallback=False,
            notes=[note],
        )
    if refs:
        note = (
            "machine scan unavailable (no usable signature in the log, or no "
            "occurrence found in log-referenced files); those files get the "
            "larger fallback budget in the prompt"
        )
        return DefectScan(block="", fallback=True, notes=[note])
    return DefectScan(
        block="",
        fallback=False,
        notes=["machine scan: no log-referenced files to scan"],
    )


def render_files(
    files: dict[str, str] | None,
    max_file_chars: int = MAX_FILE_CHARS,
    max_total_chars: int = MAX_TOTAL_FILES_CHARS,
    refs: list[str] | None = None,
    refs_full_file_chars: int = 0,
    refs_full_total_chars: int = 0,
) -> str:
    """Format a repo tree for the prompt, with size guards and honest notes.

    ``refs`` (paths mentioned by the failure log) are rendered before anything
    else: on a budget that cannot fit the whole tree, the files the log points
    at must reach the model. Remaining files keep the code-before-docs order.

    ``refs_full_file_chars``/``refs_full_total_chars`` (fallback mode, set when
    the machine defect scan found no usable occurrence list): log-referenced
    files are rendered under this larger, separate budget instead of being
    dropped by the normal caps - the model must still see the file it has to
    fix. Files that exceed even this budget are named in the note, never
    silently dropped.
    """
    if not files:
        return "(no repository files provided)"
    blocks: list[str] = []
    skipped: list[str] = []
    refset = set(refs or ())
    used = 0
    used_refs = 0
    # Log-referenced files first, then code, then everything else: real
    # repositories carry CI configs, lockfiles and docs that sort before src/
    # and tests/, and those would otherwise eat the whole budget before the
    # model ever sees a source file (observed on a real repo:
    # python-humanize/humanize - the failing module never reached the prompt).
    def _budget_rank(rel: str) -> tuple[int, str]:
        if rel in refset:
            return (0, rel)
        return (1 if rel.endswith(".py") else 2, rel)

    for rel in sorted(files, key=_budget_rank):
        content = files[rel]
        if rel in refset and refs_full_file_chars > 0:
            # Fallback mode: log-referenced files get their own larger budget -
            # they are the files the model must actually fix.
            if len(content) > refs_full_file_chars:
                skipped.append(
                    f"{rel} (referenced by the log; {len(content)} chars over "
                    "the fallback per-file budget)"
                )
                continue
            block = f"--- {rel} ---\n{content}\n"
            cap_total = refs_full_total_chars or max_total_chars
            if used_refs + len(block) > cap_total:
                skipped.append(
                    f"{rel} (referenced by the log; over the fallback total budget)"
                )
                continue
            blocks.append(block)
            used_refs += len(block)
            continue
        if len(content) > max_file_chars:
            skipped.append(f"{rel} (too large: {len(content)} chars)")
            continue
        # No rstrip: the prompt tells the model to copy `find` text exactly,
        # so the shown content must match the file byte for byte.
        block = f"--- {rel} ---\n{content}\n"
        if used + len(block) > max_total_chars:
            skipped.append(f"{rel} (over total prompt budget)")
            continue
        blocks.append(block)
        used += len(block)
    if skipped:
        # The note is capped: a repo with many skipped files must not grow the
        # prompt past the budget the note itself reports on.
        head = "; ".join(skipped[:MAX_SKIPPED_IN_NOTE])
        if len(skipped) > MAX_SKIPPED_IN_NOTE:
            head += f"; +{len(skipped) - MAX_SKIPPED_IN_NOTE} more"
        note = f"(not shown: {head})"
        if len(note) > MAX_NOTE_CHARS:
            note = note[: MAX_NOTE_CHARS - 4] + " ...)"
        blocks.append(note)
    return "\n".join(blocks)


def build_prompt(
    repo: str,
    test_command: str,
    log: str,
    n: int = 3,
    files: dict[str, str] | None = None,
    research_block: str = "",
    refs: list[str] | None = None,
    max_file_chars: int = MAX_FILE_CHARS,
    max_total_chars: int = MAX_TOTAL_FILES_CHARS,
    occurrences_block: str | None = None,
    refs_full: bool = False,
) -> str:
    if occurrences_block is None:
        # Direct callers (tests, generate_hypotheses) get the scan for free;
        # the pipeline computes it separately so it can also record the notes.
        scan = defect_scan(log, files, refs)
        occurrences_block = scan.block
        refs_full = scan.fallback
    prompt = PROMPT_TEMPLATE.format(
        n=n,
        repo=repo,
        test_command=test_command,
        log=log,
        files=render_files(
            files,
            max_file_chars=max_file_chars,
            max_total_chars=max_total_chars,
            refs=refs,
            refs_full_file_chars=REFS_FALLBACK_FILE_CHARS if refs_full else 0,
            refs_full_total_chars=REFS_FALLBACK_TOTAL_CHARS if refs_full else 0,
        ),
        occurrences_block=occurrences_block,
    )
    if research_block:
        prompt += "\n" + research_block.rstrip() + "\n"
    return prompt


def extract_json_array(text: str) -> str:
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise HypothesisError("no JSON array found in model reply")
    return text[start : end + 1]


# --- Tolerant parsing of near-valid model JSON -----------------------------
# Live evidence (2026-10-01, greynoise run on OpenCTI connectors #7778): the
# diagnosis reply was a 3-hypothesis array with ONE missing "]" mid-stream;
# strict parsing aborted the whole run after ~$0.006. Models occasionally
# emit such near-misses (missing closer, trailing comma, cut-off reply), so
# parsing gets a BOUNDED repair pass: at most MAX_JSON_REPAIR_FIXES small
# edits, applied only outside string literals, accepted only when the result
# actually parses. The raw reply is still kept verbatim in the run report,
# so a repair stays auditable.
MAX_JSON_REPAIR_FIXES = 6


def _append_missing_closers(text: str) -> str:
    """Append closers for every bracket/brace left open, scanning outside
    string literals (escape-aware).

    Refuses to touch text that ends INSIDE a string literal (including a
    dangling escape): closing an unterminated string would fabricate a
    truncated value as if it were complete, and any closer appended while the
    string is still open parses as part of that string - useless AND
    misleading. Such a reply is budget-truncated, not near-valid; the right
    handling is an honest rejection (and a bigger-budget retry upstream).
    """
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            stack.append("]" if ch == "[" else "}")
        elif ch in "]}":
            if stack and stack[-1] == ch:
                stack.pop()
    if in_str:
        return text
    return text + "".join(reversed(stack))


def repair_json_text(
    raw: str, max_fixes: int = MAX_JSON_REPAIR_FIXES
) -> tuple[str | None, int]:
    """Best-effort repair of near-valid JSON from a model reply.

    Returns ``(repaired_text, n_fixes)``, or ``(None, n)`` when the text cannot
    be repaired within the bound. Each step re-parses and uses the parser's
    error position: insert a missing closer before the offending token, drop a
    trailing comma, or close off a truncated reply.
    """
    text = raw
    fixes = 0
    while fixes <= max_fixes:
        try:
            json.loads(text)
            return text, fixes
        except json.JSONDecodeError as exc:
            if fixes >= max_fixes:
                return None, fixes
            pos = exc.pos
            ch = text[pos : pos + 1]
            candidates: list[str] = []
            # Missing closer right before the offending token: e.g. an array
            # missing its "]" just before the enclosing object's "}".
            if ch == "}":
                candidates.append(text[:pos] + "]" + text[pos:])
            elif ch == "]":
                candidates.append(text[:pos] + "}" + text[pos:])
            # Trailing comma right before a closer.
            if ch in "]}" and pos >= 1 and text[pos - 1] == ",":
                candidates.append(text[: pos - 1] + text[pos:])
            # Reply cut off: close whatever is still open.
            if pos >= len(text):
                candidates.append(_append_missing_closers(text))
            best = None
            for cand in candidates:
                if cand == text:
                    continue
                try:
                    json.loads(cand)
                    return cand, fixes + 1
                except json.JSONDecodeError as exc2:
                    if exc2.pos > pos:  # the fix moved the failure forward
                        best = cand
                        break
            if best is None:
                tail = _append_missing_closers(text)
                if tail != text:
                    best = tail
            if best is None or best == text:
                return None, fixes
            text, fixes = best, fixes + 1
    return None, fixes


class _ParseFailure:
    """Distinct "could not be parsed, even after the repair pass" marker.

    ``None`` cannot carry this meaning: ``json.loads("null")`` is a VALID
    parse whose value is ``None``. Using ``None`` as the failure sentinel
    conflated a null reply with unparseable output (soi chéo 01/10, aibox).
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<JSON parse failure>"


PARSE_FAILURE = _ParseFailure()


def _parse_python_literal(raw: str):
    """A reply whose structure is sound but written as a Python literal.

    Live evidence (2026-10-01, greynoise run 3, fixfork dc5782a): the model
    wrote every ``find``/``replace`` value with Python-style single quotes
    (``'...'``), so strict JSON parsing aborted a live run whose reply already
    covered all 7 defect sites. ``ast.literal_eval`` evaluates only literals -
    no calls, no attributes, no names beyond True/False/None - and the input is
    bounded by the completion budget; like every other repair path it is
    accepted only when it parses, and the raw reply stays verbatim in the
    report. Returns the value, or the PARSE_FAILURE sentinel.
    """
    text = raw.strip()
    if not text or text[0] not in "[{":
        return PARSE_FAILURE
    try:
        return ast.literal_eval(text)
    except Exception:  # noqa: BLE001 - any parse failure just falls through
        return PARSE_FAILURE


def loads_json_tolerant(raw: str) -> tuple[object, int]:
    """``json.loads`` with the bounded repair passes above.

    Returns ``(value, n_fixes)``; ``(PARSE_FAILURE, n)`` when even the repair
    fails. The failure marker is a distinct sentinel, NOT ``None``, because
    ``null`` is a valid JSON value.
    """
    try:
        return json.loads(raw), 0
    except json.JSONDecodeError:
        pass
    literal = _parse_python_literal(raw)
    if literal is not PARSE_FAILURE:
        return literal, 1
    repaired, fixes = repair_json_text(raw)
    if repaired is None:
        return PARSE_FAILURE, fixes
    try:
        return json.loads(repaired), fixes
    except json.JSONDecodeError:
        return PARSE_FAILURE, fixes


def _require_text(value, where: str) -> str:
    """Strict type guard: a non-string (None, number, dict...) is a broken
    reply, NOT something to coerce with str() - coercing None writes the
    literal word "None" into a source file, which is the same class of bug as
    the earlier AttributeError-escapes-the-except crashes."""
    if not isinstance(value, str) or not value.strip():
        raise HypothesisError(
            f"{where} must be a non-empty string, got {type(value).__name__}"
        )
    return value


def parse_edit_object(raw, where: str) -> Edit:
    """Validate one edit object from a model reply.

    Shared by both reply paths - the diagnosis reply (parse_hypotheses) and the
    in-branch iteration reply (pipeline._parse_loop_edits) - so the strictness
    cannot drift between them. ``find`` is kept byte-exact (no strip): edits are
    applied by exact string match against the repository files.
    """
    if not isinstance(raw, dict):
        raise HypothesisError(f"{where} has a non-object edit")
    file = _require_text(raw.get("file"), f"{where} edit 'file'")
    find = _require_text(raw.get("find"), f"{where} edit 'find'")
    replace = raw.get("replace")
    # `replace` may be an empty string: that deletes `find` from the file
    # (the no-op case `find == replace` is rejected below).
    if not isinstance(replace, str):
        raise HypothesisError(
            f"{where} edit 'replace' must be a string, got {type(replace).__name__}"
        )
    edit = Edit(file=file.strip(), find=find, replace=replace)
    if edit.find == edit.replace:
        raise HypothesisError(f"{where} has a no-op edit")
    return edit


def parse_hypotheses(
    text: str,
    n: int = 3,
    notes: list[str] | None = None,
    on_duplicates: str = "raise",
) -> list[Hypothesis]:
    """Parse the diagnosis reply into ``n`` hypotheses.

    ``on_duplicates`` controls duplicate handling: "raise" (strict, default -
    duplicate titles or edit sets reject the whole reply) or "collapse" (keep
    the first hypothesis of every unique edit set; exact duplicates are
    dropped with a note). The pipeline collapses: a measured reply (greynoise
    case study) carried ONE correct fix written under three different titles,
    and rejecting the whole reply threw a working fix away.
    """
    if on_duplicates not in ("raise", "collapse"):
        raise ValueError(
            f"on_duplicates must be 'raise' or 'collapse', got {on_duplicates!r}"
        )
    raw = extract_json_array(text)
    items, repairs = loads_json_tolerant(raw)
    if items is PARSE_FAILURE:
        try:
            json.loads(raw)
        except json.JSONDecodeError as exc:  # keep the parser's detail for debugging
            raise HypothesisError(f"hypothesis reply is not valid JSON: {exc}") from exc
        # Defensive: PARSE_FAILURE means the strict parse above already failed,
        # so this line is unreachable today (soi chéo 01/10).
        raise HypothesisError("hypothesis reply is not valid JSON")
    if repairs and notes is not None:
        notes.append(
            f"hypothesis reply was lightly repaired ({repairs} repair step(s)) before parsing"
        )
    if not isinstance(items, list) or len(items) != n:
        got = len(items) if isinstance(items, list) else type(items).__name__
        raise HypothesisError(f"expected {n} hypotheses, got {got}")

    hypotheses: list[Hypothesis] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise HypothesisError(f"hypothesis #{index} is not an object")
        title = _require_text(item.get("title"), f"hypothesis #{index} title").strip()
        rationale = _require_text(
            item.get("rationale"), f"hypothesis #{index} rationale"
        ).strip()
        raw_edits = item.get("edits")
        if not isinstance(raw_edits, list) or not raw_edits:
            raise HypothesisError(f"hypothesis #{index} needs at least one edit")
        edits = [parse_edit_object(raw, f"hypothesis #{index}") for raw in raw_edits]
        hypotheses.append(Hypothesis(id=index, title=title, rationale=rationale, edits=edits))

    if on_duplicates == "collapse":
        hypotheses = collapse_duplicates(hypotheses, notes=notes)
    else:
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


def collapse_duplicates(
    hypotheses: list[Hypothesis], notes: list[str] | None = None
) -> list[Hypothesis]:
    """Keep the first hypothesis of every unique edit set; drop exact duplicates.

    A measured diagnosis reply (greynoise case study) returned three
    differently titled hypotheses carrying the SAME edits - it was one fix
    written three times. Rejecting the whole reply lost a fix that
    hand-verification proved correct (11/11 tests), so the pipeline collapses
    instead: the fix still races, the report says how many unique approaches
    actually ran, and no extra model call is made.

    Identity is the edit-set multiset - the same rule the strict check uses,
    so an edit list permuted into a different order counts as a duplicate.
    """
    kept: list[Hypothesis] = []
    seen: set[tuple[tuple[str, str, str], ...]] = set()
    for hypothesis in hypotheses:
        key = _edit_keys(hypothesis)
        if key in seen:
            continue
        seen.add(key)
        kept.append(hypothesis)
    dropped = len(hypotheses) - len(kept)
    if dropped and notes is not None:
        notes.append(
            f"duplicate hypotheses collapsed: {len(hypotheses)} proposed, "
            f"{len(kept)} unique edit set(s) kept, {dropped} duplicate(s) dropped "
            f"(no extra model call)"
        )
    return kept


def generate_hypotheses(
    router: ModelRouter,
    repo: str,
    test_command: str,
    log: str,
    n: int = 3,
    files: dict[str, str] | None = None,
    refs: list[str] | None = None,
) -> tuple[list[Hypothesis], ModelReply]:
    if refs is None:
        # Same priority rule as the pipeline: files the log points at first.
        refs = extract_refs(log, files)
    reply: ModelReply = router.complete(
        "reason", build_prompt(repo, test_command, log, n=n, files=files, refs=refs)
    )
    return parse_hypotheses(reply.text, n=n), reply
