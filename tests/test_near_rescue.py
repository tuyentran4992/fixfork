"""Near-miss copy rescue: guarded, measured on the real race-2c reply.

Race 2c (2026-10-02) refused EVERY branch pre-flight because 4 of 30 edit
blocks were near-miss copies of real file text: one long line truncated
(``value.isoformat(),`` for ``value.isoformat().replace("+00:00", "Z"),``)
and one ':' written where the file has ','. The rescue accepts a window only
under ``near_locate``'s guards (score, margin, slip count) and merges the
model's find->replace delta onto the real text so the file's own wording
survives where the model changed nothing.

The blocks below mirror the recorded reply (trimmed to the measured shapes);
the full replay against the untouched reply runs from
``nghien-cuu/race-tomlkit-02-10c/verify-near-rescue.py`` (0-dollar replay).
"""

import py_compile
import tempfile
import unittest
from pathlib import Path

from fixfork.models import Edit
from fixfork.sandbox_runner import (
    LocalSandbox,
    corrected_replace,
    locate_edit,
    merge_near_delta,
    near_locate,
    preflight_edits,
)

# -- measured shape 1: one long line truncated while copying -----------------

DATETIME_FILE = (
    "def item(value):\n"
    "    if isinstance(value, int):\n"
    "        return Integer(value)\n"
    "    elif isinstance(value, datetime):\n"
    "        return DateTime(\n"
    "            value.year,\n"
    "            value.month,\n"
    "            value.day,\n"
    "            value.hour,\n"
    "            value.minute,\n"
    "            value.second,\n"
    "            value.microsecond,\n"
    "            value.tzinfo,\n"
    "            Trivia(),\n"
    '            value.isoformat().replace("+00:00", "Z"),\n'
    "        )\n"
    "    return None\n"
)

FIND_TRUNCATED = (
    "    elif isinstance(value, datetime):\n"
    "        return DateTime(\n"
    "            value.year,\n"
    "            value.month,\n"
    "            value.day,\n"
    "            value.hour,\n"
    "            value.minute,\n"
    "            value.second,\n"
    "            value.microsecond,\n"
    "            value.tzinfo,\n"
    "            Trivia(),\n"
    "            value.isoformat(),\n"
    "        )"
)

REPLACE_TRUNCATED = FIND_TRUNCATED.replace(
    "            value.tzinfo,\n",
    "            value.tzinfo,\n            value.fold,\n",
)

# -- measured shape 2: a ':' written where the file has ',' ------------------

SIGNATURE_FILE = (
    "class DateTime:\n"
    "    def __new__(\n"
    "        cls,\n"
    "        year: int,\n"
    "        month: int,\n"
    "        day: int,\n"
    "        microsecond: int,\n"
    "        tzinfo: object | None,\n"
    "        trivia: object | None = None,\n"
    "    ) -> DateTime:\n"
    "        return datetime.__new__(\n"
    "            cls,\n"
    "            year,\n"
    "            month,\n"
    "            day,\n"
    "            microsecond,\n"
    "            tzinfo=tzinfo,\n"
    "        )\n"
)

FIND_TYPO = (
    "    def __new__(\n"
    "        cls,\n"
    "        year: int,\n"
    "        month: int,\n"
    "        day: int,\n"
    "        microsecond: int,\n"
    "        tzinfo: object | None,\n"
    "        trivia: object | None = None,\n"
    "    ) -> DateTime:\n"
    "        return datetime.__new__(\n"
    "            cls,\n"
    "            year,\n"
    "            month,\n"
    "            day,\n"
    "            microsecond:\n"
    "            tzinfo=tzinfo,\n"
    "        )"
)

REPLACE_TYPO = FIND_TYPO.replace(
    "        microsecond: int,\n",
    "        microsecond: int,\n        fold: int = 0,\n",
)


def edit(file, find, replace):
    return Edit(file=file, find=find, replace=replace)


