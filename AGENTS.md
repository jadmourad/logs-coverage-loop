# AGENTS.md — the shared spine

Every agent in this repo reads this. It holds the parts that are the **same for
everyone**: the goal, the file layout, the commands, and the rules nobody may
break. Each agent's own file (`.claude/agents/*.md`) holds only *its job*.

---

## 1. What this system is for

A monitoring tool parses a large, ungoverned logs folder looking for errors. It
is driven by `logs-parsing-config.yml`. Plenty of that folder is invisible to
it — files whose names no rule matches, and log lines inside parsed files that
no pattern matches.

This repo closes that gap, automatically. A standalone offline mode of the
monitoring tool scans everything and reports what it can and cannot see. A loop
of agents then walks that report and, for every gap, either:

- **configures** the tool to parse it (it is a real log we care about), or
- **ignores** it deliberately, with a written reason (it is not an error), or
- **escalates** it to a human (the wiki does not settle it).

**The goal is met when every item in the report is in one of those three
states.** Not "mostly". Every item.

---

## 2. The loop

```
Human
  └─ /coverage-start ......... coverage lead (your main VS Code session)
       │   runs the first full scan, builds the ledger, owns the goal
       │
       └─ coverage-orchestrator ......... one fresh subagent per TICK
            │   claims ONE item, runs its mini-loop, updates the ledger, exits
            │
            ├─ worker-a-runner ........ isolate the finding, run the tool, report
            ├─ worker-b-assessor ...... read the report + wiki, decide
            └─ worker-c-configurator .. apply the decision to the config
```

The mini-loop for one item is **A → B → C → A → …**, capped at 3 attempts.
Worker A both opens and closes it: it produces the evidence B judges, and it is
the only agent allowed to declare an item resolved, because only a re-run
proves it.

Each orchestrator tick starts with an empty context window. That is deliberate:
it is what lets this run for hundreds of items without degrading. **All memory
between ticks lives in `runs/<run-id>/ledger.json`, never in an agent's head.**

---

## 3. Rules nobody may break

1. **Never hand-edit `ledger.json` or `tasks.md`.** Go through
   `tools/ledger.py`. `tasks.md` is a generated mirror; edits to it are lost.
2. **Never hand-edit `config/logs-parsing-config.yml`.** Go through
   `tools/config_edit.py`. It backs up, inserts without destroying comments,
   validates, and rolls back on a bad edit.
3. **Never run the monitoring tool any way except `bash tools/standalone.sh`.**
   That file is the single swap point for the real tool.
4. **Never write to the logs folder.** It is read-only input. Isolation copies
   out of it, never into it.
5. **An item is only `done` when a re-run proves it.** A plan is not proof, a
   config edit is not proof, and a confident explanation is not proof. The only
   thing that closes an item is a coverage report showing the file/line is now
   parsed or ignored. Pass that report path as `--evidence`.
6. **Escalate rather than guess.** If the wiki does not settle whether
   something is a real error, escalate it. A wrong `ignore` rule makes the
   monitoring tool permanently blind to a real failure. That is the worst
   outcome in this system — worse than a slow loop, worse than a human having
   to answer a question.
7. **Every ignore rule needs a `reason`, and a `wiki_ref` when one exists.**
   That is the audit trail for why a log is not monitored.
8. **Hand off through files, not prose.** Workers write their result to a JSON
   file and return the path plus a one-line summary. Never paste a report body
   into a reply.

---

## 4. Layout

```
config/logs-parsing-config.yml   the monitoring tool's config (edited by Worker C)
logs-sample/                     FILLER logs folder — swap for the real one
wiki/KNOWLEDGE_BASE.md           distilled decision rules (Worker B's only wiki source)
wiki/export/                     raw Confluence snapshot the KB was built from
tools/standalone.sh              THE SEAM — the only way to run the monitoring tool
tools/ledger.py                  run state: the durable task list
tools/isolate.py                 rebuild one file/line into a throwaway logs root
tools/config_edit.py             safe, validated, comment-preserving config edits
runs/CURRENT                     the active run id
runs/<run-id>/
  ledger.json                    THE source of truth for progress
  tasks.md                       generated human-readable mirror
  scans/initial/                 first full-folder report
  scans/sweep-<n>/               later full-folder reports
  items/<item-id>/attempt-<n>/   one mini-loop attempt (see §6)
  escalations/<item-id>.md       questions waiting on a human
  config-backups/                every config version, newest last
docs/                            contracts and reference — read when unsure
tests/                           non-regression suite — `python3 tests/run_tests.py`
```

