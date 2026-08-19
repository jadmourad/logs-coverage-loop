"""config_edit.py is the only thing that writes the monitoring tool's config.

This is the highest-stakes mechanical component in the repo: it edits a live
production file, unattended, hundreds of times in a run. What can go wrong:

  * a bad edit lands and the config no longer parses -> every later scan fails
    and every remaining item reports a false "still uncovered"
  * a regex is mangled in transit -> a rule that looks right matches nothing
  * comments and ordering are destroyed -> the human loses the "why" behind
    every existing rule
  * a retry adds the same rule twice -> the config grows without bound
  * an over-broad rule is accepted -> the monitoring tool goes blind
"""

from __future__ import annotations

import re
import unittest

from helpers import ToolTestCase, config_edit, jout, make_config

CONFIG_WITH_COMMENTS = """\
version: 1

# ---------------------------------------------------------------
# Owned by the platform team. Talk to #obs before changing a rule.
# ---------------------------------------------------------------
files:
  # Added after INC-4471: the edge tier logs upstream timeouts here.
  - id: app-log
    pattern: '(^|/)app-\\d{4}-\\d{2}-\\d{2}\\.log$'
    line_patterns:
      - '\\b(ERROR|FATAL)\\b'

  - id: edge-log            # trailing comment
    pattern: '(^|/)edge\\.log$'
    line_patterns:
      - 'upstream timed out'

ignore:
  files:
    # Transient files the writer has not finished with.
    - id: tmpfiles
      pattern: '\\.tmp$'
      reason: 'transient'
  lines:
    - id: banner
      file_scope: '.*'
      pattern: '^={10,}$'
      reason: 'ascii banners'
"""


class ConfigTestCase(ToolTestCase):
    def edit(self, *args, config=None):
        return config_edit("--config", str(config or self.config),
                           "--backup-dir", str(self.backups), *args)

    def yaml(self, config=None):
        import yaml as y
        return y.safe_load((config or self.config).read_text())

    def rule_ids(self, section, config=None):
        cfg = self.yaml(config)
        pool = {"files": cfg.get("files"),
                "ignore.files": (cfg.get("ignore") or {}).get("files"),
                "ignore.lines": (cfg.get("ignore") or {}).get("lines")}[section] or []
        return [r["id"] for r in pool]


