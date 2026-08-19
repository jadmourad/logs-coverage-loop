---
name: apply-config-change
description: Reference for changing logs-parsing-config.yml safely — which config_edit.py op maps to which decision, shell-quoting regexes correctly, and what to do when an edit is refused, rolled back, or silently a no-op. Load when an edit does not apply cleanly or when the op to use is not obvious.
---

# Changing the config

The command list is in the `worker-c-configurator` prompt. This covers the
cases where an edit does not simply work.

## Picking the op

The trap is treating a line problem as a file problem, or the reverse. Read
`a-result.json` → `file_entry.status` first; it tells you which layer is
actually broken.

| `file_entry.status` | what is wrong | op |
|---|---|---|
| `undetected` | no file rule matches the path | `add-file-rule` or `add-ignore-file` |
| `detected`, lines unmatched | file is parsed, these lines are not | `add-line-pattern` or `add-ignore-line` |
| `ignored` | nothing is wrong | do not edit; red-light it |

**If the file is `undetected`, a line rule cannot help.** The tool never opens
an undetected file, so no line pattern of any kind will ever be tested against
it. Adding one produces a green light and a config change that does nothing,
and the next attempt fails identically. If Worker B proposed a line op on an
undetected file, red-light it and say exactly that.

For `add-line-pattern`, `--rule-id` is the **host file rule** — the one already
parsing the file, named in `a-result.json` as `file_entry.matched_by`. It is
not a new id.

## Quoting

Single quotes, always:

```bash
--pattern '(^|/)db-audit-\d{4}-\d{2}-\d{2}\.log$'
```

Double quotes let the shell eat `\d`, `$` and backticks, and the failure is
silent — you get a rule that looks right in the config and matches nothing. If
the pattern itself contains a single quote, that is the one case to escape:
`'it'\''s'`.

`config_edit.py` writes patterns as single-quoted YAML scalars, which do not
process escapes, so what you pass is what the tool compiles.

## Reading the outcome

**`{"changed": true}`** — applied, validated, backed up. Green light.

**`{"changed": false, "reason": "pattern already covered by rule 'x'"}`** — the
pattern is already in the config. Red light. This is information, not a
failure: Worker A found the item uncovered *with that pattern already present*,
so the pattern does not match what everyone thinks it matches. Put that
sentence in `notes` — it is the single most useful thing the next Worker B
attempt can be told.

**`{"changed": false, "reason": "rule id 'x' already exists"}`** — a different
rule owns the id. Re-run with a suffixed id and note the rename.

**Exit 1, `"rolled_back": true`** — the edit made the config invalid and the
previous version was restored. The config on disk is safe. Copy the `problems`
list into `notes`; it names the exact regex that would not compile.

**Exit 2, anchor not found** — the config has no `# @anchor:` comment for that
section and the fallback could not locate the section by indentation. This is a
setup problem, not a retry: red-light it and say the config needs its anchor
comments back (see `config/logs-parsing-config.yml` for where they go).

## Recovering by hand

Every write is backed up first, newest last:

```bash
ls -t runs/<RID>/config-backups/ | head
python3 tools/config_edit.py validate
```

If the config is somehow invalid despite the rollback, restore the newest
backup that validates and report what happened. Never leave a run with a config
that does not validate — every subsequent scan would fail and every item after
this one would report a false `still_uncovered`.

## What not to do

- **Do not remove or narrow an existing rule** to make an item pass. Losing
  coverage to close a coverage item is backwards. Red-light it and explain.
- **Do not add a rule for a decision you invented.** You apply Worker B's
  decision. If it is unusable, hand it back.
- **Do not batch several changes into one attempt.** One change, then Worker A
  re-runs. If two changes go in together and the item is still uncovered,
  nobody can tell which one was wrong.
