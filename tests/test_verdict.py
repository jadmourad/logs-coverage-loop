"""verdict.py decides whether an item is resolved. It is the loop's exit
condition, and it is code rather than a model judgement for exactly that reason.

A wrong `covered` here closes a real coverage gap and reports success. A wrong
`still_uncovered` burns three attempts and escalates something that was already
fine. This file is the full truth table.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from helpers import ToolTestCase, jload, jout, verdict


def file_entry(path="app/s1/app-2026-08-01.log", status="detected", matched_by="app-log",
               ignored_by=None, binary=False, seen=10, matched=0, ignored=0, unmatched=0):
    return {"path": path, "size_bytes": 100, "status": status, "matched_by": matched_by,
            "ignored_by": ignored_by, "binary": binary,
            "lines": {"seen": seen, "matched": matched, "ignored": ignored,
                      "unmatched": unmatched},
            "unmatched_sample_path": None, "unmatched_samples": []}


class VerdictCase(ToolTestCase):
    REL = "app/s1/app-2026-08-01.log"

    def given(self, kind, entries, line_no=None, attempt=1):
        item = {"id": "abc123abc123", "kind": kind, "signature": "sig",
                "example_path": self.REL, "example_line_no": line_no,
                "example_text": "2026-08-01 MYSTERY x", "occurrences": 1,
                "matched_by": "app-log"}
        report = {"schema_version": 1, "logs_root": "/x", "files": entries,
                  "config_fingerprint": "sha256:abc",
                  "totals": {"files_undetected": 0, "lines_unmatched": 0}}
        (self.tmp / "item.json").write_text(json.dumps(item))
        (self.tmp / "report.json").write_text(json.dumps(report))
        p = verdict("--item-json", self.tmp / "item.json",
                    "--report", self.tmp / "report.json",
                    "--attempt", attempt, "--out", self.tmp / "a-result.json",
                    "--isolated-root", self.tmp / "isolated")
        self.assertEqual(p.returncode, 0, p.stderr)
        return jload(self.tmp / "a-result.json")


class TestUndetectedFileVerdicts(VerdictCase):

    def test_still_undetected_keeps_the_loop_going(self):
        r = self.given("undetected_file", [file_entry(status="undetected", matched_by=None)])
        self.assertEqual(r["verdict"], "still_uncovered")
        self.assertFalse(r["loop_exits"])

    def test_now_parsed_is_covered(self):
        r = self.given("undetected_file", [file_entry(status="detected")])
        self.assertEqual(r["verdict"], "covered")
        self.assertTrue(r["loop_exits"])

    def test_now_skipped_is_ignored(self):
        r = self.given("undetected_file",
                       [file_entry(status="ignored", matched_by=None, ignored_by="arch")])
        self.assertEqual(r["verdict"], "ignored")
        self.assertTrue(r["loop_exits"])

    def test_binary_that_a_parse_rule_matched_is_flagged(self):
        """Covered, but the rule is almost certainly too broad. The note is how
        that reaches a human instead of quietly becoming a config rule."""
        r = self.given("undetected_file", [file_entry(status="detected", binary=True)])
        self.assertEqual(r["verdict"], "covered")
        self.assertIn("too broad", r["note"])


class TestUnmatchedLineVerdicts(VerdictCase):

    def test_lines_still_unmatched_keeps_the_loop_going(self):
        r = self.given("unmatched_lines",
                       [file_entry(matched=0, unmatched=1, seen=1)], line_no=3)
        self.assertEqual(r["verdict"], "still_uncovered")
        self.assertIn("1 line", r["detail"])

    def test_line_now_parsed_is_covered(self):
        r = self.given("unmatched_lines",
                       [file_entry(matched=1, unmatched=0, seen=1)], line_no=3)
        self.assertEqual(r["verdict"], "covered")
        self.assertIn("app-log", r["detail"])

    def test_line_now_suppressed_is_ignored(self):
        r = self.given("unmatched_lines",
                       [file_entry(matched=0, ignored=1, unmatched=0, seen=1)], line_no=3)
        self.assertEqual(r["verdict"], "ignored")

    def test_whole_file_ignored_also_resolves_the_line(self):
        r = self.given("unmatched_lines",
                       [file_entry(status="ignored", matched_by=None, ignored_by="arch",
                                   seen=0)], line_no=3)
        self.assertEqual(r["verdict"], "ignored")
        self.assertIn("arch", r["detail"])

    def test_host_file_no_longer_detected_is_a_regression_not_a_win(self):
        """The subtle one. A line item whose host file stopped being parsed is
        WORSE than before -- the file is invisible again. It must never read as
        resolved just because no unmatched lines were counted."""
        r = self.given("unmatched_lines",
                       [file_entry(status="undetected", matched_by=None, seen=0)],
                       line_no=3)
        self.assertEqual(r["verdict"], "still_uncovered")
        self.assertFalse(r["loop_exits"])
        self.assertIn("no longer detected", r["detail"])


class TestIsolationFailure(VerdictCase):

    def test_target_absent_from_the_report(self):
        r = self.given("undetected_file", [file_entry(path="some/other/file.log")])
        self.assertEqual(r["verdict"], "isolation_failed")
        self.assertFalse(r["loop_exits"])
        self.assertIn("does not mirror", r["detail"])

    def test_empty_report(self):
        r = self.given("unmatched_lines", [], line_no=3)
        self.assertEqual(r["verdict"], "isolation_failed")


class TestResultShape(VerdictCase):
    """a-result.json is Worker A's handoff to the orchestrator and Worker B.
    Its keys are the contract in docs/CONTRACTS.md §4."""

    def test_every_documented_key_is_present(self):
        r = self.given("unmatched_lines", [file_entry(unmatched=1)], line_no=7)
        for key in ("item_id", "kind", "attempt", "verdict", "detail", "note",
                    "target", "isolated_root", "report_path", "uncovered_dir",
                    "file_entry", "totals", "config_fingerprint", "loop_exits"):
            self.assertIn(key, r, f"a-result.json is missing {key}")
        self.assertEqual(r["target"], {"rel_path": self.REL, "line_no": 7})
        self.assertEqual(r["attempt"], 1)

    def test_loop_exits_agrees_with_the_verdict_in_every_case(self):
        cases = [
            ("undetected_file", [file_entry(status="undetected", matched_by=None)], False),
            ("undetected_file", [file_entry(status="detected")], True),
            ("undetected_file", [file_entry(status="ignored", ignored_by="a")], True),
            ("unmatched_lines", [file_entry(unmatched=1)], False),
            ("unmatched_lines", [file_entry(matched=1)], True),
            ("unmatched_lines", [file_entry(path="other.log")], False),
        ]
        for kind, entries, expected in cases:
            with self.subTest(kind=kind, expected=expected):
                r = self.given(kind, entries, line_no=1)
                self.assertEqual(r["loop_exits"], expected)
                self.assertEqual(r["loop_exits"], r["verdict"] in ("covered", "ignored"))

    def test_the_uncovered_dir_points_next_to_the_report(self):
        r = self.given("unmatched_lines", [file_entry(unmatched=1)], line_no=1)
        self.assertEqual(Path(r["uncovered_dir"]).name, "uncovered")
        self.assertEqual(Path(r["uncovered_dir"]).parent, self.tmp)

    def test_output_directory_is_created(self):
        item = {"id": "x", "kind": "undetected_file", "example_path": "a.log",
                "example_line_no": None}
        (self.tmp / "item.json").write_text(json.dumps(item))
        (self.tmp / "report.json").write_text(json.dumps(
            {"files": [file_entry(path="a.log", status="detected")], "totals": {}}))
        out = self.tmp / "deep" / "nested" / "a-result.json"
        p = verdict("--item-json", self.tmp / "item.json", "--report",
                    self.tmp / "report.json", "--attempt", 1, "--out", out)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(out.is_file())

    def test_stdout_summary_matches_the_written_file(self):
        item = {"id": "x", "kind": "undetected_file", "example_path": "a.log",
                "example_line_no": None}
        (self.tmp / "item.json").write_text(json.dumps(item))
        (self.tmp / "report.json").write_text(json.dumps(
            {"files": [file_entry(path="a.log", status="ignored", ignored_by="z")],
             "totals": {}}))
        p = verdict("--item-json", self.tmp / "item.json", "--report",
                    self.tmp / "report.json", "--attempt", 2,
                    "--out", self.tmp / "a-result.json")
        summary = jout(p)
        written = jload(self.tmp / "a-result.json")
        self.assertEqual(summary["verdict"], written["verdict"])
        self.assertEqual(summary["loop_exits"], written["loop_exits"])


if __name__ == "__main__":
    unittest.main()
