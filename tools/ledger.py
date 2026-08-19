#!/usr/bin/env python3
"""Ledger: the durable task list for a coverage run.

This is the Ralph-loop "task list" piece. Every agent in the loop reads and
writes run state ONLY through this CLI. Nothing hand-edits ledger.json --
that is what keeps the loop crash-safe and resumable.

Commands
  init      build a ledger from an initial full-folder coverage report
  next      atomically claim the next pending item (prints it as JSON)
  get       print one item
  attempt   record a new attempt on an item (auto-escalates past the cap)
  update    set status / resolution / decision on an item
  sweep     auto-close items that a fresh full report shows are now covered
  status    summary counts (use --json for machine reads)
  render    (re)write the human-readable tasks.md mirror
  tick      increment the orchestrator tick counter, enforcing max_ticks

Run ids: every command takes --run-id; omit it to use runs/CURRENT.
Exit codes: 0 ok | 2 usage/not-found | 3 nothing to do | 4 guardrail tripped
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# LOGS_COVERAGE_ROOT relocates the whole run tree. Set it to keep runs/ on a
# bigger disk, or to point at a different checkout. The test suite uses it to
# stay out of the real runs/ directory.
ROOT = Path(os.environ.get("LOGS_COVERAGE_ROOT")
            or Path(__file__).resolve().parent.parent)
RUNS = ROOT / "runs"
SCHEMA_VERSION = 1

TERMINAL = {"done", "escalated"}
STATUSES = {"pending", "in_progress", "done", "escalated", "blocked"}
RESOLUTIONS = {
    "configured_parse",     # config now parses this file/line
    "configured_ignore",    # config now deliberately ignores it
    "already_covered",      # a previous item's change already covered it
    "swept",                # a full re-scan showed it covered; closed without work
    "escalated",            # handed to a human
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def die(msg: str, code: int = 2):
    print(f"ledger: {msg}", file=sys.stderr)
    sys.exit(code)


def resolve_run_id(run_id: str | None) -> str:
    if run_id and run_id != "current":
        return run_id
    cur = RUNS / "CURRENT"
    if not cur.exists():
        die("no --run-id given and runs/CURRENT does not exist", 2)
    return cur.read_text().strip()


def run_dir(run_id: str) -> Path:
    return RUNS / run_id


def ledger_path(run_id: str) -> Path:
    return run_dir(run_id) / "ledger.json"


def load(run_id: str) -> dict:
    p = ledger_path(run_id)
    if not p.exists():
        die(f"no ledger at {p}", 2)
    return json.loads(p.read_text())


def save(run_id: str, data: dict):
    """Atomic write, so a crash mid-write cannot corrupt the task list."""
    p = ledger_path(run_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, p)


class Lock:
    """Crude but sufficient inter-process lock -- ticks are seconds apart."""

    def __init__(self, run_id: str, timeout: float = 30.0):
        self.path = run_dir(run_id) / ".ledger.lock"
        self.timeout = timeout

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                if time.time() > deadline:
                    # Stale lock from a killed process: take it.
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                time.sleep(0.1)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def log_event(item: dict, actor: str, event: str, detail: str = ""):
    item.setdefault("history", []).append(
        {"ts": now(), "actor": actor, "event": event, "detail": detail}
    )


# --------------------------------------------------------------------------
# signatures -- how many report rows collapse into one work item
# --------------------------------------------------------------------------

_SIG_RULES = [
    (re.compile(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)?(Z|[+-]\d{2}:?\d{2})?"), "<TS>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<HEX>"),
    (re.compile(r"(/[\w.\-]+){2,}"), "<PATH>"),
    # Deliberately NOT \b-anchored: log numbers carry unit suffixes and sit
    # inside identifiers -- 524288K, 1240ms, http-7, ORA-00060, server01. With
    # word boundaries none of those normalise, and lines that need one config
    # rule between them fragment into dozens of separate work items.
    (re.compile(r"\d+(\.\d+)?"), "<N>"),
    (re.compile(r"\s+"), " "),
]


def line_signature(text: str) -> str:
    """Normalise a log line down to its *shape*.

    Two lines with the same shape need the same config decision, so they are
    one work item. This is what stops a 40,000-line report from becoming
    40,000 tasks.
    """
    s = text.strip()
    for rx, repl in _SIG_RULES:
        s = rx.sub(repl, s)
    return s.strip()[:200]


_PATH_RULES = [
    (re.compile(r"\d{4}-\d{2}-\d{2}"), "<DATE>"),
    (re.compile(r"\d{8}"), "<DATE>"),
    (re.compile(r"\d+"), "<N>"),
]


def path_signature(rel_path: str) -> str:
    """Normalise a path down to its shape: app/server-01/app-2026-08-01.log
    -> app/server-<N>/app-<DATE>.log"""
    s = rel_path
    for rx, repl in _PATH_RULES:
        s = rx.sub(repl, s)
    return s[:200]


def item_id(kind: str, signature: str) -> str:
    return hashlib.sha1(f"{kind}::{signature}".encode()).hexdigest()[:12]


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------

def build_items(report: dict, granularity: str, max_files_listed: int = 20) -> list[dict]:
    """Turn a coverage report into work items.

    granularity=cluster (default) -- one item per distinct file-shape /
      line-shape. Recommended: a single config change usually resolves a
      whole shape at once.
    granularity=line -- one item per undetected file and per unmatched line
      sample, literally. Exhaustive, far more expensive.
    """
    buckets: dict[str, dict] = {}

    def bucket(kind: str, signature: str, seed: dict) -> dict:
        key = item_id(kind, signature)
        if key not in buckets:
            buckets[key] = {
                "id": key,
                "kind": kind,
                "signature": signature,
                "occurrences": 0,
                "affected_files": [],
                "affected_file_count": 0,
                "status": "pending",
                "attempts": 0,
                "claimed_at": None,
                "resolution": None,
                "decision": None,
                "history": [],
                **seed,
            }
        return buckets[key]

    for f in report.get("files", []):
        rel = f.get("path", "")
        if f.get("status") == "undetected":
            sig = path_signature(rel) if granularity == "cluster" else rel
            it = bucket(
                "undetected_file",
                sig,
                {
                    "example_path": rel,
                    "example_line_no": None,
                    "example_text": None,
                    "size_bytes": f.get("size_bytes"),
                },
            )
            it["occurrences"] += 1
            if len(it["affected_files"]) < max_files_listed:
                it["affected_files"].append(rel)
            it["affected_file_count"] += 1

        elif f.get("status") == "detected":
            for s in f.get("unmatched_samples", []):
                text = s.get("text", "")
                sig = line_signature(text) if granularity == "cluster" else f"{rel}#{s.get('line_no')}"
                it = bucket(
                    "unmatched_lines",
                    sig,
                    {
                        "example_path": rel,
                        "example_line_no": s.get("line_no"),
                        "example_text": text,
                        "matched_by": f.get("matched_by"),
                    },
                )
                it["occurrences"] += 1
                if rel not in it["affected_files"] and len(it["affected_files"]) < max_files_listed:
                    it["affected_files"].append(rel)
                    it["affected_file_count"] += 1

    items = list(buckets.values())
    # Biggest blast radius first: fixing those closes the most report rows.
    items.sort(key=lambda i: (-i["occurrences"], i["kind"], i["signature"]))
    return items


def cmd_init(a):
    report = json.loads(Path(a.report).read_text())
    rid = a.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    d = run_dir(rid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "items").mkdir(exist_ok=True)
    (d / "escalations").mkdir(exist_ok=True)
    (d / "config-backups").mkdir(exist_ok=True)

    items = build_items(report, a.granularity)
    data = {
        "schema_version": SCHEMA_VERSION,
        "run_id": rid,
        "created_at": now(),
        "goal": a.goal
        or "Every item in the initial coverage report is parsed, deliberately ignored, or escalated to a human.",
        "logs_root": report.get("logs_root") or a.logs_root,
        "config_path": report.get("config_path") or a.config,
        "granularity": a.granularity,
        "initial_report": str(Path(a.report).resolve()),
        "guardrails": {
            "max_attempts_per_item": a.max_attempts,
            "max_ticks": a.max_ticks,
        },
        "ticks_used": 0,
        "sweeps": [],
        "totals_at_init": report.get("totals", {}),
        "items": items,
    }
    save(rid, data)
    RUNS.mkdir(exist_ok=True)
    (RUNS / "CURRENT").write_text(rid)
    render(rid, data)
    print(json.dumps({
        "run_id": rid,
        "run_dir": str(d),
        "items": len(items),
        "granularity": a.granularity,
        "report_rows_collapsed": sum(i["occurrences"] for i in items),
    }, indent=2))


# --------------------------------------------------------------------------
# loop operations
# --------------------------------------------------------------------------

def cmd_next(a):
    rid = resolve_run_id(a.run_id)
    with Lock(rid):
        data = load(rid)
        for it in data["items"]:
            if it["status"] == "pending":
                it["status"] = "in_progress"
                it["claimed_at"] = now()
                log_event(it, "orchestrator", "claimed")
                save(rid, data)
                print(json.dumps(it, indent=2, ensure_ascii=False))
                return
    print("no pending items", file=sys.stderr)
    sys.exit(3)


def cmd_get(a):
    rid = resolve_run_id(a.run_id)
    data = load(rid)
    for it in data["items"]:
        if it["id"] == a.item_id:
            print(json.dumps(it, indent=2, ensure_ascii=False))
            return
    die(f"no item {a.item_id}", 2)


def cmd_attempt(a):
    """Record an attempt. Returns the new count and whether the cap is hit."""
    rid = resolve_run_id(a.run_id)
    with Lock(rid):
        data = load(rid)
        cap = data["guardrails"]["max_attempts_per_item"]
        for it in data["items"]:
            if it["id"] == a.item_id:
                it["attempts"] += 1
                it["status"] = "in_progress"
                log_event(it, a.actor, "attempt", f"#{it['attempts']} of {cap}")
                exhausted = it["attempts"] > cap
                if exhausted:
                    it["status"] = "escalated"
                    it["resolution"] = "escalated"
                    log_event(it, "ledger", "auto_escalated",
                              f"exceeded {cap} attempts")
                save(rid, data)
                render(rid, data)
                print(json.dumps({
                    "item_id": it["id"], "attempts": it["attempts"],
                    "max_attempts": cap, "exhausted": exhausted,
                    "status": it["status"],
                }, indent=2))
                return
    die(f"no item {a.item_id}", 2)


def cmd_update(a):
    if a.status not in STATUSES:
        die(f"status must be one of {sorted(STATUSES)}", 2)
    if a.resolution and a.resolution not in RESOLUTIONS:
        die(f"resolution must be one of {sorted(RESOLUTIONS)}", 2)
    if a.status == "done" and not a.resolution:
        die("refusing to mark done without --resolution (what proved it?)", 2)

    rid = resolve_run_id(a.run_id)
    with Lock(rid):
        data = load(rid)
        for it in data["items"]:
            if it["id"] == a.item_id:
                if a.decision_json:
                    it["decision"] = json.loads(Path(a.decision_json).read_text())
                if a.evidence:
                    it["evidence"] = a.evidence
                it["status"] = a.status
                if a.resolution:
                    it["resolution"] = a.resolution
                log_event(it, a.actor, f"status={a.status}",
                          a.note or (a.resolution or ""))
                save(rid, data)
                render(rid, data)
                print(json.dumps({"item_id": it["id"], "status": it["status"],
                                  "resolution": it["resolution"]}, indent=2))
                return
    die(f"no item {a.item_id}", 2)


def cmd_tick(a):
    rid = resolve_run_id(a.run_id)
    with Lock(rid):
        data = load(rid)
        cap = data["guardrails"]["max_ticks"]
        data["ticks_used"] += 1
        save(rid, data)
        used = data["ticks_used"]
    if used > cap:
        print(json.dumps({"tick": used, "max_ticks": cap, "tripped": True}, indent=2))
        sys.exit(4)
    print(json.dumps({"tick": used, "max_ticks": cap, "tripped": False}, indent=2))


# --------------------------------------------------------------------------
# sweep -- the "already covered by another workflow" closer
# --------------------------------------------------------------------------

def cmd_sweep(a):
    """Re-scan closure.

    Given a FRESH full-folder report produced with the current config, close
    every open item whose signature no longer appears as uncovered. One regex
    added for item X routinely covers dozens of other items; this finds them
    instead of re-doing the work item by item.
    """
    rid = resolve_run_id(a.run_id)
    report = json.loads(Path(a.report).read_text())
    with Lock(rid):
        data = load(rid)
        still_open = set()
        for it in build_items(report, data["granularity"]):
            still_open.add(it["id"])

        closed = []
        for it in data["items"]:
            if it["status"] in TERMINAL:
                continue
            if it["id"] not in still_open:
                it["status"] = "done"
                it["resolution"] = it["resolution"] or "swept"
                log_event(it, "sweep", "closed_by_sweep",
                          f"absent from {Path(a.report).name}")
                closed.append(it["id"])

        data.setdefault("sweeps", []).append({
            "ts": now(), "report": str(Path(a.report).resolve()),
            "closed": closed, "totals": report.get("totals", {}),
        })
        save(rid, data)
        render(rid, data)
    print(json.dumps({"closed": len(closed), "item_ids": closed,
                      "totals": report.get("totals", {})}, indent=2))


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def summarise(data: dict) -> dict:
    counts = {s: 0 for s in STATUSES}
    for it in data["items"]:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    total = len(data["items"])
    closed = counts["done"] + counts["escalated"]
    return {
        "run_id": data["run_id"],
        "goal_met": counts["pending"] == 0 and counts["in_progress"] == 0 and counts["blocked"] == 0,
        "items_total": total,
        "counts": counts,
        "percent_closed": round(100.0 * closed / total, 1) if total else 100.0,
        "ticks_used": data["ticks_used"],
        "max_ticks": data["guardrails"]["max_ticks"],
        "sweeps": len(data.get("sweeps", [])),
    }


def cmd_status(a):
    rid = resolve_run_id(a.run_id)
    data = load(rid)
    s = summarise(data)
    if a.json:
        print(json.dumps(s, indent=2))
        return
    c = s["counts"]
    print(f"run {s['run_id']}  --  {s['percent_closed']}% closed  "
          f"({s['items_total']} items, tick {s['ticks_used']}/{s['max_ticks']})")
    print(f"  pending {c['pending']} | in_progress {c['in_progress']} | "
          f"done {c['done']} | escalated {c['escalated']} | blocked {c['blocked']}")
    print(f"  goal met: {s['goal_met']}")
    if c["escalated"]:
        print("\n  escalated -- need a human:")
        for it in data["items"]:
            if it["status"] == "escalated":
                print(f"    {it['id']}  {it['kind']}  {it['signature'][:70]}")


def render(rid: str, data: dict | None = None):
    """Human-readable mirror of the ledger. Read-only: editing it does
    nothing. The Ralph guide's tasks.md, generated rather than hand-kept."""
    data = data or load(rid)
    s = summarise(data)
    icon = {"pending": "[ NOT DONE ]", "in_progress": "[IN PROGRESS]",
            "done": "[   DONE   ]", "escalated": "[ ESCALATED]",
            "blocked": "[  BLOCKED ]"}
    out = [
        f"# Coverage run {data['run_id']}",
        "",
        f"> Generated mirror of `ledger.json`. **Do not edit** -- rewritten on every update.",
        "",
        f"**Goal:** {data['goal']}",
        "",
        f"- logs root: `{data['logs_root']}`",
        f"- config: `{data['config_path']}`",
        f"- granularity: `{data['granularity']}`",
        f"- progress: **{s['percent_closed']}% closed** ({s['counts']['done']} done, "
        f"{s['counts']['escalated']} escalated, {s['counts']['pending']} pending, "
        f"{s['counts']['in_progress']} in progress)",
        f"- ticks: {s['ticks_used']}/{s['max_ticks']} | sweeps: {s['sweeps']}",
        f"- goal met: **{s['goal_met']}**",
        "",
        "## Items",
        "",
    ]
    for it in data["items"]:
        out.append(
            f"{icon.get(it['status'], it['status'])} `{it['id']}` "
            f"**{it['kind']}** x{it['occurrences']} "
            f"-- attempts {it['attempts']}/{data['guardrails']['max_attempts_per_item']}"
            + (f" -- {it['resolution']}" if it.get("resolution") else "")
        )
        out.append(f"  - signature: `{it['signature'][:160]}`")
        out.append(f"  - example: `{it['example_path']}`"
                   + (f" line {it['example_line_no']}" if it.get("example_line_no") else ""))
        if it.get("example_text"):
            out.append(f"  - text: `{it['example_text'][:160]}`")
        out.append("")
    (run_dir(rid) / "tasks.md").write_text("\n".join(out))


