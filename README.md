# logs-coverage-loop

An agent loop that closes the gaps between what a monitoring tool *can* see in
a logs folder and what is actually in it.

The monitoring tool parses logs for errors, driven by `logs-parsing-config.yml`.
It only opens files the config recognises and only reports lines the config
matches — so anything the config does not describe is invisible, and invisible
looks exactly like healthy. A standalone offline mode scans **everything** and
reports the gaps. This repo is what walks that report and closes them.

For every gap, one of three things happens:

- **parse it** — a real log we need to monitor; add a rule to the config
- **ignore it** — not an error; add an ignore rule, with a written reason
- **escalate it** — the wiki does not settle it; a human decides, and the answer
  is written back into the knowledge base so the question is never asked twice

The run is finished when every item is in one of those states. Not most. Every.

---

## Running it

**First time only:** open this folder in VS Code and accept the workspace trust
dialog. Until you do, Claude Code ignores `.claude/settings.json` entirely — so
the loop's pre-approved commands are not approved and the first tick stalls on
a permission prompt. `preflight.py` fails loudly if this is still outstanding.

Then, from this folder:

```
/coverage-start
```

That is the only command you need. It preflights the setup, scans the whole
logs folder, turns the report into a ledger of work items, shows you the shape
of the work, and then asks how much to run — one item, a batch, or to
completion.

The rest:

| | |
|---|---|
| `/coverage-status` | where the run stands; coverage before → after |
| `/coverage-escalations` | answer what the loop could not settle, and teach it |
| `/wiki-ingest` | (re)build the knowledge base from Confluence or an export |
| `/run-tests` | the non-regression suite — run it before any real run |
| `./ralph.sh` | optional: run unattended overnight instead of in-session |

Everything is resumable. Close VS Code mid-run and `/coverage-start` picks up
exactly where it stopped — all state is on disk, none of it in an agent's head.

---

## How it is put together

```
Human ─▶ coverage lead ─▶ coverage-orchestrator ─▶ worker A / B / C
         (your session)    (one fresh subagent      (isolate+scan / decide /
                            per item)                configure)
```

Per item, the mini-loop is **A → B → C → A**, capped at 3 attempts, then it
escalates:

- **Worker A** rebuilds that one file — or that one line — into a throwaway
  folder, runs the monitoring tool on just that, and reports a verdict.
- **Worker B** reads A's evidence and the wiki, and decides: parse, ignore, or
  escalate. It drafts the exact config change.
- **Worker C** applies it through a safe editor: backed up, comments preserved,
  every regex validated, rolled back if the result is invalid.
- **Worker A runs again** and that is what closes the item. A decision is a
  hypothesis and a config edit is an intervention; only a re-run is evidence.

Each orchestrator tick is a fresh subagent with an empty context window, and
all memory between ticks lives in `runs/<id>/ledger.json`. That is what lets a
run be hundreds of items long without degrading — the Ralph-loop pattern, with
the subagent boundary doing the job the restart script used to do.

Full detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Two design points worth knowing before you run it

**Report rows are clustered into items.** A report can have 40,000 unmatched
lines, but a config change is a regex and a regex covers a shape. Rows are
normalised (timestamps, ids, numbers, paths replaced) and grouped by shape, so
40,000 rows become a few dozen items, worked biggest-first.
`ledger.py init --granularity line` gives you literal one-item-per-row if you
want it.

**Ignoring is treated as more dangerous than parsing.** A wrong parse rule adds
noise. A wrong escalation costs someone two minutes. A wrong *ignore* rule makes
the monitoring tool permanently and silently blind to a real production error.
So Worker B may only ignore something when the wiki says so or when it plainly
carries no error signal — otherwise it escalates. Expect escalations early on;
that is the system working, and each one you answer becomes a permanent rule.

---

## It ships with fillers

The real monitoring tool, logs folder, config and wiki are not here yet. Each
has a working stand-in so the whole loop runs today, and each has exactly one
swap point:

| filler | swap point |
|---|---|
| monitoring tool | `tools/adapter.env` — one line |
| logs folder | pass `--logs-root` |
| config | drop the real `config/logs-parsing-config.yml` in |
| wiki | `/wiki-ingest` |

`python3 tools/preflight.py` tells you which are still in place, every run.

`tools/fake/fake_standalone.py` is a real scanner, not a stub — it reads real
YAML and real log files and emits the exact report contract. So the loop can be
exercised end to end before the real tool exists.

**Two things to fill in:**

- [docs/STANDALONE_MODE.md](docs/STANDALONE_MODE.md) — a skeleton describing the
  real standalone tool. Three sections are marked LOAD-BEARING; those are the
  ones that make the loop confidently wrong if they are wrong.
- [docs/SWAP_IN_REAL_TOOL.md](docs/SWAP_IN_REAL_TOOL.md) — the order to do the
  swaps in, and the five things most likely to break.

---

## Non-regression suite

```
python3 tests/run_tests.py
```

~190 tests, stdlib `unittest`, no new dependencies, and nothing in it touches
the real config, logs folder or run tree. Run it before any run against a real
logs folder — the loop edits a production config unattended, hundreds of times.

Two of them earn their keep beyond the usual:

- **`tests/test_scanner_contract.py` is the acceptance test for the real
  monitoring tool.** It runs through `tools/standalone.sh`, so the day you
  point that at the real standalone mode, this file tells you every place the
  tool and the loop disagree.
- **`tests/test_integration.py` is an executable spec of the workflow.** It
  runs the orchestrator's algorithm against the real tools with only Worker B
  scripted, so a change to the retry rule or to when an item may close shows up
  there immediately.

Details in [tests/README.md](tests/README.md).

## Layout

```
AGENTS.md                    the shared spine every agent reads
.claude/agents/              orchestrator + workers A, B, C
.claude/skills/              /coverage-start, /coverage-status,
                             /coverage-escalations, /wiki-ingest,
                             and the worker reference skills
config/                      logs-parsing-config.yml (the loop edits this)
wiki/KNOWLEDGE_BASE.md       distilled decision rules — Worker B's only source
wiki/export/                 raw Confluence snapshot
tools/standalone.sh          THE SEAM — the only way the tool is ever run
tools/*.py                   ledger, isolation, config edits, verdict, preflight
runs/<id>/                   ledger, scans, per-item attempts, escalations, backups
docs/                        contracts, architecture, the two fill-in docs
tests/                       non-regression suite
ralph.sh                     optional unattended outer loop
```

The Python tools have no dependencies beyond `pyyaml`.
