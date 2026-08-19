#!/usr/bin/env python3
"""Safe, surgical edits to logs-parsing-config.yml.

Worker C never hand-edits the config. It calls this, because:

  * edits are TEXT-ANCHORED, not parse-and-redump -- your comments, ordering
    and formatting survive. A real production config full of "why" comments
    would be destroyed by a naive yaml.load/yaml.dump round trip.
  * every write is backed up first, then validated (YAML parses, every regex
    compiles, rule ids are unique). If validation fails the file is RESTORED
    and the command exits non-zero. A broken config can never reach the loop.
  * adding a rule that is already present is a no-op, not a duplicate -- the
    mini-loop retries, and retries must be idempotent.

Commands
  validate                                    check the config as it stands
  show                                        list current rule ids
  add-ignore-file   --rule-id --pattern --reason [--wiki-ref]
  add-ignore-line   --rule-id --pattern --reason [--file-scope] [--wiki-ref]
  add-file-rule     --rule-id --pattern [--line-pattern P ...]
  add-line-pattern  --rule-id --pattern      (extend an existing file rule)

Exit: 0 ok | 1 validation failed (file restored) | 2 usage/anchor not found
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("LOGS_COVERAGE_ROOT")
            or Path(__file__).resolve().parent.parent)
DEFAULT_CONFIG = ROOT / "config" / "logs-parsing-config.yml"

ANCHORS = {
    "file-rules": "# @anchor:file-rules",
    "ignore-files": "# @anchor:ignore-files",
    "ignore-lines": "# @anchor:ignore-lines",
}


def q(s: str) -> str:
    """Single-quoted YAML scalar: no escape processing, so regex backslashes
    survive verbatim. Only the quote itself needs doubling."""
    return "'" + str(s).replace("'", "''") + "'"


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def validate(path: Path) -> list[str]:
    problems = []
    try:
        cfg = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        return [f"YAML does not parse: {e}"]

    if not isinstance(cfg, dict):
        return ["top level of the config is not a mapping"]

    seen_ids = {}

    def check(section, items, needed):
        for i, r in enumerate(items or []):
            where = f"{section}[{i}]"
            if not isinstance(r, dict):
                problems.append(f"{where}: not a mapping")
                continue
            rid = r.get("id")
            if not rid:
                problems.append(f"{where}: missing 'id'")
            elif rid in seen_ids:
                problems.append(f"{where}: duplicate id {rid!r} (also in {seen_ids[rid]})")
            else:
                seen_ids[rid] = where
            for k in needed:
                if not r.get(k):
                    problems.append(f"{where} ({rid}): missing {k!r}")
            for k in ("pattern", "file_scope"):
                if r.get(k):
                    try:
                        re.compile(r[k])
                    except re.error as e:
                        problems.append(f"{where} ({rid}): {k} is not a valid regex: {e}")
            for j, lp in enumerate(r.get("line_patterns") or []):
                try:
                    re.compile(lp)
                except re.error as e:
                    problems.append(f"{where} ({rid}).line_patterns[{j}]: invalid regex: {e}")

    check("files", cfg.get("files"), ["pattern"])
    ig = cfg.get("ignore") or {}
    check("ignore.files", ig.get("files"), ["pattern", "reason"])
    check("ignore.lines", ig.get("lines"), ["pattern", "reason"])
    return problems


def rule_ids(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text()) or {}
    ig = cfg.get("ignore") or {}
    return {
        "files": [r.get("id") for r in (cfg.get("files") or [])],
        "ignore.files": [r.get("id") for r in (ig.get("files") or [])],
        "ignore.lines": [r.get("id") for r in (ig.get("lines") or [])],
    }


# --------------------------------------------------------------------------
# text-anchored insertion
# --------------------------------------------------------------------------

def find_anchor(lines: list[str], anchor: str) -> int | None:
    for i, ln in enumerate(lines):
        if ln.strip() == anchor:
            return i
    return None


def find_block_end(lines: list[str], key_path: list[str]) -> tuple[int, int] | None:
    """Fallback when anchors are absent (i.e. a real config).

    Walks to the nested key and returns (insert_index, indent_for_new_entry).
    """
    idx, parent_indent = 0, -1
    for depth, key in enumerate(key_path):
        pat = re.compile(rf"^(\s*){re.escape(key)}:\s*(#.*)?$")
        found = None
        for i in range(idx, len(lines)):
            m = pat.match(lines[i])
            if m and len(m.group(1)) > parent_indent:
                found = (i, len(m.group(1)))
                break
            # left the parent block
            stripped = lines[i].strip()
            if depth > 0 and stripped and not stripped.startswith("#"):
                cur = len(lines[i]) - len(lines[i].lstrip())
                if cur <= parent_indent:
                    break
        if not found:
            return None
        idx, parent_indent = found[0] + 1, found[1]

    end = idx
    for i in range(idx, len(lines)):
        s = lines[i].strip()
        if not s or s.startswith("#"):
            continue
        cur = len(lines[i]) - len(lines[i].lstrip())
        if cur <= parent_indent:
            break
        end = i + 1
    return end, parent_indent + 2


def insert(path: Path, anchor_key: str, key_path: list[str], block: list[str]) -> list[str]:
    lines = path.read_text().split("\n")
    at = find_anchor(lines, ANCHORS[anchor_key])
    if at is not None:
        indent = len(lines[at]) - len(lines[at].lstrip())
    else:
        loc = find_block_end(lines, key_path)
        if loc is None:
            print(f"config_edit: could not locate '{'.'.join(key_path)}' and no "
                  f"'{ANCHORS[anchor_key]}' anchor comment is present. Add the anchor "
                  f"comment to the config, or extend find_block_end().", file=sys.stderr)
            sys.exit(2)
        at, indent = loc
    pad = " " * indent
    return lines[:at] + [pad + b if b else "" for b in block] + lines[at:]


def already_present(path: Path, section: str, pattern: str, rule_id: str) -> str | None:
    cfg = yaml.safe_load(path.read_text()) or {}
    ig = cfg.get("ignore") or {}
    pool = {"files": cfg.get("files"), "ignore.files": ig.get("files"),
            "ignore.lines": ig.get("lines")}[section] or []
    for r in pool:
        if r.get("pattern") == pattern:
            return f"pattern already covered by rule {r.get('id')!r}"
        if r.get("id") == rule_id:
            return f"rule id {rule_id!r} already exists"
    return None


def commit(path: Path, new_lines: list[str], op: str, backup_dir: Path, dry_run: bool) -> dict:
    body = "\n".join(new_lines)
    if dry_run:
        return {"changed": False, "dry_run": True, "would_write": body[-800:]}

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{stamp}-{op}.yml"
    shutil.copy2(path, backup)

    path.write_text(body)
    problems = validate(path)
    if problems:
        shutil.copy2(backup, path)          # roll back -- never ship a broken config
        print(json.dumps({"changed": False, "rolled_back": True,
                          "backup": str(backup), "problems": problems}, indent=2),
              file=sys.stderr)
        sys.exit(1)
    return {"changed": True, "op": op, "config": str(path), "backup": str(backup)}


# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--backup-dir", default=None)
    p.add_argument("--dry-run", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate")
    sub.add_parser("show")

    sp = sub.add_parser("add-ignore-file")
    sp.add_argument("--rule-id", required=True)
    sp.add_argument("--pattern", required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--wiki-ref", default=None)

    sp = sub.add_parser("add-ignore-line")
    sp.add_argument("--rule-id", required=True)
    sp.add_argument("--pattern", required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--file-scope", default=".*")
    sp.add_argument("--wiki-ref", default=None)

    sp = sub.add_parser("add-file-rule")
    sp.add_argument("--rule-id", required=True)
    sp.add_argument("--pattern", required=True)
    sp.add_argument("--line-pattern", action="append", default=[])
    sp.add_argument("--wiki-ref", default=None)

    sp = sub.add_parser("add-line-pattern")
    sp.add_argument("--rule-id", required=True)
    sp.add_argument("--pattern", required=True)

    a = p.parse_args()
    cfg_path = Path(a.config).resolve()
    if not cfg_path.exists():
        print(f"config_edit: no config at {cfg_path}", file=sys.stderr)
        sys.exit(2)

    if a.backup_dir:
        backup_dir = Path(a.backup_dir)
    else:
        cur = ROOT / "runs" / "CURRENT"
        backup_dir = (ROOT / "runs" / cur.read_text().strip() / "config-backups") \
            if cur.exists() else (ROOT / "runs" / "_backups")

    if a.cmd == "validate":
        problems = validate(cfg_path)
        print(json.dumps({"valid": not problems, "config": str(cfg_path),
                          "problems": problems}, indent=2))
        sys.exit(0 if not problems else 1)

    if a.cmd == "show":
        print(json.dumps(rule_ids(cfg_path), indent=2))
        return

    if a.cmd == "add-ignore-file":
        dup = already_present(cfg_path, "ignore.files", a.pattern, a.rule_id)
        if dup:
            print(json.dumps({"changed": False, "reason": dup}, indent=2))
            return
        block = [f"- id: {a.rule_id}", f"  pattern: {q(a.pattern)}", f"  reason: {q(a.reason)}"]
        if a.wiki_ref:
            block.append(f"  wiki_ref: {q(a.wiki_ref)}")
        new = insert(cfg_path, "ignore-files", ["ignore", "files"], block)
        print(json.dumps(commit(cfg_path, new, f"add-ignore-file-{a.rule_id}",
                                backup_dir, a.dry_run), indent=2))
        return

    if a.cmd == "add-ignore-line":
        dup = already_present(cfg_path, "ignore.lines", a.pattern, a.rule_id)
        if dup:
            print(json.dumps({"changed": False, "reason": dup}, indent=2))
            return
        block = [f"- id: {a.rule_id}", f"  file_scope: {q(a.file_scope)}",
                 f"  pattern: {q(a.pattern)}", f"  reason: {q(a.reason)}"]
        if a.wiki_ref:
            block.append(f"  wiki_ref: {q(a.wiki_ref)}")
        new = insert(cfg_path, "ignore-lines", ["ignore", "lines"], block)
        print(json.dumps(commit(cfg_path, new, f"add-ignore-line-{a.rule_id}",
                                backup_dir, a.dry_run), indent=2))
        return

    if a.cmd == "add-file-rule":
        dup = already_present(cfg_path, "files", a.pattern, a.rule_id)
        if dup:
            print(json.dumps({"changed": False, "reason": dup}, indent=2))
            return
        block = [f"- id: {a.rule_id}", f"  pattern: {q(a.pattern)}"]
        if a.wiki_ref:
            block.append(f"  wiki_ref: {q(a.wiki_ref)}")
        if a.line_pattern:
            block.append("  line_patterns:")
            block += [f"    - {q(lp)}" for lp in a.line_pattern]
        new = insert(cfg_path, "file-rules", ["files"], block)
        print(json.dumps(commit(cfg_path, new, f"add-file-rule-{a.rule_id}",
                                backup_dir, a.dry_run), indent=2))
        return

    if a.cmd == "add-line-pattern":
        # Duplicate check against the parsed document, not the raw text: an
        # entry carrying a trailing comment would slip past a string compare
        # and get inserted twice on every retry.
        host = next((r for r in (yaml.safe_load(cfg_path.read_text()) or {}).get("files") or []
                     if r.get("id") == a.rule_id), None)
        if host and a.pattern in (host.get("line_patterns") or []):
            print(json.dumps({"changed": False,
                              "reason": "pattern already on this rule"}, indent=2))
            return

        lines = cfg_path.read_text().split("\n")
        # Locate the file rule item by id. The trailing `(#.*)?` matters: real
        # configs annotate rules inline, and without it this op cannot find the
        # host rule at all -- every line-pattern item would fail to resolve.
        start = next((i for i, ln in enumerate(lines)
                      if re.match(rf"^\s*-\s+id:\s*{re.escape(a.rule_id)}\s*(#.*)?$", ln)), None)
        if start is None:
            print(json.dumps({"changed": False,
                              "reason": f"no file rule with id {a.rule_id!r}"}, indent=2),
                  file=sys.stderr)
            sys.exit(2)
        item_indent = len(lines[start]) - len(lines[start].lstrip())
        end = len(lines)
        for i in range(start + 1, len(lines)):
            s = lines[i].strip()
            if not s or s.startswith("#"):
                continue
            cur = len(lines[i]) - len(lines[i].lstrip())
            if cur <= item_indent:
                end = i
                break
        lp_at = next((i for i in range(start, end)
                      if re.match(r"^\s*line_patterns:\s*(#.*)?$", lines[i])), None)
        if lp_at is None:
            tail = end                       # do not orphan below trailing blanks
            while tail > start + 1 and not lines[tail - 1].strip():
                tail -= 1
            new = lines[:tail] + [" " * (item_indent + 2) + "line_patterns:",
                                  " " * (item_indent + 4) + f"- {q(a.pattern)}"] + lines[tail:]
        else:
            # Insert directly after the last existing entry, not at the end of
            # the block -- otherwise a blank line inside the rule leaves the new
            # pattern orphaned below it. Valid YAML either way, but unreadable.
            ins = lp_at + 1
            for i in range(lp_at + 1, end):
                s = lines[i].strip()
                if s.startswith("- "):
                    ins = i + 1
                elif s and not s.startswith("#"):
                    break
            new = lines[:ins] + [" " * (item_indent + 4) + f"- {q(a.pattern)}"] + lines[ins:]
        print(json.dumps(commit(cfg_path, new, f"add-line-pattern-{a.rule_id}",
                                backup_dir, a.dry_run), indent=2))
        return


if __name__ == "__main__":
    main()
