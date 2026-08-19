"""The report contract -- docs/CONTRACTS.md sections 1 and 2.

*** THIS FILE IS ALSO THE ACCEPTANCE TEST FOR THE REAL MONITORING TOOL. ***

Every test here goes through `tools/standalone.sh`, so it tests whatever
STANDALONE_CMD points at. Today that is the filler scanner. The day you point
it at the real standalone mode, run this file: anything that fails is a place
where the real tool and the loop disagree, and every one of those makes the
loop confidently wrong rather than loudly broken.

Some tests assert behaviour that is a *choice* the filler makes -- blank-line
accounting, ignore-before-parse ordering. Those are marked. If the real tool
chooses differently, change the assertion AND docs/STANDALONE_MODE.md §4-5,
then re-check Worker B's prompt, which assumes the documented order.
"""

from __future__ import annotations

import json
import unittest

from helpers import ToolTestCase, jload, make_config, scan


class TestFileStates(ToolTestCase):
    """Three states, not two. `ignored` means "deliberately skipped, here is
    the rule"; `undetected` means "invisible, nobody decided that". Collapsing
    them makes the report useless -- the loop cannot tell finished work from
    unexamined work, and would rework every ignore rule forever."""

    def test_all_three_states_are_reported(self):
        self.given_logs({
            "app/app-2026-08-01.log": "2026-08-01 ERROR boom\n",
            "artifacts/out.zip": b"\x00bin",
            "traces/t.json": '{"a":1}\n',
        })
        self.given_config(ignore_files=[("archives", r"\.zip$", "no log content")])
        _, rep = self.scan_to()
        self.assertEqual(self.file_entry(rep, "app/app-2026-08-01.log")["status"], "detected")
        self.assertEqual(self.file_entry(rep, "artifacts/out.zip")["status"], "ignored")
        self.assertEqual(self.file_entry(rep, "traces/t.json")["status"], "undetected")

    def test_detected_names_the_rule_that_matched(self):
        self.given_logs({"app/app-2026-08-01.log": "2026-08-01 ERROR boom\n"})
        self.given_config()
        _, rep = self.scan_to()
        self.assertEqual(self.file_entry(rep, "app/app-2026-08-01.log")["matched_by"],
                         "app-log")

    def test_ignored_names_the_rule_that_skipped_it(self):
        self.given_logs({"artifacts/out.zip": b"\x00bin"})
        self.given_config(ignore_files=[("archives", r"\.zip$", "x")])
        _, rep = self.scan_to()
        self.assertEqual(self.file_entry(rep, "artifacts/out.zip")["ignored_by"], "archives")

    def test_ignore_beats_parse_at_file_level(self):
        """CHOICE (docs/STANDALONE_MODE.md §4). Worker B's decision tree
        assumes a suppression rule wins over a parse rule."""
        self.given_logs({"app/app-2026-08-01.log": "2026-08-01 ERROR boom\n"})
        self.given_config(ignore_files=[("all-app", r"app-.*\.log$", "suppressed")])
        _, rep = self.scan_to()
        self.assertEqual(self.file_entry(rep, "app/app-2026-08-01.log")["status"],
                         "ignored")

    def test_ignore_beats_parse_at_line_level(self):
        """CHOICE. Same reasoning, one level down."""
        self.given_logs({"app/app-2026-08-01.log":
                         "2026-08-01 ERROR boom\n2026-08-01 ERROR quiet\n"})
        self.given_config(ignore_lines=[("quiet", ".*", "quiet", "suppressed")])
        _, rep = self.scan_to()
        e = self.file_entry(rep, "app/app-2026-08-01.log")
        self.assertEqual(e["lines"]["ignored"], 1)
        self.assertEqual(e["lines"]["matched"], 1)

    def test_first_matching_file_rule_wins(self):
        self.given_logs({"app/app-2026-08-01.log": "2026-08-01 ERROR boom\n"})
        make_config(self.config, files=[
            ("first", r"(^|/)app-.*\.log$", [r"ERROR"]),
            ("second", r"(^|/)app-\d{4}-\d{2}-\d{2}\.log$", [r"ERROR"]),
        ])
        _, rep = self.scan_to()
        self.assertEqual(self.file_entry(rep, "app/app-2026-08-01.log")["matched_by"],
                         "first")

    def test_binary_files_are_flagged(self):
        """A parse rule matching a binary means the rule is too broad. The
        report has to surface that, not silently report zero lines."""
        self.given_logs({"app/app-2026-08-01.log": b"\x00\x01\x02binary\x00"})
        self.given_config()
        _, rep = self.scan_to()
        e = self.file_entry(rep, "app/app-2026-08-01.log")
        self.assertEqual(e["status"], "detected")
        self.assertTrue(e["binary"])


