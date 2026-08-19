#!/usr/bin/env bash
# =============================================================================
# Unattended outer loop — the Ralph-loop script, adapted.
#
# OPTIONAL. The normal way to run this system is `/coverage-start` inside VS
# Code, where you can watch the workers and answer escalations as they come up.
# Use this instead when you want to leave a few hundred items running
# overnight: it calls a fresh headless `claude` per tick, so every tick starts
# with an empty context window, and the ledger carries the state between them.
#
#   ./ralph.sh                       # resume the current run
#   MAX_TICKS=50 ./ralph.sh          # cap the ticks
#   SWEEP_EVERY=10 ./ralph.sh        # full re-scan every N ticks
#
# Start the run first (`/coverage-start` in VS Code, up to the point where the
# ledger exists). This script only advances an existing run; it will not scan a
# folder or build a ledger for you.
#
# PERMISSIONS: each tick shells out to python/bash. `.claude/settings.json`
# pre-approves exactly those commands, which is what keeps this unattended. If
# a tick stalls waiting for approval it will hit the timeout below and be
# counted as a failure — check `runs/<id>/tasks.md` and the log to see where.
# =============================================================================
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

MAX_TICKS="${MAX_TICKS:-100}"
SWEEP_EVERY="${SWEEP_EVERY:-5}"
TICK_TIMEOUT="${TICK_TIMEOUT:-900}"
PROMPT="${PROMPT:-Use the coverage-orchestrator subagent to run exactly one tick. Reply with only its one-line result.}"

if [ ! -f runs/CURRENT ]; then
  echo "ralph: no active run (runs/CURRENT is missing)."
  echo "       Start one with /coverage-start in VS Code first."
  exit 2
fi
RID="$(cat runs/CURRENT)"
LOGS_ROOT="$(python3 -c "import json;print(json.load(open('runs/$RID/ledger.json'))['logs_root'])")"
LOG="runs/$RID/ralph.log"

echo "run $RID | logs $LOGS_ROOT | max $MAX_TICKS ticks | sweep every $SWEEP_EVERY" | tee -a "$LOG"
python3 tools/ledger.py status | tee -a "$LOG"

sweep() {
  local n="$1"
  echo "--- sweep $n: full re-scan ---" | tee -a "$LOG"
  bash tools/standalone.sh --logs-root "$LOGS_ROOT" \
    --config config/logs-parsing-config.yml \
    --out "runs/$RID/scans/sweep-$n" --label "sweep-$n" >/dev/null 2>>"$LOG" \
    && python3 tools/ledger.py sweep --report "runs/$RID/scans/sweep-$n/coverage-report.json" \
       2>&1 | tee -a "$LOG" \
    || echo "sweep $n failed — continuing" | tee -a "$LOG"
}

failures=0
for ((i = 1; i <= MAX_TICKS; i++)); do
  echo "=== tick $i/$MAX_TICKS ===" | tee -a "$LOG"

  out="$(timeout "$TICK_TIMEOUT" claude -p "$PROMPT" 2>&1 | tail -5)"
  echo "$out" | tee -a "$LOG"

  case "$out" in
    *NO_PENDING*)
      echo "All items closed." | tee -a "$LOG"
      sweep "final"
      python3 tools/ledger.py status | tee -a "$LOG"
      echo "ALL_COMPLETE" | tee -a "$LOG"
      exit 0
      ;;
    *TICK_LIMIT*)
      echo "Tick ceiling tripped in the ledger — stopping. This is a loop" | tee -a "$LOG"
      echo "problem, not a finished run. Check runs/$RID/tasks.md." | tee -a "$LOG"
      exit 4
      ;;
    *DONE*|*ESCALATED*)
      failures=0
      ;;
    *)
      # A tick that returned neither is a stalled or errored session.
      failures=$((failures + 1))
      echo "tick $i returned no recognisable result (consecutive: $failures)" | tee -a "$LOG"
      if [ "$failures" -ge 3 ]; then
        echo "Three ticks in a row produced nothing. Stopping rather than" | tee -a "$LOG"
        echo "burning the remaining $((MAX_TICKS - i)) ticks. See $LOG." | tee -a "$LOG"
        exit 5
      fi
      ;;
  esac

  if [ $((i % SWEEP_EVERY)) -eq 0 ]; then
    sweep "$i"
  fi
done

echo "Reached MAX_TICKS ($MAX_TICKS) with work still pending." | tee -a "$LOG"
sweep "final"
python3 tools/ledger.py status | tee -a "$LOG"
exit 3
