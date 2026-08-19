---
name: assess-finding
description: Reference for judging an uncovered log file or line — how to search the wiki knowledge base for the right rule, how to write a parse or ignore regex that matches the shape without over-matching, and worked examples of each decision. Load when a coverage decision is not obvious, on a retry after a pattern failed, or before writing an escalation.
---

# Judging a finding

The decision tree and the JSON shape live in the `worker-b-assessor` prompt.
This is the reference for the parts that are actually hard: finding the right
wiki rule, and writing a pattern that works.

---

## Searching the knowledge base

Grep by **what the log is**, not by what the file is called. A rule that says
"database audit logs" never mentions `db-audit-2026-08-01.log`.

Search, in this order, stopping when you get a hit:

1. **The identifying token** from `example_text` — `ORA-`, `[DB-ERR]`,
   `SEVERE`, `GC`, the exception class name.
2. **The component or subsystem** — the service name in the path
   (`app/`, `db/`, `legacy/`), the logger name in brackets.
3. **The artefact kind** — "archive", "trace", "heartbeat", "timing",
   "audit", "rotated".
4. **The extension**, last — `.csv`, `.json`, `.zip`.

```bash
grep -rin -E 'ORA-|deadlock|audit' wiki/
```

Also grep `wiki/export/` when `KNOWLEDGE_BASE.md` comes up empty — the
distillation may have dropped a detail that the raw page has. If the raw export
settles it and the knowledge base does not, still escalate, but say so: the
knowledge base needs the rule added.

A rule with verdict `ESCALATE` is a **hit**, not a miss. It means the wiki
knows about this class and deliberately has not settled it. Cite it and
escalate — do not fall through to your own judgement.

---

## Writing the pattern

`item.signature` is the shape with the variable parts already normalised. Read
it, do not re-derive it from `example_text`.

| in the signature | in your regex |
|---|---|
| `<TS>` | `\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}` |
| `<DATE>` | `\d{4}-\d{2}-\d{2}` |
| `<N>` | `\d+` |
| `<HEX>`, `<UUID>` | `[0-9a-fA-F-]+` |
| `<PATH>` | `[\w./-]+` |

### The over-match test

Before you propose an ignore pattern, ask: **what else in this logs folder
would this match?** Say it out loud in the rationale.

- `\.log$` as an ignore-file pattern would silence every log in the folder.
- `ERROR` as an ignore-line pattern would silence the whole point of the tool.
- `^\s*\[` would silence every bracketed line, including `[DB-ERR]`.

An ignore pattern should be so specific that it reads as a description of one
thing. If you cannot describe in one sentence what it matches and nothing else,
it is too broad.

Parse patterns are the opposite: a slightly broad parse pattern costs noise,
which is recoverable. Prefer covering the shape.

### Anchoring

- File patterns: `(^|/)name-\d+\.log$`. The `(^|/)` makes it work at any depth;
  the `$` stops `output.zip.bak` matching an `\.zip$` rule.
- Line patterns: anchor to the left with `^` when the marker starts the line,
  otherwise anchor on the literal token itself — `\[DB-ERR\]` needs no `^`.
- Escape `.` in every extension. `\.zip$`, never `.zip$`.
- Rotated files are their own trap: `app.log.1`, `app.log.2026-08-01.gz`.
  If the wiki says the base log is monitored, cover the rotations in the same
  pattern: `(^|/)app\.log(\.\d+)?$`.

### `file_scope` on ignore-line rules

`file_scope` decides *which files* the ignore applies to. Default it to the
host file rule's own `pattern` — copy it from the config:

```bash
python3 tools/config_edit.py show   # then read config/logs-parsing-config.yml
```

Use `.*` only when the wiki says the noise is genuinely global. `[GC` lines are
the classic case: they turn up in every JVM log, so a global scope is right.
An application-specific `AUDIT` line is not.

---

## Worked examples

**Undetected file, wiki says parse.** `db/db-audit-2026-08-01.log`, signature
`db/db-audit-<DATE>.log`, KB-004 says PARSE. The file has two line shapes,
`[DB-ERR]` and `[DB-OK ]`, and KB-004 covers both.

```json
{"decision":"configure_parse","wiki_refs":["KB-004"],
 "proposed_change":{"op":"add-file-rule","rule_id":"db-audit-log",
   "pattern":"(^|/)db-audit-\\d{4}-\\d{2}-\\d{2}\\.log$",
   "line_patterns":["\\[DB-ERR\\]","\\[DB-OK ?\\]"]}}
```

Note the `?` — `[DB-OK ]` is padded to align with `[DB-ERR]`, and that padding
will not survive a format change.

**Unmatched line, wiki says ignore.** `[GC (Allocation Failure) …]` inside an
already-parsed `app-<date>.log`. KB-002 says IGNORE and says it is global.

```json
{"decision":"configure_ignore","wiki_refs":["KB-002"],
 "proposed_change":{"op":"add-ignore-line","rule_id":"gc-noise",
   "pattern":"^\\s*\\[(Full )?GC ","file_scope":".*",
   "reason":"JVM GC lines are performance diagnostics, not errors (KB-002)"}}
```

**Unmatched line, real signal, host file already parsed.** An `AUDIT` line in
`app-<date>.log`. Extend the existing rule rather than creating a new file
rule — `item.matched_by` names it.

```json
{"decision":"configure_parse","wiki_refs":["KB-003"],
 "proposed_change":{"op":"add-line-pattern","rule_id":"app-log",
   "pattern":"\\bAUDIT\\b user="}}
```

**Nothing in the wiki, and it looks like an error.** `legacy/old_app.log.1`
carrying `SEVERE LegacyBatch aborted`. If no rule covers legacy logs, this is
an escalation — never an ignore. It reads like a real failure, and the cost of
being wrong is that nobody hears about it.

```json
{"decision":"escalate","confidence":"low","wiki_refs":[],
 "escalation":{
   "question":"Is the legacy nightly batch still in production, and should its SEVERE lines page someone?",
   "options":["Parse it — add a rule for old_app.log and its rotations",
              "Ignore it — the batch is retired; record that as the reason"],
   "what_i_checked":"grepped wiki/ for 'legacy', 'old_app', 'batch', 'SEVERE' — no rule found"}}
```

---

## On a retry

Read `attempt-1/decision.json` and `attempt-1/c-result.json` first. Worker A
still says uncovered after a rule was applied, so **the pattern was wrong, not
the verdict.** Usual causes, in order of likelihood:

1. An unescaped or wrongly escaped character — `.`, `[`, `(`, `|`.
2. `$` anchoring against a path that has a suffix you did not expect.
3. `file_scope` on an ignore-line rule not matching the host file.
4. The pattern applied to the wrong op — an ignore-line rule where the *file*
   was never detected in the first place, so no line ever gets tested.

Cause 4 is the subtle one: if `a-result.json` says the file entry status is
`undetected`, no line rule of any kind will ever fire. Fix the file rule first.

If `c-result.json` says `"changed": false, "reason": "pattern already covered
by rule 'x'"`, the pattern is literally already in the config and does not
match. Propose a different one, or escalate with that fact — it is strong
evidence that the shape is not what it looks like.