class TestPaths(ToolTestCase):
    """The loop matches report entries to ledger items by `path`, and rebuilds
    that exact path when isolating. An absolute path or a backslash here breaks
    both, and the symptom is every item returning isolation_failed."""

    def test_paths_are_relative_with_forward_slashes(self):
        self.given_logs({"a/b/c/deep-2026-08-01.log": "x\n"})
        self.given_config()
        _, rep = self.scan_to()
        paths = [f["path"] for f in rep["files"]]
        self.assertEqual(paths, ["a/b/c/deep-2026-08-01.log"])
        for p in paths:
            self.assertFalse(p.startswith("/"), f"{p} is absolute")
            self.assertNotIn("\\", p, f"{p} uses backslashes")

    def test_logs_root_is_absolute_in_the_report(self):
        self.given_logs({"x.log": "a\n"})
        self.given_config()
        _, rep = self.scan_to()
        self.assertTrue(rep["logs_root"].startswith("/"))

    def test_nested_depth_is_preserved(self):
        self.given_logs({"a/b/c/d/e/f/g/app-2026-08-01.log": "2026-08-01 ERROR x\n"})
        self.given_config()
        _, rep = self.scan_to()
        self.assertEqual(rep["files"][0]["path"], "a/b/c/d/e/f/g/app-2026-08-01.log")


class TestUncoveredMirror(ToolTestCase):
    """Unmatched lines are mirrored into <out>/uncovered/<same path>. Raw lines
    only -- a human opens it and sees log lines they recognise, and the file can
    be fed straight back to the tool as a test input."""

    def setUp(self):
        super().setUp()
        self.given_logs({"app/s1/app-2026-08-01.log":
                         "2026-08-01 ERROR real\n"
                         "2026-08-01 MYSTERY one\n"
                         "2026-08-01 MYSTERY two\n"})
        self.given_config()

    def test_structure_and_filename_are_recreated(self):
        out, rep = self.scan_to()
        mirror = out / "uncovered" / "app/s1/app-2026-08-01.log"
        self.assertTrue(mirror.is_file(), "mirror must recreate the full path")
        self.assertEqual(self.file_entry(rep, "app/s1/app-2026-08-01.log")
                         ["unmatched_sample_path"], "uncovered/app/s1/app-2026-08-01.log")

    def test_mirror_holds_raw_lines_only(self):
        out, _ = self.scan_to()
        body = (out / "uncovered" / "app/s1/app-2026-08-01.log").read_text()
        self.assertEqual(body.splitlines(),
                         ["2026-08-01 MYSTERY one", "2026-08-01 MYSTERY two"])
        self.assertNotIn("2026-08-01 ERROR real", body, "matched lines must not appear")

    def test_sample_cap_is_honoured(self):
        self.given_logs({"app/s1/app-2026-08-01.log":
                         "".join(f"2026-08-01 MYSTERY {i}\n" for i in range(100))})
        out, rep = self.scan_to(max_lines=7)
        e = self.file_entry(rep, "app/s1/app-2026-08-01.log")
        self.assertEqual(len(e["unmatched_samples"]), 7)
        self.assertEqual(e["lines"]["unmatched"], 100,
                         "the CAP is on samples kept, not on lines counted")
        self.assertEqual(
            len((out / "uncovered" / "app/s1/app-2026-08-01.log").read_text().splitlines()), 7)

    def test_samples_carry_real_line_numbers(self):
        _, rep = self.scan_to()
        e = self.file_entry(rep, "app/s1/app-2026-08-01.log")
        self.assertEqual([s["line_no"] for s in e["unmatched_samples"]], [2, 3])

    def test_no_mirror_when_a_file_is_fully_covered(self):
        self.given_logs({"app/s1/app-2026-08-01.log": "2026-08-01 ERROR real\n"})
        out, rep = self.scan_to()
        self.assertIsNone(self.file_entry(rep, "app/s1/app-2026-08-01.log")
                          ["unmatched_sample_path"])
        self.assertFalse((out / "uncovered" / "app/s1/app-2026-08-01.log").exists())


