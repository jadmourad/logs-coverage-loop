"""isolate.py rebuilds one finding into a throwaway logs root.

The whole point is that the *relative path is preserved exactly*, because the
monitoring tool decides what it can parse by matching the path. Copy a file to
a flat temp dir and the tool correctly calls it undetected -- you have proved
nothing except that you renamed it.
"""

from __future__ import annotations

import unittest

from helpers import ToolTestCase, isolate, jout, make_logs


class TestPathPreservation(ToolTestCase):

    def setUp(self):
        super().setUp()
        make_logs(self.logs, {
            "app/server-01/app-2026-08-01.log": "one\ntwo\nthree\nfour\nfive\n",
            "deep/a/b/c/d/thing.log": "x\n",
        })

    def test_relative_path_is_rebuilt_exactly(self):
        r = jout(isolate("--logs-root", self.logs, "--path",
                         "app/server-01/app-2026-08-01.log", "--out", self.out))
        self.assertTrue((self.out / "app/server-01/app-2026-08-01.log").is_file())
        self.assertEqual(r["rel_path"], "app/server-01/app-2026-08-01.log")
        self.assertEqual(r["isolated_root"], str(self.out))

    def test_deep_nesting_is_rebuilt(self):
        isolate("--logs-root", self.logs, "--path", "deep/a/b/c/d/thing.log",
                "--out", self.out)
        self.assertTrue((self.out / "deep/a/b/c/d/thing.log").is_file())

    def test_whole_file_copy_keeps_the_content(self):
        isolate("--logs-root", self.logs, "--path", "app/server-01/app-2026-08-01.log",
                "--out", self.out)
        self.assertEqual(
            (self.out / "app/server-01/app-2026-08-01.log").read_text(),
            "one\ntwo\nthree\nfour\nfive\n")


class TestLineExtraction(ToolTestCase):
    """A one-line isolated file is the sharpest possible test: if the re-scan
    comes back clean, it is that line that got covered, not something else."""

    def setUp(self):
        super().setUp()
        make_logs(self.logs, {"a/app-2026-08-01.log":
                              "".join(f"line{i}\n" for i in range(1, 11))})

    def read(self):
        return (self.out / "a/app-2026-08-01.log").read_text().splitlines()

    def test_single_line(self):
        r = jout(isolate("--logs-root", self.logs, "--path", "a/app-2026-08-01.log",
                         "--lines", "4", "--out", self.out))
        self.assertEqual(self.read(), ["line4"])
        self.assertEqual(r["lines_written"], 1)

    def test_several_lines_keep_file_order(self):
        isolate("--logs-root", self.logs, "--path", "a/app-2026-08-01.log",
                "--lines", "7,2,9", "--out", self.out)
        self.assertEqual(self.read(), ["line2", "line7", "line9"])

    def test_context_expands_around_each_line(self):
        isolate("--logs-root", self.logs, "--path", "a/app-2026-08-01.log",
                "--lines", "5", "--context", "2", "--out", self.out)
        self.assertEqual(self.read(), ["line3", "line4", "line5", "line6", "line7"])

    def test_context_clamps_at_the_start_of_the_file(self):
        isolate("--logs-root", self.logs, "--path", "a/app-2026-08-01.log",
                "--lines", "1", "--context", "3", "--out", self.out)
        self.assertEqual(self.read(), ["line1", "line2", "line3", "line4"])

    def test_line_number_past_the_end_yields_an_empty_file_not_a_crash(self):
        r = jout(isolate("--logs-root", self.logs, "--path", "a/app-2026-08-01.log",
                         "--lines", "9999", "--out", self.out))
        self.assertEqual(r["lines_written"], 0)
        self.assertTrue((self.out / "a/app-2026-08-01.log").is_file())

    def test_duplicate_line_numbers_are_written_once(self):
        isolate("--logs-root", self.logs, "--path", "a/app-2026-08-01.log",
                "--lines", "3,3,3", "--out", self.out)
        self.assertEqual(self.read(), ["line3"])


class TestSafety(ToolTestCase):

    def setUp(self):
        super().setUp()
        make_logs(self.logs, {"a/app-2026-08-01.log": "x\n"})
        (self.tmp / "secret.txt").write_text("not part of the logs folder\n")

    def test_path_traversal_is_refused(self):
        """A report path is machine-generated input. It must not be able to
        reach outside the logs root."""
        p = isolate("--logs-root", self.logs, "--path", "../secret.txt",
                    "--out", self.out)
        self.assertEqual(p.returncode, 2)
        self.assertIn("escapes", p.stderr)
        self.assertFalse((self.out / "secret.txt").exists())

    def test_deep_traversal_is_refused(self):
        p = isolate("--logs-root", self.logs, "--path", "a/../../secret.txt",
                    "--out", self.out)
        self.assertEqual(p.returncode, 2)

    def test_absolute_path_is_treated_as_relative(self):
        p = isolate("--logs-root", self.logs, "--path", "/a/app-2026-08-01.log",
                    "--out", self.out)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((self.out / "a/app-2026-08-01.log").is_file())

    def test_missing_file_exits_2(self):
        p = isolate("--logs-root", self.logs, "--path", "nope.log", "--out", self.out)
        self.assertEqual(p.returncode, 2)
        self.assertIn("no such file", p.stderr)

    def test_a_directory_is_not_a_file(self):
        p = isolate("--logs-root", self.logs, "--path", "a", "--out", self.out)
        self.assertEqual(p.returncode, 2)

    def test_the_source_logs_folder_is_never_written_to(self):
        before = {p: p.read_bytes() for p in self.logs.rglob("*") if p.is_file()}
        isolate("--logs-root", self.logs, "--path", "a/app-2026-08-01.log",
                "--out", self.out)
        after = {p: p.read_bytes() for p in self.logs.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "the logs folder is read-only input")


