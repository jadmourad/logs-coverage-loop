---
name: coverage-start
description: Start (or resume) the log-coverage loop. Runs the monitoring tool's standalone mode across the whole logs folder, turns the coverage report into a ledger of work items, then drives the orchestrator tick by tick until every item is parsed, ignored, or escalated. Use when the human says "start the coverage run", "close the coverage gaps", or asks to resume an existing run.
---

# Coverage lead

Running this skill makes you the **coverage lead**. You own the goal and you
talk to the human. You do not do per-item work — you spawn one
`coverage-orchestrator` per tick and each of those handles exactly one item.

Read `AGENTS.md` first. Run everything from the repo root.

---

## 1. Preflight

```bash
python3 tools/preflight.py --logs-root <LOGS> --config config/logs-parsing-config.yml
```

Any `FAIL` stops you. Report it and fix it with the human — do not start a run
on a broken setup. Pass on the warnings, but **tell the human which FILLER
pieces are still in place**, because it changes what the run means: with the
placeholder wiki, Worker B will escalate nearly everything, which is correct
behaviour but not a useful run.

If `--logs-root` was not given, ask for it. Never assume `logs-sample/` is what
they meant unless they are deliberately testing the loop.

## 2. Resume, or start fresh

If `runs/CURRENT` exists, read it and check:

```bash
python3 tools/ledger.py status
```

If it has open items (`pending` or `in_progress` > 0), **ask the human**: resume
that run, or start a new one? Resuming is almost always right — the ledger is
complete and correct, and a fresh scan throws away the attempt history. Skip
straight to §5 when resuming.

Items left `in_progress` are from a session that stopped mid-tick. Reset them so
they can be claimed again:

```bash
python3 tools/ledger.py update --item-id <ID> --status pending --actor lead \
  --note "reclaimed after interrupted session"
```

## 3. The first full scan

```bash
bash tools/standalone.sh --logs-root <LOGS> \
  --config config/logs-parsing-config.yml \
  --out runs/_incoming/initial --max-unmatched-lines 50 --label initial
```

On a large folder this is the slow step. Say so before you start it.

## 4. Build the ledger

```bash
python3 tools/ledger.py init --report runs/_incoming/initial/coverage-report.json \
  --logs-root <LOGS> --config config/logs-parsing-config.yml \
  --granularity cluster --max-attempts 3 --max-ticks 200
```

Then move the scan into the run: `mv runs/_incoming/initial runs/<RID>/scans/`
(create `scans/` first).

Now **show the human the shape of the work** before burning a single tick:

- files seen / detected / ignored / undetected, and lines matched / unmatched
- how many ledger items that collapsed into, and how many report rows they cover
- the top 10 items by `occurrences` from `runs/<RID>/tasks.md`

Then ask how they want to proceed:

- **run to completion** — keep ticking until nothing is pending
- **run a batch of N** — tick N times, then report back
- **one tick** — a single item, to watch the loop work before trusting it

For a first run against a real logs folder, recommend a batch of 5. It is
enough to see whether Worker B's decisions are sound before you commit to a
few hundred of them.

## 5. The tick loop

Repeat, up to the agreed count:

1. Spawn `coverage-orchestrator` with exactly: `Run one tick.`
2. It returns one line:
   `<DONE|ESCALATED|NO_PENDING|TICK_LIMIT> <item-id> <kind> x<n> attempts=<n> <reason>`
3. Act on it:
   - `DONE` / `ESCALATED` — record it, continue.
   - `NO_PENDING` — every item is closed. Go to §6.
   - `TICK_LIMIT` — the tick ceiling tripped. Stop and tell the human; this
     means the loop is spinning, not that the work is done.
4. **Every 5 closed items, sweep** (§5a).

Keep your own running tally short — one line per tick. Do not accumulate
transcripts; the ledger has the detail and you may be here for hours.

### 5a. The sweep — do not skip this

One regex routinely covers items far beyond the one it was written for. The
sweep finds those instead of paying a full mini-loop for each:

```bash
bash tools/standalone.sh --logs-root <LOGS> --config config/logs-parsing-config.yml \
  --out runs/<RID>/scans/sweep-<n> --label sweep-<n>
python3 tools/ledger.py sweep --report runs/<RID>/scans/sweep-<n>/coverage-report.json
```

It closes every open item the fresh report no longer shows as uncovered. On a
messy folder this is the difference between 40 ticks and 200.

If the logs folder is large enough that a full scan is expensive, sweep every
10–15 closed items instead, and always sweep once at the end.

## 6. Close out

Final scan + sweep, then:

```bash
python3 tools/ledger.py status
```

Report to the human, in plain terms:

- **Coverage before → after.** Files undetected and lines unmatched, initial
  scan versus final. This is the number the whole exercise exists to move.
- **What was done:** how many items parsed, how many deliberately ignored, and
  the new rules added to the config (`python3 tools/config_edit.py show`).
- **What needs them:** every escalated item, with the question, one line each.
  Point them at `/coverage-escalations` to answer.
- **Goal met?** Only true when `pending`, `in_progress` and `blocked` are all
  zero. If escalations are outstanding, say the goal is *blocked on N human
  decisions* — do not report a blocked run as finished.

---

## Rules

- **Never close an item yourself.** Only the orchestrator writes to the ledger
  during the loop.
- **Never widen the guardrails** because a run is going slowly. If items keep
  hitting 3 attempts, the wiki or the patterns are the problem; say so.
- **Never report the goal as met while anything is escalated or pending.**
- **Stop and ask** if the first few ticks all escalate. That means the knowledge
  base cannot answer this logs folder, and a few hundred more ticks will not
  change that — better to fix the wiki first.
