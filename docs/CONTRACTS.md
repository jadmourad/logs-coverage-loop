# Contracts

Everything the loop depends on, written down. If the real monitoring tool
honours §1 and §2, nothing else in this repo has to change.

---

## 1. The standalone tool — CLI

Invoked only through `tools/standalone.sh`, which execs `$STANDALONE_CMD` from
`tools/adapter.env`.

```
<cmd> --logs-root DIR --config FILE --out DIR [--max-unmatched-lines N] [--label STR]
```

| flag | meaning |
|---|---|
| `--logs-root` | folder to scan, recursively. **Read-only** — the tool must never write here. |
| `--config` | the `logs-parsing-config.yml` to apply. |
| `--out` | where to write the report and the mirrored uncovered lines. Created if absent. |
| `--max-unmatched-lines` | cap on unmatched-line samples kept **per file**. Default 50. |
| `--label` | free-text tag echoed into the report. Used to tell scans apart. |

Exit `0` on a completed scan (even one with zero coverage — that is a result,
not an error). Non-zero on a config or scan failure, with the reason on stderr.

The tool must scan **every file**, not only files its config recognises. That
is the whole difference between standalone mode and production mode.

---

## 2. The standalone tool — output

### `<out>/coverage-report.json`

```jsonc
{
  "schema_version": 1,
  "generated_at": "2026-08-04T12:00:00+00:00",
  "produced_by": "...",              // free text; used to detect the FILLER tool
  "label": "initial",
  "logs_root": "/abs/path/to/logs",
  "config_path": "/abs/path/logs-parsing-config.yml",
  "config_fingerprint": "sha256:ab12…",   // changes when the config changes
  "max_unmatched_lines_per_file": 50,

  "totals": {
    "files_seen": 0, "files_detected": 0, "files_ignored": 0, "files_undetected": 0,
    "lines_seen": 0, "lines_matched": 0, "lines_ignored": 0, "lines_unmatched": 0
  },

  "files": [
    {
      "path": "app/server-01/app-2026-08-01.log",  // RELATIVE to logs_root, forward slashes
      "size_bytes": 12043,
      "status": "detected",            // detected | undetected | ignored
      "matched_by": "app-log",         // config rule id, when detected
      "ignored_by": null,              // ignore rule id, when ignored
      "binary": false,
      "lines": { "seen": 900, "matched": 300, "ignored": 100, "unmatched": 500 },
      "unmatched_sample_path": "uncovered/app/server-01/app-2026-08-01.log",
      "unmatched_samples": [ { "line_no": 12, "text": "…" } ]   // capped at max_unmatched_lines
    }
  ]
}
```

Required by the loop: `logs_root`, `totals`, and for each file `path`,
`status`, `lines`, `unmatched_samples`. The rest is useful but not load-bearing.

**`path` must be relative to `logs_root`.** The loop matches report entries to
ledger items by this string, and isolation rebuilds this exact path. An
absolute path here breaks both.

**Three file states, not two.** `ignored` is distinct from `undetected`:
"deliberately skipped, here is the rule" versus "invisible, nobody decided
that". Collapsing them makes the report useless — the loop cannot tell
finished work from unexamined work.

### `<out>/uncovered/<same relative path>`

For every parsed file with unmatched lines: the raw unmatched lines, up to
`--max-unmatched-lines`, in the same folder structure with the same filename.

Raw lines only — no line-number prefixes, no headers. Two reasons: a human
opens it and sees log lines they recognise, and the file can be fed straight
back to the tool as a test input.

### `<out>/summary.md`

Optional, human-facing. Not read by any agent.

---

## 3. `logs-parsing-config.yml`

```yaml
version: 1

files:                                  # what the tool can parse
  - id: app-log                         # unique across the WHOLE config
    pattern: '(^|/)app-\d{4}-\d{2}-\d{2}\.log$'
    line_patterns:
      - '\b(ERROR|FATAL)\b'
  # @anchor:file-rules

ignore:                                 # what it deliberately skips
  files:
    - id: archives
      pattern: '\.(zip|gz|tar|7z)$'
      reason: 'why this is not monitored'     # required
      wiki_ref: 'KB-001'                      # when one exists
    # @anchor:ignore-files
  lines:
    - id: gc-noise
      file_scope: '.*'                        # which files this ignore applies to
      pattern: '^\s*\[(Full )?GC '
      reason: 'why these lines are not errors'
      wiki_ref: 'KB-002'
    # @anchor:ignore-lines
```

