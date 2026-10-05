"""Machine-scanned defect occurrences (JEV-p4 plan).

Live evidence (2026-10-01, greynoise #7778 on OpenCTI connectors): the
32,214-char failing file was over the per-file prompt cap and was dropped
whole; the model patched only the site quoted in the last traceback, and every
branch stayed red (needed 7 sites, 3 test groups). The scan must put every site
of the defect in front of the model - or, when no usable signature exists,
fall back to rendering the log-referenced files under a larger budget.
"""

import unittest

from fixfork.hypotheses import (
    REFS_FALLBACK_FILE_CHARS,
    build_prompt,
    defect_scan,
    extract_defect_signature,
    scan_occurrences,
)

KEYERROR_LOG = """\
tests/test_x.py::test_a FAILED
tests/test_x.py::test_b FAILED
=========================== short test summary info ============================
FAILED tests/test_x.py::test_a
FAILED tests/test_x.py::test_b
E       KeyError: 'classification'
"""

QUOTED_LINE_LOG = """\
    def use(self, data):
        if (
>           and (data["scanner"]["classification"] != "benign")
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
        ):
ValueError: boom
"""


def connector_fixture() -> str:
    """A small source file with three defect sites (two quote styles)."""
    return (
        "def labels(data):\n"
        '    if data["scanner"]["classification"] == "benign":\n'
        "        pass\n"
        "def actor(data):\n"
        "    if (data['scanner']['classification'] != 'benign'):\n"
        "        pass\n"
        "def bundle(data):\n"
        '    return data["scanner"]["classification"]\n'
    )


class SignatureTest(unittest.TestCase):
    def test_keyerror_is_first_choice(self):
        self.assertEqual(
            extract_defect_signature(KEYERROR_LOG),
            ['["classification"]', "['classification']"],
        )

    def test_quoted_failing_line_uses_last_literal(self):
        self.assertEqual(
            extract_defect_signature(QUOTED_LINE_LOG),
            ['["classification"]', "['classification']"],
        )

    def test_no_signature_returns_none(self):
        self.assertIsNone(
            extract_defect_signature("FAILED tests/test_x.py::test_a\n")
        )


class ScanTest(unittest.TestCase):
    def test_scan_finds_every_occurrence_in_refs(self):
        lines, total, truncated = scan_occurrences(
            {"connector.py": connector_fixture()},
            ["connector.py"],
            ['["classification"]', "['classification']"],
        )
        self.assertEqual(total, 3)
        self.assertFalse(truncated)
        self.assertEqual(
            [line.split(":")[1] for line in lines], ["2", "5", "8"]
        )

    def test_non_ref_and_missing_files_are_not_scanned(self):
        lines, total, _ = scan_occurrences(
            {
                "connector.py": connector_fixture(),
                "other.py": 'x = data["classification"]\n',
            },
            ["missing.py"],
            ['["classification"]'],
        )
        self.assertEqual((lines, total), ([], 0))

    def test_scan_truncation_counts_honestly(self):
        lines, total, truncated = scan_occurrences(
            {"connector.py": connector_fixture()},
            ["connector.py"],
            ['["classification"]', "['classification']"],
            max_lines=2,
        )
        self.assertEqual(total, 3)
        self.assertEqual(len(lines), 2)
        self.assertTrue(truncated)


