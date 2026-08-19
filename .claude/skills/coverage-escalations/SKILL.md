---
name: coverage-escalations
description: Walk the human through the coverage items the loop could not settle, apply their decisions to the config, and write each answer back into the wiki knowledge base so the same question never has to be asked twice. Use when asked to "handle the escalations", "answer the coverage questions", or after a run reports items waiting on a human.
---

# Escalations

Items land here when the wiki could not settle whether something is a real
error, or when three attempts failed. Each one is a question only the human can
answer.

The point of this skill is not just to unblock the items. It is to make sure
**every answer becomes a rule**, so the next run resolves that whole class by
itself. An escalation answered but not written down is a question you will ask
again next month.

Run from the repo root.

## 1. Gather

```bash
python3 tools/ledger.py status
ls runs/<RID>/escalations/
```

Read each escalation file. Group them before presenting: identical questions
about the same class of log get asked once, not five times.

## 2. Ask

For each group, give the human, briefly:

- the log line or filename, verbatim — that is what they actually recognise
- where it lives and how many report rows ride on it (`occurrences`)
- what Worker B checked in the wiki and did not find
- the concrete choice: **parse it** (and what the rule would be) or **ignore
  it** (and what reason goes in the audit trail)

Use `AskUserQuestion` when the choice is genuinely a pick-one. Ask about a
whole group in one question. Do not editorialise about which is likelier —
they know their logs; the loop escalated precisely because the evidence did not
decide it.

## 3. Apply

For each answered item:

```bash
# parse it
python3 tools/config_edit.py add-file-rule --rule-id <ID> --pattern '<RX>' --line-pattern '<RX>'
python3 tools/config_edit.py add-line-pattern --rule-id <HOST> --pattern '<RX>'

# ignore it -- the reason is not optional, it is the audit trail
python3 tools/config_edit.py add-ignore-file --rule-id <ID> --pattern '<RX>' --reason '<their words>'
python3 tools/config_edit.py add-ignore-line --rule-id <ID> --pattern '<RX>' --file-scope '<RX>' --reason '<their words>'
```

Then put the item back in the loop so **Worker A proves the rule works**. Do
not mark it done from the human's say-so — the human decided the policy, the
re-run verifies the regex:

```bash
python3 tools/ledger.py update --item-id <ID> --status pending --actor human \
  --note "human decision applied: <parse|ignore> -- returned to the loop for verification"
```

Then run one tick per returned item (spawn `coverage-orchestrator`), or tell
the human to run `/coverage-start` and resume.

## 4. Write it back to the wiki — do not skip this

Append a rule to `wiki/KNOWLEDGE_BASE.md` for every answer, in the house format:

```markdown
### KB-<next number> — <short name>
- **Applies to:** <file shape or line shape, plainly described>
- **Pattern hint:** `<the regex that was added>`
- **Verdict:** PARSE | IGNORE
- **Why:** <the human's reason, in their words>
- **Source:** human decision, <date>, coverage run <RID>, item <ID>
```

Then draft the same rule for the real Confluence wiki in
`wiki/pending-wiki-updates/<item-id>.md` — the knowledge base is a snapshot, so
a rule that only lives here is lost the next time `/wiki-ingest` rebuilds it.
Tell the human which pages to update. If a Confluence MCP tool is connected and
they explicitly ask you to push the update, you may — **publishing to the team
wiki is their call, not yours.** Never push unasked.

## 5. Report

Say how many were answered, how many rules were added, how many items went back
into the loop, and what is still open. If any item is still unresolved after
their answer, say exactly which and why — that is a wrong regex, not a wrong
decision, and it needs another pass.
