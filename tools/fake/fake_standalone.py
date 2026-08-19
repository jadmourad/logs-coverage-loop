#!/usr/bin/env python3
"""FILLER -- stand-in for the real monitoring tool's standalone mode.

This is a working scanner, not a stub. It genuinely walks a logs folder,
applies config/logs-parsing-config.yml, and emits the exact report contract in
docs/CONTRACTS.md. That means the whole agent loop can be built and tested
today, and swapping in the real tool is a one-line change in tools/adapter.env.

Matching order (documented in docs/CONTRACTS.md -- CHECK THE REAL TOOL'S ORDER
WHEN YOU SWAP IT IN, and update the doc if it differs):

  file level   ignore.files  ->  files[].pattern  ->  undetected
  line level   ignore.lines  ->  files[].line_patterns  ->  unmatched

Usage:
  fake_standalone.py --logs-root DIR --config FILE --out DIR
                     [--max-unmatched-lines N] [--label STR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

BINARY_SNIFF = 8192


def compile_rules(cfg: dict):
    """Compile every regex up front so a bad pattern fails loudly here rather
    than silently skipping files at scan time."""
    errors = []

    def rx(pattern, where):
        try:
            return re.compile(pattern)
        except re.error as e:
            errors.append(f"{where}: bad regex {pattern!r}: {e}")
            return None

    file_rules = []
    for i, r in enumerate(cfg.get("files") or []):
        rid = r.get("id") or f"files[{i}]"
        file_rules.append({
            "id": rid,
            "rx": rx(r.get("pattern", ""), f"files.{rid}.pattern"),
            "line_rx": [rx(p, f"files.{rid}.line_patterns") for p in (r.get("line_patterns") or [])],
        })

    ig = cfg.get("ignore") or {}
    ignore_files = []
    for i, r in enumerate(ig.get("files") or []):
        rid = r.get("id") or f"ignore.files[{i}]"
        ignore_files.append({
            "id": rid,
            "rx": rx(r.get("pattern", ""), f"ignore.files.{rid}.pattern"),
            "reason": r.get("reason", ""),
        })

    ignore_lines = []
    for i, r in enumerate(ig.get("lines") or []):
        rid = r.get("id") or f"ignore.lines[{i}]"
        ignore_lines.append({
            "id": rid,
            "scope_rx": rx(r.get("file_scope", ".*"), f"ignore.lines.{rid}.file_scope"),
            "rx": rx(r.get("pattern", ""), f"ignore.lines.{rid}.pattern"),
            "reason": r.get("reason", ""),
        })

    if errors:
        for e in errors:
            print(f"config error: {e}", file=sys.stderr)
        sys.exit(1)
    return file_rules, ignore_files, ignore_lines


def is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\x00" in fh.read(BINARY_SNIFF)
    except OSError:
        return True


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--logs-root", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--max-unmatched-lines", type=int, default=50)
    p.add_argument("--label", default="scan")
    a = p.parse_args()

    logs_root = Path(a.logs_root).resolve()
    if not logs_root.is_dir():
        print(f"no such logs root: {logs_root}", file=sys.stderr)
        sys.exit(2)

    cfg_path = Path(a.config).resolve()
    raw = cfg_path.read_bytes()
    try:
        cfg = yaml.safe_load(raw.decode()) or {}
    except yaml.YAMLError as e:
        print(f"config is not valid YAML: {e}", file=sys.stderr)
        sys.exit(1)

    file_rules, ignore_files, ignore_lines = compile_rules(cfg)

    out = Path(a.out).resolve()
    (out / "uncovered").mkdir(parents=True, exist_ok=True)

    files_out = []
    totals = dict(files_seen=0, files_detected=0, files_ignored=0, files_undetected=0,
                  lines_seen=0, lines_matched=0, lines_ignored=0, lines_unmatched=0)

    for path in sorted(logs_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(logs_root).as_posix()
        totals["files_seen"] += 1

        entry = {
            "path": rel,
            "size_bytes": path.stat().st_size,
            "status": None,
            "matched_by": None,
            "ignored_by": None,
            "binary": False,
            "lines": {"seen": 0, "matched": 0, "ignored": 0, "unmatched": 0},
            "unmatched_sample_path": None,
            "unmatched_samples": [],
        }

        hit = next((r for r in ignore_files if r["rx"] and r["rx"].search(rel)), None)
        if hit:
            entry["status"] = "ignored"
            entry["ignored_by"] = hit["id"]
            totals["files_ignored"] += 1
            files_out.append(entry)
            continue

        rule = next((r for r in file_rules if r["rx"] and r["rx"].search(rel)), None)
        if not rule:
            entry["status"] = "undetected"
            totals["files_undetected"] += 1
            files_out.append(entry)
            continue

        entry["status"] = "detected"
        entry["matched_by"] = rule["id"]
        totals["files_detected"] += 1

        if is_binary(path):
            # Detected by pattern but not line-parseable. Worth surfacing: it
            # usually means a file rule is too broad.
            entry["binary"] = True
            files_out.append(entry)
            continue

        scoped_ignores = [r for r in ignore_lines if r["scope_rx"] and r["scope_rx"].search(rel)]
        samples, raw_samples = [], []
        with path.open("r", errors="replace") as fh:
            for line_no, line in enumerate(fh, 1):
                text = line.rstrip("\n")
                entry["lines"]["seen"] += 1
                if not text.strip():
                    entry["lines"]["ignored"] += 1
                    continue
                if any(r["rx"] and r["rx"].search(text) for r in scoped_ignores):
                    entry["lines"]["ignored"] += 1
                elif any(rx and rx.search(text) for rx in rule["line_rx"]):
                    entry["lines"]["matched"] += 1
                else:
                    entry["lines"]["unmatched"] += 1
                    if len(samples) < a.max_unmatched_lines:
                        samples.append({"line_no": line_no, "text": text[:2000]})
                        raw_samples.append(text)

        if samples:
            # Mirror the folder structure and filename so a human -- or the
            # loop itself -- can look at exactly this file's misses. Raw lines
            # only, no line-number prefixes, so it stays feedable to the tool.
            mirror = out / "uncovered" / rel
            mirror.parent.mkdir(parents=True, exist_ok=True)
            mirror.write_text("\n".join(raw_samples) + "\n")
            entry["unmatched_sample_path"] = f"uncovered/{rel}"
            entry["unmatched_samples"] = samples

        for k in ("seen", "matched", "ignored", "unmatched"):
            totals[f"lines_{k}"] += entry["lines"][k]
        files_out.append(entry)

    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "produced_by": "FILLER fake_standalone.py -- replace via tools/adapter.env",
        "label": a.label,
        "logs_root": str(logs_root),
        "config_path": str(cfg_path),
        "config_fingerprint": "sha256:" + hashlib.sha256(raw).hexdigest()[:16],
        "max_unmatched_lines_per_file": a.max_unmatched_lines,
        "totals": totals,
        "files": files_out,
    }
    (out / "coverage-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))

    pct_f = 100.0 * (totals["files_detected"] + totals["files_ignored"]) / max(totals["files_seen"], 1)
    pct_l = 100.0 * (totals["lines_matched"] + totals["lines_ignored"]) / max(totals["lines_seen"], 1)
    (out / "summary.md").write_text(
        f"# Coverage scan -- {a.label}\n\n"
        f"- logs root: `{logs_root}`\n- config: `{cfg_path}` ({report['config_fingerprint']})\n\n"
        f"## Files\n\n"
        f"| seen | detected | ignored | **undetected** |\n|---|---|---|---|\n"
        f"| {totals['files_seen']} | {totals['files_detected']} | {totals['files_ignored']} "
        f"| **{totals['files_undetected']}** |\n\n"
        f"File coverage: **{pct_f:.1f}%**\n\n"
        f"## Lines (in detected files)\n\n"
        f"| seen | matched | ignored | **unmatched** |\n|---|---|---|---|\n"
        f"| {totals['lines_seen']} | {totals['lines_matched']} | {totals['lines_ignored']} "
        f"| **{totals['lines_unmatched']}** |\n\n"
        f"Line coverage: **{pct_l:.1f}%**\n\n"
        f"Unmatched line samples mirrored under `uncovered/`.\n"
    )

    print(json.dumps({
        "report": str(out / "coverage-report.json"),
        "summary": str(out / "summary.md"),
        "uncovered_dir": str(out / "uncovered"),
        "totals": totals,
        "clear": totals["files_undetected"] == 0 and totals["lines_unmatched"] == 0,
    }, indent=2))


if __name__ == "__main__":
    main()
