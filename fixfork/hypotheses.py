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

Each hypothesis must be a COMPLETE fix for its own explanation - if the repair
requires coordinated changes (a caller must pass the value AND the class or
function must accept it AND related methods must stay consistent), all of those
edits belong in the SAME hypothesis, and only the edits that its own
explanation needs. Do not split one explanation's edits across hypotheses:
every branch runs the whole test suite, so a hypothesis that covers only part
of its own fix is red anyway.

Ground every call you write in evidence from the files above. The failing
test(s) are the referee: their mocks, fakes and assertions show the real call
shapes and types (which accessor returns a dict, which returns a model
object), and a rendered type-context file is the ground truth for that class.
Never call `.get(...)` - or any dict-only access - on an object whose type you
cannot show is a dict; a wrong-type call fails at runtime and verifies
nothing. A verification must READ state the system produced (re-fetch and
compare) - an edit whose check passes only because it wrote the expected value
into the returned object is fabricated, not verified. A verification that
cannot obtain an inspectable result is INCONCLUSIVE: it is not evidence that
the update was dropped, and must not send the code down a fallback path.
When the re-fetch fails, raises, or returns a value whose shape you cannot
safely check, keep the original behavior. Only a read that succeeds and
positively shows the update was dropped may branch to a fallback
("could not verify" is not "dropped"). When the same defect
pattern repeats at call sites or return paths the referee exercises, one
hypothesis must cover every exercised site.

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
{files}{type_context_note}

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


# --- Type context: definitions of names imported by the failure-referenced files
# Measured live (2026-10-05, mcp-atlassian #1578): every branch's "verify the
# update persisted" check called dict access (``.get(...)``) on an object that
# is a pydantic model, not a dict - the class that proves the type never
# reached the prompt, so the model guessed it (5/5 branches red). This pass
# resolves the local imports of the files the failure log references and adds
# the files that actually DEFINE the imported names to the render set, so the
# diagnosis model can ground object types before writing edits. Bounded and
# deterministic: refs in order, top-level imports in source order, first N
# located definition files win; anything not locatable in the repo is skipped.
MAX_TYPE_CONTEXT_FILES = 6
MAX_TYPE_CONTEXT_FILE_CHARS = 100_000
MAX_TYPE_CONTEXT_NAME_SCAN = 60


