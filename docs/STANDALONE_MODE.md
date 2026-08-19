# Monitoring tool — standalone mode

> **THIS DOCUMENT IS A SKELETON FOR YOU TO FILL.**
>
> Every heading below is something the agent loop needs to know about the real
> tool. Where it says `FILL:`, replace it. Where a section already has content,
> it describes the assumption the loop currently runs on — correct it if the
> real tool differs, because the agents are built against these assumptions.
>
> The three sections marked **LOAD-BEARING** are the ones that break the loop
> if they are wrong. The rest is context.
>
> When this doc and `docs/CONTRACTS.md` disagree, CONTRACTS.md wins — it is the
> machine-readable contract. Fix this doc, or fix the adapter, so they agree.

---

## 1. What standalone mode is for

Production mode parses the logs folder looking for errors, guided by
`logs-parsing-config.yml`. It only ever opens files the config recognises, and
only ever reports lines the config matches. Anything the config does not
describe is invisible — and invisible is indistinguishable from healthy.

Standalone mode runs the **same production parsing code** offline against a
logs folder, with one difference: it attempts every file and every line
regardless of configuration, and reports what the configuration did and did not
cover. It is a coverage instrument, not a monitor. It raises no alerts.

<!-- FILL: anything else this mode exists to do — a compliance report, a
     pre-deployment gate, onboarding a new client's logs folder. -->

## 2. How it is invoked — **LOAD-BEARING**

The loop calls it only through `tools/standalone.sh`. The contract that script
must satisfy is in `docs/CONTRACTS.md` §1.

```
FILL: the real command line, exactly.
e.g.  /opt/monitoring/bin/monitor --standalone --input <DIR> --rules <FILE> --report-dir <DIR>
```

| the loop passes | the real tool's flag |
|---|---|
| `--logs-root DIR` | `FILL:` |
| `--config FILE` | `FILL:` |
| `--out DIR` | `FILL:` |
| `--max-unmatched-lines N` | `FILL:` (or "not supported — the adapter truncates") |
| `--label STR` | `FILL:` (or "not supported — the adapter ignores it") |

- Exit codes: `FILL:` — which mean success, which mean a config error, which mean a partial scan?
- Runtime on a realistic folder: `FILL:` — this sets how often the loop can afford to sweep.
- Does it need anything besides the logs folder (a database, credentials, a licence)? `FILL:`
- Can two copies run at once? `FILL:` — the loop scans an isolated folder while a full scan may be running.

## 3. What it outputs — **LOAD-BEARING**

Target shape: `docs/CONTRACTS.md` §2.

- Does it already emit that JSON? `FILL: yes / no`
- If no, what does it emit? `FILL:` — attach or link a real sample.
- Where does the translation happen? `FILL: tools/real/adapt_real_tool.py`

Specifically:

- Are report paths relative to the logs root, or absolute? `FILL:` — the loop needs **relative**.
- Does it distinguish *ignored* from *undetected*? `FILL:` — the loop needs all three states, see CONTRACTS §2.
- Does it write the mirrored uncovered-lines folder? `FILL:`
- Per-file line counts — seen / matched / ignored / unmatched? `FILL:`

## 4. Matching semantics — **LOAD-BEARING**

The loop currently assumes, and the filler implements:

```
file:  ignore.files  →  files[].pattern  →  otherwise UNDETECTED
line:  ignore.lines  →  files[].line_patterns  →  otherwise UNMATCHED
first match wins within each list
```

- Is that the real order? `FILL:` — **if ignore is evaluated after parse, say so.** Worker B's decisions assume ignore wins.
- Regex flavour: `FILL:` — PCRE, RE2, Java, .NET? Python's `re` is what `config_edit.py` validates with. Differences that matter: lookbehind, possessive quantifiers, named groups, `\d` under Unicode.
- Are patterns matched against the full relative path, the basename, or both? `FILL:` — the loop writes `(^|/)name$` patterns, which assume full relative path.
- Case sensitivity: `FILL:`
- Is matching `search` (anywhere) or `fullmatch` (whole string)? `FILL:` — the filler uses `search`.

## 5. What counts as a line

- How are multi-line records handled — a Java stack trace, a JSON blob spanning lines? `FILL:`
  The loop currently treats every physical line as one line. If the real tool
  joins stack frames into one record, item counts will differ and Worker B's
  line patterns need to match the *record*, not the frame.
- Encoding, and what happens on invalid bytes? `FILL:`
- Very long lines — truncated, split, skipped? `FILL:`
- Blank lines: the filler counts them as ignored. `FILL:` the real behaviour.

## 6. What it does with files it cannot read

- Binary files: `FILL:` — detected how? Reported how?
- Compressed files — does it look inside `.gz`? `FILL:` This matters: if it
  does, an `\.gz$` ignore rule silently drops real logs.
- Permission denied, broken symlinks, files being written to right now: `FILL:`
- Does it follow symlinks out of the logs root? `FILL:`

## 7. Scale

- Largest folder it has run against: `FILL:` files / GB / wall-clock.
- Memory profile: `FILL:` — does it hold the folder in memory?
- Is there a way to scan a subfolder or a file list? `FILL:` — useful for
  scoping a first run, and for making sweeps cheaper.

## 8. Config file

- Path in production: `FILL:`
- Is `config/logs-parsing-config.yml` in this repo the real schema? `FILL:`
- Is it hot-reloaded, or read once at start? `FILL:`
- Is it under version control / change control elsewhere? `FILL:` — the loop
  edits it, so say where the authoritative copy lives and how changes get back
  to it.
- Are there config sections beyond `files` and `ignore` that the loop must not
  disturb? `FILL:`

## 9. Safety

- Confirm the tool never writes into the logs folder: `FILL:`
- Does it need production credentials, and does running it offline touch
  anything live? `FILL:`
- Do the logs contain anything that must not leave the machine (PII, secrets,
  customer data)? `FILL:` — the loop copies log lines into `runs/` and shows
  them to a model. If yes, say what must be redacted and where.

## 10. Anything else the loop should know

<!-- FILL: quirks, known bugs, a rule that behaves surprisingly, a folder that
     always fails, the one person who knows why a pattern is written that way. -->
