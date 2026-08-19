---
name: run-tests
description: Run the non-regression suite and explain any failure in terms of what it means for the loop. Use when asked to run the tests, check nothing is broken, before starting a run against a real logs folder, or after changing any tool, prompt, skill or config schema.
---

# Run the non-regression suite

```bash
python3 tests/run_tests.py
```

One module at a time while iterating:

```bash
python3 tests/run_tests.py ledger        # or config, scanner, verdict,
                                         # isolate, integration, wiring
python3 tests/run_tests.py -v            # one line per test
```

No dependencies beyond `pyyaml`. It never touches the real `runs/`,
`logs-sample/` or `config/logs-parsing-config.yml` -- every test builds its own
throwaway copies.

## Run it when

- **before any run against a real logs folder** -- the loop edits a production
  config unattended, hundreds of times
- after changing anything under `tools/`
- after editing an agent prompt, a skill, or `.claude/settings.json`
- after swapping in the real monitoring tool, the real config, or the real
  wiki -- `test_scanner_contract.py` in particular is the acceptance test for
  the real tool
- when a run behaves oddly and you want to know whether the machinery or the
  judgement is at fault

## Reporting a failure

Do not just paste the traceback. Say what it means:

| module | a failure here means |
|---|---|
| `test_ledger` | run state is unreliable -- items may be lost, double-worked, or closed without a reason |
| `test_config_edit` | the production config could be corrupted, or a rule silently does nothing |
| `test_scanner_contract` | the tool and the loop disagree about the report; every verdict downstream is suspect |
| `test_verdict` | the loop's exit condition is wrong -- it can close a real gap and report success |
| `test_isolate` | findings are tested against the wrong file, so verdicts are meaningless |
| `test_integration` | the workflow itself is broken -- retries, escalation, or the sweep |
| `test_agent_wiring` | a prompt tells an agent to do something that is no longer possible |

Then decide which of two things happened, and say which:

1. **A real defect.** Fix the code. The test stays as written.
2. **A deliberate change** the test has not been told about. Update the test
   *and* say plainly what behaviour changed -- a test edited to match new
   behaviour is a decision, not a formality.

Two failures are load-bearing enough to call out explicitly:

- **`test_known_signatures_are_frozen`** -- item ids are derived from
  signatures, and the sweep matches items across scans by id. Changing the
  normaliser invalidates every in-flight run's ledger. If that is intended, say
  so and tell the human their open runs need re-initialising.
- **`test_path_rules_use_edit_not_write`** -- a `Write(...)` permission rule
  looks correct and matches nothing, so a protection you think you have is not
  there.

Finish with the count and a plain verdict: green and safe to run, or not.
