# Non-regression suite

```bash
python3 tests/run_tests.py          # everything
python3 tests/run_tests.py ledger   # one module
python3 tests/run_tests.py -v       # one line per test
```

Or just ask: `/run-tests`.

Stdlib `unittest` only. The single dependency is `pyyaml`, which the tools
already need. Nothing to install.

**Nothing here touches the real thing.** Every test builds its own throwaway
logs folder, config and run tree in a temp directory. `LOGS_COVERAGE_ROOT`
redirects the run tree, so `runs/`, `logs-sample/` and
`config/logs-parsing-config.yml` are never read or written by a test.

---

## What each module protects

| module | protects against |
|---|---|
| `test_ledger` | items lost, double-claimed, closed with no recorded reason; clustering drift; guardrails that stop firing |
| `test_config_edit` | a corrupted production config; a regex mangled in transit; comments destroyed; duplicate rules from retries |
| `test_scanner_contract` | the tool and the loop disagreeing about the report — **also the acceptance test for the real tool** |
| `test_verdict` | the loop's exit condition being wrong, which closes real gaps and reports success |
| `test_isolate` | testing a finding against the wrong file, which makes every verdict meaningless |
| `test_integration` | the workflow itself — retries, escalation, resolution mapping, the sweep, convergence |
| `test_agent_wiring` | a prompt telling an agent to do something no longer possible; permission rules that silently match nothing |

## The two that matter most

**`test_scanner_contract.py` is the acceptance test for the real monitoring
tool.** Every test in it runs through `tools/standalone.sh`, so it tests
whatever `STANDALONE_CMD` points at. The day you point that at the real
standalone mode, run this file. Anything that fails is a place where the real
tool and the loop disagree — and every one of those makes the loop confidently
wrong rather than loudly broken.

A few of its assertions are *choices* the filler makes, marked `CHOICE` in the
test: blank-line accounting, and ignore-before-parse ordering. If the real tool
chooses differently, change the assertion **and** `docs/STANDALONE_MODE.md`
§4–5, then re-read Worker B's prompt, which assumes the documented order.

**`test_integration.py` is an executable spec of the workflow.** It runs the
orchestrator's algorithm step for step against the real tools, with only
Worker B's judgement scripted. If you change the loop — the retry rule, when an
item may close, how resolutions are assigned — this is where it shows up.

## Failures that are decisions, not bugs

Two tests are deliberately brittle, because the thing they guard is expensive
to get wrong:

- **`test_known_signatures_are_frozen`** — item ids come from signatures, and
  `sweep` matches items across two scans by id. Change the normaliser and every
  id changes, so every in-flight run silently stops closing anything. Updating
  this test is allowed; doing it without re-initialising open runs is not.
- **`test_the_attempt_cap_is_the_same_everywhere`** — "3 attempts" is written
  into five prompts. If the default moves and the prompts do not, agents plan
  around a budget they do not have.

## Adding a test

Subclass `helpers.ToolTestCase`. It gives you `self.tmp`, `self.root` (an
isolated run tree), `self.logs`, `self.config`, and wrappers:

```python
class TestSomething(ToolTestCase):
    def test_it(self):
        self.given_logs({"app/app-2026-08-01.log": "2026-08-01 ERROR x\n"})
        self.given_config(ignore_files=[("arch", r"\.zip$", "no log content")])
        out, report = self.scan_to()
        self.init_ledger(out / "coverage-report.json")
        self.assertEqual(self.file_entry(report, "app/app-2026-08-01.log")["status"],
                         "detected")
```

For a workflow test, subclass `test_integration.MiniLoop` instead and script a
`decide` function in place of Worker B.

Write the failure message as the thing that would go wrong in production, not
as "expected 3, got 4". Every assertion here should answer "and then what?".

## What this suite does not cover

It tests the machinery, not the judgement. Whether Worker B correctly decides
that a `[DB-ERR]` line is a real error is not checked here — that depends on
the wiki and on the model, and it is what the human sees when reviewing a run.

The one live check of the agent layer is a real tick:

```
/coverage-start   →  "one tick"
```

Run that after changing a prompt. Two minutes, and it catches what static
checks cannot.
