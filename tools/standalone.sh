#!/usr/bin/env bash
# =============================================================================
# THE SEAM.
#
# Every agent in this loop runs the monitoring tool's standalone mode through
# THIS FILE and nothing else. When the real tool exists, you change this file
# (or tools/adapter.env) and nothing else in the repo needs to move.
#
# Contract this script must honour -- see docs/CONTRACTS.md:
#
#   standalone.sh --logs-root DIR --config FILE --out DIR \
#                 [--max-unmatched-lines N] [--label STR]
#
#   MUST write  <out>/coverage-report.json   (schema in docs/CONTRACTS.md)
#   MUST write  <out>/uncovered/<mirrored path>  -- up to N raw unmatched
#               lines per parsed file, same folder structure and filename
#   MUST exit 0 on a successful scan, non-zero on a scan/config failure.
#
# If the real tool already speaks this contract, just point STANDALONE_CMD at
# it in tools/adapter.env. If it does not, translate here: run the real tool,
# then normalise its output into the contract above.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# adapter.env is the one file you edit to swap the fake for the real tool.
if [ -f "$HERE/adapter.env" ]; then
  # shellcheck disable=SC1091
  . "$HERE/adapter.env"
fi

: "${STANDALONE_CMD:=python3 $HERE/fake/fake_standalone.py}"

# word-splitting on STANDALONE_CMD is intentional (it may carry an interpreter)
# shellcheck disable=SC2086
exec $STANDALONE_CMD "$@"
