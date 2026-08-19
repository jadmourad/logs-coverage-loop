#!/usr/bin/env python3
"""Rebuild one file (or one line) into a throwaway logs root.

Worker A uses this to test a single finding in isolation. The relative path is
preserved exactly, because the monitoring tool's file-detection rules match on
path shape -- change the path and you are no longer testing the same thing.

  isolate.py --logs-root LOGS --path app/srv-01/app.log --out /tmp/iso
  isolate.py --logs-root LOGS --path app/srv-01/app.log --out /tmp/iso --lines 12,44

With --lines, only those raw lines are written (order preserved, deduped), so
the re-run report is about exactly the line under test and nothing else.
Without --lines, the file is copied, truncated at --max-bytes.

Prints JSON: {"isolated_root", "file", "rel_path", "lines_written", "truncated"}
Exit: 0 ok | 2 source missing/unreadable
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--logs-root", required=True)
    p.add_argument("--path", required=True, help="path relative to --logs-root")
    p.add_argument("--out", required=True, help="throwaway logs root to build")
    p.add_argument("--lines", default=None,
                   help="comma-separated 1-based line numbers to extract")
    p.add_argument("--context", type=int, default=0,
                   help="also include N lines either side of each --lines entry")
    p.add_argument("--max-bytes", type=int, default=5_000_000,
                   help="cap on a whole-file copy (default 5MB)")
    p.add_argument("--clean", action="store_true",
                   help="wipe --out first (it is disposable by design)")
    a = p.parse_args()

    src_root = Path(a.logs_root).resolve()
    rel = Path(a.path.lstrip("/"))
    src = (src_root / rel).resolve()

    # Never let a crafted report path walk out of the logs root.
    if not str(src).startswith(str(src_root)):
        print(f"isolate: {a.path} escapes --logs-root", file=sys.stderr)
        sys.exit(2)
    if not src.is_file():
        print(f"isolate: no such file: {src}", file=sys.stderr)
        sys.exit(2)

    out_root = Path(a.out).resolve()
    if a.clean and out_root.exists():
        shutil.rmtree(out_root)
    dst = out_root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)

    truncated = False
    if a.lines:
        wanted: set[int] = set()
        for tok in a.lines.split(","):
            tok = tok.strip()
            if not tok:
                continue
            n = int(tok)
            for k in range(n - a.context, n + a.context + 1):
                if k >= 1:
                    wanted.add(k)
        picked = []
        with src.open("r", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if i in wanted:
                    picked.append(line.rstrip("\n"))
                if wanted and i > max(wanted):
                    break
        dst.write_text("\n".join(picked) + ("\n" if picked else ""))
        written = len(picked)
    else:
        size = src.stat().st_size
        if size > a.max_bytes:
            with src.open("rb") as fh:
                dst.write_bytes(fh.read(a.max_bytes))
            truncated = True
        else:
            shutil.copy2(src, dst)
        try:
            written = sum(1 for _ in dst.open("r", errors="replace"))
        except OSError:
            written = -1  # binary or unreadable as text

    print(json.dumps({
        "isolated_root": str(out_root),
        "file": str(dst),
        "rel_path": str(rel),
        "lines_written": written,
        "truncated": truncated,
    }, indent=2))


if __name__ == "__main__":
    main()
