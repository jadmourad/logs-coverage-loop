---
name: wiki-ingest
description: Build or refresh wiki/KNOWLEDGE_BASE.md — the offline, distilled set of decision rules Worker B uses to judge whether a log is a real error or noise. Pulls from a Confluence MCP tool or a local export, snapshots the raw source, then distils it. Use before the first coverage run, and whenever the wiki changes.
---

# Wiki ingest

Worker B decides, hundreds of times, whether a log line is a real error. It
does that against `wiki/KNOWLEDGE_BASE.md` and nothing else — a local,
distilled file, not a live Confluence call. That is deliberate: hundreds of
network round-trips would be slow, would make the loop dependent on the wiki
being up, and would make two runs of the same input give different answers.

This skill is what keeps that snapshot honest.

## 1. Get the source

**If `wiki/export/` already has content**, use it. That is a snapshot the human
saved deliberately; do not go to the network behind their back.

**Otherwise, if a Confluence MCP tool is connected**, fetch the pages the human
names. Search for the space or page tree they point you at; do not guess at
page ids. **Save the raw fetched content under `wiki/export/`** as you go —
one file per page, named after the page title. That snapshot is what makes the
next rebuild reproducible and what lets the loop run with the network down.

**Otherwise**, ask the human for one of: an export into `wiki/export/`, the page
URLs plus a connected MCP, or a pasted page. Do not invent rules to fill the
gap — a knowledge base with made-up rules is worse than an empty one, because
Worker B will trust it and stop escalating.

## 2. Distil

Read everything in `wiki/export/` and write `wiki/KNOWLEDGE_BASE.md`. You are
extracting **decision rules**, not summarising pages. One rule per distinct
class of file or log line:

```markdown
### KB-007 — JVM garbage collection lines
- **Applies to:** lines beginning `[GC` or `[Full GC` in any application log
- **Pattern hint:** `^\s*\[(Full )?GC `
- **Verdict:** IGNORE
- **Why:** GC activity is a performance diagnostic, not an error. Memory
  pressure is alerted on separately from JVM metrics.
- **Source:** Logging Standards / GC Logging, page id 88213
```

Rules for the distillation:

- **Verdict is `PARSE`, `IGNORE`, or `ESCALATE`.** Use `ESCALATE` when a page
  discusses a class but does not settle it — that is real information, and it
  stops Worker B re-deriving the same ambiguity every time.
- **Carry the human-recognisable tokens** into "Applies to" and "Pattern hint":
  the error prefix (`ORA-`, `[DB-ERR]`), the component name, the filename
  shape. That is what Worker B greps on.
- **Keep the `Why`.** Worker B cites it, escalation writeups quote it, and in
  six months it is the only thing that explains why a log is not monitored.
- **Always cite the `Source` page.** A rule with no source cannot be checked.
- **Do not resolve contradictions yourself.** If two pages disagree, write one
  rule with verdict `ESCALATE` and note both. That is an honest snapshot.
- Preserve any `Source: human decision` rules already in the file — those came
  from `/coverage-escalations` and are not in Confluence yet. Check
  `wiki/pending-wiki-updates/` and carry those forward too.

## 3. Report

Tell the human: how many pages were read, how many rules came out, the split
across PARSE / IGNORE / ESCALATE, and — most usefully — **which classes of log
in their folder the wiki does not cover at all**. That last list predicts
exactly where the coverage run will escalate, and it is cheaper to fix the wiki
now than to answer the same question forty times mid-run.