class TestOpsWithAnchors(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.given_config()

    def test_add_ignore_file(self):
        r = jout(self.edit("add-ignore-file", "--rule-id", "archives",
                           "--pattern", r"\.(zip|gz)$", "--reason", "no log content",
                           "--wiki-ref", "KB-001"))
        self.assertTrue(r["changed"])
        rule = self.yaml()["ignore"]["files"][0]
        self.assertEqual(rule["id"], "archives")
        self.assertEqual(rule["pattern"], r"\.(zip|gz)$")
        self.assertEqual(rule["reason"], "no log content")
        self.assertEqual(rule["wiki_ref"], "KB-001")

    def test_add_ignore_line(self):
        self.edit("add-ignore-line", "--rule-id", "gc-noise",
                  "--pattern", r"^\s*\[(Full )?GC ", "--file-scope", r".*\.log$",
                  "--reason", "GC is not an error")
        rule = self.yaml()["ignore"]["lines"][0]
        self.assertEqual(rule["pattern"], r"^\s*\[(Full )?GC ")
        self.assertEqual(rule["file_scope"], r".*\.log$")

    def test_add_file_rule_with_line_patterns(self):
        self.edit("add-file-rule", "--rule-id", "db-audit",
                  "--pattern", r"(^|/)db-audit-\d{4}\.log$",
                  "--line-pattern", r"\[DB-ERR\]", "--line-pattern", r"\[DB-OK ?\]")
        rule = next(r for r in self.yaml()["files"] if r["id"] == "db-audit")
        self.assertEqual(rule["line_patterns"], [r"\[DB-ERR\]", r"\[DB-OK ?\]"])

    def test_add_line_pattern_extends_an_existing_rule(self):
        self.edit("add-line-pattern", "--rule-id", "app-log",
                  "--pattern", r"\bAUDIT\b")
        rule = next(r for r in self.yaml()["files"] if r["id"] == "app-log")
        self.assertEqual(rule["line_patterns"], [r"\b(ERROR|FATAL)\b", r"\bAUDIT\b"],
                         "the new pattern must be appended, not replace the list")

    def test_add_line_pattern_creates_the_list_when_absent(self):
        make_config(self.config, files=[("bare-log", r"(^|/)bare\.log$")])
        self.edit("add-line-pattern", "--rule-id", "bare-log", "--pattern", "BOOM")
        rule = next(r for r in self.yaml()["files"] if r["id"] == "bare-log")
        self.assertEqual(rule["line_patterns"], ["BOOM"])

    def test_anchors_survive_every_op(self):
        """The anchors are how the next edit finds its place. An op that
        consumes its own anchor works once and then silently falls back."""
        self.edit("add-ignore-file", "--rule-id", "a", "--pattern", r"\.a$",
                  "--reason", "x")
        self.edit("add-ignore-line", "--rule-id", "b", "--pattern", "B", "--reason", "x")
        self.edit("add-file-rule", "--rule-id", "c", "--pattern", r"(^|/)c\.log$")
        body = self.config.read_text()
        for anchor in ("@anchor:file-rules", "@anchor:ignore-files", "@anchor:ignore-lines"):
            self.assertIn(anchor, body, f"{anchor} was consumed by an edit")

    def test_repeated_edits_accumulate_in_order(self):
        for i in range(4):
            self.edit("add-ignore-file", "--rule-id", f"r{i}",
                      "--pattern", rf"\.x{i}$", "--reason", "x")
        self.assertEqual(self.rule_ids("ignore.files"), ["r0", "r1", "r2", "r3"])


class TestOpsWithoutAnchors(ConfigTestCase):
    """A real production config has no @anchor comments. The fallback locates
    sections by indentation, and it has to get all four ops right."""

    def setUp(self):
        super().setUp()
        self.config.write_text(CONFIG_WITH_COMMENTS)

    def test_all_four_ops_apply(self):
        self.assertTrue(jout(self.edit("add-ignore-file", "--rule-id", "archives",
                                       "--pattern", r"\.zip$", "--reason", "x"))["changed"])
        self.assertTrue(jout(self.edit("add-ignore-line", "--rule-id", "gc",
                                       "--pattern", r"^\[GC ", "--reason", "x"))["changed"])
        self.assertTrue(jout(self.edit("add-file-rule", "--rule-id", "db",
                                       "--pattern", r"(^|/)db\.log$"))["changed"])
        self.assertTrue(jout(self.edit("add-line-pattern", "--rule-id", "edge-log",
                                       "--pattern", "SSL_ERROR"))["changed"])
        cfg = self.yaml()
        self.assertEqual(self.rule_ids("ignore.files"), ["tmpfiles", "archives"])
        self.assertEqual(self.rule_ids("ignore.lines"), ["banner", "gc"])
        self.assertIn("db", self.rule_ids("files"))
        edge = next(r for r in cfg["files"] if r["id"] == "edge-log")
        self.assertEqual(edge["line_patterns"], ["upstream timed out", "SSL_ERROR"])

    def test_every_comment_survives(self):
        self.edit("add-ignore-file", "--rule-id", "archives",
                  "--pattern", r"\.zip$", "--reason", "x")
        self.edit("add-line-pattern", "--rule-id", "edge-log", "--pattern", "SSL_ERROR")
        body = self.config.read_text()
        for comment in ("Owned by the platform team", "Added after INC-4471",
                        "trailing comment", "Transient files the writer"):
            self.assertIn(comment, body, f"lost comment: {comment!r}")

    def test_existing_rules_are_untouched(self):
        before = self.yaml()
        self.edit("add-file-rule", "--rule-id", "db", "--pattern", r"(^|/)db\.log$")
        after = self.yaml()
        for rid in ("app-log", "edge-log"):
            self.assertEqual(next(r for r in before["files"] if r["id"] == rid),
                             next(r for r in after["files"] if r["id"] == rid))

    def test_new_pattern_lands_next_to_its_siblings(self):
        """Cosmetic but real: appending after a trailing blank line leaves the
        entry orphaned below it. Valid YAML, unreadable diff."""
        self.edit("add-line-pattern", "--rule-id", "app-log", "--pattern", "NEW_ONE")
        lines = self.config.read_text().split("\n")
        i = next(n for n, l in enumerate(lines) if "NEW_ONE" in l)
        self.assertIn("ERROR|FATAL", lines[i - 1],
                      "the new pattern must sit directly after the previous entry")

    def test_missing_section_is_a_clear_setup_error(self):
        self.config.write_text("version: 1\nfiles:\n  - id: a\n    pattern: 'x'\n")
        p = self.edit("add-ignore-line", "--rule-id", "gc",
                      "--pattern", r"^\[GC ", "--reason", "x")
        self.assertEqual(p.returncode, 2, "no ignore.lines section must exit 2, not guess")
        self.assertIn("anchor", p.stderr.lower())


class TestRegexFidelity(ConfigTestCase):
    """A pattern that is mangled in transit produces a rule that looks correct
    and matches nothing. That failure is invisible until someone misses an
    incident, so it gets its own class."""

    def setUp(self):
        super().setUp()
        self.given_config()

    def test_backslashes_survive_verbatim(self):
        hard = r"^\s*\[(Full )?GC \(\w+\)\] \d{2,4}K->\d+K \[\d+\.\d+s\]"
        self.edit("add-ignore-line", "--rule-id", "gc", "--pattern", hard, "--reason", "x")
        self.assertEqual(self.yaml()["ignore"]["lines"][0]["pattern"], hard)
        re.compile(self.yaml()["ignore"]["lines"][0]["pattern"])

    def test_apostrophes_are_escaped_not_lost(self):
        self.edit("add-ignore-file", "--rule-id", "quoted", "--pattern", r"can't\.log$",
                  "--reason", "the writer's own scratch file")
        rule = self.yaml()["ignore"]["files"][0]
        self.assertEqual(rule["pattern"], r"can't\.log$")
        self.assertEqual(rule["reason"], "the writer's own scratch file")

    def test_yaml_special_characters_do_not_break_the_document(self):
        for pattern in [r"^#\s", r"key: value", r"- dash", r"a|b", r"\{brace\}",
                        r"[bracket]", r"@at", r"\*star", r"&amp", r"%pct", r'"dq"',
                        r"!bang", r">fold", r"|pipe", r"'sq'", r"\ttab"]:
            with self.subTest(pattern=pattern):
                make_config(self.config)
                r = jout(self.edit("add-ignore-line", "--rule-id", "p",
                                   "--pattern", pattern, "--reason", "x"))
                self.assertTrue(r["changed"], f"{pattern!r} was refused")
                self.assertEqual(self.yaml()["ignore"]["lines"][0]["pattern"], pattern)

    def test_unicode_reason_survives(self):
        self.edit("add-ignore-file", "--rule-id", "u", "--pattern", r"\.x$",
                  "--reason", "décision de l'équipe — 中文")
        self.assertIn("中文", self.yaml()["ignore"]["files"][0]["reason"])

    def test_crlf_config_is_not_corrupted(self):
        """A config authored on Windows. The edit must still parse afterwards."""
        self.config.write_text(CONFIG_WITH_COMMENTS.replace("\n", "\r\n"),
                               newline="")
        r = jout(self.edit("add-ignore-file", "--rule-id", "archives",
                           "--pattern", r"\.zip$", "--reason", "x"))
        self.assertTrue(r["changed"])
        self.assertIn("archives", self.rule_ids("ignore.files"))


class TestRefusalAndRollback(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.given_config()
        self.before = self.config.read_bytes()

    def assert_unchanged(self):
        self.assertEqual(self.config.read_bytes(), self.before,
                         "a refused edit must leave the file byte-identical")

    def test_invalid_regex_rolls_back(self):
        p = self.edit("add-ignore-file", "--rule-id", "bad",
                      "--pattern", r"(unclosed[", "--reason", "x")
        self.assertEqual(p.returncode, 1)
        self.assertIn("rolled_back", p.stderr)
        self.assert_unchanged()

    def test_invalid_line_pattern_rolls_back(self):
        p = self.edit("add-file-rule", "--rule-id", "bad",
                      "--pattern", r"(^|/)x\.log$", "--line-pattern", r"a{2,1}")
        self.assertEqual(p.returncode, 1)
        self.assert_unchanged()

    def test_duplicate_pattern_is_a_no_op(self):
        self.edit("add-ignore-file", "--rule-id", "a", "--pattern", r"\.zip$",
                  "--reason", "x")
        snapshot = self.config.read_bytes()
        r = jout(self.edit("add-ignore-file", "--rule-id", "b", "--pattern", r"\.zip$",
                           "--reason", "y"))
        self.assertFalse(r["changed"])
        self.assertIn("already covered", r["reason"])
        self.assertEqual(self.config.read_bytes(), snapshot)

    def test_duplicate_rule_id_is_a_no_op(self):
        r = jout(self.edit("add-file-rule", "--rule-id", "app-log",
                           "--pattern", r"(^|/)other\.log$"))
        self.assertFalse(r["changed"])
        self.assertIn("already exists", r["reason"])
        self.assert_unchanged()

    def test_duplicate_line_pattern_is_a_no_op(self):
        r = jout(self.edit("add-line-pattern", "--rule-id", "app-log",
                           "--pattern", r"\b(ERROR|FATAL)\b"))
        self.assertFalse(r["changed"])
        self.assert_unchanged()

    def test_line_pattern_on_unknown_rule_exits_2(self):
        p = self.edit("add-line-pattern", "--rule-id", "nope", "--pattern", "X")
        self.assertEqual(p.returncode, 2)
        self.assert_unchanged()

    def test_dry_run_writes_nothing(self):
        p = config_edit("--config", str(self.config), "--backup-dir", str(self.backups),
                        "--dry-run", "add-ignore-file", "--rule-id", "a",
                        "--pattern", r"\.zip$", "--reason", "x")
        self.assertEqual(p.returncode, 0)
        self.assertTrue(jout(p)["dry_run"])
        self.assert_unchanged()

    def test_missing_config_exits_2(self):
        p = config_edit("--config", str(self.tmp / "nope.yml"), "validate")
        self.assertEqual(p.returncode, 2)


class TestBackups(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.given_config()

    def test_backup_holds_the_pre_edit_content(self):
        before = self.config.read_bytes()
        self.edit("add-ignore-file", "--rule-id", "a", "--pattern", r"\.zip$",
                  "--reason", "x")
        backups = sorted(self.backups.glob("*.yml"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)

    def test_every_edit_leaves_its_own_backup(self):
        for i in range(3):
            self.edit("add-ignore-file", "--rule-id", f"r{i}",
                      "--pattern", rf"\.x{i}$", "--reason", "x")
        self.assertEqual(len(list(self.backups.glob("*.yml"))), 3,
                         "config history is the change record for a run")

    def test_backup_name_records_the_operation(self):
        self.edit("add-ignore-file", "--rule-id", "archives",
                  "--pattern", r"\.zip$", "--reason", "x")
        name = next(self.backups.glob("*.yml")).name
        self.assertIn("add-ignore-file-archives", name)


class TestValidate(ConfigTestCase):
    def validate(self, text):
        self.config.write_text(text)
        p = config_edit("--config", str(self.config), "validate")
        return p, jout(p) if p.stdout.strip() else {}

    def test_clean_config_passes(self):
        self.given_config()
        p = config_edit("--config", str(self.config), "validate")
        self.assertEqual(p.returncode, 0)
        self.assertTrue(jout(p)["valid"])

    def test_duplicate_ids_across_sections_are_caught(self):
        p, r = self.validate(
            "version: 1\nfiles:\n  - id: dup\n    pattern: 'a'\n"
            "ignore:\n  files:\n    - id: dup\n      pattern: 'b'\n      reason: 'r'\n")
        self.assertEqual(p.returncode, 1)
        self.assertTrue(any("duplicate id" in x for x in r["problems"]))

    def test_missing_reason_on_an_ignore_rule_is_caught(self):
        """The reason is the audit trail for why a log is not monitored. A rule
        without one is an untraceable blind spot."""
        p, r = self.validate(
            "version: 1\nfiles: []\nignore:\n  files:\n    - id: a\n      pattern: 'x'\n")
        self.assertEqual(p.returncode, 1)
        self.assertTrue(any("reason" in x for x in r["problems"]))

    def test_missing_id_is_caught(self):
        p, r = self.validate("version: 1\nfiles:\n  - pattern: 'x'\n")
        self.assertEqual(p.returncode, 1)
        self.assertTrue(any("missing 'id'" in x for x in r["problems"]))

    def test_bad_regex_is_caught_in_every_field(self):
        for text, field in [
            ("version: 1\nfiles:\n  - id: a\n    pattern: '(['\n", "pattern"),
            ("version: 1\nfiles:\n  - id: a\n    pattern: 'x'\n    line_patterns:\n      - '(['\n",
             "line_patterns"),
            ("version: 1\nfiles: []\nignore:\n  lines:\n    - id: a\n      pattern: 'x'\n"
             "      reason: 'r'\n      file_scope: '(['\n", "file_scope"),
        ]:
            with self.subTest(field=field):
                p, r = self.validate(text)
                self.assertEqual(p.returncode, 1)
                self.assertTrue(any("regex" in x for x in r["problems"]))

    def test_unparseable_yaml_is_caught(self):
        p, r = self.validate("version: 1\nfiles:\n  - id: a\n   pattern: broken\n")
        self.assertEqual(p.returncode, 1)
        self.assertTrue(any("YAML" in x for x in r["problems"]))

    def test_non_mapping_top_level_is_caught(self):
        p, r = self.validate("- just\n- a\n- list\n")
        self.assertEqual(p.returncode, 1)

    def test_empty_sections_are_valid(self):
        """A brand-new config with no rules yet must not be an error."""
        p, r = self.validate("version: 1\nfiles:\nignore:\n  files:\n  lines:\n")
        self.assertEqual(p.returncode, 0, r)

    def test_show_lists_ids_per_section(self):
        self.given_config(ignore_files=[("arch", r"\.zip$", "x")])
        r = jout(config_edit("--config", str(self.config), "show"))
        self.assertEqual(r["files"], ["app-log"])
        self.assertEqual(r["ignore.files"], ["arch"])
        self.assertEqual(r["ignore.lines"], [])


if __name__ == "__main__":
    unittest.main()