def cmd_render(a):
    rid = resolve_run_id(a.run_id)
    render(rid)
    print(str(run_dir(rid) / "tasks.md"))


# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_run(sp):
        sp.add_argument("--run-id", default=None,
                        help="run id, or omit for runs/CURRENT")

    sp = sub.add_parser("init", help="build a ledger from a full coverage report")
    sp.add_argument("--report", required=True)
    sp.add_argument("--run-id", default=None)
    sp.add_argument("--logs-root", default=None)
    sp.add_argument("--config", default=None)
    sp.add_argument("--goal", default=None)
    sp.add_argument("--granularity", choices=["cluster", "line"], default="cluster")
    sp.add_argument("--max-attempts", type=int, default=3)
    sp.add_argument("--max-ticks", type=int, default=200)
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("next", help="claim the next pending item")
    add_run(sp)
    sp.set_defaults(func=cmd_next)

    sp = sub.add_parser("get", help="print one item")
    add_run(sp)
    sp.add_argument("--item-id", required=True)
    sp.set_defaults(func=cmd_get)

    sp = sub.add_parser("attempt", help="record an attempt (auto-escalates past the cap)")
    add_run(sp)
    sp.add_argument("--item-id", required=True)
    sp.add_argument("--actor", default="orchestrator")
    sp.set_defaults(func=cmd_attempt)

    sp = sub.add_parser("update", help="set status/resolution on an item")
    add_run(sp)
    sp.add_argument("--item-id", required=True)
    sp.add_argument("--status", required=True)
    sp.add_argument("--resolution", default=None)
    sp.add_argument("--decision-json", default=None, help="path to worker B's decision.json")
    sp.add_argument("--evidence", default=None, help="path to the report that proves it")
    sp.add_argument("--note", default=None)
    sp.add_argument("--actor", default="orchestrator")
    sp.set_defaults(func=cmd_update)

    sp = sub.add_parser("sweep", help="close items a fresh full report shows are covered")
    add_run(sp)
    sp.add_argument("--report", required=True)
    sp.set_defaults(func=cmd_sweep)

    sp = sub.add_parser("status", help="summary counts")
    add_run(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("render", help="rewrite tasks.md")
    add_run(sp)
    sp.set_defaults(func=cmd_render)

    sp = sub.add_parser("tick", help="increment the orchestrator tick counter")
    add_run(sp)
    sp.set_defaults(func=cmd_tick)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
