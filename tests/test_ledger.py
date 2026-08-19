"""The ledger is the run's memory. If it lies, the loop lies.

What can go wrong here, in rough order of how badly it hurts:

  * item ids drift  -> sweep stops matching, every item is reworked forever
  * clustering changes -> item counts move, work explodes or collapses silently
  * an item closes without evidence -> a coverage gap is quietly marked handled
  * a claim is not atomic -> two ticks work the same item
  * a guardrail stops firing -> the loop runs forever
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

from helpers import PROJECT, TOOLS, ToolTestCase, jout, ledger


class TestClustering(ToolTestCase):
    """Report rows -> work items. This is the step that decides whether a
    40,000-row report is 40,000 tasks or 30."""

    def _report_with(self, files):
        self.given_logs(files)
        self.given_config()
        _, report = self.scan_to()
        return report

    def test_same_shape_collapses_into_one_item(self):
        """The same log statement in three files, with different timestamps,
        thread ids and values. One config rule fixes all three, so it is one
        item -- otherwise the loop pays three full mini-loops for one decision."""
        self._report_with({
            "app/s1/app-2026-08-01.log":
                "2026-08-01 09:00:00,113 WARN [pool-2] Retry 1/3 for inventory-svc\n",
            "app/s2/app-2026-08-02.log":
                "2026-08-02 10:41:52,004 WARN [pool-9] Retry 2/3 for inventory-svc\n",
            "app/s3/app-2026-08-03.log":
                "2026-08-03 23:59:59,900 WARN [pool-11] Retry 3/3 for inventory-svc\n",
        })
        self.init_ledger(self.tmp / "scan" / "coverage-report.json")
        lines = self.items_of_kind("unmatched_lines")
        self.assertEqual(len(lines), 1,
                         f"one statement must be one item, got: "
                         f"{[i['signature'] for i in lines]}")
        self.assertEqual(lines[0]["occurrences"], 3)
        self.assertEqual(lines[0]["affected_file_count"], 3)

    def test_numbers_with_unit_suffixes_normalise(self):
        """Regression: `\\b\\d+\\b` never matches 524288K, because there is no
        word boundary between a digit and a letter. Log numbers almost always
        carry a unit, so a \\b-anchored rule fragments GC lines, timing lines
        and byte counts into one item each."""
        self._report_with({
            "app/s1/app-2026-08-01.log": (
                "[GC (Allocation Failure) 524288K->131072K(2097152K), 0.0421 secs]\n"
                "[GC (Allocation Failure) 913001K->240118K(2097152K), 0.0512 secs]\n"
                "[GC (Allocation Failure) 44K->12K(2097152K), 1.9 secs]\n"),
        })
        self.init_ledger(self.tmp / "scan" / "coverage-report.json")
        lines = self.items_of_kind("unmatched_lines")
        self.assertEqual(len(lines), 1,
                         f"GC lines differ only in sizes and must be one item, got: "
                         f"{[i['signature'] for i in lines]}")
        self.assertEqual(lines[0]["occurrences"], 3)

    def test_different_shapes_stay_separate(self):
        """Under-clustering only costs ticks; over-clustering applies one
        decision to genuinely different logs. The normaliser must not collapse
        different statements."""
        self._report_with({
            "app/s1/app-2026-08-01.log":
                "2026-08-01 09:00:00 WEIRD one\n2026-08-01 09:00:01 AUDIT user=x\n",
        })
        self.init_ledger(self.tmp / "scan" / "coverage-report.json")
        self.assertEqual(len(self.items_of_kind("unmatched_lines")), 2)

    def test_undetected_files_cluster_by_path_shape(self):
        self._report_with({
            "artifacts/build-4471/out.zip": b"\x00bin",
            "artifacts/build-4472/out.zip": b"\x00bin",
            "artifacts/build-4473/out.zip": b"\x00bin",
            "traces/trace-a1.json": '{"a":1}\n',
        })
        self.init_ledger(self.tmp / "scan" / "coverage-report.json")
        files = self.items_of_kind("undetected_file")
        sigs = sorted(i["signature"] for i in files)
        self.assertIn("artifacts/build-<N>/out.zip", sigs)
        zip_item = next(i for i in files if i["signature"].endswith("out.zip"))
        self.assertEqual(zip_item["occurrences"], 3)

    def test_granularity_line_does_not_cluster(self):
        self._report_with({
            "app/s1/app-2026-08-01.log": "2026-08-01 09:00:00 WEIRD one\n",
            "app/s2/app-2026-08-02.log": "2026-08-02 10:00:00 WEIRD two\n",
        })
        self.init_ledger(self.tmp / "scan" / "coverage-report.json",
                         "--granularity", "line")
        self.assertEqual(len(self.items_of_kind("unmatched_lines")), 2,
                         "--granularity line must produce one item per report row")

    def test_items_ordered_by_blast_radius(self):
        self._report_with({
            "app/s1/app-2026-08-01.log":
                "2026-08-01 09:00:00 RARE x\n" + "2026-08-01 09:00:01 COMMON y\n" * 5,
        })
        self.init_ledger(self.tmp / "scan" / "coverage-report.json")
        items = self.read_ledger()["items"]
        occ = [i["occurrences"] for i in items]
        self.assertEqual(occ, sorted(occ, reverse=True),
                         "highest-occurrence items must be worked first")

    def test_ignored_files_produce_no_items(self):
        self.given_logs({"artifacts/out.zip": b"\x00bin",
                         "app/s1/app-2026-08-01.log": "2026-08-01 ERROR boom\n"})
        self.given_config(ignore_files=[("archives", r"\.zip$", "archives")])
        _, _ = self.scan_to()
        self.init_ledger(self.tmp / "scan" / "coverage-report.json")
        self.assertEqual(len(self.read_ledger()["items"]), 0,
                         "a deliberately ignored file is finished work, not a task")


class TestSignatureStability(unittest.TestCase):
    """Item ids are derived from signatures, and `sweep` matches items across
    two independent scans by id. If normalisation changes, every id changes and
    the sweep silently stops closing anything."""

    def setUp(self):
        sys.path.insert(0, str(TOOLS))
        import ledger as mod           # noqa: E402
        self.mod = mod

    def test_line_signature_normalises_variable_parts(self):
        sig = self.mod.line_signature
        self.assertEqual(
            sig("2026-08-01 09:03:17,882 ERROR [http-7] order 88213 failed"),
            sig("2026-11-30 23:59:59,001 ERROR [http-2] order 41 failed"),
            "timestamps, thread numbers and ids must normalise to one shape")

    def test_signature_keeps_the_identifying_token(self):
        sig = self.mod.line_signature
        self.assertNotEqual(sig("2026-08-01 09:00:00 [DB-ERR] deadlock"),
                            sig("2026-08-01 09:00:00 [DB-OK ] backup done"),
                            "different markers must not collapse into one item")

    def test_uuid_and_hex_normalise(self):
        sig = self.mod.line_signature
        self.assertEqual(
            sig("trace 3f2a1b4c-1111-2222-3333-444455556666 done"),
            sig("trace 99999999-aaaa-bbbb-cccc-dddddddddddd done"))
        self.assertEqual(sig("addr 0xdeadbeef"), sig("addr 0x1234abcd"))

    def test_path_signature_normalises_dates_and_numbers(self):
        psig = self.mod.path_signature
        self.assertEqual(psig("app/server-01/app-2026-08-01.log"),
                         psig("app/server-14/app-2026-11-30.log"))
        self.assertNotEqual(psig("app/server-01/app-2026-08-01.log"),
                            psig("db/server-01/app-2026-08-01.log"))

    def test_item_id_is_a_pure_function_of_kind_and_signature(self):
        a = self.mod.item_id("unmatched_lines", "<TS> ERROR thing")
        b = self.mod.item_id("unmatched_lines", "<TS> ERROR thing")
        c = self.mod.item_id("undetected_file", "<TS> ERROR thing")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c, "kind must be part of the identity")
        self.assertEqual(len(a), 12)

    def test_known_signatures_are_frozen(self):
        """Golden values. If these change, ids change, and every in-flight run
        loses its sweep matching. Changing them is allowed -- but it is a
        breaking change and this test is where you acknowledge that."""
        sig = self.mod.line_signature
        self.assertEqual(
            sig("[GC (Allocation Failure) 524288K->131072K(2097152K), 0.0421 secs]"),
            "[GC (Allocation Failure) <N>K-><N>K(<N>K), <N> secs]")
        self.assertEqual(
            sig("2026-08-01 09:03:17,882 ERROR [http-7] order 88213 failed in 1240ms"),
            "<TS> ERROR [http-<N>] order <N> failed in <N>ms")
        self.assertEqual(
            self.mod.path_signature("artifacts/build-4471/output.zip"),
            "artifacts/build-<N>/output.zip")
        self.assertEqual(
            self.mod.path_signature("app/server-01/app-2026-08-01.log"),
            "app/server-<N>/app-<DATE>.log")


class TestClaimAndAttempts(ToolTestCase):

    # Distinct WORDS, not numbered tokens: the normaliser correctly collapses
    # SHAPE1/SHAPE2/SHAPE3 into one item, which would make these tests seed
    # fewer items than they ask for.
    SHAPES = ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT",
              "GOLF", "HOTEL"]

    def _seed(self, n=3):
        assert n <= len(self.SHAPES)
        lines = "".join(f"2026-08-01 09:00:00 {s} happened\n" for s in self.SHAPES[:n])
        self.given_logs({"app/s1/app-2026-08-01.log": lines})
        self.given_config()
        self.scan_to()
        self.init_ledger(self.tmp / "scan" / "coverage-report.json")
        self.assertEqual(len(self.read_ledger()["items"]), n,
                         "fixture must seed exactly n distinct items")

    def test_next_claims_and_marks_in_progress(self):
        self._seed()
        item = jout(ledger("next", "--run-id", "testrun", root=self.root))
        self.assertEqual(item["status"], "in_progress")
        self.assertIsNotNone(item["claimed_at"])
        self.assertEqual(self.item(item["id"])["status"], "in_progress")

    def test_next_never_hands_out_the_same_item_twice(self):
        self._seed(3)
        ids = []
        for _ in range(3):
            ids.append(jout(ledger("next", "--run-id", "testrun", root=self.root))["id"])
        self.assertEqual(len(set(ids)), 3, "each tick must get a distinct item")

    def test_next_exits_3_when_nothing_pending(self):
        self._seed(1)
        ledger("next", "--run-id", "testrun", root=self.root)
        p = ledger("next", "--run-id", "testrun", root=self.root)
        self.assertEqual(p.returncode, 3, "exhausted queue must exit 3, not 0")

    def test_concurrent_claims_do_not_collide(self):
        """Two ticks racing must not both get the same item -- that is double
        work, two config edits for one finding, and a corrupted attempt count."""
        self._seed(6)

        def claim():
            return ledger("next", "--run-id", "testrun", root=self.root)

        with ThreadPoolExecutor(max_workers=6) as ex:
            procs = list(ex.map(lambda _: claim(), range(6)))
        ids = [json.loads(p.stdout)["id"] for p in procs if p.returncode == 0]
        self.assertEqual(len(ids), len(set(ids)),
                         f"the lock let an item be claimed twice: {ids}")

    def test_attempts_increment_and_escalate_on_the_fourth(self):
        self._seed(1)
        item_id = jout(ledger("next", "--run-id", "testrun", root=self.root))["id"]
        for n in (1, 2, 3):
            r = jout(ledger("attempt", "--item-id", item_id, "--run-id", "testrun",
                            root=self.root))
            self.assertEqual(r["attempts"], n)
            self.assertFalse(r["exhausted"], f"attempt {n} of 3 must not be exhausted")
            self.assertEqual(r["status"], "in_progress")
        r = jout(ledger("attempt", "--item-id", item_id, "--run-id", "testrun",
                        root=self.root))
        self.assertTrue(r["exhausted"])
        self.assertEqual(r["status"], "escalated",
                         "past the cap the ledger must escalate by itself")
        self.assertEqual(self.item(item_id)["resolution"], "escalated")

    def test_attempt_cap_is_configurable_but_enforced(self):
        self.given_logs({"app/s1/app-2026-08-01.log": "2026-08-01 09:00:00 X y\n"})
        self.given_config()
        self.scan_to()
        self.init_ledger(self.tmp / "scan" / "coverage-report.json",
                         "--max-attempts", "1")
        item_id = jout(ledger("next", "--run-id", "testrun", root=self.root))["id"]
        jout(ledger("attempt", "--item-id", item_id, "--run-id", "testrun", root=self.root))
        r = jout(ledger("attempt", "--item-id", item_id, "--run-id", "testrun", root=self.root))
        self.assertTrue(r["exhausted"])


class TestClosingRules(ToolTestCase):
    """The single most important invariant: nothing closes without a recorded
    reason. 'Done, for reasons I did not write down' is how a real coverage gap
    gets quietly marked handled."""

    def setUp(self):
        super().setUp()
        self.given_logs({"app/s1/app-2026-08-01.log": "2026-08-01 09:00:00 X y\n"})
        self.given_config()
        self.scan_to()
        self.init_ledger(self.tmp / "scan" / "coverage-report.json")
        self.iid = jout(ledger("next", "--run-id", "testrun", root=self.root))["id"]

    def test_done_without_resolution_is_refused(self):
        p = ledger("update", "--item-id", self.iid, "--status", "done",
                   "--run-id", "testrun", root=self.root)
        self.assertEqual(p.returncode, 2)
        self.assertIn("resolution", p.stderr)
        self.assertNotEqual(self.item(self.iid)["status"], "done",
                            "a refused update must not have changed anything")

    def test_unknown_status_is_refused(self):
        p = ledger("update", "--item-id", self.iid, "--status", "finished",
                   "--run-id", "testrun", root=self.root)
        self.assertEqual(p.returncode, 2)

    def test_unknown_resolution_is_refused(self):
        p = ledger("update", "--item-id", self.iid, "--status", "done",
                   "--resolution", "looks-fine", "--run-id", "testrun", root=self.root)
        self.assertEqual(p.returncode, 2)

    def test_unknown_item_is_refused(self):
        p = ledger("update", "--item-id", "deadbeefdead", "--status", "done",
                   "--resolution", "swept", "--run-id", "testrun", root=self.root)
        self.assertEqual(p.returncode, 2)

    def test_valid_close_records_resolution_evidence_and_history(self):
        p = ledger("update", "--item-id", self.iid, "--status", "done",
                   "--resolution", "configured_parse",
                   "--evidence", "runs/testrun/items/x/report/coverage-report.json",
                   "--note", "covered on attempt 2", "--actor", "orchestrator",
                   "--run-id", "testrun", root=self.root)
        self.assertEqual(p.returncode, 0, p.stderr)
        it = self.item(self.iid)
        self.assertEqual(it["status"], "done")
        self.assertEqual(it["resolution"], "configured_parse")
        self.assertTrue(it["evidence"].endswith("coverage-report.json"))
        self.assertTrue(any("status=done" in h["event"] for h in it["history"]))

    def test_reclaiming_an_interrupted_item_is_allowed(self):
        """/coverage-start resets in_progress items after a killed session."""
        p = ledger("update", "--item-id", self.iid, "--status", "pending",
                   "--actor", "lead", "--note", "reclaimed",
                   "--run-id", "testrun", root=self.root)
        self.assertEqual(p.returncode, 0, p.stderr)
        again = jout(ledger("next", "--run-id", "testrun", root=self.root))
        self.assertEqual(again["id"], self.iid)


class TestSweep(ToolTestCase):
    """The sweep closes items a later full scan shows are already covered. It
    must close what is gone, keep what remains, and never resurrect anything."""

    def setUp(self):
        super().setUp()
        self.given_logs({
            "artifacts/b1/out.zip": b"\x00bin",
            "artifacts/b2/out.zip": b"\x00bin",
            "traces/trace-a1.json": '{"a":1}\n',
        })
        self.given_config()
        self.scan_to("scan1")
        self.init_ledger(self.tmp / "scan1" / "coverage-report.json")

    def _rescan_with_zip_ignored(self, name="scan2"):
        self.given_config(ignore_files=[("archives", r"\.zip$", "archives")])
        out, _ = self.scan_to(name)
        return out / "coverage-report.json"

    def test_sweep_closes_items_the_new_report_no_longer_shows(self):
        before = {i["signature"]: i["status"] for i in self.read_ledger()["items"]}
        self.assertEqual(set(before.values()), {"pending"})
        r = jout(ledger("sweep", "--report", self._rescan_with_zip_ignored(),
                        "--run-id", "testrun", root=self.root))
        self.assertEqual(r["closed"], 1)
        zip_item = next(i for i in self.read_ledger()["items"]
                        if "zip" in i["signature"])
        self.assertEqual(zip_item["status"], "done")
        self.assertEqual(zip_item["resolution"], "swept")

    def test_sweep_leaves_still_uncovered_items_alone(self):
        ledger("sweep", "--report", self._rescan_with_zip_ignored(),
               "--run-id", "testrun", root=self.root)
        trace = next(i for i in self.read_ledger()["items"]
                     if "trace" in i["signature"])
        self.assertEqual(trace["status"], "pending",
                         "an item still in the report must stay open")

    def test_sweep_does_not_reopen_or_relabel_terminal_items(self):
        iid = next(i["id"] for i in self.read_ledger()["items"] if "trace" in i["signature"])
        ledger("update", "--item-id", iid, "--status", "escalated",
               "--resolution", "escalated", "--run-id", "testrun", root=self.root)
        ledger("sweep", "--report", self._rescan_with_zip_ignored(),
               "--run-id", "testrun", root=self.root)
        it = self.item(iid)
        self.assertEqual(it["status"], "escalated",
                         "sweep must never touch an item a human owns")

    def test_sweep_is_recorded_for_the_status_report(self):
        ledger("sweep", "--report", self._rescan_with_zip_ignored(),
               "--run-id", "testrun", root=self.root)
        sweeps = self.read_ledger()["sweeps"]
        self.assertEqual(len(sweeps), 1)
        self.assertIn("totals", sweeps[0])
        self.assertIn("files_undetected", sweeps[0]["totals"])

    def test_sweep_with_an_unchanged_report_closes_nothing(self):
        r = jout(ledger("sweep", "--report", self.tmp / "scan1" / "coverage-report.json",
                        "--run-id", "testrun", root=self.root))
        self.assertEqual(r["closed"], 0,
                         "no config change means no item may close")


class TestGuardrailsAndState(ToolTestCase):

    def setUp(self):
        super().setUp()
        self.given_logs({"app/s1/app-2026-08-01.log": "2026-08-01 09:00:00 X y\n"})
        self.given_config()
        self.scan_to()

    def test_tick_ceiling_trips(self):
        self.init_ledger(self.tmp / "scan" / "coverage-report.json", "--max-ticks", "2")
        for i in (1, 2):
            p = ledger("tick", "--run-id", "testrun", root=self.root)
            self.assertEqual(p.returncode, 0, f"tick {i} should be allowed")
        p = ledger("tick", "--run-id", "testrun", root=self.root)
        self.assertEqual(p.returncode, 4, "past max_ticks must exit 4")
        self.assertTrue(json.loads(p.stdout)["tripped"])

    def test_current_pointer_lets_commands_omit_run_id(self):
        self.init_ledger(self.tmp / "scan" / "coverage-report.json", run_id="pointed")
        self.assertEqual((self.root / "runs" / "CURRENT").read_text().strip(), "pointed")
        p = ledger("status", "--json", root=self.root)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(jout(p)["run_id"], "pointed")

    def test_missing_current_pointer_is_a_clear_error(self):
        p = ledger("status", root=self.root)
        self.assertEqual(p.returncode, 2)
        self.assertIn("CURRENT", p.stderr)

    def test_goal_met_only_when_nothing_is_open(self):
        self.init_ledger(self.tmp / "scan" / "coverage-report.json")
        self.assertFalse(jout(ledger("status", "--json", "--run-id", "testrun",
                                     root=self.root))["goal_met"])
        for it in self.read_ledger()["items"]:
            ledger("update", "--item-id", it["id"], "--status", "done",
                   "--resolution", "swept", "--run-id", "testrun", root=self.root)
        self.assertTrue(jout(ledger("status", "--json", "--run-id", "testrun",
                                    root=self.root))["goal_met"])

    def test_escalated_items_do_not_block_goal_met(self):
        """Escalated is a terminal state -- the loop has done all it can."""
        self.init_ledger(self.tmp / "scan" / "coverage-report.json")
        for it in self.read_ledger()["items"]:
            ledger("update", "--item-id", it["id"], "--status", "escalated",
                   "--resolution", "escalated", "--run-id", "testrun", root=self.root)
        s = jout(ledger("status", "--json", "--run-id", "testrun", root=self.root))
        self.assertTrue(s["goal_met"])
        self.assertEqual(s["counts"]["escalated"], len(self.read_ledger()["items"]))

    def test_tasks_md_mirror_is_regenerated_on_every_update(self):
        self.init_ledger(self.tmp / "scan" / "coverage-report.json")
        tasks = self.root / "runs" / "testrun" / "tasks.md"
        self.assertIn("NOT DONE", tasks.read_text())
        iid = self.read_ledger()["items"][0]["id"]
        ledger("update", "--item-id", iid, "--status", "done",
               "--resolution", "swept", "--run-id", "testrun", root=self.root)
        self.assertIn("DONE", tasks.read_text())
        self.assertIn("Do not edit", tasks.read_text())

    def test_ledger_json_stays_valid_and_leaves_no_temp_file(self):
        self.init_ledger(self.tmp / "scan" / "coverage-report.json")
        iid = self.read_ledger()["items"][0]["id"]
        for _ in range(5):
            ledger("attempt", "--item-id", iid, "--run-id", "testrun", root=self.root)
        d = self.root / "runs" / "testrun"
        json.loads((d / "ledger.json").read_text())
        self.assertFalse((d / "ledger.json.tmp").exists(),
                         "atomic write must not leave a temp file behind")
        self.assertFalse((d / ".ledger.lock").exists(),
                         "the lock must always be released")

    def test_run_directories_are_created(self):
        self.init_ledger(self.tmp / "scan" / "coverage-report.json")
        d = self.root / "runs" / "testrun"
        for sub in ("items", "escalations", "config-backups"):
            self.assertTrue((d / sub).is_dir(), f"{sub}/ must exist for the workers")

    def test_unicode_and_control_characters_survive_a_round_trip(self):
        """Real logs contain anything. A crash while rendering tasks.md would
        take down a tick for a cosmetic reason."""
        self.given_logs({"app/s1/app-2026-08-01.log":
                         "2026-08-01 09:00:00 WEIRD é中文 — `back` |pipe| <tag>\n"})
        self.scan_to("scan2")
        self.init_ledger(self.tmp / "scan2" / "coverage-report.json", run_id="uni")
        self.assertIn("WEIRD", (self.root / "runs" / "uni" / "tasks.md").read_text())


if __name__ == "__main__":
    unittest.main()
