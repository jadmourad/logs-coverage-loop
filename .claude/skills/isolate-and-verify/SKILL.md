---
name: isolate-and-verify
description: Reference for rebuilding a single log file or log line into a throwaway folder and scanning it — including the cases that go wrong, such as moved line numbers, rotated and huge files, binaries, and shapes that need more than one example to verify. Load when isolation fails or when a single-file test is not enough to trust the result.
---

# Isolating a finding

The happy path is in the `worker-a-runner` prompt. This is what to do when it
does not work.

## Why the path must be preserved exactly

The monitoring tool decides whether it can parse a file by matching its
**path**. Copy `app/server-01/app-2026-08-01.log` to `/tmp/test.log` and the
tool correctly reports it as undetected — you have proved nothing except that
you renamed it. `tools/isolate.py` rebuilds the directory chain for this
reason, and `--out` must be a folder you then pass as `--logs-root`.

## When the report does not contain your file

`verdict.py` returns `isolation_failed`. Check in this order:

```bash
ls -R <ATT>/isolated
```

1. **Nothing there** — `isolate.py` exited non-zero. Re-run it and read stderr.
   Usually the source path does not exist: the logs folder has changed since
   the initial scan, or `example_path` has a character that got mangled.
2. **File there, wrong path** — `--path` must be relative to `--logs-root`,
   with no leading slash. `isolate.py` refuses paths that escape the root.
3. **File there, right path, still missing from the report** — the scan wrote
   its report somewhere else. Confirm `--out` and re-read
   `<ATT>/report/coverage-report.json`.

## When the line number has moved

For `unmatched_lines`, `example_line_no` comes from the *initial* scan. A live
logs folder rotates and appends; by the time the mini-loop reaches this item
the line may have moved or gone.

Verify before you trust it — compare what you isolated against
`example_text`. If they differ, find the line by content instead:

```bash
grep -n -F -- '<a distinctive fragment of example_text>' <LOGS>/<example_path> | head -3
```

Then isolate that line number. If the text is gone from the file entirely, look
in another entry of `affected_files`. If it is gone everywhere, the line no
longer occurs — return `isolation_failed` with a note saying so, and let the
orchestrator escalate it. **Do not close the item yourself**: a shape that
vanished between scans is exactly the kind of thing a human should see.

## Rotated files

`app.log.1`, `app.log.2026-08-01.gz`. Isolate the exact path from the report,
not the base name — the rotation suffix is usually the whole reason the file is
undetected, and a rule written against the base name will not fix it.

## Huge files

`isolate.py` truncates a whole-file copy at `--max-bytes` (5MB) and reports
`"truncated": true`. That is fine for `undetected_file` items — file detection
looks at the path, not the content. It is **not** fine if you are trying to
show a line near the end; pass `--lines` instead, which seeks by line number
and ignores the cap.

## Binary files

A binary that a parse rule matches is a finding worth reporting, not a
success. `verdict.py` flags it in the note. Surface it: it almost always means
a file rule is too broad, and a too-broad parse rule will fill the monitoring
tool with garbage.

## When one example is not enough

`item.occurrences` is how many report rows the item stands for, and
`affected_files` lists up to 20 of them. One example is the right default on
the first attempt — you are testing a shape, and the fix is a regex.

Isolate several when:

- the item is a **retry after a config change** — a pattern that matches one
  example and not its siblings is the most common failure, and testing one
  example cannot see it;
- `affected_files` entries differ in a way the regex has to span — different
  depths (`app/server-01/` vs `app/edge/eu/server-14/`), zero-padded versus
  bare numbers, different date formats.

Isolate two or three into the **same** `isolated/` root, then scan once:

```bash
python3 tools/isolate.py --logs-root <LOGS> --path <first>  --out <ATT>/isolated --clean
python3 tools/isolate.py --logs-root <LOGS> --path <second> --out <ATT>/isolated
python3 tools/isolate.py --logs-root <LOGS> --path <third>  --out <ATT>/isolated
```

`--clean` only on the first — it wipes the folder.

`verdict.py` judges the entry for `example_path`. When you isolate several,
also read the report yourself and say in `note` whether the *others* came back
covered. A pattern that fixes one of three siblings has not fixed the item, and
Worker B needs to know that on the next attempt.
