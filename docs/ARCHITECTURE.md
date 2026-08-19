# Architecture

## The problem

A monitoring tool parses a large logs folder for errors, driven by
`logs-parsing-config.yml`. The folder has no governance: application logs sit
next to traces, build artefacts, timing tables and archives. The tool only sees
what the config describes, and **what it does not see looks exactly like
nothing being wrong**.

Closing that gap is not one task, it is a few hundred small, near-identical
ones: look at a thing the tool cannot see, decide whether it matters, change
the config, prove the change worked. That shape — many small tasks, each cheap,
the set too large to hold in one head — is what this loop is for.

## Shape

```
Human
  │  /coverage-start
  ▼
Coverage lead ....................... your main VS Code session
  │  owns the goal, runs the full scans, talks to the human
  │  spawns one orchestrator per tick, never does item work itself
  ▼
coverage-orchestrator ............... a fresh subagent, one per TICK
  │  claims ONE item, drives its mini-loop, updates the ledger, exits
  │
  ├──▶ worker-a-runner .............. isolate → scan → verdict
  ├──▶ worker-b-assessor ............ evidence + wiki → decision
  └──▶ worker-c-configurator ........ decision → config change
```

### The mini-loop, per item

```
        ┌──────────────────────────────────────────┐
        ▼                                          │
   A: isolate + scan + verdict                     │
        │                                          │
        ├─ covered / ignored ──▶ DONE              │ green light
        │                                          │
        └─ still uncovered                         │
              ▼                                    │
   B: read wiki, decide ──── escalate ──▶ HUMAN    │
              │                                    │
              └─ parse / ignore                    │
                    ▼                              │
   C: apply to config ─────────────────────────────┘

   3 attempts, then the ledger escalates it automatically.
```

Worker A both opens and closes the loop. That is the point: **A is the only
agent that re-runs the tool, so A's verdict is the only thing that can close an
item.** A decision is a hypothesis, a config edit is an intervention, and
neither is evidence.

## Where the Ralph-loop pattern maps

Built from the Ralph Loops guide. The three pieces and the two guardrails,
translated from `openclaw run` + `ralph.sh` to VS Code + Claude Code:

| Ralph | here |
|---|---|
| **The task list** (`tasks.md`, NOT DONE / IN PROGRESS / DONE) | `runs/<id>/ledger.json`, mutated only through `tools/ledger.py`. `tasks.md` is generated from it for humans. |
| **The reset** (fresh context each iteration) | Each orchestrator tick is a **new subagent** — a fresh context window, by construction. No script restart needed; the subagent boundary *is* the reset. |
| **Persistent output** (deliverables survive the reset) | The config, the run directory, every scan report, every backup. An agent that dies mid-tick loses nothing but the tick. |
| **Guardrail 1: loop counter** | `max_ticks` (default 200), enforced by `ledger.py tick`, plus `max_attempts_per_item` (3) enforced by `ledger.py attempt`. |
| **Guardrail 2: output validation before DONE** | `ledger.py update --status done` refuses without `--resolution`, and the orchestrator must pass `--evidence` pointing at a real report. A failed attempt returns the item to the loop rather than closing it — Ralph's "mark IN PROGRESS, not DONE". |

Two things the guide does not have, added because this workload needs them:

- **Clustering.** Ralph assumes you write the task list. Here it is derived from
  a machine-generated report that can have 40,000 rows. See below.
- **The sweep.** Ralph tasks are independent. Here one task's fix routinely
  completes dozens of others, and not noticing that is the difference between
  40 ticks and 200. See below.

`ralph.sh` in the repo root is the literal outer loop from the guide, for
unattended runs. The in-session tick loop is the same algorithm with a human
watching.

## Clustering: why 40,000 report rows are not 40,000 tasks

A report row is one file or one unmatched line. A config change is a **regex**,
and a regex covers a shape.

`ledger.py` normalises each row to a signature — timestamps, ids, hex, numbers
and paths replaced by placeholders — and makes **one item per distinct
signature**. `app-2026-08-01.log` and `app-2026-08-02.log` become one item;
ten thousand GC lines become one item. `item.occurrences` records how many rows
ride on it, and items are worked highest-occurrence first, so the most
expensive gaps close earliest.

This is a real deviation from "one task per report line". Literal per-row
processing is available — `ledger.py init --granularity line` — and it is the
right choice only if you have reason to believe rows with the same shape need
different decisions.

## The sweep: work that closes itself

An ignore rule for `\.zip$` closes every zip item at once. A file rule for
`db-audit-<date>.log` closes every server's copy.

Every few closed items the lead runs a **full** scan and calls `ledger.py
sweep`, which closes every open item the fresh report no longer shows as
uncovered (`resolution: swept`). Worker A also catches this for free: an item
that comes back `covered` on attempt 1, before any config change was made for
it, is closed as `already_covered`.

Without the sweep, the loop pays a full three-agent mini-loop for items that
were already fixed. On the sample folder, three config changes closed five
items.

## The decision framework

Three outcomes for anything the tool cannot see:

- **Parse it** — a human needs to know when this appears. Add a file rule or a
  line pattern.
- **Ignore it** — it carries no error signal. Add an ignore rule **with a
  written reason**, and a wiki reference where one exists.
- **Escalate it** — the wiki does not settle it. A human decides, and the
  answer is written back into the knowledge base so the class is settled
  permanently.

The outcomes are deliberately not symmetric. A wrong parse rule adds noise. A
wrong escalation costs two minutes. **A wrong ignore rule makes the monitoring
tool permanently and silently blind to a real production error** — which is the
exact failure this system exists to prevent. So Worker B may only ignore
something when the wiki says so or when it plainly carries no error signal, and
must escalate whenever it is reaching for a justification.

The authoritative statement of this lives in
`.claude/agents/worker-b-assessor.md`, with the search playbook, regex cookbook
and worked examples in the `assess-finding` skill.

## Why judgement and determinism are split

Agents decide **what a log means**. Code decides **whether a number is zero**.

- `verdict.py` computes the mini-loop's exit condition, not Worker A. A model
  that can talk itself into `covered` is exactly the failure mode that makes
  the whole run untrustworthy.
- `ledger.py` owns all state transitions and refuses to close an item without a
  recorded resolution.
- `config_edit.py` owns config writes — backup, insert without destroying
  comments, validate, roll back on failure. Worker C has no `Edit` tool at all,
  which makes hand-editing impossible rather than merely discouraged.
- `preflight.py` checks the whole setup before tick 1.

Each of these exists because it removes a way the loop could quietly produce a
wrong answer while reporting success.

## State that survives everything

Nothing lives in an agent's context between ticks. Close VS Code mid-run,
compact the session, lose the machine — `runs/CURRENT` and `ledger.json` are
enough for `/coverage-start` to pick up exactly where it stopped. Items left
`in_progress` by an interrupted tick are returned to `pending` and re-claimed.

That property is what makes the run length independent of the session length.