class TestSizeAndAccumulation(ToolTestCase):

    def test_large_file_is_truncated_and_says_so(self):
        make_logs(self.logs, {"a/big.log": "x" * 50_000 + "\n"})
        r = jout(isolate("--logs-root", self.logs, "--path", "a/big.log",
                         "--out", self.out, "--max-bytes", "1000"))
        self.assertTrue(r["truncated"])
        self.assertEqual((self.out / "a/big.log").stat().st_size, 1000)

    def test_line_extraction_ignores_the_byte_cap(self):
        """--lines seeks by line number, so a line near the end of a huge file
        is still reachable. Truncating there would silently isolate the wrong
        thing."""
        make_logs(self.logs, {"a/big.log": "".join(f"line{i}\n" for i in range(1, 5001))})
        r = jout(isolate("--logs-root", self.logs, "--path", "a/big.log",
                         "--lines", "4999", "--out", self.out, "--max-bytes", "10"))
        self.assertFalse(r["truncated"])
        self.assertEqual((self.out / "a/big.log").read_text().strip(), "line4999")

    def test_binary_file_is_copied_without_error(self):
        make_logs(self.logs, {"a/out.zip": b"PK\x03\x04\x00\x00binary\x00"})
        p = isolate("--logs-root", self.logs, "--path", "a/out.zip", "--out", self.out)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((self.out / "a/out.zip").read_bytes(), b"PK\x03\x04\x00\x00binary\x00")

    def test_several_files_accumulate_into_one_root(self):
        """The retry pattern: after a config change, verify the fix against
        two or three siblings at once. A pattern that covers one and not the
        others is the most common failure, and one file cannot see it."""
        make_logs(self.logs, {"a/app-2026-08-01.log": "x\n",
                              "b/app-2026-08-02.log": "y\n",
                              "c/app-2026-8-3.log": "z\n"})
        isolate("--logs-root", self.logs, "--path", "a/app-2026-08-01.log",
                "--out", self.out, "--clean")
        isolate("--logs-root", self.logs, "--path", "b/app-2026-08-02.log", "--out", self.out)
        isolate("--logs-root", self.logs, "--path", "c/app-2026-8-3.log", "--out", self.out)
        found = sorted(p.relative_to(self.out).as_posix()
                       for p in self.out.rglob("*") if p.is_file())
        self.assertEqual(found, ["a/app-2026-08-01.log", "b/app-2026-08-02.log",
                                 "c/app-2026-8-3.log"])

    def test_clean_wipes_a_previous_attempt(self):
        make_logs(self.logs, {"a/one.log": "x\n", "b/two.log": "y\n"})
        isolate("--logs-root", self.logs, "--path", "a/one.log", "--out", self.out)
        isolate("--logs-root", self.logs, "--path", "b/two.log", "--out", self.out, "--clean")
        found = [p.relative_to(self.out).as_posix()
                 for p in self.out.rglob("*") if p.is_file()]
        self.assertEqual(found, ["b/two.log"],
                         "--clean must not leave the previous attempt's files behind")


class TestIsolatedFolderIsScannable(ToolTestCase):
    """The output of isolate.py is the input to the next scan. If the isolated
    copy stops matching the rule the original matched, every verdict is wrong."""

    def test_isolated_file_matches_the_same_rule_as_the_original(self):
        self.given_logs({"app/server-01/app-2026-08-01.log":
                         "2026-08-01 ERROR real\n2026-08-01 MYSTERY x\n"})
        self.given_config()
        _, full = self.scan_to("full")
        self.assertEqual(self.file_entry(full, "app/server-01/app-2026-08-01.log")
                         ["matched_by"], "app-log")

        isolate("--logs-root", self.logs, "--path", "app/server-01/app-2026-08-01.log",
                "--lines", "2", "--out", self.out, "--clean")
        from helpers import scan
        scan(self.out, self.config, self.tmp / "iso-scan")
        from helpers import jload
        iso = jload(self.tmp / "iso-scan" / "coverage-report.json")
        e = self.file_entry(iso, "app/server-01/app-2026-08-01.log")
        self.assertEqual(e["matched_by"], "app-log",
                         "the isolated copy must still be detected by the same rule")
        self.assertEqual(e["lines"]["unmatched"], 1,
                         "and the one isolated line must still be the unmatched one")


if __name__ == "__main__":
    unittest.main()