Changed a tool, a prompt, a skill, or the config schema? Run the suite before
the next tick. It never touches the real config, logs folder or run tree.

---

## 5. Commands

Always run from the repo root. These exact forms are pre-approved in
`.claude/settings.json`; deviating from them will stall the loop on a
permission prompt.

```bash
# scan (the seam)
bash tools/standalone.sh --logs-root <DIR> --config config/logs-parsing-config.yml \
     --out <OUTDIR> [--max-unmatched-lines 50] [--label <TAG>]

# run state
python3 tools/ledger.py next|status|render|tick
python3 tools/ledger.py get      --item-id <ID>
python3 tools/ledger.py attempt  --item-id <ID> --actor <who>
python3 tools/ledger.py update   --item-id <ID> --status <s> [--resolution <r>] \
                                 [--evidence <report.json>] [--decision-json <f>] [--note "..."]
python3 tools/ledger.py sweep    --report <fresh-full-report.json>

# isolation
python3 tools/isolate.py --logs-root <LOGS> --path <REL> --out <DIR> [--lines 12,44] [--clean]

# config
python3 tools/config_edit.py validate | show
python3 tools/config_edit.py add-ignore-file  --rule-id ID --pattern RX --reason "..." [--wiki-ref W]
python3 tools/config_edit.py add-ignore-line  --rule-id ID --pattern RX --reason "..." [--file-scope RX] [--wiki-ref W]
python3 tools/config_edit.py add-file-rule    --rule-id ID --pattern RX [--line-pattern RX ...]
python3 tools/config_edit.py add-line-pattern --rule-id ID --pattern RX
```

`--run-id` is optional everywhere; it defaults to `runs/CURRENT`.

---

## 6. The handoff files

One attempt lives in `runs/<run-id>/items/<item-id>/attempt-<n>/`:

| file | written by | read by |
|---|---|---|
| `isolated/` | Worker A | Worker A (its own scan target) |
| `report/coverage-report.json` | Worker A | Worker B |
| `a-result.json` | Worker A | orchestrator, Worker B |
| `decision.json` | Worker B | orchestrator, Worker C |
| `c-result.json` | Worker C | orchestrator |

Exact schemas: `docs/CONTRACTS.md` §4. Each worker writes its file, then
returns **only** a short JSON summary — never the file's contents.

---

## 7. Item kinds

`undetected_file` — no rule in the config matches this file's path. The whole
file is invisible. Fix by adding a file rule, or by adding an ignore-file rule.

`unmatched_lines` — the file *is* parsed, but these lines match no
`line_patterns` entry. Fix by adding a line pattern to the file's rule, or by
adding an ignore-line rule.

Items are **clusters**, not single rows. A report with 40,000 unmatched lines
collapses into a few dozen items, one per distinct line *shape*
(`tools/ledger.py` normalises timestamps, ids, numbers and paths away). One
config change therefore usually resolves a whole cluster at once —
`item.occurrences` tells you how many report rows ride on it. Run with
`--granularity line` at init if you truly want one item per row.

---

## 8. Two guardrails

**Attempts.** 3 per item, counted by `ledger.py attempt`. On the 4th the ledger
escalates the item itself — you cannot loop forever by accident.

**Ticks.** `ledger.py tick` fails (exit 4) past `max_ticks`. If that trips,
something is wrong with the loop, not with the item. Stop and tell the human.

---

## 9. When the filler is still in place

`logs-sample/`, `config/logs-parsing-config.yml`, `wiki/KNOWLEDGE_BASE.md` and
`tools/fake/fake_standalone.py` are stand-ins so the loop can be exercised
before the real monitoring tool exists. They are marked FILLER at the top.
Behave exactly as you would with the real thing — the contracts are identical.
See `docs/SWAP_IN_REAL_TOOL.md` for what changes when they are replaced.
