#!/usr/bin/env python3
"""Run the non-regression suite.

  python3 tests/run_tests.py              everything
  python3 tests/run_tests.py ledger       just the modules matching "ledger"
  python3 tests/run_tests.py -v           verbose, one line per test

Stdlib unittest only -- no pytest, nothing to install. Exit 0 all green,
1 otherwise.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

MODULES = [
    ("test_ledger", "run state: clustering, item ids, claims, attempts, sweep"),
    ("test_config_edit", "config writes: anchors, comments, rollback, idempotence"),
    ("test_scanner_contract", "the report contract -- ALSO the real tool's acceptance test"),
    ("test_verdict", "the loop's exit condition, full truth table"),
    ("test_isolate", "rebuilding one file or line into a throwaway root"),
    ("test_integration", "the workflow end to end, agents scripted"),
    ("test_agent_wiring", "prompts, skills, permissions -- drift against the tools"),
]


def main() -> int:
    args = [a for a in sys.argv[1:]]
    verbose = "-v" in args or "--verbose" in args
    filters = [a for a in args if not a.startswith("-")]

    selected = [(m, d) for m, d in MODULES
                if not filters or any(f in m for f in filters)]
    if not selected:
        print(f"no test module matches {filters}")
        print("available: " + ", ".join(m for m, _ in MODULES))
        return 1

    loader = unittest.TestLoader()
    results, total, failed_total, started = [], 0, 0, time.time()

    for name, blurb in selected:
        suite = loader.loadTestsFromName(name)
        count = suite.countTestCases()
        print(f"\n\033[1m{name}\033[0m  --  {blurb}")
        runner = unittest.TextTestRunner(verbosity=2 if verbose else 1,
                                         stream=sys.stdout)
        r = runner.run(suite)
        bad = len(r.failures) + len(r.errors)
        total += count
        failed_total += bad
        results.append((name, count, bad, r))

    print("\n" + "=" * 68)
    for name, count, bad, _ in results:
        mark = "\033[32mPASS\033[0m" if not bad else f"\033[31mFAIL ({bad})\033[0m"
        print(f"  {mark:<22} {name:<26} {count:>4} tests")
    print("=" * 68)
    print(f"  {total} tests, {failed_total} failed, {time.time() - started:.1f}s")

    if failed_total:
        print("\n\033[31mNON-REGRESSION SUITE FAILED.\033[0m Every failure here is "
              "either a real defect\nor a deliberate behaviour change that the "
              "test has not been told about yet.\nDo not start a coverage run "
              "against a real logs folder until it is green.")
        return 1
    print("\n\033[32mAll green.\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