class DefectScanTest(unittest.TestCase):
    def test_scan_block_and_no_fallback(self):
        scan = defect_scan(
            KEYERROR_LOG, {"connector.py": connector_fixture()}, ["connector.py"]
        )
        self.assertFalse(scan.fallback)
        self.assertIn("Machine-scanned occurrences", scan.block)
        self.assertIn("connector.py:2:", scan.block)
        self.assertIn("EVERY hypothesis", scan.block)
        self.assertTrue(any("machine scan" in note for note in scan.notes))

    def test_fallback_without_signature(self):
        scan = defect_scan(
            "FAILED tests/test_x.py::test_a\n",
            {"connector.py": connector_fixture()},
            ["connector.py"],
        )
        self.assertTrue(scan.fallback)
        self.assertEqual(scan.block, "")
        self.assertTrue(any("fallback" in note for note in scan.notes))

    def test_protected_refs_are_excluded_from_the_scan(self):
        files = {
            "tests/test_x.py": 'x = data["classification"]\n',
            "connector.py": 'y = data["classification"]\n',
        }
        scan = defect_scan(KEYERROR_LOG, files, ["tests/test_x.py", "connector.py"])
        self.assertIn("connector.py:1:", scan.block)
        self.assertNotIn("tests/test_x.py:1:", scan.block)
        self.assertTrue(any("excluded" in note for note in scan.notes))

    def test_all_protected_refs_fallback_note_names_the_exclusion(self):
        # Soi chéo 05/10: with ONLY protected refs the scan is skipped for the
        # exclusion reason - the fallback note must say so instead of implying
        # there was no usable signature.
        files = {"tests/test_x.py": 'x = data["classification"]\n'}
        scan = defect_scan(KEYERROR_LOG, files, ["tests/test_x.py"])
        self.assertTrue(scan.fallback)
        self.assertEqual(scan.block, "")
        self.assertTrue(
            any("excluded from the scan" in note for note in scan.notes)
        )

    def test_no_refs_notes_nothing_to_scan(self):
        scan = defect_scan(KEYERROR_LOG, {"connector.py": connector_fixture()}, [])
        self.assertFalse(scan.fallback)
        self.assertEqual(scan.block, "")
        self.assertTrue(any("no log-referenced files" in note for note in scan.notes))


class BuildPromptScanTest(unittest.TestCase):
    def _big_connector(self) -> str:
        # ~20k chars: over MAX_FILE_CHARS (8000), like the real greynoise file;
        # seven defect sites across the file.
        lines = [f"# padding line {i}" for i in range(1, 700)]
        for pos in (10, 20, 30, 40, 50, 60, 70):
            lines[pos] = f'    value = data["scanner"]["classification"]  # site {pos}'
        return "\n".join(lines) + "\n"

    def test_scan_block_reaches_prompt_for_big_file(self):
        prompt = build_prompt(
            "repo",
            "pytest",
            KEYERROR_LOG,
            n=3,
            files={"connector/connector.py": self._big_connector()},
            refs=["connector/connector.py"],
        )
        self.assertIn("Machine-scanned occurrences", prompt)
        self.assertEqual(prompt.count("- connector/connector.py:"), 7)
        # the file itself stays over the normal cap - the scan list carries it
        self.assertNotIn("--- connector/connector.py ---", prompt)
        self.assertIn("not shown", prompt)
        # the block lands before the failure log, which the model reads last
        self.assertLess(
            prompt.index("Machine-scanned occurrences"),
            prompt.index("Failure log (tail):"),
        )

    def test_fallback_shows_big_ref_file_when_no_signature(self):
        prompt = build_prompt(
            "repo",
            "pytest",
            "FAILED tests/test_x.py::test_a\n",
            n=3,
            files={"connector/connector.py": self._big_connector()},
            refs=["connector/connector.py"],
        )
        self.assertIn("--- connector/connector.py ---", prompt)
        self.assertIn('data["scanner"]["classification"]', prompt)
        self.assertNotIn("too large", prompt)

    def test_fallback_note_when_over_even_the_fallback_budget(self):
        huge = "x" * (REFS_FALLBACK_FILE_CHARS + 1) + '\ndata["classification"]\n'
        prompt = build_prompt(
            "repo",
            "pytest",
            "FAILED tests/test_x.py::test_a\n",
            n=3,
            files={"huge.py": huge},
            refs=["huge.py"],
        )
        self.assertNotIn("--- huge.py ---", prompt)
        self.assertIn("fallback per-file budget", prompt)


if __name__ == "__main__":
    unittest.main()
