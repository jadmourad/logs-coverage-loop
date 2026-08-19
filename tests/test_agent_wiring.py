"""Static checks on the agent layer.

The Python tools have tests that run them. The agent prompts do not run, so
nothing catches it when a prompt drifts away from the tools it drives -- a
renamed flag, a subcommand that no longer exists, a skill that was moved, a
permission entry that silently matches nothing. Every one of those failures
shows up at *run time*, mid-tick, as an agent that cannot do its job.

These tests read the prompts the way the agents will and check that everything
they are told to do is actually possible.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

from helpers import PROJECT, TOOLS

AGENTS_DIR = PROJECT / ".claude" / "agents"
SKILLS_DIR = PROJECT / ".claude" / "skills"
SETTINGS = PROJECT / ".claude" / "settings.json"

EXPECTED_AGENTS = {"coverage-orchestrator", "worker-a-runner",
                   "worker-b-assessor", "worker-c-configurator"}

# Flags the standalone contract defines (docs/CONTRACTS.md §1). Checked by name
# rather than by asking the tool, so this still works after the real tool is
# swapped in behind tools/standalone.sh.
STANDALONE_FLAGS = {"--logs-root", "--config", "--out",
                    "--max-unmatched-lines", "--label", "--help"}

SCRIPTS = ["ledger.py", "config_edit.py", "isolate.py", "verdict.py", "preflight.py"]


def front_matter(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise AssertionError(f"{path.relative_to(PROJECT)} has no YAML front matter")
    return yaml.safe_load(m.group(1)) or {}, m.group(2)


def tools_of(meta: dict) -> set[str]:
    raw = meta.get("tools")
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {t.strip() for t in raw.split(",") if t.strip()}
    return {str(t).strip() for t in raw}


def markdown_files() -> list[Path]:
    out = [PROJECT / "AGENTS.md", PROJECT / "README.md"]
    out += sorted((PROJECT / "docs").glob("*.md"))
    out += sorted(AGENTS_DIR.glob("*.md"))
    out += sorted(SKILLS_DIR.glob("*/SKILL.md"))
    return [p for p in out if p.exists()]


# --------------------------------------------------------------------------
# what the CLIs actually accept
# --------------------------------------------------------------------------

def _help(*args) -> str:
    r = subprocess.run([sys.executable, *[str(a) for a in args], "--help"],
                       capture_output=True, text=True, cwd=str(PROJECT))
    return r.stdout + r.stderr


def build_spec() -> dict:
    """{script: {"global": {flags}, "subs": {name: {flags}}}} from --help."""
    spec = {}
    for script in SCRIPTS:
        top = _help(TOOLS / script)
        flags = set(re.findall(r"--[a-z][a-z0-9-]*", top))
        subs = {}
        m = re.search(r"\{([a-z0-9,\-]+)\}", top)
        if m:
            for name in m.group(1).split(","):
                subs[name] = set(re.findall(r"--[a-z][a-z0-9-]*",
                                            _help(TOOLS / script, name)))
        spec[script] = {"global": flags, "subs": subs}
    return spec


SPEC = build_spec()

CMD_RE = re.compile(
    r"(?:python3\s+)?(?:tools/)?(?P<script>ledger|config_edit|isolate|verdict|preflight)"
    r"\.py(?P<rest>[^\n`]*)")
SEAM_RE = re.compile(r"bash\s+tools/standalone\.sh(?P<rest>[^\n`]*)")


FLAG_RE = re.compile(r"^--[a-z][a-z0-9-]*")


def _strip_comment(s: str) -> str:
    """Drop a trailing shell comment without eating a '#' inside a regex
    argument like --pattern '^#\\s'. Only whitespace-separated hashes go."""
    s = re.split(r"\s{2,}#", s, maxsplit=1)[0]
    return re.split(r"\s#\s", s, maxsplit=1)[0]


def _parse_args(rest: str):
    toks = [t.strip("[]`,'\"") for t in _strip_comment(rest).split()]
    toks = [t for t in toks if t and t != "|"]
    subs, flags = [], []
    for t in toks:
        if FLAG_RE.match(t):
            flags.append(t.split("=")[0])
        elif not flags and not t.startswith("-"):
            subs.extend(s for s in t.split("|") if s)
    return subs, flags


def extract_commands(path: Path):
    """Yield (script, subcommands, flags, raw) for every command in a doc.

    A bare `tools/ledger.py` in prose or in the layout table is a reference,
    not a command. Only treat a match as a command when it is written as a
    `python3 ...` invocation, or when the token after the script is a real
    subcommand -- otherwise the file map's descriptions parse as arguments.
    """
    text = re.sub(r"\\\n\s*", " ", path.read_text())      # join continuations
    for line in text.split("\n"):
        for m in CMD_RE.finditer(line):
            script = m.group("script") + ".py"
            subs, flags = _parse_args(m.group("rest"))
            invoked = m.group(0).lstrip().startswith("python3")
            recognised = subs and subs[0] in SPEC[script]["subs"]
            if not invoked and not recognised:
                continue
            yield script, subs, flags, m.group(0).strip()
        for m in SEAM_RE.finditer(line):
            _, flags = _parse_args(m.group("rest"))
            yield "standalone.sh", [], flags, m.group(0).strip()


# --------------------------------------------------------------------------


class TestFrontMatterParses(unittest.TestCase):
    """Found live: a `description` containing an unquoted ': ' is not valid
    YAML, and the whole agent or skill silently fails to load. Nothing else
    catches it -- the file looks completely normal."""

    def test_every_agent_and_skill_has_parseable_front_matter(self):
        files = list(AGENTS_DIR.glob("*.md")) + list(SKILLS_DIR.glob("*/SKILL.md"))
        self.assertGreater(len(files), 5)
        for path in files:
            with self.subTest(file=str(path.relative_to(PROJECT))):
                m = re.match(r"^---\n(.*?)\n---\n", path.read_text(), re.S)
                self.assertIsNotNone(m, "missing YAML front matter")
                try:
                    meta = yaml.safe_load(m.group(1))
                except yaml.YAMLError as e:
                    self.fail(f"front matter is not valid YAML ({e}). A ': ' in an "
                              f"unquoted description is the usual cause -- quote the "
                              f"value or reword it.")
                self.assertIsInstance(meta, dict)


class TestAgentDefinitions(unittest.TestCase):

    def test_all_four_agents_exist(self):
        found = {p.stem for p in AGENTS_DIR.glob("*.md")}
        self.assertEqual(found, EXPECTED_AGENTS,
                         "the loop spawns these by name; an extra or missing "
                         "file means a spawn will fail at run time")

    def test_front_matter_is_complete_and_name_matches_filename(self):
        for path in AGENTS_DIR.glob("*.md"):
            with self.subTest(agent=path.stem):
                meta, body = front_matter(path)
                self.assertEqual(meta.get("name"), path.stem,
                                 "agents are spawned by `name`, not by filename")
                self.assertTrue(meta.get("description"),
                                "without a description the agent cannot be selected")
                self.assertTrue(tools_of(meta), "an agent with no tools can do nothing")
                self.assertGreater(len(body.strip()), 400,
                                   "the prompt body looks truncated")

    def test_orchestrator_can_spawn_workers(self):
        meta, _ = front_matter(AGENTS_DIR / "coverage-orchestrator.md")
        self.assertIn("Agent", tools_of(meta),
                      "the orchestrator's whole job is delegating to A, B and C")

    def test_no_worker_can_hand_edit_files(self):
        """Worker C must go through config_edit.py -- backup, validate,
        roll back. Handing it the Edit tool makes hand-editing possible again,
        and the first thing it would skip is validation."""
        for name in EXPECTED_AGENTS:
            with self.subTest(agent=name):
                meta, _ = front_matter(AGENTS_DIR / f"{name}.md")
                self.assertNotIn("Edit", tools_of(meta),
                                 f"{name} must not have the Edit tool")

    def test_workers_that_are_told_to_load_a_skill_can_load_one(self):
        for name in ("worker-a-runner", "worker-b-assessor", "worker-c-configurator"):
            with self.subTest(agent=name):
                meta, body = front_matter(AGENTS_DIR / f"{name}.md")
                if "skill" in body.lower():
                    self.assertIn("Skill", tools_of(meta),
                                  f"{name} is told to load a skill but has no Skill tool")

    def test_workers_cannot_spawn_more_agents(self):
        """Only the orchestrator delegates. A worker that can spawn workers
        makes the loop's cost and its audit trail unbounded."""
        for name in EXPECTED_AGENTS - {"coverage-orchestrator"}:
            with self.subTest(agent=name):
                meta, _ = front_matter(AGENTS_DIR / f"{name}.md")
                self.assertNotIn("Agent", tools_of(meta))

    def test_only_worker_a_is_told_to_run_the_scanner(self):
        """If Worker B or C runs a scan, the loop can close an item on evidence
        nobody recorded."""
        for name in ("worker-b-assessor", "worker-c-configurator"):
            with self.subTest(agent=name):
                _, body = front_matter(AGENTS_DIR / f"{name}.md")
                self.assertNotIn("bash tools/standalone.sh", body)

    def test_only_worker_c_is_told_to_edit_the_config(self):
        for name in ("worker-a-runner", "worker-b-assessor"):
            with self.subTest(agent=name):
                _, body = front_matter(AGENTS_DIR / f"{name}.md")
                for op in ("add-ignore-file", "add-ignore-line", "add-file-rule",
                           "add-line-pattern"):
                    self.assertNotIn(f"config_edit.py {op}", body)

    def test_only_the_orchestrator_writes_to_the_ledger(self):
        for name in EXPECTED_AGENTS - {"coverage-orchestrator"}:
            with self.subTest(agent=name):
                _, body = front_matter(AGENTS_DIR / f"{name}.md")
                self.assertNotIn("ledger.py update", body)
                self.assertNotIn("ledger.py attempt", body)


