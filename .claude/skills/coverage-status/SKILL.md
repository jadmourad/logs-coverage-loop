---
name: coverage-status
description: Report where the log-coverage run stands — how much of the logs folder the monitoring tool now sees, what the loop closed, and what is waiting on a human. Use when asked "how is the coverage run going", "what's left", or for a progress check mid-run.
---

# Where the run stands

Read-only. Never change the ledger or the config from here.

```bash
python3 tools/ledger.py status
python3 tools/config_edit.py show
```

Read `runs/<RID>/ledger.json` for `totals_at_init` and the `sweeps` array — the
last sweep's `totals` are the current coverage numbers.

Report in this order, in plain language. Lead with the number that matters:

**1. Coverage, then versus now.**

| | at start | now | change |
|---|---|---|---|
| files the tool cannot see | `totals_at_init.files_undetected` | latest sweep | |
| lines it cannot parse | `totals_at_init.lines_unmatched` | latest sweep | |

If there has been no sweep yet, say so — the initial numbers are all you have,
and the config may already be ahead of them.

**2. The work.** Items done / escalated / pending / in progress, and the
percentage closed. Break `done` down by resolution — it tells a different
story each way:

- `configured_parse` — the tool now monitors something it used to miss. **This
  is the win.**
- `configured_ignore` — deliberately out of scope now, with a written reason.
- `already_covered` / `swept` — closed for free by another item's rule.

**3. What is waiting on them.** Every escalated item: the question, one line
each, and the file path. Tell them `/coverage-escalations` answers these.

**4. Whether the goal is met.** Only when pending, in_progress and blocked are
all zero. If escalations are open, the honest statement is "blocked on N
decisions from you", not "finished".

If any item is sitting at `attempts: 3` but not yet escalated, or an item has
been `in_progress` since before the last tick, flag it — that is a stuck loop,
not slow progress.
