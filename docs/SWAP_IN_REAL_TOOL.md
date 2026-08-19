# Swapping the fillers for the real thing

Four things in this repo are placeholders. Each has exactly one swap point, and
they are independent — you can replace them one at a time and the loop keeps
working with the rest still faked.

| filler | swap point | until you do |
|---|---|---|
| the monitoring tool | `tools/adapter.env` | `tools/fake/fake_standalone.py` scans for real, against the filler config |
| the logs folder | a `--logs-root` argument | `logs-sample/`, 12 deliberately messy files |
| the config | `config/logs-parsing-config.yml` | a filler with the same schema |
| the wiki | `/wiki-ingest` → `wiki/KNOWLEDGE_BASE.md` | 8 invented rules, marked FILLER |

`python3 tools/preflight.py` names which fillers are still in place on every run.

---

## 1. The monitoring tool

**If the real standalone mode already speaks the contract** (`docs/CONTRACTS.md`
§1–2), it is one line:

```bash
# tools/adapter.env
STANDALONE_CMD="/opt/monitoring/bin/monitor --standalone"
```

**If it does not**, write a translator and point at that instead:

```bash
STANDALONE_CMD="python3 $HERE/real/adapt_real_tool.py"
```

The translator takes the contract's flags, runs the real tool however it wants
to be run, and normalises its output into `coverage-report.json` plus the
`uncovered/` mirror. `tools/fake/fake_standalone.py` is a working reference for
the output shape — copy its report-building code.

Then verify:

```bash
python3 tools/preflight.py --logs-root <a small real folder>
```

Preflight runs the tool against a one-file folder and checks the report
actually appears and parses. If `produced_by` stops saying FILLER, the swap
took.

### The five things most likely to be wrong

Fill in `docs/STANDALONE_MODE.md` as you go — the LOAD-BEARING sections are
these, and getting one wrong makes the loop confidently wrong rather than
loudly broken:

1. **Relative paths.** `files[].path` must be relative to `logs_root`. Absolute
   paths break item matching and isolation, and the symptom is every item
   returning `isolation_failed`.
2. **Three file states.** `ignored` must be distinct from `undetected`. If the
   real tool reports both as "not parsed", the loop cannot tell finished work
   from unexamined work and will redo the ignores forever.
3. **Ignore-before-parse.** Worker B's decisions assume an ignore rule beats a
   parse rule at both levels. If the real tool is the other way round, say so
   in `STANDALONE_MODE.md` §4 and fix the worker prompt.
4. **Regex flavour.** `config_edit.py` validates with Python's `re`. If the real
   tool uses Java, .NET or RE2, a pattern can validate here and behave
   differently there. Lookbehind and possessive quantifiers are the usual
   offenders. Worth one deliberate test.
5. **Multi-line records.** If the real tool joins a stack trace into one record
   and the filler counts each frame as a line, item counts and line patterns
   both shift.

## 2. The logs folder

Nothing to edit — pass the real path:

```
/coverage-start on /mnt/logs/prod-client-a
```

For the first real run, **scope it to a subfolder**. A full scan of a very
large folder is the slowest step in the system and you want to see whether
Worker B's decisions are sound before committing to a few hundred of them.

Check `docs/STANDALONE_MODE.md` §9 first: log lines get copied into `runs/` and
shown to a model. If the folder holds PII or secrets, decide what happens about
that before the first scan, not after.

## 3. The config

Drop the real `logs-parsing-config.yml` into `config/`, then:

```bash
python3 tools/config_edit.py validate
```

- **Keep the three `# @anchor:` comments** (see the filler for placement). They
  are where new rules get inserted. Without them `config_edit.py` falls back to
  locating sections by indentation — which works, but do one
  `--dry-run` edit by hand to confirm before trusting it in a loop.
- **If the real schema differs** — different key names, nesting, or rule shape
  — that is the one change that touches more than a config file. Update
  `config_edit.py` (the `insert` calls and `validate`), the schema in
  `CONTRACTS.md` §3, and the op table in the `apply-config-change` skill.
  Nothing else refers to the schema.
- **Decide where the authoritative copy lives.** The loop edits this file. If
  production reads its config from somewhere else, the run produces a *proposed*
  config and someone has to move it — say so in `STANDALONE_MODE.md` §8, and
  keep `runs/<id>/config-backups/` as the change record.

## 4. The wiki

```
/wiki-ingest
```

It uses `wiki/export/` if that has content, otherwise a connected Confluence
MCP, saving a snapshot as it goes. Then it distils rules into
`wiki/KNOWLEDGE_BASE.md`.

**Delete the filler rules before the first real run.** They are invented, and
they are exactly the shape Worker B trusts to justify an ignore. An invented
ignore rule that survives into a real run is how the monitoring tool ends up
blind to something real.

The ingest report tells you which classes of log in your folder the wiki does
not cover. That list predicts where the run will escalate — fixing the wiki
first is much cheaper than answering the same question forty times mid-run.

---

## Order to do it in

1. **Wiki first.** It is the only piece a human has to think about, and it
   determines the quality of every decision the loop makes.
2. **Config and logs folder**, against the filler tool. The fake scanner reads
   real YAML and real logs, so this already tells you whether clustering and
   the report shape work on your data.
3. **The tool last.** By then everything else is known-good, so anything that
   breaks is the adapter.

Run one tick after each swap. `/coverage-start` → "one tick" → look at what the
three workers did. That is a two-minute check that catches nearly everything.