**Matching order** — the FILLER tool implements this. **Verify it against the
real tool and correct this section if it differs**, because Worker B's decision
tree assumes it:

```
file:  ignore.files  →  files[].pattern  →  otherwise UNDETECTED
line:  ignore.lines  →  files[].line_patterns  →  otherwise UNMATCHED
```

First match wins within each list. Ignore is checked first at both levels, so
an ignore rule suppresses a parse rule.

**The `# @anchor:` comments** mark where `config_edit.py` inserts new rules.
Keep them in the real config. Without them the editor falls back to locating
sections by indentation, which works but is worth verifying once by hand.

---

## 4. Worker handoff files

All under `runs/<run-id>/items/<item-id>/attempt-<n>/`.

### `a-result.json` — Worker A → orchestrator, Worker B

Written by `tools/verdict.py`, never by hand.

```jsonc
{
  "item_id": "a1b2c3d4e5f6",
  "kind": "unmatched_lines",
  "attempt": 1,
  "verdict": "still_uncovered",   // covered | ignored | still_uncovered | isolation_failed
  "detail": "1 line(s) still unmatched in a detected file",
  "note": "",                     // Worker A's own observations
  "target": { "rel_path": "app/server-01/app-2026-08-01.log", "line_no": 6 },
  "isolated_root": "…/attempt-1/isolated",
  "report_path": "…/attempt-1/report/coverage-report.json",
  "uncovered_dir": "…/attempt-1/report/uncovered",
  "file_entry": { },              // the file's entry from the isolated scan
  "totals": { },
  "config_fingerprint": "sha256:…",
  "loop_exits": false             // true when verdict is covered or ignored
}
```

`loop_exits` is the mini-loop's exit condition. It is computed by code, not by
a model — see the header of `tools/verdict.py` for why.

### `decision.json` — Worker B → orchestrator, Worker C

```jsonc
{
  "item_id": "a1b2c3d4e5f6",
  "attempt": 1,
  "decision": "configure_ignore",   // configure_parse | configure_ignore | escalate
  "confidence": "high",             // high | medium | low
  "wiki_refs": ["KB-002"],
  "rationale": "…",
  "proposed_change": {              // required unless decision is escalate
    "op": "add-ignore-line",        // add-file-rule | add-ignore-file | add-line-pattern | add-ignore-line
    "rule_id": "gc-noise",
    "pattern": "^\\s*\\[(Full )?GC ",
    "line_patterns": [],            // add-file-rule only
    "file_scope": ".*",             // add-ignore-line only
    "reason": "…",                  // required for both ignore ops
    "wiki_ref": "KB-002"
  },
  "escalation": {                   // required when decision is escalate
    "question": "…",
    "options": ["…", "…"],
    "what_i_checked": "…"
  }
}
```

### `c-result.json` — Worker C → orchestrator

```jsonc
{
  "item_id": "a1b2c3d4e5f6",
  "attempt": 1,
  "applied": true,
  "op": "add-ignore-line",
  "rule_id": "gc-noise",
  "pattern": "^\\s*\\[(Full )?GC ",
  "changed": true,                  // false = no-op; see notes for why
  "backup": "runs/<rid>/config-backups/…yml",
  "validated": true,
  "green_light": true,              // config changed AND validates
  "notes": "…"
}
```

`green_light` means the config moved and is valid. It does **not** mean the
item is resolved — only Worker A's next run decides that.

---

## 5. Item states

```
pending ──claim──> in_progress ──┬──> done       (a re-run proved it)
   ^                             ├──> escalated  (a human must decide)
   └────── returned by ──────────┘
        /coverage-escalations
```

`blocked` exists in the schema for a human to park something manually. The loop
never sets it.

Resolutions on `done`:

| resolution | meaning |
|---|---|
| `configured_parse` | a parse rule was added and the re-run proved it |
| `configured_ignore` | an ignore rule was added and the re-run proved it |
| `already_covered` | resolved on attempt 1 with no change — an earlier item's rule had it |
| `swept` | a full re-scan showed it covered; closed without a mini-loop |

`ledger.py update --status done` refuses to run without a `--resolution`. That
is deliberate: "done, for reasons I did not record" is how a coverage gap gets
quietly closed.
