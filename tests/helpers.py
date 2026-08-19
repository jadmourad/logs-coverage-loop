"""Shared fixtures and runners for the non-regression suite.

Everything here builds throwaway logs folders, configs and run trees in a temp
directory. No test ever touches the real `config/logs-parsing-config.yml`,
`logs-sample/` or `runs/` -- a suite that can corrupt the thing it is testing
is worse than no suite.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
TOOLS = PROJECT / "tools"


# --------------------------------------------------------------------------
# running the tools
# --------------------------------------------------------------------------

def run_tool(script: str, *args, root: Path | None = None, cwd: Path | None = None):
    """Run one of tools/*.py. `root` sets LOGS_COVERAGE_ROOT so the tool keeps
    its run tree inside the test's temp dir."""
    env = dict(os.environ)
    if root is not None:
        env["LOGS_COVERAGE_ROOT"] = str(root)
    return subprocess.run(
        [sys.executable, str(TOOLS / script), *[str(a) for a in args]],
        capture_output=True, text=True, cwd=str(cwd or PROJECT), env=env)


def ledger(*args, root: Path):
    return run_tool("ledger.py", *args, root=root)


def config_edit(*args, root: Path | None = None):
    return run_tool("config_edit.py", *args, root=root)


def isolate(*args):
    return run_tool("isolate.py", *args)


def verdict(*args):
    return run_tool("verdict.py", *args)


def scan(logs_root, config, out, max_lines: int | None = None, label: str = "test"):
    """Run the monitoring tool through the seam, exactly as the loop does."""
    args = ["--logs-root", str(logs_root), "--config", str(config),
            "--out", str(out), "--label", label]
    if max_lines is not None:
        args += ["--max-unmatched-lines", str(max_lines)]
    return subprocess.run(["bash", str(TOOLS / "standalone.sh"), *args],
                          capture_output=True, text=True, cwd=str(PROJECT))


def jload(path) -> dict:
    return json.loads(Path(path).read_text())


def jout(proc) -> dict:
    """Parse a tool's stdout as JSON, with a readable failure if it is not."""
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"expected JSON on stdout, got:\n--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}\n({e})")


# --------------------------------------------------------------------------
# building fixtures
# --------------------------------------------------------------------------

def make_logs(root: Path, files: dict[str, str | bytes]) -> Path:
    """files maps a relative path to its content. bytes content is written raw,
    which is how binary fixtures are made."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(textwrap.dedent(content).lstrip("\n"))
    return root


def make_config(path: Path, files=(), ignore_files=(), ignore_lines=(),
                anchors: bool = True, header: str = "") -> Path:
    """Build a logs-parsing-config.yml.

    files:        [(id, pattern, [line_patterns])]
    ignore_files: [(id, pattern, reason)]
    ignore_lines: [(id, file_scope, pattern, reason)]
    anchors=False produces a config with no '# @anchor:' comments, which is
    what a real production config looks like.
    """
    def q(s):
        return "'" + str(s).replace("'", "''") + "'"

    out = ["version: 1", ""]
    if header:
        out += [header, ""]

    out.append("files:")
    for rid, pattern, *rest in files:
        out.append(f"  - id: {rid}")
        out.append(f"    pattern: {q(pattern)}")
        lps = rest[0] if rest else []
        if lps:
            out.append("    line_patterns:")
            out += [f"      - {q(lp)}" for lp in lps]
    if anchors:
        out.append("  # @anchor:file-rules")

    out += ["", "ignore:", "  files:"]
    for rid, pattern, reason in ignore_files:
        out += [f"    - id: {rid}", f"      pattern: {q(pattern)}",
                f"      reason: {q(reason)}"]
    if anchors:
        out.append("    # @anchor:ignore-files")

    out.append("  lines:")
    for rid, scope, pattern, reason in ignore_lines:
        out += [f"    - id: {rid}", f"      file_scope: {q(scope)}",
                f"      pattern: {q(pattern)}", f"      reason: {q(reason)}"]
    if anchors:
        out.append("    # @anchor:ignore-lines")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n")
    return path


# A config that parses app logs, used by most tests as the starting point.
BASE_FILES = [("app-log", r"(^|/)app-\d{4}-\d{2}-\d{2}\.log$", [r"\b(ERROR|FATAL)\b"])]

# A small logs folder with one of everything the loop has to handle.
SAMPLE_LOGS = {
    "app/server-01/app-2026-08-01.log": """
        2026-08-01 09:00:01 INFO  Startup complete
        2026-08-01 09:03:17 ERROR OrderService failed to settle order 88213
        [GC (Allocation Failure) 524288K->131072K(2097152K), 0.0421 secs]
        2026-08-01 09:09:12 AUDIT user=jmourad action=export.orders
        """,
    "db/db-audit-2026-08-01.log": """
        2026-08-01 08:00:00 [DB-ERR] ORA-00060: deadlock detected
        2026-08-01 08:00:41 [DB-OK ] backup completed
        """,
    "artifacts/build-4471/output.zip": b"PK\x03\x04\x00\x00binary\x00\x00",
    "runner/heartbeat.txt": "alive 2026-08-01T09:00:00Z\n",
}


class ToolTestCase(unittest.TestCase):
    """Base class giving every test its own temp project root."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="covloop-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "root"          # LOGS_COVERAGE_ROOT
        (self.root / "runs").mkdir(parents=True)
        self.logs = self.tmp / "logs"
        self.config = self.tmp / "logs-parsing-config.yml"
        self.out = self.tmp / "out"
        self.backups = self.tmp / "backups"

    # -- convenience wrappers ------------------------------------------------

    def given_logs(self, files=None) -> Path:
        return make_logs(self.logs, files if files is not None else SAMPLE_LOGS)

    def given_config(self, **kw) -> Path:
        kw.setdefault("files", BASE_FILES)
        return make_config(self.config, **kw)

    def scan_to(self, out_name="scan", **kw):
        out = self.tmp / out_name
        proc = scan(self.logs, self.config, out, **kw)
        self.assertEqual(proc.returncode, 0,
                         f"scan failed:\n{proc.stdout}\n{proc.stderr}")
        return out, jload(out / "coverage-report.json")

    def init_ledger(self, report_path, *extra, run_id="testrun"):
        p = ledger("init", "--report", report_path, "--run-id", run_id,
                   "--logs-root", self.logs, "--config", self.config, *extra,
                   root=self.root)
        self.assertEqual(p.returncode, 0, p.stderr)
        return run_id, jout(p)

    def read_ledger(self, run_id="testrun") -> dict:
        return jload(self.root / "runs" / run_id / "ledger.json")

    def item(self, item_id, run_id="testrun") -> dict:
        for it in self.read_ledger(run_id)["items"]:
            if it["id"] == item_id:
                return it
        raise AssertionError(f"no item {item_id} in ledger")

    def items_of_kind(self, kind, run_id="testrun") -> list[dict]:
        return [i for i in self.read_ledger(run_id)["items"] if i["kind"] == kind]

    def file_entry(self, report: dict, rel: str) -> dict:
        for f in report["files"]:
            if f["path"] == rel:
                return f
        raise AssertionError(
            f"{rel!r} not in report; present: {[f['path'] for f in report['files']]}")
