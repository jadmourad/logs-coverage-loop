"""The workflow, end to end, with the agents replaced by a scripted stand-in.

The orchestrator prompt describes an algorithm. This file *executes* that
algorithm against the real tools, so it is an executable spec of the workflow:
if someone changes the loop -- the retry rule, the resolution mapping, when an
item may close -- these tests are where that shows up.

Worker B is the only agent whose job is judgement, so it is the only thing
stubbed here (`decide`). Workers A and C are pure mechanism, so they run for
real: real isolation, real scans, real config edits.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from helpers import (ToolTestCase, config_edit, isolate, jload, jout, ledger,
                     make_logs, scan, verdict)


class MiniLoop(ToolTestCase):
    """Mirrors .claude/agents/coverage-orchestrator.md step for step."""

    RUN = "testrun"

    # -- Worker A (real) -----------------------------------------------------

    def worker_a(self, item, attempt, paths=None):
        att = self.root / "runs" / self.RUN / "items" / item["id"] / f"attempt-{attempt}"
        att.mkdir(parents=True, exist_ok=True)
        item_json = att.parent / "item.json"
        item_json.write_text(json.dumps(item))

        targets = paths or [item["example_path"]]
        for i, rel in enumerate(targets):
            args = ["--logs-root", self.logs, "--path", rel, "--out", att / "isolated"]
            if item["kind"] == "unmatched_lines" and rel == item["example_path"]:
                args += ["--lines", str(item["example_line_no"])]
            if i == 0:
                args.append("--clean")
            p = isolate(*args)
            self.assertEqual(p.returncode, 0, p.stderr)

        p = scan(att / "isolated", self.config, att / "report",
                 label=f"item-{item['id']}-attempt-{attempt}")
        self.assertEqual(p.returncode, 0, p.stderr)

        p = verdict("--item-json", item_json, "--report",
                    att / "report" / "coverage-report.json", "--attempt", attempt,
                    "--isolated-root", att / "isolated", "--out", att / "a-result.json")
        self.assertEqual(p.returncode, 0, p.stderr)
        return jload(att / "a-result.json"), att

    # -- Worker C (real) -----------------------------------------------------

    def worker_c(self, change, att):
        op = change["op"]
        args = ["--config", str(self.config), "--backup-dir",
                str(self.root / "runs" / self.RUN / "config-backups"), op,
                "--rule-id", change["rule_id"], "--pattern", change["pattern"]]
        if op in ("add-ignore-file", "add-ignore-line"):
            args += ["--reason", change.get("reason", "test")]
        if op == "add-ignore-line":
            args += ["--file-scope", change.get("file_scope", ".*")]
        if op == "add-file-rule":
            for lp in change.get("line_patterns") or []:
                args += ["--line-pattern", lp]
        p = config_edit(*args)
        result = json.loads(p.stdout or p.stderr)
        green = bool(result.get("changed"))
        (att / "c-result.json").write_text(json.dumps(
            {"item_id": "x", "applied": green, "op": op, "green_light": green,
             "changed": result.get("changed"), "notes": result.get("reason", "")}))
        return green

    # -- the orchestrator tick ----------------------------------------------

    def tick(self, decide, isolate_paths=None):
        """One tick: claim an item, drive its mini-loop, return a result line."""
        p = ledger("tick", "--run-id", self.RUN, root=self.root)
        if p.returncode == 4:
            return {"outcome": "TICK_LIMIT"}
        p = ledger("next", "--run-id", self.RUN, root=self.root)
        if p.returncode == 3:
            return {"outcome": "NO_PENDING"}
        item = jout(p)

        config_changed = False
        while True:
            a = jout(ledger("attempt", "--item-id", item["id"], "--run-id", self.RUN,
                            root=self.root))
            if a["exhausted"]:
                return {"outcome": "ESCALATED", "item": item["id"], "reason": "attempts"}
            attempt = a["attempts"]

            paths = isolate_paths(item, attempt) if isolate_paths else None
            a_result, att = self.worker_a(item, attempt, paths)

            if a_result["loop_exits"]:
                resolution = ("already_covered" if not config_changed else
                              "configured_parse" if a_result["verdict"] == "covered"
                              else "configured_ignore")
                r = ledger("update", "--item-id", item["id"], "--status", "done",
                           "--resolution", resolution, "--evidence",
                           str(att / "report" / "coverage-report.json"),
                           "--note", f"{a_result['verdict']} on attempt {attempt}",
                           "--run-id", self.RUN, root=self.root)
                self.assertEqual(r.returncode, 0, r.stderr)
                return {"outcome": "DONE", "item": item["id"],
                        "resolution": resolution, "attempts": attempt}

            decision = decide(item, a_result, attempt)
            (att / "decision.json").write_text(json.dumps(decision))

            if decision["decision"] == "escalate":
                ledger("update", "--item-id", item["id"], "--status", "escalated",
                       "--resolution", "escalated", "--decision-json",
                       str(att / "decision.json"), "--run-id", self.RUN, root=self.root)
                return {"outcome": "ESCALATED", "item": item["id"], "reason": "worker-b"}

            if self.worker_c(decision["proposed_change"], att):
                config_changed = True

    def run_to_completion(self, decide, max_ticks=50, isolate_paths=None):
        outcomes = []
        for _ in range(max_ticks):
            r = self.tick(decide, isolate_paths)
            outcomes.append(r)
            if r["outcome"] in ("NO_PENDING", "TICK_LIMIT"):
                break
        return outcomes

    def sweep(self, name="sweep-1"):
        out = self.root / "runs" / self.RUN / "scans" / name
        p = scan(self.logs, self.config, out, label=name)
        self.assertEqual(p.returncode, 0, p.stderr)
        return jout(ledger("sweep", "--report", out / "coverage-report.json",
                           "--run-id", self.RUN, root=self.root))

    def start_run(self, logs, config_kw=None):
        make_logs(self.logs, logs)
        self.given_config(**(config_kw or {}))
        out, rep = self.scan_to("initial")
        self.init_ledger(out / "coverage-report.json", run_id=self.RUN)
        return rep


# --------------------------------------------------------------------------
# scripted Worker B decisions
# --------------------------------------------------------------------------

def ignore_zip(item, a_result, attempt):
    return {"decision": "configure_ignore", "wiki_refs": ["KB-001"],
            "proposed_change": {"op": "add-ignore-file", "rule_id": "archives",
                                "pattern": r"\.(zip|gz)$", "reason": "no log content"}}


def always_escalate(item, a_result, attempt):
    return {"decision": "escalate", "wiki_refs": [],
            "escalation": {"question": "?", "options": [], "what_i_checked": "-"}}


class TestHappyPath(MiniLoop):

    def test_undetected_file_is_ignored_and_closed_with_evidence(self):
        self.start_run({"artifacts/b1/out.zip": b"\x00bin",
                        "app/app-2026-08-01.log": "2026-08-01 ERROR x\n"})
        r = self.tick(ignore_zip)
        self.assertEqual(r["outcome"], "DONE")
        self.assertEqual(r["resolution"], "configured_ignore")
        self.assertEqual(r["attempts"], 2, "A -> B -> C -> A is two A runs")

        it = self.item(r["item"], self.RUN)
        self.assertEqual(it["status"], "done")
        evidence = Path(it["evidence"])
        self.assertTrue(evidence.is_file(), "evidence must be a real report file")
        self.assertEqual(jload(evidence)["totals"]["files_undetected"], 0,
                         "the evidence report must actually show it covered")

    def test_undetected_file_is_parsed_and_closed(self):
        self.start_run({"db/db-audit-2026-08-01.log":
                        "2026-08-01 08:00:00 [DB-ERR] ORA-00060 deadlock\n"})

        def parse_db(item, a_result, attempt):
            return {"decision": "configure_parse", "wiki_refs": ["KB-004"],
                    "proposed_change": {
                        "op": "add-file-rule", "rule_id": "db-audit",
                        "pattern": r"(^|/)db-audit-\d{4}-\d{2}-\d{2}\.log$",
                        "line_patterns": [r"\[DB-ERR\]"]}}

        r = self.tick(parse_db)
        self.assertEqual(r["outcome"], "DONE")
        self.assertEqual(r["resolution"], "configured_parse")

    def test_unmatched_line_gets_a_line_pattern(self):
        self.start_run({"app/app-2026-08-01.log":
                        "2026-08-01 ERROR real\n2026-08-01 AUDIT user=jad action=export\n"})

        def parse_audit(item, a_result, attempt):
            self.assertEqual(item["kind"], "unmatched_lines")
            return {"decision": "configure_parse", "wiki_refs": ["KB-003"],
                    "proposed_change": {"op": "add-line-pattern", "rule_id": "app-log",
                                        "pattern": r"\bAUDIT\b"}}

        r = self.tick(parse_audit)
        self.assertEqual(r["outcome"], "DONE")
        self.assertEqual(r["resolution"], "configured_parse")

    def test_noise_line_gets_an_ignore_rule(self):
        self.start_run({"app/app-2026-08-01.log":
                        "2026-08-01 ERROR real\n"
                        "[GC (Allocation Failure) 524288K->131072K(2097152K), 0.04 secs]\n"})

        def ignore_gc(item, a_result, attempt):
            return {"decision": "configure_ignore", "wiki_refs": ["KB-002"],
                    "proposed_change": {"op": "add-ignore-line", "rule_id": "gc-noise",
                                        "pattern": r"^\s*\[(Full )?GC ",
                                        "file_scope": ".*", "reason": "GC is not an error"}}

        r = self.tick(ignore_gc)
        self.assertEqual(r["outcome"], "DONE")
        self.assertEqual(r["resolution"], "configured_ignore")


class TestAlreadyCovered(MiniLoop):
    """An item resolved on attempt 1, before any change was made *for it*, was
    fixed by an earlier item's rule. It must close as `already_covered` and not
    be credited as new work -- and it must cost one scan, not a full mini-loop."""

    def test_item_covered_by_a_previous_rule_closes_for_free(self):
        self.start_run({"artifacts/b1/out.zip": b"\x00bin",
                        "artifacts/b2/out.tar": b"\x00bin"})
        # A rule added earlier in the run covers both shapes.
        config_edit("--config", str(self.config), "--backup-dir", str(self.backups),
                    "add-ignore-file", "--rule-id", "arch",
                    "--pattern", r"\.(zip|tar)$", "--reason", "x")

        def should_not_be_called(item, a_result, attempt):
            raise AssertionError("Worker B must not be consulted for a covered item")

        r = self.tick(should_not_be_called)
        self.assertEqual(r["outcome"], "DONE")
        self.assertEqual(r["resolution"], "already_covered")
        self.assertEqual(r["attempts"], 1, "one A run and nothing else")


class TestRetryAndEscalation(MiniLoop):

    def test_a_wrong_pattern_is_retried_with_a_better_one(self):
        """The single most common real failure: attempt 1's regex looks right
        and matches nothing. The loop must come back with a different one."""
        self.start_run({"legacy/old_app.log.1":
                        "01-Aug-2026 07:11:04 SEVERE LegacyBatch aborted\n"})
        seen = []

        def decide(item, a_result, attempt):
            seen.append(attempt)
            if attempt == 1:
                pattern = r"(^|/)old_app\.log$"        # misses the .1 rotation
            else:
                self.assertEqual(a_result["verdict"], "still_uncovered")
                pattern = r"(^|/)old_app\.log(\.\d+)?$"
            return {"decision": "configure_parse", "wiki_refs": ["KB-008"],
                    "proposed_change": {"op": "add-file-rule",
                                        "rule_id": f"legacy-{attempt}",
                                        "pattern": pattern,
                                        "line_patterns": [r"\bSEVERE\b"]}}

        r = self.tick(decide)
        self.assertEqual(r["outcome"], "DONE")
        self.assertEqual(seen, [1, 2], "Worker B must be re-consulted on the retry")
        self.assertEqual(r["attempts"], 3)

    def test_three_failed_attempts_escalate(self):
        self.start_run({"traces/trace-a1.json": '{"a":1}\n'})
        calls = []

        def useless(item, a_result, attempt):
            calls.append(attempt)
            return {"decision": "configure_ignore", "wiki_refs": [],
                    "proposed_change": {"op": "add-ignore-file",
                                        "rule_id": f"nope-{attempt}",
                                        "pattern": rf"(^|/)never-matches-{attempt}$",
                                        "reason": "x"}}

        r = self.tick(useless)
        self.assertEqual(r["outcome"], "ESCALATED")
        self.assertEqual(r["reason"], "attempts")
        self.assertEqual(calls, [1, 2, 3], "exactly three attempts, then a human")
        it = self.item(r["item"], self.RUN)
        self.assertEqual(it["status"], "escalated")
        self.assertEqual(it["attempts"], 4, "the 4th call is what trips the cap")

    def test_worker_b_escalation_stops_the_loop_immediately(self):
        self.start_run({"traces/trace-a1.json": '{"a":1}\n'})
        r = self.tick(always_escalate)
        self.assertEqual(r["outcome"], "ESCALATED")
        self.assertEqual(r["reason"], "worker-b")
        it = self.item(r["item"], self.RUN)
        self.assertEqual(it["attempts"], 1, "escalating must not burn the retry budget")
        self.assertIsNotNone(it["decision"], "the decision must be kept for the human")

    def test_a_rejected_config_edit_does_not_close_the_item(self):
        """Worker C red-lights an invalid regex. The item must stay open and be
        retried, never closed on the strength of an edit that did not land."""
        self.start_run({"traces/trace-a1.json": '{"a":1}\n'})
        before = self.config.read_bytes()

        def bad_then_good(item, a_result, attempt):
            pattern = r"(unclosed[" if attempt == 1 else r"(^|/)traces/"
            return {"decision": "configure_ignore", "wiki_refs": ["KB-005"],
                    "proposed_change": {"op": "add-ignore-file",
                                        "rule_id": f"traces-{attempt}",
                                        "pattern": pattern, "reason": "x"}}

        r = self.tick(bad_then_good)
        self.assertEqual(r["outcome"], "DONE")
        self.assertNotEqual(self.config.read_bytes(), before)
        self.assertNotIn("unclosed", self.config.read_text(),
                         "the rolled-back edit must not be in the config")


class TestSweepCascade(MiniLoop):
    """One rule closes many items. Without the sweep the loop pays a full
    three-agent mini-loop for findings that were already fixed."""

    def test_one_rule_closes_every_sibling_item(self):
        self.start_run({
            "artifacts/b1/out.zip": b"\x00bin",
            "artifacts/b2/out.gz": b"\x00bin",
            "artifacts/b3/bundle.tar": b"\x00bin",
            "traces/trace-a1.json": '{"a":1}\n',
        })
        open_before = len([i for i in self.read_ledger(self.RUN)["items"]
                           if i["status"] == "pending"])
        self.assertGreaterEqual(open_before, 4)

        config_edit("--config", str(self.config), "--backup-dir", str(self.backups),
                    "add-ignore-file", "--rule-id", "arch",
                    "--pattern", r"\.(zip|gz|tar)$", "--reason", "x")
        r = self.sweep()

        self.assertEqual(r["closed"], 3, "all three archive items close at once")
        for it in self.read_ledger(self.RUN)["items"]:
            if "trace" in it["signature"]:
                self.assertEqual(it["status"], "pending")
            else:
                self.assertEqual(it["status"], "done")
                self.assertEqual(it["resolution"], "swept")


class TestConvergence(MiniLoop):
    """The goal: every item parsed, ignored or escalated, and the coverage
    numbers actually moved. This is the whole system in one test."""

    LOGS = {
        "app/server-01/app-2026-08-01.log":
            "2026-08-01 09:03:17 ERROR OrderService failed\n"
            "[GC (Allocation Failure) 524288K->131072K(2097152K), 0.04 secs]\n"
            "2026-08-01 09:09:12 AUDIT user=jad action=export\n",
        "app/server-02/app-2026-08-02.log":
            "2026-08-02 11:22:09 FATAL pool exhausted\n"
            "[GC (Metadata GC Threshold) 812340K->221004K(2097152K), 0.05 secs]\n",
        "db/db-audit-2026-08-01.log": "2026-08-01 08:00:00 [DB-ERR] ORA-00060\n",
        "artifacts/build-4471/output.zip": b"\x00bin",
        "artifacts/build-4472/output.zip": b"\x00bin",
        "artifacts/build-4471/timings.csv": "stage,ms\ncompile,41221\n",
        "traces/trace-a1b2.json": '{"traceId":"a1b2"}\n',
    }

    def decide(self, item, a_result, attempt):
        sig, text = item["signature"], (item.get("example_text") or "")
        if item["kind"] == "undetected_file":
            if sig.endswith(".zip"):
                return self._ignore("arch", r"\.zip$", "archives")
            if sig.endswith(".csv"):
                return self._ignore("timings", r"(^|/)timings\.csv$", "build telemetry")
            if "db-audit" in sig:
                return {"decision": "configure_parse", "wiki_refs": ["KB-004"],
                        "proposed_change": {
                            "op": "add-file-rule", "rule_id": "db-audit",
                            "pattern": r"(^|/)db-audit-\d{4}-\d{2}-\d{2}\.log$",
                            "line_patterns": [r"\[DB-ERR\]"]}}
            return always_escalate(item, a_result, attempt)   # traces: wiki unresolved
        if text.lstrip().startswith("[GC") or "[GC" in sig:
            return {"decision": "configure_ignore", "wiki_refs": ["KB-002"],
                    "proposed_change": {"op": "add-ignore-line", "rule_id": "gc-noise",
                                        "pattern": r"^\s*\[(Full )?GC ", "file_scope": ".*",
                                        "reason": "GC is not an error"}}
        if "AUDIT" in text:
            return {"decision": "configure_parse", "wiki_refs": ["KB-003"],
                    "proposed_change": {"op": "add-line-pattern", "rule_id": "app-log",
                                        "pattern": r"\bAUDIT\b"}}
        return always_escalate(item, a_result, attempt)

    @staticmethod
    def _ignore(rule_id, pattern, reason):
        return {"decision": "configure_ignore", "wiki_refs": ["KB-001"],
                "proposed_change": {"op": "add-ignore-file", "rule_id": rule_id,
                                    "pattern": pattern, "reason": reason}}

    def test_the_run_converges_and_coverage_improves(self):
        initial = self.start_run(self.LOGS)
        self.assertGreater(initial["totals"]["files_undetected"], 0)
        self.assertGreater(initial["totals"]["lines_unmatched"], 0)

        outcomes = self.run_to_completion(self.decide)
        self.assertEqual(outcomes[-1]["outcome"], "NO_PENDING",
                         f"the loop did not drain the queue: {outcomes}")

        status = jout(ledger("status", "--json", "--run-id", self.RUN, root=self.root))
        self.assertTrue(status["goal_met"],
                        f"items left open: {status['counts']}")
        self.assertEqual(status["counts"]["pending"], 0)
        self.assertEqual(status["counts"]["in_progress"], 0)

        out = self.root / "runs" / self.RUN / "scans" / "final"
        scan(self.logs, self.config, out, label="final")
        final = jload(out / "coverage-report.json")

        self.assertLess(final["totals"]["files_undetected"],
                        initial["totals"]["files_undetected"],
                        "the run must leave fewer invisible files than it found")
        self.assertLess(final["totals"]["lines_unmatched"],
                        initial["totals"]["lines_unmatched"])

        # Everything still open is a human decision, and it is written down.
        for it in self.read_ledger(self.RUN)["items"]:
            self.assertIn(it["status"], ("done", "escalated"))
            if it["status"] == "escalated":
                self.assertIsNotNone(it["resolution"])

    def test_a_mid_run_sweep_closes_items_the_loop_would_have_reworked(self):
        """Clustering already collapses same-shape rows into one item, so the
        sweep earns its keep on a rule that spans *different* shapes: one
        archive rule closes the .gz and .tar items too, and neither ever costs
        a mini-loop."""
        self.start_run({
            "artifacts/build-4471/output.zip": b"\x00bin",
            "artifacts/build-4472/output.zip": b"\x00bin",
            "backup/nightly.gz": b"\x00bin",
            "export/bundle.tar": b"\x00bin",
        })
        shapes = {i["signature"] for i in self.read_ledger(self.RUN)["items"]}
        self.assertEqual(len(shapes), 3, f"expected three distinct shapes: {shapes}")

        def broad_archive_rule(item, a_result, attempt):
            return self._ignore("arch", r"\.(zip|gz|tar)$", "archives")

        r = self.tick(broad_archive_rule)
        self.assertEqual(r["outcome"], "DONE")

        closed = self.sweep()["closed"]
        self.assertEqual(closed, 2,
                         "the .gz and .tar items must close without a mini-loop")
        self.assertTrue(jout(ledger("status", "--json", "--run-id", self.RUN,
                                    root=self.root))["goal_met"])

    def test_config_stays_valid_through_the_whole_run(self):
        self.start_run(self.LOGS)
        self.run_to_completion(self.decide)
        p = config_edit("--config", str(self.config), "validate")
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_the_logs_folder_is_never_modified(self):
        self.start_run(self.LOGS)
        before = {p.relative_to(self.logs).as_posix(): p.read_bytes()
                  for p in self.logs.rglob("*") if p.is_file()}
        self.run_to_completion(self.decide)
        after = {p.relative_to(self.logs).as_posix(): p.read_bytes()
                 for p in self.logs.rglob("*") if p.is_file()}
        self.assertEqual(before, after,
                         "the source logs folder must come out byte-identical")

    def test_every_ignore_rule_written_carries_a_reason(self):
        """The audit trail for why a log is not monitored. Without it, in six
        months nobody can tell a deliberate decision from an accident."""
        self.start_run(self.LOGS)
        self.run_to_completion(self.decide)
        import yaml
        cfg = yaml.safe_load(self.config.read_text())
        for section in ("files", "lines"):
            for rule in (cfg.get("ignore") or {}).get(section) or []:
                self.assertTrue(rule.get("reason"),
                                f"ignore.{section} rule {rule.get('id')} has no reason")


class TestResume(MiniLoop):
    """A session dies mid-tick. Nothing may be lost, and nothing may be
    double-worked."""

    def test_an_interrupted_item_is_reclaimed_and_completes(self):
        self.start_run({"artifacts/b1/out.zip": b"\x00bin"})
        item = jout(ledger("next", "--run-id", self.RUN, root=self.root))
        ledger("attempt", "--item-id", item["id"], "--run-id", self.RUN, root=self.root)
        self.assertEqual(self.item(item["id"], self.RUN)["status"], "in_progress")

        # /coverage-start reclaims it after the interrupted session.
        ledger("update", "--item-id", item["id"], "--status", "pending",
               "--actor", "lead", "--note", "reclaimed", "--run-id", self.RUN,
               root=self.root)
        r = self.tick(ignore_zip)
        self.assertEqual(r["outcome"], "DONE")
        it = self.item(item["id"], self.RUN)
        self.assertGreater(it["attempts"], 1,
                           "the earlier attempt still counts against the cap")
        self.assertTrue(any(h["detail"] == "reclaimed" for h in it["history"]),
                        "the interruption must stay in the history")

    def test_ledger_survives_being_read_between_every_step(self):
        self.start_run({"artifacts/b1/out.zip": b"\x00bin",
                        "traces/t.json": "{}\n"})
        for _ in range(3):
            self.tick(always_escalate)
            json.loads((self.root / "runs" / self.RUN / "ledger.json").read_text())


if __name__ == "__main__":
    unittest.main()