class TestTotals(ToolTestCase):
    """Coverage percentages come straight from these numbers, and they are what
    the human is shown at the end of a run."""

    def setUp(self):
        super().setUp()
        self.given_logs({
            "app/app-2026-08-01.log": "2026-08-01 ERROR a\n2026-08-01 NOISE b\n2026-08-01 SKIP c\n",
            "app/app-2026-08-02.log": "2026-08-02 ERROR d\n",
            "artifacts/out.zip": b"\x00bin",
            "traces/t.json": "{}\n",
        })
        self.given_config(ignore_files=[("arch", r"\.zip$", "x")],
                          ignore_lines=[("skip", ".*", "SKIP", "x")])
        _, self.rep = self.scan_to()

    def test_file_totals_add_up(self):
        t = self.rep["totals"]
        self.assertEqual(t["files_seen"],
                         t["files_detected"] + t["files_ignored"] + t["files_undetected"])
        self.assertEqual(t["files_seen"], 4)
        self.assertEqual(t["files_detected"], 2)
        self.assertEqual(t["files_ignored"], 1)
        self.assertEqual(t["files_undetected"], 1)

    def test_line_totals_add_up(self):
        t = self.rep["totals"]
        self.assertEqual(t["lines_seen"],
                         t["lines_matched"] + t["lines_ignored"] + t["lines_unmatched"])

    def test_totals_match_the_sum_of_the_per_file_entries(self):
        for key in ("seen", "matched", "ignored", "unmatched"):
            self.assertEqual(
                self.rep["totals"][f"lines_{key}"],
                sum(f["lines"][key] for f in self.rep["files"]),
                f"lines_{key} does not match the per-file sum")

    def test_lines_are_only_counted_in_parsed_files(self):
        """An undetected file is never opened, so it contributes no lines. If
        that changes, line coverage silently includes files nobody parses."""
        self.assertEqual(self.file_entry(self.rep, "traces/t.json")["lines"]["seen"], 0)
        self.assertEqual(self.file_entry(self.rep, "artifacts/out.zip")["lines"]["seen"], 0)


class TestSchemaAndFailureModes(ToolTestCase):

    def test_report_carries_every_key_the_loop_reads(self):
        self.given_logs({"app/app-2026-08-01.log": "2026-08-01 ERROR a\nx\n"})
        self.given_config()
        _, rep = self.scan_to()
        for key in ("schema_version", "generated_at", "logs_root", "config_path",
                    "config_fingerprint", "totals", "files",
                    "max_unmatched_lines_per_file", "label"):
            self.assertIn(key, rep, f"report is missing {key}")
        for key in ("path", "size_bytes", "status", "matched_by", "ignored_by",
                    "binary", "lines", "unmatched_sample_path", "unmatched_samples"):
            self.assertIn(key, rep["files"][0], f"file entry is missing {key}")
        for key in ("seen", "matched", "ignored", "unmatched"):
            self.assertIn(key, rep["files"][0]["lines"])

    def test_fingerprint_changes_when_the_config_changes(self):
        """The loop uses this to tell two scans apart. If it is constant, a
        stale report can be mistaken for a fresh one."""
        self.given_logs({"app/app-2026-08-01.log": "2026-08-01 ERROR a\n"})
        self.given_config()
        _, a = self.scan_to("s1")
        self.given_config(ignore_files=[("x", r"\.zip$", "x")])
        _, b = self.scan_to("s2")
        self.assertNotEqual(a["config_fingerprint"], b["config_fingerprint"])

    def test_label_is_echoed_back(self):
        self.given_logs({"x.log": "a\n"})
        self.given_config()
        out = self.tmp / "labelled"
        scan(self.logs, self.config, out, label="sweep-3")
        self.assertEqual(jload(out / "coverage-report.json")["label"], "sweep-3")

    def test_missing_logs_root_exits_non_zero(self):
        self.given_config()
        p = scan(self.tmp / "does-not-exist", self.config, self.tmp / "o")
        self.assertNotEqual(p.returncode, 0)

    def test_unparseable_config_exits_non_zero(self):
        self.given_logs({"x.log": "a\n"})
        self.config.write_text("files:\n  - id: a\n   pattern: broken\n")
        p = scan(self.logs, self.config, self.tmp / "o")
        self.assertNotEqual(p.returncode, 0, "a broken config must fail loudly")

    def test_invalid_regex_in_config_exits_non_zero(self):
        """Fail at startup, not silently mid-scan: a skipped rule would report
        real files as undetected and send the loop chasing phantoms."""
        self.given_logs({"x.log": "a\n"})
        make_config(self.config, files=[("bad", r"(unclosed[", [])])
        p = scan(self.logs, self.config, self.tmp / "o")
        self.assertNotEqual(p.returncode, 0)

    def test_empty_logs_folder_is_a_clean_result_not_a_crash(self):
        self.logs.mkdir(parents=True)
        self.given_config()
        out, rep = self.scan_to()
        self.assertEqual(rep["totals"]["files_seen"], 0)
        self.assertEqual(rep["files"], [])


