#!/usr/bin/env python3
"""Check everything the loop depends on, before the loop starts.

A run that dies on tick 40 because the logs root was a typo has burned 40
ticks of the human's money and attention. Everything checkable is checked here,
loudly, in about two seconds.

  preflight.py [--logs-root DIR] [--config FILE] [--json]

Exit: 0 all clear (warnings allowed) | 1 at least one FAIL
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ["coverage-orchestrator", "worker-a-runner",
          "worker-b-assessor", "worker-c-configurator"]


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--logs-root", default=str(ROOT / "logs-sample"))
    p.add_argument("--config", default=str(ROOT / "config" / "logs-parsing-config.yml"))
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    checks: list[tuple[str, str, str]] = []   # (level, name, detail)

    def ok(n, d=""):
        checks.append(("PASS", n, d))

    def warn(n, d=""):
        checks.append(("WARN", n, d))

    def fail(n, d=""):
        checks.append(("FAIL", n, d))

    # --- runtime -----------------------------------------------------------
    ok("python", sys.version.split()[0])
    try:
        import yaml  # noqa: F401
        ok("pyyaml", "importable")
    except ImportError:
        fail("pyyaml", "not installed -- run: pip install pyyaml")

    # --- the seam ----------------------------------------------------------
    seam = ROOT / "tools" / "standalone.sh"
    if not seam.exists():
        fail("standalone.sh", "missing -- this is the only way to run the monitoring tool")
    else:
        probe = Path(tempfile.mkdtemp(prefix="preflight-"))
        (probe / "logs").mkdir()
        (probe / "logs" / "probe.txt").write_text("hello\n")
        try:
            r = subprocess.run(
                ["bash", str(seam), "--logs-root", str(probe / "logs"),
                 "--config", a.config, "--out", str(probe / "out"), "--label", "preflight"],
                capture_output=True, text=True, timeout=180, cwd=ROOT)
            if r.returncode != 0:
                fail("standalone.sh runs", (r.stderr or r.stdout).strip()[:300])
            elif not (probe / "out" / "coverage-report.json").exists():
                fail("standalone.sh contract",
                     "ran but wrote no coverage-report.json -- see docs/CONTRACTS.md")
            else:
                rep = json.loads((probe / "out" / "coverage-report.json").read_text())
                missing = [k for k in ("totals", "files", "logs_root") if k not in rep]
                if missing:
                    fail("report schema", f"missing keys: {missing}")
                else:
                    ok("standalone.sh contract",
                       f"produced by: {rep.get('produced_by', 'unknown')[:60]}")
                    if "FILLER" in str(rep.get("produced_by", "")):
                        warn("FILLER tool in use",
                             "still the fake scanner -- see docs/SWAP_IN_REAL_TOOL.md")
        except subprocess.TimeoutExpired:
            fail("standalone.sh runs", "timed out after 180s on a one-file folder")
        finally:
            shutil.rmtree(probe, ignore_errors=True)

    # --- config ------------------------------------------------------------
    cfg = Path(a.config)
    if not cfg.exists():
        fail("config", f"no config at {cfg}")
    else:
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "config_edit.py"),
                            "--config", str(cfg), "validate"],
                           capture_output=True, text=True, cwd=ROOT)
        if r.returncode == 0:
            ok("config validates", str(cfg))
        else:
            fail("config validates", r.stdout.strip()[:400])
        text = cfg.read_text()
        missing = [k for k in ("@anchor:file-rules", "@anchor:ignore-files",
                               "@anchor:ignore-lines") if k not in text]
        if missing:
            warn("config anchors", f"missing {missing} -- config_edit falls back to "
                                   "indentation detection; verify one edit by hand first")
        else:
            ok("config anchors", "all three present")
        if "FILLER" in text:
            warn("FILLER config in use", "swap in the real logs-parsing-config.yml")

    # --- logs root ---------------------------------------------------------
    logs = Path(a.logs_root)
    if not logs.is_dir():
        fail("logs root", f"not a directory: {logs}")
    else:
        n = sum(1 for _ in logs.rglob("*") if _.is_file())
        if n == 0:
            fail("logs root", f"{logs} contains no files")
        else:
            ok("logs root", f"{n} files under {logs}")
            if n > 200_000:
                warn("logs root size", f"{n} files -- the initial scan will be slow; "
                                       "consider scoping to a subfolder first")

    # --- wiki knowledge base ----------------------------------------------
    kb = ROOT / "wiki" / "KNOWLEDGE_BASE.md"
    if not kb.exists():
        fail("wiki knowledge base", "missing -- run /wiki-ingest before starting")
    else:
        body = kb.read_text()
        if "FILLER" in body:
            warn("FILLER knowledge base",
                 "still the placeholder wiki -- Worker B will escalate almost everything")
        rules = body.count("\n### ")
        (ok if rules else fail)("wiki knowledge base", f"{rules} rules")

    # --- agents ------------------------------------------------------------
    for name in AGENTS:
        f = ROOT / ".claude" / "agents" / f"{name}.md"
        (ok if f.exists() else fail)(f"agent {name}", "present" if f.exists() else "missing")

    # --- workspace trust ---------------------------------------------------
    # Until this folder is trusted, Claude Code silently ignores every entry in
    # .claude/settings.json -- so the loop's pre-approved commands are not
    # approved, and the first tick stalls on a permission prompt.
    cc = Path.home() / ".claude.json"
    if not cc.exists():
        warn("workspace trust", "no ~/.claude.json yet -- open this folder in VS Code once")
    else:
        try:
            trusted = (json.loads(cc.read_text())
                       .get("projects", {})
                       .get(str(ROOT), {})
                       .get("hasTrustDialogAccepted"))
            if trusted:
                ok("workspace trust", "settings.json permissions are active")
            else:
                fail("workspace trust",
                     f"{ROOT} is not trusted -- .claude/settings.json is being IGNORED and "
                     f"the loop will stall on its first command. Open this folder in VS Code "
                     f"and accept the trust dialog.")
        except (json.JSONDecodeError, OSError) as e:
            warn("workspace trust", f"could not read ~/.claude.json ({e.__class__.__name__})")

    # --- writable state ----------------------------------------------------
    try:
        (ROOT / "runs").mkdir(exist_ok=True)
        probe = ROOT / "runs" / ".preflight"
        probe.write_text("x")
        probe.unlink()
        ok("runs/ writable")
    except OSError as e:
        fail("runs/ writable", str(e))

    fails = [c for c in checks if c[0] == "FAIL"]
    warns = [c for c in checks if c[0] == "WARN"]

    if a.json:
        print(json.dumps({
            "ok": not fails,
            "fails": [{"check": c[1], "detail": c[2]} for c in fails],
            "warns": [{"check": c[1], "detail": c[2]} for c in warns],
            "checks": [{"level": c[0], "check": c[1], "detail": c[2]} for c in checks],
        }, indent=2))
    else:
        for level, name, detail in checks:
            mark = {"PASS": "ok  ", "WARN": "warn", "FAIL": "FAIL"}[level]
            print(f"  [{mark}] {name}" + (f" -- {detail}" if detail else ""))
        print()
        if fails:
            print(f"PREFLIGHT FAILED -- {len(fails)} blocking problem(s). Fix these before starting.")
        else:
            print(f"Preflight clear" + (f" ({len(warns)} warning(s))" if warns else "") + ".")

    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
