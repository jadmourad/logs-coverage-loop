# Log coverage knowledge base

> **FILLER.** These rules are invented placeholders written against
> `logs-sample/` so the loop can be exercised end to end. Replace this whole
> file by running `/wiki-ingest` against your real Confluence export.
>
> Worker B treats this file as the source of truth for every ignore/parse
> decision. Leaving invented rules in place once the real logs folder is
> connected would let the loop deliberately blind the monitoring tool to real
> errors. Delete them before the first real run.

Generated: filler, never ingested
Source: none — hand-written placeholder

## How to read this

One rule per class of file or log line. Worker B matches on **Applies to** and
**Pattern hint**, and cites the rule id in its decision.

**Verdict** is one of:

- `PARSE` — a human needs to know when this appears; configure the tool to read it
- `IGNORE` — not an error signal; configure the tool to skip it, with this reason
- `ESCALATE` — the wiki knows about this class but does not settle it; ask a human

---

## Rules

### KB-001 — Compressed build archives
- **Applies to:** files ending `.zip`, `.gz`, `.tar`, `.7z` anywhere under the logs folder
- **Pattern hint:** `\.(zip|gz|tar|7z)$`
- **Verdict:** IGNORE
- **Why:** Archives carry no directly parseable log content. Anything inside them that matters is written separately as a plain log by the same job.
- **Source:** FILLER

### KB-002 — JVM garbage collection lines
- **Applies to:** lines beginning `[GC` or `[Full GC`, in any log
- **Pattern hint:** `^\s*\[(Full )?GC `
- **Verdict:** IGNORE
- **Why:** GC activity is a performance diagnostic, not an error. Memory pressure is alerted on from JVM metrics, not from log parsing.
- **Source:** FILLER

### KB-003 — Application error and stack-trace lines
- **Applies to:** application logs (`app-<date>.log`); lines carrying `ERROR` or `FATAL`, and the stack frames under them
- **Pattern hint:** `\b(ERROR|FATAL)\b`, `^\s+at [\w.$/]+\(`
- **Verdict:** PARSE
- **Why:** This is the primary error signal the monitoring tool exists to catch.
- **Source:** FILLER

### KB-004 — Database audit logs
- **Applies to:** `db-audit-<date>.log`; lines tagged `[DB-ERR]` or `[DB-OK ]`
- **Pattern hint:** `\[DB-ERR\]`
- **Verdict:** PARSE
- **Why:** `ORA-` conditions such as deadlocks and snapshot-too-old are real production incidents and are only reported here. `[DB-OK ]` lines are the completion records that prove a job ran, so they are parsed too.
- **Source:** FILLER

### KB-005 — Distributed trace files
- **Applies to:** `traces/trace-<id>.json`
- **Pattern hint:** `(^|/)traces/`
- **Verdict:** ESCALATE
- **Why:** Traces contain a per-span `status` field that can read `ERROR`, but the tracing backend already alerts on span errors. Whether the log parser should duplicate that has not been decided.
- **Source:** FILLER — a deliberate example of an honest unresolved rule

### KB-006 — Build timing tables
- **Applies to:** `artifacts/build-<n>/timings.csv`
- **Pattern hint:** `(^|/)timings\.csv$`
- **Verdict:** IGNORE
- **Why:** Stage durations are build telemetry, reported by the CI system. A slow stage is not a production error.
- **Source:** FILLER

### KB-007 — Runner heartbeat files
- **Applies to:** `runner/heartbeat.txt` and similar liveness files
- **Pattern hint:** `(^|/)heartbeat\.txt$`
- **Verdict:** IGNORE
- **Why:** Liveness is monitored by the scheduler. A missing heartbeat is detected by absence, which log parsing cannot see anyway.
- **Source:** FILLER

### KB-008 — Legacy batch logs
- **Applies to:** `legacy/old_app.log`, including rotated suffixes `.1`, `.2`
- **Pattern hint:** `(^|/)old_app\.log(\.\d+)?$`
- **Verdict:** PARSE
- **Why:** The legacy batch still runs nightly and `SEVERE` lines from it are real failures. It is out of support but not out of production.
- **Source:** FILLER