class NearLocateTest(unittest.TestCase):
    def test_truncated_long_line_is_rescued_and_delta_merged(self):
        files = {"src/items.py": DATETIME_FILE}
        e = edit("src/items.py", FIND_TRUNCATED, REPLACE_TRUNCATED)
        self.assertEqual(preflight_edits(files, [e]), [])
        span = locate_edit(DATETIME_FILE, FIND_TRUNCATED)
        self.assertIsNotNone(span)
        assert span is not None
        start, end, mode = span
        self.assertEqual(mode, "near")
        merged = corrected_replace(
            e.replace, mode, find=e.find, span_text=DATETIME_FILE[start:end]
        )
        assert merged is not None
        # the model's insertion lands ...
        self.assertIn("            value.fold,\n", merged)
        # ... while the file's own long line survives (the model never
        # touched it - measured: a whole-span overwrite would drop it)
        self.assertIn('value.isoformat().replace("+00:00", "Z"),', merged)
        self.assertNotIn("            value.isoformat(),\n", merged)

    def test_colon_typo_line_is_rescued(self):
        files = {"src/items.py": SIGNATURE_FILE}
        e = edit("src/items.py", FIND_TYPO, REPLACE_TYPO)
        self.assertEqual(preflight_edits(files, [e]), [])
        span = locate_edit(SIGNATURE_FILE, FIND_TYPO)
        self.assertIsNotNone(span)
        assert span is not None
        start, end, mode = span
        self.assertEqual(mode, "near")
        merged = corrected_replace(
            e.replace, mode, find=e.find, span_text=SIGNATURE_FILE[start:end]
        )
        assert merged is not None
        self.assertIn("        fold: int = 0,\n", merged)
        # delta did not touch the typo'd call line -> the file's own line stays
        self.assertIn("            microsecond,\n", merged)

    def test_delta_touching_a_slipped_line_takes_the_replacement(self):
        # When the model DID rewrite the drifted line itself, its replacement
        # wins (that is what "apply the delta" means); pin it explicitly.
        f = FIND_TYPO
        r = REPLACE_TYPO.replace(
            "            microsecond:\n", "            microsecond,\n"
        )
        span = locate_edit(SIGNATURE_FILE, f)
        self.assertIsNotNone(span)
        assert span is not None
        start, end, mode = span
        self.assertEqual(mode, "near")
        merged = corrected_replace(r, mode, find=f, span_text=SIGNATURE_FILE[start:end])
        assert merged is not None
        self.assertIn("            microsecond,\n", merged)
        self.assertNotIn("            microsecond:\n", merged)

    def test_ambiguous_near_windows_are_refused(self):
        block = DATETIME_FILE.split("\n")[3:16]
        text = "\n".join(block) + "\n" + "\n".join(block) + "\n"
        self.assertIsNone(near_locate(text, FIND_TRUNCATED))
        problems = preflight_edits(
            {"src/items.py": text},
            [edit("src/items.py", FIND_TRUNCATED, REPLACE_TRUNCATED)],
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("not found", problems[0])

    def test_three_slipped_lines_are_refused(self):
        bad = FIND_TRUNCATED.replace(
            "            value.year,\n", "            value.years,\n"
        ).replace("            value.month,\n", "            value.months,\n"
        ).replace("            value.day,\n", "            value.days,\n")
        self.assertIsNone(near_locate(DATETIME_FILE, bad))

    def test_low_similarity_block_is_refused(self):
        noise = "qqq = 987\nwww = 654\nzzz = 321"
        self.assertIsNone(near_locate(DATETIME_FILE, noise))
        problems = preflight_edits(
            {"src/items.py": DATETIME_FILE}, [edit("src/items.py", noise, "x")]
        )
        self.assertEqual(len(problems), 1)

    def test_single_line_near_miss_is_refused(self):
        text = "value = compute_x(1)\nother = 2\n"
        self.assertIsNone(near_locate(text, "value = compute_y(1)"))
        problems = preflight_edits(
            {"src/a.py": text}, [edit("src/a.py", "value = compute_y(1)", "value = 1")]
        )
        self.assertEqual(len(problems), 1)

    def test_merge_refuses_shape_mismatch(self):
        self.assertIsNone(merge_near_delta("a\nb", "a", "a\nb\nc"))
        self.assertIsNone(corrected_replace("R", "near"))

    def test_exact_path_is_untouched(self):
        span = locate_edit(DATETIME_FILE, "    return None")
        self.assertEqual(span, (DATETIME_FILE.index("    return None"), DATETIME_FILE.index("    return None") + len("    return None"), "exact"))

    def test_sequential_edits_with_rescue(self):
        # edit 1 changes earlier text exactly; edit 2 is the rescue against
        # the updated content - the chain must pre-flight clean.
        files = {"src/items.py": DATETIME_FILE}
        e1 = edit("src/items.py", "    if isinstance(value, int):", "    if isinstance(value, bool):")
        e2 = edit("src/items.py", FIND_TRUNCATED, REPLACE_TRUNCATED)
        self.assertEqual(preflight_edits(files, [e1, e2]), [])


class NearRescueApplyTest(unittest.TestCase):
    def test_end_to_end_apply_via_local_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / "src").mkdir(parents=True)
            (root / "src" / "items.py").write_text(DATETIME_FILE)
            box = LocalSandbox()
            sid = box.create(root, "near-t1")
            changed = box.apply_edits(
                sid, [edit("src/items.py", FIND_TRUNCATED, REPLACE_TRUNCATED)]
            )
            self.assertEqual(changed, len(FIND_TRUNCATED.split("\n")))
            content = box.read_tree(sid)["src/items.py"]
            self.assertIn("            value.fold,\n", content)
            self.assertIn('value.isoformat().replace("+00:00", "Z"),', content)
            compiled = Path(tmp) / "merged_items.py"
            compiled.write_text(content)
            py_compile.compile(str(compiled), doraise=True)  # must stay parseable


if __name__ == "__main__":
    unittest.main()
