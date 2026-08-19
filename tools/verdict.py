#!/usr/bin/env python3
"""Decide, mechanically, whether an isolated re-run resolved an item.

Worker A calls this instead of eyeballing the report. Agents should make
judgement calls; "is this number zero" is not a judgement call, and a model
that talks itself into `covered` is exactly the failure this loop must not
have. The verdict here is the loop's exit condition, so it is code.

  verdict.py --item-json ITEM --report REPORT --attempt N --out a-result.json

Verdicts
  covered          the tool now parses it
  ignored          the tool now deliberately skips it
  still_uncovered  no change; the mini-loop continues
  isolation_failed the target is not in the report at all; A must re-isolate

Exit: 0 always (the verdict is data, not an error) | 2 bad input
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--item-json", required=True, help="the ledger item under test")
    p.add_argument("--report", required=True, help="coverage-report.json from the isolated re-run")
    p.add_argument("--attempt", type=int, required=True)
    p.add_argument("--out", required=True, help="where to write a-result.json")
    p.add_argument("--isolated-root", default=None)
    p.add_argument("--note", default="")
    a = p.parse_args()

    item = json.loads(Path(a.item_json).read_text())
    report = json.loads(Path(a.report).read_text())

    rel = item.get("example_path")
    entry = next((f for f in report.get("files", []) if f.get("path") == rel), None)

    note = a.note
    if entry is None:
        verdict, detail = "isolation_failed", (
            f"{rel!r} is not in the isolated report -- the isolated folder does not "
            f"mirror the original path, or the file was not copied")
    elif item["kind"] == "undetected_file":
        verdict = {"ignored": "ignored", "detected": "covered",
                   "undetected": "still_uncovered"}[entry["status"]]
        detail = f"file status: {entry['status']}"
        if verdict == "covered" and entry.get("binary"):
            note = (note + " WARNING: file is binary but a parse rule now matches it -- "
                            "that rule is probably too broad.").strip()
    else:  # unmatched_lines
        st = entry["status"]
        if st == "ignored":
            verdict, detail = "ignored", f"whole file ignored by {entry.get('ignored_by')}"
        elif st == "undetected":
            verdict, detail = "still_uncovered", (
                "host file is no longer detected -- a file rule regressed, or the "
                "isolated copy does not match the file pattern")
        elif entry["lines"]["unmatched"] == 0:
            if entry["lines"]["matched"] > 0:
                verdict, detail = "covered", f"parsed by {entry.get('matched_by')}"
            else:
                verdict, detail = "ignored", "all lines fall under an ignore-line rule"
        else:
            verdict, detail = "still_uncovered", (
                f"{entry['lines']['unmatched']} line(s) still unmatched in a detected file")

    result = {
        "item_id": item["id"],
        "kind": item["kind"],
        "attempt": a.attempt,
        "verdict": verdict,
        "detail": detail,
        "note": note,
        "target": {"rel_path": rel, "line_no": item.get("example_line_no")},
        "isolated_root": a.isolated_root,
        "report_path": str(Path(a.report).resolve()),
        "uncovered_dir": str(Path(a.report).parent / "uncovered"),
        "file_entry": entry,
        "totals": report.get("totals", {}),
        "config_fingerprint": report.get("config_fingerprint"),
        "loop_exits": verdict in ("covered", "ignored"),
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    print(json.dumps({"a_result": str(out), "verdict": verdict, "detail": detail,
                      "loop_exits": result["loop_exits"]}, indent=2))


if __name__ == "__main__":
    main()