class TestMessyInput(ToolTestCase):
    """Ungoverned logs folders contain everything. None of it may crash a scan
    -- a crashed scan stalls the whole run."""

    def test_unicode_and_invalid_bytes_do_not_crash(self):
        self.given_logs({
            "app/app-2026-08-01.log":
                "2026-08-01 ERROR café 中文 🙂\n".encode() + b"\xff\xfe invalid utf8\n",
        })
        self.given_config()
        _, rep = self.scan_to()
        self.assertEqual(self.file_entry(rep, "app/app-2026-08-01.log")["lines"]["seen"], 2)

    def test_blank_lines_are_accounted_as_ignored(self):
        """CHOICE (docs/STANDALONE_MODE.md §5). Blank lines must not become
        work items -- there is no config rule that could ever cover them."""
        self.given_logs({"app/app-2026-08-01.log": "2026-08-01 ERROR a\n\n\n   \n"})
        self.given_config()
        _, rep = self.scan_to()
        e = self.file_entry(rep, "app/app-2026-08-01.log")
        self.assertEqual(e["lines"]["unmatched"], 0)
        self.assertEqual(e["lines"]["ignored"], 3)

    def test_file_without_a_trailing_newline(self):
        self.given_logs({"app/app-2026-08-01.log": "2026-08-01 MYSTERY x"})
        self.given_config()
        _, rep = self.scan_to()
        self.assertEqual(self.file_entry(rep, "app/app-2026-08-01.log")["lines"]["seen"], 1)

    def test_very_long_line_is_truncated_in_the_sample_not_dropped(self):
        self.given_logs({"app/app-2026-08-01.log": "2026-08-01 MYSTERY " + "x" * 50_000 + "\n"})
        self.given_config()
        _, rep = self.scan_to()
        e = self.file_entry(rep, "app/app-2026-08-01.log")
        self.assertEqual(e["lines"]["unmatched"], 1)
        self.assertLessEqual(len(e["unmatched_samples"][0]["text"]), 2000,
                             "a 50k-character line must not be pasted into the report whole")

    def test_empty_file_is_handled(self):
        self.given_logs({"app/app-2026-08-01.log": ""})
        self.given_config()
        _, rep = self.scan_to()
        self.assertEqual(self.file_entry(rep, "app/app-2026-08-01.log")["lines"]["seen"], 0)

    def test_filenames_with_spaces_and_unicode(self):
        self.given_logs({"odd dir/app-2026-08-01.log": "2026-08-01 ERROR a\n",
                         "données/rapport.txt": "x\n"})
        self.given_config()
        _, rep = self.scan_to()
        self.assertEqual(self.file_entry(rep, "odd dir/app-2026-08-01.log")["status"],
                         "detected")
        self.assertEqual(self.file_entry(rep, "données/rapport.txt")["status"], "undetected")


if __name__ == "__main__":
    unittest.main()