def _top_level_definition(source: str, name: str) -> bool:
    """True when ``source`` defines ``name`` at module level (class/def/assign)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return True
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return True
    return False


def _module_base(module: str, files: dict[str, str]) -> str | None:
    """Repo path (without ``.py``) of an absolutely named local module, or None.

    ``mcp_atlassian.models.jira`` must match ``src/mcp_atlassian/models/jira``
    in a src-layout tree: the module path is matched at a path-component
    boundary, and the shortest repo path wins for determinism. Stdlib and
    site-packages modules match nothing and yield None.
    """
    suffix = module.replace(".", "/")
    found: list[str] = []
    for key in files:
        if not key.endswith(".py"):
            continue
        if key == suffix + ".py" or key.endswith("/" + suffix + ".py"):
            found.append(key[: -len(".py")])
            continue
        idx = key.find(suffix + "/")
        if idx != -1 and (idx == 0 or key[idx - 1] == "/"):
            found.append(key[: idx + len(suffix)])
    if not found:
        return None
    return min(found, key=lambda p: (len(p), p))


def _name_location(base: str, name: str, files: dict[str, str]) -> str | None:
    """File under ``base`` (module file or package dir) defining ``name``.

    A module file is accepted only when it defines the name itself; re-export
    chains are NOT followed. A package is scanned (depth, path) order and the
    first defining file wins. Oversized files are skipped rather than blamed.
    """
    mod = base + ".py"
    if mod in files:
        source = files[mod]
        if len(source) <= MAX_TYPE_CONTEXT_FILE_CHARS and _top_level_definition(
            source, name
        ):
            return mod
    prefix = base + "/"
    scanned = 0
    for key in sorted(
        (p for p in files if p.startswith(prefix) and p.endswith(".py")),
        key=lambda p: (p.count("/"), p),
    ):
        if scanned >= MAX_TYPE_CONTEXT_NAME_SCAN:
            break
        scanned += 1
        source = files[key]
        if len(source) > MAX_TYPE_CONTEXT_FILE_CHARS:
            continue
        if _top_level_definition(source, name):
            return key
    return None


def _resolve_imported_name(
    rel: str, level: int, module: str | None, name: str, files: dict[str, str]
) -> str | None:
    """Repo file defining ``name`` imported by ``rel``, or None.

    Relative imports resolve against the importing file's package; absolute
    ones against the repo tree (see ``_module_base``). ``from . import x``
    (module is None) resolves the submodule ``x`` directly.
    """
    if level > 0:
        parts = rel.split("/")[:-1]
        drop = level - 1
        if drop >= len(parts):
            return None
        base_parts = parts[: len(parts) - drop] if drop else list(parts)
        if module is None:
            pkg = "/".join(base_parts)
            for cand in (f"{pkg}/{name}.py", f"{pkg}/{name}/__init__.py"):
                if cand in files:
                    return cand
            return None
        base_parts += module.split(".")
        return _name_location("/".join(base_parts), name, files)
    if not module:
        return None
    base = _module_base(module, files)
    if base is None:
        return None
    return _name_location(base, name, files)


def resolve_type_context(
    files: dict[str, str] | None,
    refs: list[str] | None,
    max_files: int = MAX_TYPE_CONTEXT_FILES,
) -> list[str]:
    """Files defining names imported by the log-referenced files, best first.

    Deterministic and bounded: refs in order, their top-level imports in source
    order, at most ``max_files`` results, never a file already in ``refs``.
    Names that cannot be located in the repo tree are skipped silently
    (stdlib/third-party imports resolve nowhere).
    """
    if not files or not refs:
        return []
    refset = set(refs)
    out: list[str] = []
    seen: set[str] = set()
    for rel in refs:
        if not rel.endswith(".py") or rel not in files:
            continue
        source = files[rel]
        if len(source) > MAX_TYPE_CONTEXT_FILE_CHARS:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                target = _resolve_imported_name(
                    rel, node.level or 0, node.module, alias.name, files
                )
                if target is None or target in refset or target in seen:
                    continue
                seen.add(target)
                out.append(target)
                if len(out) >= max_files:
                    return out
    return out


def type_context_note(paths: list[str], rendered: str) -> str:
    """One-line explanation of the type-context files, or "" when none shown.

    Lists only files that actually made it into the rendered prompt (the
    budget may have dropped some) - the note must not claim evidence the model
    cannot see. The marker is matched as a WHOLE LINE (soi chéo 05/10: an
    inline mention of ``--- path ---`` inside some file's content must not
    count as that file being rendered).
    """
    haystack = "\n" + rendered
    shown = [p for p in paths if f"\n--- {p} ---\n" in haystack]
    if not shown:
        return ""
    text = (
        "(type context above: "
        + ", ".join(shown)
        + " - definitions of names imported by the log-referenced files;"
        " use them to ground real object types and call shapes.)"
    )
    if len(text) > MAX_NOTE_CHARS:
        # Same cap render_files applies to its own "(not shown: ...)" note: a
        # note must never grow the prompt unboundedly (soi chéo 05/10).
        text = text[: MAX_NOTE_CHARS - 4] + " ...)"
    return "\n" + text + "\n"


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
        if excluded:
            # Soi chéo 05/10: when every ref was protected the note claimed
            # "no usable signature" without saying the scan was skipped for
            # another reason - name the exclusion explicitly.
            note += "; test/CI/build files were excluded from the scan"
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
    type_context: list[str] | None = None,
    refs_full_file_chars: int = 0,
    refs_full_total_chars: int = 0,
) -> str:
    """Format a repo tree for the prompt, with size guards and honest notes.

    ``refs`` (paths mentioned by the failure log) are rendered before anything
    else: on a budget that cannot fit the whole tree, the files the log points
    at must reach the model. ``type_context`` (definition files resolved from
    those refs' imports) renders right after the refs and before the rest of
    the code - the model needs the real types to ground its edits.

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
    type_set = set(type_context or ())
    used = 0
    used_refs = 0
    # Log-referenced files first, then their type-context definitions, then
    # code, then everything else: real repositories carry CI configs, lockfiles
    # and docs that sort before src/ and tests/, and those would otherwise eat
    # the whole budget before the model ever sees a source file (observed on a
    # real repo: python-humanize/humanize - the failing module never reached
    # the prompt).
    def _budget_rank(rel: str) -> tuple[int, str]:
        if rel in refset:
            return (0, rel)
        if rel in type_set:
            return (1, rel)
        return (2 if rel.endswith(".py") else 3, rel)

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
    type_context: list[str] | None = None,
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
    rendered = render_files(
        files,
        max_file_chars=max_file_chars,
        max_total_chars=max_total_chars,
        refs=refs,
        type_context=type_context,
        # The fallback budget for log-referenced files is an UPGRADE,
        # never a downgrade: take the max against the caller's caps so
        # an explicitly raised --max-file-chars/--max-total-chars is
        # respected, and couple the total to the per-file value so a
        # referenced file the caller made room for is not dropped by a
        # total cap they did not touch. Found while preparing the tomlkit
        # #619 re-run: in fallback mode a referenced file above the 48k
        # fallback cap was dropped even when the caller raised the cap
        # (the tomlkit items.py itself is not log-referenced - it renders
        # through the normal caps, which the raised flags cover).
        refs_full_file_chars=(
            max(max_file_chars, REFS_FALLBACK_FILE_CHARS) if refs_full else 0
        ),
        refs_full_total_chars=(
            max(
                max_total_chars,
                REFS_FALLBACK_TOTAL_CHARS,
                max(max_file_chars, REFS_FALLBACK_FILE_CHARS),
            )
            if refs_full
            else 0
        ),
    )
    prompt = PROMPT_TEMPLATE.format(
        n=n,
        repo=repo,
        test_command=test_command,
        log=log,
        files=rendered,
        type_context_note=type_context_note(type_context or [], rendered),
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


def parse_edit_object(raw, where: str, on_noop: str = "raise") -> Edit | None:
    """Validate one edit object from a model reply.

    Shared by both reply paths - the diagnosis reply (parse_hypotheses) and the
    in-branch iteration reply (pipeline._parse_loop_edits) - so the strictness
    cannot drift between them. ``find`` is kept byte-exact (no strip): edits are
    applied by exact string match against the repository files.

    ``on_noop`` handles the ``find == replace`` case (an edit that changes
    nothing): "raise" (strict, default - loop replies) rejects the edit;
    "drop" returns ``None`` so the caller can strip it and keep the rest of the
    reply. Chosen for the diagnosis path after a measured race (02/10): ONE
    junk no-op edit - whose ``find`` did not even exist in the target file -
    made the parser throw away all three hypotheses and zero branches raced.
    """
    if on_noop not in ("raise", "drop"):
        raise ValueError(f"on_noop must be 'raise' or 'drop', got {on_noop!r}")
    if not isinstance(raw, dict):
        raise HypothesisError(f"{where} has a non-object edit")
    file = _require_text(raw.get("file"), f"{where} edit 'file'")
    find = _require_text(raw.get("find"), f"{where} edit 'find'")
    replace = raw.get("replace")
    # `replace` may be an empty string: that deletes `find` from the file
    # (the no-op case `find == replace` is handled below).
    if not isinstance(replace, str):
        raise HypothesisError(
            f"{where} edit 'replace' must be a string, got {type(replace).__name__}"
        )
    edit = Edit(file=file.strip(), find=find, replace=replace)
    if edit.find == edit.replace:
        if on_noop == "drop":
            return None
        raise HypothesisError(f"{where} has a no-op edit")
    return edit


def _is_bare_edit_list(items: list) -> bool:
    """True when every element is a bare edit object - no hypothesis wrapper.

    A bare edit carries exactly the edit keys (``file``/``find``/``replace``)
    and NO hypothesis-envelope keys (``edits``/``title``/``rationale``) - so
    metadata is never silently stripped. Measured on a real diagnosis reply
    (02/10/2026): the model returned its edits directly, as a flat array, and
    the strict count check threw the entire fix away.
    """
    if not items:
        return False
    for item in items:
        if not isinstance(item, dict):
            return False
        if any(key in item for key in ("edits", "title", "rationale")):
            return False
        if not all(key in item for key in ("file", "find", "replace")):
            return False
    return True


def _parse_bare_edits(items: list, notes: list[str] | None) -> list[Hypothesis]:
    """Rescue a flat array of bare edits as ONE hypothesis (measured 02/10/2026).

    The no-op rules match the wrapper path: ``find == replace`` edits drop with
    a note, and an all-no-op reply still rejects.
    """
    edits: list[Edit] = []
    dropped_noop = 0
    for index, raw in enumerate(items, start=1):
        edit = parse_edit_object(raw, f"bare edit #{index}", on_noop="drop")
        if edit is None:
            dropped_noop += 1
        else:
            edits.append(edit)
    if notes is not None:
        # appended BEFORE the all-noop raise below: the note is the only record
        # of WHY the reply was rejected (same rule as the wrapper path)
        detail = f"{len(edits)} real edit(s) kept"
        if dropped_noop:
            detail += f", {dropped_noop} no-op edit(s) dropped"
        notes.append(
            f"reply carried a flat edit list (no hypothesis wrapper): "
            f"rescued as ONE hypothesis ({detail})"
        )
    if not edits:
        raise HypothesisError(
            "reply had no real edits: every edit was a no-op (find == replace)"
        )
    return [
        Hypothesis(
            id=1,
            title="(recovered) flat edit list",
            rationale=(
                "The reply carried its edits directly, without the hypothesis "
                "wrapper; parsed as one complete fix."
            ),
            edits=edits,
        )
    ]


def _extract_balanced_objects(text: str) -> list[str]:
    """Top-level ``{...}`` chunks, scanning OUTSIDE string literals.

    Used only by the salvage pass below, for replies that already failed both
    the strict parse and the bounded repair pass. Escape-aware, so a ``{``
    inside a string value cannot open a chunk and an unbalanced brace in
    prose cannot swallow one. Bounded by the reply itself (the completion
    budget caps its size), so no separate cap is needed.

    The scanner tracks double-quoted strings (JSON style), which is what this
    pass sees: a reply written as a Python literal is handled earlier by
    ``_parse_python_literal`` and never reaches salvage; a reply that still
    mis-chunks here can only lose candidates (every chunk must parse and
    validate on its own), never produce a wrong one.
    """
    chunks: list[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if esc:
            esc = False
            continue
        if in_str:
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    chunks.append(text[start : i + 1])
                    start = -1
    return chunks


def salvage_top_level_objects(raw: str) -> tuple[list[dict], int, int]:
    """Keep the intact top-level objects from a reply that fails to parse.

    Returns ``(items, n_chunks, n_repaired)``. ``items`` are dicts already
    validated so the normal downstream checks can take them without raising:
    hypothesis objects (non-empty ``title``/``rationale`` strings, at least
    one structurally sound edit) come first; when none survives but bare edit
    objects do, those are returned so the flat-edit rescue can still run.
    Invalid objects are dropped - this pass never fabricates and never loosens
    the edit rules; it only keeps what already validates (each object gets the
    same per-object bounded repair pass as the whole reply would).

    Live evidence (04/10/2026, docket#498 live run): the reply's
    LAST object closed with ``]`` plus trailing junk, so the whole array
    failed strict parsing AND the bounded repair (one step moved the failure
    forward but left trailing ``}``/``]``) while objects 1 and 2 parsed
    strictly - the run raced zero branches and $0.0205 was thrown away.
    Recovery here: 2 of 3.
    """
    chunks = _extract_balanced_objects(raw)
    hypothesis_items: list[dict] = []
    edit_items: list[dict] = []
    n_repaired = 0
    for chunk in chunks:
        value, fixes = loads_json_tolerant(chunk)
        if value is PARSE_FAILURE or not isinstance(value, dict):
            continue
        title = value.get("title")
        rationale = value.get("rationale")
        raw_edits = value.get("edits")
        if (
            isinstance(title, str)
            and title.strip()
            and isinstance(rationale, str)
            and rationale.strip()
            and isinstance(raw_edits, list)
            and raw_edits
        ):
            edits = []
            for raw_edit in raw_edits:
                try:
                    parse_edit_object(raw_edit, "salvaged edit", on_noop="drop")
                except HypothesisError:
                    continue  # malformed edit: drop it, keep the rest
                edits.append(raw_edit)
            if edits:
                hypothesis_items.append(
                    {"title": title, "rationale": rationale, "edits": edits}
                )
                if fixes:
                    # Counted only for KEPT objects (soi chéo 04/10, two
                    # rounds): the note's "needed light repair" must not
                    # count chunks that were later dropped by validation.
                    n_repaired += 1
            continue
        if all(key in value for key in ("file", "find", "replace")):
            try:
                parse_edit_object(value, "salvaged edit", on_noop="drop")
            except HypothesisError:
                continue
            edit_items.append(value)
            if fixes:
                n_repaired += 1
    items = hypothesis_items or edit_items
    return items, len(chunks), n_repaired


def parse_hypotheses(
    text: str,
    n: int = 3,
    notes: list[str] | None = None,
    on_duplicates: str = "raise",
) -> list[Hypothesis]:
    """Parse the diagnosis reply into hypotheses (``n`` = requested count).

    The reply may carry FEWER complete hypotheses than requested: that is
    accepted and raced, not rejected - measured (02/10/2026, race tomlkit
    2d): a reply carrying ONE complete hypothesis of the three requested
    (title + rationale + six real edits, none a no-op) was thrown away by
    the exact-count check and zero branches raced. The count shortfall gets
    a note. A reply carrying MORE is not thrown away either: duplicates are
    handled first (see ``on_duplicates``), then the remainder is capped at
    ``n`` branches (first in reply order) with a note.

    The RESULT may still hold fewer than the reply carried: a hypothesis
    left with no real edit (every edit a no-op, ``find == replace``) is
    dropped, with a note; exact duplicates collapse in "collapse" mode. Only
    when nothing usable remains is the reply rejected (``HypothesisError``) -
    together with reply-level malformed content: wrong shapes, missing
    title/rationale, an empty array.

    Exception: a reply shaped as a flat array of bare edits (``file``/``find``/
    ``replace`` objects, no hypothesis envelope) is RESCUED as a single
    hypothesis holding all of its edits - measured with a real model reply
    (02/10/2026): the whole fix was thrown away over the missing wrapper. Such
    an array is rescued only when every element is edit-shaped; any other
    shape falls through to the strict per-hypothesis checks above.

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
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        # soi chéo 03/10: with n=0 a non-empty reply would slice to [] and
        # return silently (and the flat rescue runs before any cap); n<0
        # sliced from the wrong end. Reject the parameter, not the reply.
        raise ValueError(f"n must be an int >= 1, got {n!r}")
    raw = extract_json_array(text)
    items, repairs = loads_json_tolerant(raw)
    if items is PARSE_FAILURE:
        # Salvage pass (JEV p1, conf 0.92; measured on a real reply - the
        # docket#498 run, 04/10/2026): one broken object can take down the
        # whole-array parse while the other objects are intact. Keep the
        # intact ones instead of throwing a usable fix away; the raw reply
        # stays verbatim in the report and every kept object still passes the
        # normal checks below.
        salvaged, n_chunks, n_repaired = salvage_top_level_objects(raw)
        if salvaged:
            if notes is not None:
                detail = (
                    f"salvaged {len(salvaged)} of {n_chunks} top-level "
                    "object(s) from the raw text"
                )
                if n_repaired:
                    detail += f" ({n_repaired} needed light repair)"
                notes.append(f"reply failed to parse as a whole; {detail}")
            items = salvaged
            # The failed whole-reply pass must not produce the "lightly
            # repaired" note below as if parsing had succeeded.
            repairs = 0
        else:
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
    if not isinstance(items, list):
        raise HypothesisError(f"expected {n} hypotheses, got {type(items).__name__}")
    if _is_bare_edit_list(items):
        # Shape rescue (measured 02/10/2026, JEV p1, conf 1.00): the reply
        # carried its edits directly - a flat array of {"file", "find",
        # "replace"} objects with no hypothesis wrapper. One array = ONE
        # hypothesis (a single complete fix); the fix still races instead of
        # being thrown away over the missing envelope.
        return _parse_bare_edits(items, notes)
    if not items:
        raise HypothesisError(f"expected {n} hypotheses, got an empty list")

    hypotheses: list[Hypothesis] = []
    dropped_noop = 0
    dropped_hypotheses = 0
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
        edits: list[Edit] = []
        for raw in raw_edits:
            edit = parse_edit_object(raw, f"hypothesis #{index}", on_noop="drop")
            if edit is None:
                dropped_noop += 1
            else:
                edits.append(edit)
        if not edits:
            # every edit was a no-op: this hypothesis carries no real change to
            # race - drop it, keep the rest of the reply (measured: rejecting
            # the whole reply over one junk edit lost two healthy hypotheses)
            dropped_hypotheses += 1
            continue
        hypotheses.append(Hypothesis(id=index, title=title, rationale=rationale, edits=edits))

    if (dropped_noop or dropped_hypotheses) and notes is not None:
        # appended BEFORE the all-dropped raise below: this note is the only
        # record of WHY the reply was rejected (callers keep their notes list)
        notes.append(
            f"no-op edits dropped: {dropped_noop} unchanged edit(s) removed, "
            f"{dropped_hypotheses} hypothesis(es) dropped (find == replace)"
        )
    if not hypotheses:
        raise HypothesisError(
            "reply had no real edits: every edit was a no-op (find == replace)"
        )

    if len(items) < n and notes is not None:
        # measured (02/10/2026, race tomlkit 2d): a complete 1-of-3 reply was
        # rejected by the exact-count check and zero branches raced - a count
        # shortfall is not a reason to discard a real fix
        notes.append(
            f"reply carried {len(items)} of {n} requested hypotheses: kept the "
            "complete one(s) instead of discarding over the count (later drops "
            "and the branch cap still apply)"
        )

    if on_duplicates == "collapse":
        hypotheses = collapse_duplicates(hypotheses, notes=notes)
    else:
        assert_divergent(hypotheses)

    if len(hypotheses) > n:
        # count-above is not a discard reason either: race at most n branches
        # (first in reply order, after duplicate handling)
        over = len(hypotheses) - n
        hypotheses = hypotheses[:n]
        if notes is not None:
            notes.append(
                f"reply carried more unique hypotheses than the {n} requested: "
                f"kept the first {n}, dropped {over} over the branch cap"
            )
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
    type_context: list[str] | None = None,
) -> tuple[list[Hypothesis], ModelReply]:
    if refs is None:
        # Same priority rule as the pipeline: files the log points at first.
        refs = extract_refs(log, files)
    reply: ModelReply = router.complete(
        "reason",
        build_prompt(
            repo,
            test_command,
            log,
            n=n,
            files=files,
            refs=refs,
            type_context=type_context,
        ),
    )
    return parse_hypotheses(reply.text, n=n), reply