class TestSkills(unittest.TestCase):

    EXPECTED = {"coverage-start", "coverage-status", "coverage-escalations",
                "wiki-ingest", "isolate-and-verify", "assess-finding",
                "apply-config-change", "run-tests"}

    def test_every_skill_directory_has_a_valid_skill_file(self):
        found = {p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md")}
        self.assertEqual(found, self.EXPECTED)

    def test_skill_name_matches_its_directory(self):
        for path in SKILLS_DIR.glob("*/SKILL.md"):
            with self.subTest(skill=path.parent.name):
                meta, body = front_matter(path)
                self.assertEqual(meta.get("name"), path.parent.name,
                                 "the slash command is the directory name")
                self.assertTrue(meta.get("description"))
                self.assertGreater(len(body.strip()), 400)

    def test_skills_referenced_by_agents_exist(self):
        """`Load the **assess-finding** skill` has to resolve, or the worker
        silently proceeds without the reference it was told to read."""
        available = {p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md")}
        for path in AGENTS_DIR.glob("*.md"):
            _, body = front_matter(path)
            for name in re.findall(r"\*\*`?([a-z][a-z0-9-]+)`?\*\* skill", body):
                with self.subTest(agent=path.stem, skill=name):
                    self.assertIn(name, available)

    def test_agents_referenced_by_skills_and_agents_exist(self):
        for path in list(SKILLS_DIR.glob("*/SKILL.md")) + list(AGENTS_DIR.glob("*.md")):
            _, body = front_matter(path)
            for name in re.findall(r"`(worker-[a-z-]+|coverage-orchestrator)`", body):
                with self.subTest(source=path.parent.name, agent=name):
                    self.assertIn(name, EXPECTED_AGENTS)


class TestCommandsInPrompts(unittest.TestCase):
    """Every command an agent is told to run must actually be runnable. This is
    what catches a renamed flag or a dropped subcommand before a tick does."""

    def test_every_documented_command_parses(self):
        checked = 0
        for path in markdown_files():
            for script, subs, flags, raw in extract_commands(path):
                where = f"{path.relative_to(PROJECT)}: {raw[:90]}"
                if script == "standalone.sh":
                    for f in flags:
                        with self.subTest(where=where, flag=f):
                            self.assertIn(f, STANDALONE_FLAGS,
                                          f"not in the standalone contract -- {where}")
                    checked += 1
                    continue

                spec = SPEC[script]
                valid = set(spec["global"])
                for sub in subs:
                    with self.subTest(where=where, sub=sub):
                        self.assertIn(sub, spec["subs"],
                                      f"{script} has no subcommand {sub!r} -- {where}")
                    valid |= spec["subs"].get(sub, set())
                if not subs and spec["subs"] and flags:
                    # a script with subcommands, invoked with only flags
                    valid |= set().union(*spec["subs"].values())
                for f in flags:
                    with self.subTest(where=where, flag=f):
                        self.assertIn(f, valid, f"{script} has no flag {f} -- {where}")
                checked += 1
        self.assertGreater(checked, 30,
                           "the extractor found almost no commands -- it has "
                           "probably stopped matching the docs' format")

    def test_the_shared_command_list_covers_every_subcommand(self):
        """AGENTS.md §5 is the one place agents look up how to call a tool. A
        subcommand missing from it is a subcommand no agent will use."""
        body = (PROJECT / "AGENTS.md").read_text()
        for script in ("ledger.py", "config_edit.py"):
            for sub in SPEC[script]["subs"]:
                with self.subTest(script=script, sub=sub):
                    self.assertIn(sub, body, f"{script} {sub} is not in AGENTS.md")


class TestSettings(unittest.TestCase):

    def setUp(self):
        self.settings = json.loads(SETTINGS.read_text())
        self.perms = self.settings["permissions"]

    def test_settings_json_parses(self):
        self.assertIn("allow", self.perms)

    def test_path_rules_use_edit_not_write(self):
        """Regression, found live: Claude Code matches file permissions on
        Edit(...) rules only. A Write(...) deny rule looks correct, silently
        matches nothing, and the protection it appears to give does not exist."""
        for rule in self.perms.get("deny", []) + self.perms.get("allow", []):
            with self.subTest(rule=rule):
                self.assertFalse(rule.startswith("Write("),
                                 "use Edit(...) -- it covers every file-editing tool")

    def test_the_logs_folder_and_config_are_protected(self):
        deny = " ".join(self.perms.get("deny", []))
        self.assertIn("logs-sample", deny, "the logs folder must be read-only")
        self.assertIn("logs-parsing-config.yml", deny,
                      "the config must only change through config_edit.py")

    def test_every_allowed_bash_command_points_at_something_real(self):
        for rule in self.perms["allow"]:
            m = re.match(r"Bash\((?:bash|python3)\s+(tools/[\w./-]+)", rule)
            if m:
                with self.subTest(rule=rule):
                    self.assertTrue((PROJECT / m.group(1)).exists(),
                                    f"allow rule references a missing script: {m.group(1)}")

    def test_every_tool_the_loop_needs_is_pre_approved(self):
        """A tool that is not pre-approved stalls the tick on a prompt, which
        for an unattended run means the loop simply stops."""
        allow = " ".join(self.perms["allow"])
        for script in SCRIPTS + ["standalone.sh"]:
            with self.subTest(script=script):
                self.assertIn(f"tools/{script}", allow,
                              f"{script} is used by the loop but not pre-approved")


class TestDocsMatchCode(unittest.TestCase):
    """Contract drift: the docs are what a human and an agent both reason from.
    When they disagree with the code, the code wins silently."""

    def setUp(self):
        sys.path.insert(0, str(TOOLS))
        import ledger as mod
        self.mod = mod
        self.contracts = (PROJECT / "docs" / "CONTRACTS.md").read_text()

    def test_every_item_status_is_documented(self):
        for status in self.mod.STATUSES:
            with self.subTest(status=status):
                self.assertIn(status, self.contracts)

    def test_every_resolution_is_documented(self):
        for resolution in self.mod.RESOLUTIONS:
            with self.subTest(resolution=resolution):
                self.assertIn(resolution, self.contracts,
                              "an undocumented resolution is one nobody can interpret")

    def test_documented_resolutions_all_exist_in_code(self):
        table = re.findall(r"^\| `(\w+)` \|", self.contracts, re.M)
        for name in table:
            if name in ("configured_parse", "configured_ignore", "already_covered",
                        "swept"):
                with self.subTest(resolution=name):
                    self.assertIn(name, self.mod.RESOLUTIONS)

    def test_the_attempt_cap_is_the_same_everywhere(self):
        """`3 attempts` appears in five prompts. If the default changes and the
        prompts do not, agents plan around a budget they do not have."""
        # Read the real default by initialising a throwaway run, rather than
        # scraping --help: this asserts against what the loop will actually use.
        import tempfile
        from helpers import make_config, make_logs, run_tool, scan
        tmp = Path(tempfile.mkdtemp(prefix="covloop-cap-"))
        (tmp / "root" / "runs").mkdir(parents=True)
        make_logs(tmp / "logs", {"a.txt": "x\n"})
        make_config(tmp / "c.yml", files=[])
        scan(tmp / "logs", tmp / "c.yml", tmp / "out")
        run_tool("ledger.py", "init", "--report", tmp / "out" / "coverage-report.json",
                 "--run-id", "capcheck", root=tmp / "root")
        cap = str(json.loads((tmp / "root" / "runs" / "capcheck" / "ledger.json")
                             .read_text())["guardrails"]["max_attempts_per_item"])
        for path in [PROJECT / "AGENTS.md",
                     AGENTS_DIR / "coverage-orchestrator.md",
                     PROJECT / "docs" / "ARCHITECTURE.md"]:
            with self.subTest(doc=path.name):
                self.assertIn(cap, path.read_text(),
                              f"{path.name} does not mention the {cap}-attempt cap")

    def test_the_report_schema_in_the_docs_matches_a_real_report(self):
        import tempfile
        from helpers import make_config, make_logs, scan
        tmp = Path(tempfile.mkdtemp(prefix="covloop-docs-"))
        make_logs(tmp / "logs", {"app/app-2026-08-01.log": "2026-08-01 ERROR a\nx\n"})
        make_config(tmp / "c.yml",
                    files=[("app-log", r"(^|/)app-\d{4}-\d{2}-\d{2}\.log$", [r"ERROR"])])
        scan(tmp / "logs", tmp / "c.yml", tmp / "out")
        report = json.loads((tmp / "out" / "coverage-report.json").read_text())
        for key in report:
            with self.subTest(key=key):
                self.assertIn(key, self.contracts,
                              f"the report emits {key!r} but CONTRACTS.md §2 does not "
                              f"document it")
        for key in report["files"][0]:
            with self.subTest(file_key=key):
                self.assertIn(key, self.contracts)


class TestShippedConfig(unittest.TestCase):

    def test_the_anchor_comments_are_present(self):
        """config_edit.py inserts at these. Losing one drops that op onto the
        indentation fallback, which is a quieter and less predictable path."""
        body = (PROJECT / "config" / "logs-parsing-config.yml").read_text()
        for anchor in ("@anchor:file-rules", "@anchor:ignore-files",
                       "@anchor:ignore-lines"):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, body)

    def test_the_shipped_config_validates(self):
        r = subprocess.run([sys.executable, str(TOOLS / "config_edit.py"), "validate"],
                           capture_output=True, text=True, cwd=str(PROJECT))
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_filler_pieces_are_labelled(self):
        """preflight warns on the string FILLER. If a filler loses its label it
        stops being reported, and a run against invented wiki rules looks real."""
        for rel in ("config/logs-parsing-config.yml", "wiki/KNOWLEDGE_BASE.md",
                    "tools/fake/fake_standalone.py"):
            with self.subTest(file=rel):
                self.assertIn("FILLER", (PROJECT / rel).read_text())


if __name__ == "__main__":
    unittest.main()
