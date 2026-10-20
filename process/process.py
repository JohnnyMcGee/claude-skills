#!/usr/bin/env python3
"""process — the state directory behind the daily process pipeline: items, queue, ledger, watermarks and brief.

stdlib only. Every external command (claude) goes through run().
"""

import fcntl
import hashlib
import inspect
import json
import os
import re
import shlex
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path


SKILL_DIR = Path(os.path.realpath(__file__)).parent
SCHEMA = json.loads((SKILL_DIR / "schema.json").read_text())


def default_run(cmd, input=None, cwd=None):
    return subprocess.run(cmd, input=input, cwd=cwd, capture_output=True, text=True, check=True).stdout


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


# --- schema -----------------------------------------------------------------------------------------------------

TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def type_ok(value, name):
    if name in ("integer", "number"):
        return isinstance(value, (int, float) if name == "number" else int) and not isinstance(value, bool)
    return isinstance(value, TYPES[name])


def schema_errors(value, schema, path="$"):
    """The subset of JSON Schema schema.json uses. Returns a list of '<path>: <problem>' strings."""
    if "$ref" in schema:
        schema = {**SCHEMA["$defs"][schema["$ref"].rsplit("/", 1)[1]], **{k: v for k, v in schema.items() if k != "$ref"}}
    errors = []
    types = schema.get("type")
    if types and not any(type_ok(value, t) for t in ([types] if isinstance(types, str) else types)):
        return [f"{path}: expected {types}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: too short")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: needs at least {schema['minItems']} item(s)")
        if "items" in schema:
            for i, element in enumerate(value):
                errors += schema_errors(element, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errors += [f"{path}: missing {key!r}" for key in schema.get("required", []) if key not in value]
        for key, element in value.items():
            if key in props:
                errors += schema_errors(element, props[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected {key!r}")
    for sub in schema.get("allOf", []):
        errors += schema_errors(value, sub, path)
    if "anyOf" in schema:
        branches = [schema_errors(value, sub, path) for sub in schema["anyOf"]]
        if all(branches):
            closest = min(branches, key=lambda b: (len(b), -max(e.split(":")[0].count(".") for e in b)))  # got furthest
            errors.append(f"{path}: matches no allowed shape ({closest[0]})")
    return errors


def validate(value, kind):
    return schema_errors(value, {"$ref": f"#/$defs/{kind}"})


# --- profiles ---------------------------------------------------------------------------------------------------

# Words that name a send, review, RSVP, merge or other write others see. A tool whose name holds one never belongs
# in a sweep or triage allowlist, unless it only reads (get_/list_/search_/read_ prefixes, a _read suffix).
SEND_WORDS = re.compile(
    r"send|reply|post|comment|review|rsvp|respond|event|merge|approve|submit|forward|publish|schedule|share|invite"
    r"|write|delete|push|create|update|add_|reaction|assign"
)
READ_ONLY = re.compile(r"__(get|list|search|read|fetch)_|_read$")
# Built-in tools that reach any file or any host unless the rule names a path or a domain.
UNSCOPED = {"Bash", "Edit", "MultiEdit", "NotebookEdit", "Write", "WebFetch", "WebSearch"}
# Bash prefixes that run arbitrary commands, so no prefix after them is safe.
LAUNCHERS = {"sh", "bash", "zsh", "env", "xargs", "eval", "exec", "sudo", "nohup", "timeout", "python", "python3", "node", "perl", "ruby", "curl", "wget"}


def forbidden_tool(tool):
    """True when an allowlist entry could let a phase act in my name: a send tool, or a wildcard that covers one."""
    bash = re.fullmatch(r"Bash\((.*)\)", tool)
    if bash:
        words = bash[1].removesuffix(":*").removesuffix("*").split()
        too_broad = len(words) < 2 or words[0] in LAUNCHERS or (words[0] == "gh" and (len(words) < 3 or words[1] == "api"))
        return too_broad or bool(SEND_WORDS.search(" ".join(words)))
    if tool in UNSCOPED or "*" in tool or (tool.startswith("mcp__") and tool.count("__") < 2):
        return True
    return not READ_ONLY.search(tool) and bool(SEND_WORDS.search(tool.lower()))


# Never actions, or what reaches one: merging, approving, deleting, deploying, and pushing (the executor pushes only
# through `process execute push`, which checks the branch is my PR's). No phase may hold these, the executor included.
EXECUTE_STEPS = {"plan", "review", "reply", "checkout", "git", "push", "result"}
# A review tool is one too: its event can be APPROVE, so reviews go through `process execute review`, which pins it.
NEVER_WORDS = re.compile(r"merge|approve|review_write|delete|deploy|release|push|create_or_update_file|transfer|fork|secret")
# The executor pushes code, so it reads only its worktree (granted per run): nothing else can end up in a commit.
EXECUTOR_UNSCOPED = {"Read", "Glob", "Grep"}


def never_tool(tool):
    """True when an executor allowlist entry could take a never action, or is broad enough to reach one."""
    bash = re.fullmatch(r"Bash\((.*)\)", tool)
    if bash:
        words = bash[1].removesuffix(":*").removesuffix("*").split()
        too_broad = len(words) < 2 or words[0] in LAUNCHERS or (words[0] in ("gh", "git") and (len(words) < 3 or words[1] == "api"))
        if words[:2] == ["process", "execute"]:  # its steps are the guarded way to send and push; bare, a second executor
            return len(words) < 3 or words[2] not in EXECUTE_STEPS
        return too_broad or words[:3] == ["gh", "pr", "review"] or bool(NEVER_WORDS.search(" ".join(words)))
    if tool in UNSCOPED | EXECUTOR_UNSCOPED or "*" in tool or (tool.startswith("mcp__") and tool.count("__") < 2):
        return True
    return bool(NEVER_WORDS.search(tool.lower()))


# Denied to every sweep and triage phase, as a backstop for allow rules in the user's own settings.
DENIED_TOOLS = [
    "Bash(gh pr merge:*)",
    "Bash(gh pr review:*)",
    "Bash(gh pr comment:*)",
    "Bash(gh api:*)",
    "Bash(git push:*)",
    "mcp__github__merge_pull_request",
    "mcp__github__pull_request_review_write",
    "mcp__github__add_comment_to_pending_review",
    "mcp__github__add_issue_comment",
    "mcp__github__add_reply_to_pull_request_comment",
    "mcp__slack__slack_send_message",
    "mcp__slack__slack_schedule_message",
    "mcp__claude_ai_Gmail__send_message",
    "mcp__claude_ai_Gmail__reply",
    "mcp__claude_ai_Gmail__forward",
    "mcp__claude_ai_Google_Calendar__respond_to_event",
    "mcp__linear-server__save_comment",
]


# Denied to the executor: the never actions among DENIED_TOOLS. It keeps the send tools its allowlist grants.
NEVER_DENIED = [t for t in DENIED_TOOLS if never_tool(t)] + ["mcp__github__push_files", "mcp__github__create_or_update_file"]


def load_profile(path):
    """Parse a profile: TOML front matter between +++ lines, then the how-to-sweep prose. Raises on any problem."""
    match = re.match(r"\+\+\+\n(.*?)\n\+\+\+\n(.*)", path.read_text(), re.S)
    if not match:
        raise SystemExit(f"process: {path}: no +++ TOML front matter")
    try:
        profile = tomllib.loads(match[1])
    except tomllib.TOMLDecodeError as error:
        raise SystemExit(f"process: {path}: {error}")
    problems = validate(profile, "profile")
    if profile.get("name") != path.stem:
        problems.append(f"name {profile.get('name')!r} does not match the file name {path.stem!r}")
    for heading in ("## How to sweep", "## Changelog"):
        if heading not in match[2]:
            problems.append(f"the body has no {heading!r} section")
    phases = profile.get("phases", {})
    for source in profile.get("sources", []):
        if source not in profile:
            problems.append(f"source {source!r} has no [{source}] table")
        if f"sweep-{source}" not in phases:
            problems.append(f"source {source!r} has no [phases.sweep-{source}]")
    for name, phase in phases.items():
        if name == "execute":
            problems += [f"phases.execute grants {t!r}, which can merge, approve, delete or push" for t in phase.get("allowed_tools", []) if never_tool(t)]
        else:
            problems += [f"phases.{name} grants {t!r}, which can send, review, RSVP or merge" for t in phase.get("allowed_tools", []) if forbidden_tool(t)]
    if problems:
        raise SystemExit(f"process: profile {path}:\n" + "\n".join(f"  {p}" for p in problems))
    return {**profile, "body": match[2]}


# --- the app ----------------------------------------------------------------------------------------------------


class Process:
    def __init__(self, run, env, stdin, stdout, clock):
        self.run = run
        self.env = env
        self.stdin = stdin
        self.stdout = stdout
        self.clock = clock

    def out(self, line):
        print(line, file=self.stdout)

    def state_root(self):
        base = self.env.get("XDG_STATE_HOME") or os.path.join(self.env["HOME"], ".local", "state")
        return Path(base) / "process"

    def profiles_dir(self):
        base = self.env.get("XDG_CONFIG_HOME") or os.path.join(self.env["HOME"], ".config")
        return Path(base) / "process" / "profiles"

    def profiles(self):
        """Every profile, validated. One bad profile stops the run: a phase must never start under a bad allowlist."""
        paths = sorted(self.profiles_dir().glob("*.md"))
        if not paths:
            raise SystemExit(f"process: no profiles in {self.profiles_dir()}")
        loaded, problems = {}, []
        for path in paths:
            try:
                loaded[path.stem] = load_profile(path)
            except SystemExit as error:
                problems.append(str(error.code))
        if problems:
            raise SystemExit("\n".join(problems))
        return loaded

    def profile(self, name):
        profiles = self.profiles()
        if name not in profiles:
            raise SystemExit(f"process: no profile {name!r} in {self.profiles_dir()}")
        return profiles[name]

    def profile_state(self, name):
        path = self.state_root() / "profiles" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def read_json(self, path, default):
        return json.loads(path.read_text()) if path.exists() else default

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n")

    def read_jsonl(self, path):
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []

    def append_jsonl(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.writelines(json.dumps(r) + "\n" for r in records)

    def parse_jsonl(self, text, what):
        records = []
        for n, line in enumerate(text.splitlines(), 1):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise SystemExit(f"process: {what} line {n} is not JSON: {error}")
        return records

    def cmd_run(self, *args):
        """Sweep every source, triage, then execute what triage approved silently, for each active profile (or
        --project), each phase headless under its profile's model and allowlist; then render the brief. --fixtures
        points sweepers at fixture data and makes triage and the executor dry runs."""
        opts = flags(args, "--delta", "--project", "--fixtures")
        dry = bool(opts.get("--fixtures"))
        if dry:
            self.guard_dry_run()
            opts["--fixtures"] = os.path.abspath(opts["--fixtures"])  # phases run in the profile's workdir
        elif (self.state_root() / "dry-run").exists():
            raise SystemExit(f"process: {self.state_root()} holds dry-run data; unset XDG_STATE_HOME for a real run")
        profiles = self.profiles()  # refuses before any phase starts if one grants a send tool
        if opts.get("--project") and opts["--project"] not in profiles:
            raise SystemExit(f"process: no profile {opts['--project']!r} in {self.profiles_dir()}")
        names = [opts["--project"]] if opts.get("--project") else [n for n, p in profiles.items() if p["active"]]
        run = {"id": iso(self.clock()), "delta": bool(opts.get("--delta")), "problems": {}}
        self.write_json(self.state_root() / "run.json", run)
        sweep_args = (" --delta" if opts.get("--delta") else "") + (f" --fixtures {opts['--fixtures']}" if dry else "")
        try:
            for name in names:
                profile = profiles[name]
                phases = [(f"sweep-{s}", f"/sweep-{s} {name}{sweep_args}") for s in profile["sources"]]
                phases.append(("triage", f"/triage {name}" + (" --dry-run" if dry else "")))
                for phase, prompt in phases:
                    self.out(f"{name}: {phase}")
                    before = self.watermarks(name)
                    try:
                        self.headless(profile, phase, prompt, dry)
                        problem = self.phase_problem(name, phase, before)
                    except (subprocess.CalledProcessError, OSError) as error:  # OSError: no workdir, no claude
                        problem = failure(phase, error)
                    self.report(run, name, problem)
                lock = self.executor_lock()
                try:  # what triage approved silently (bot fixes, merges of the base) happens now, not when I open proc
                    problem = self.execute(profile, dry) if lock else "execute skipped: another executor is running"
                except (subprocess.CalledProcessError, OSError) as error:
                    problem = failure("execute", error)
                finally:
                    if lock:
                        lock.close()
                self.report(run, name, problem)
            self.cmd_brief(*(["--delta"] if opts.get("--delta") else []), *(["--project", opts["--project"]] if opts.get("--project") else []))
        finally:
            (self.state_root() / "run.json").unlink()

    def report(self, run, name, problem):
        if problem:
            run["problems"].setdefault(name, []).append(problem)
            self.write_json(self.state_root() / "run.json", run)

    def inbox(self, name):
        """The one directory a phase may write: sweepers' item files and triage's drafts, each consumed on use."""
        path = self.profile_state(name) / "inbox"
        path.mkdir(exist_ok=True)
        return path

    def inbox_file(self, name, path):
        """A --file argument, refused before any read or delete unless it sits directly in the profile's inbox."""
        path = Path(path).resolve()
        if path.parent != self.inbox(name).resolve():
            raise SystemExit(f"process: {path} is outside the {name} inbox ({self.inbox(name)}); write it there first")
        return path

    def watermarks(self, name):
        return self.read_json(self.state_root() / "profiles" / name / "watermarks.json", {})

    def phase_problem(self, name, phase, before):
        """A phase can exit 0 having done nothing (e.g. every tool denied). Judge it by its effect on the state."""
        if phase.startswith("sweep-"):
            source = phase.removeprefix("sweep-")
            if self.watermarks(name).get(source) in (None, before.get(source)):
                return f"{phase} stored nothing: its watermark did not move"
        elif self.unhandled(name):
            return f"triage left {len(self.unhandled(name))} item(s) unhandled"

    def unhandled(self, name):
        pdir = self.state_root() / "profiles" / name
        handled = {line["item_id"] for line in self.read_jsonl(pdir / "ledger.jsonl")}
        return [i for i in self.read_jsonl(pdir / "items.jsonl") if i["id"] not in handled]

    def guard_dry_run(self):
        """A dry run writes only to a scratch state directory: absent, empty or already a dry run's, never the default."""
        root = self.state_root()
        real = Path(self.env["HOME"]) / ".local" / "state" / "process"
        used = root.exists() and any(root.iterdir()) and not (root / "dry-run").exists()  # absent, empty or marked only
        if root.resolve() == real.resolve() or used:
            raise SystemExit(f"process: a --fixtures dry run needs a scratch XDG_STATE_HOME, not the real queue in {root}")
        root.mkdir(parents=True, exist_ok=True)
        (root / "dry-run").touch()

    def headless(self, profile, phase, prompt, dry=False):
        """One phase as headless Claude in dontAsk mode: only its allowlist is approved, everything else is denied, and
        known send tools (for the executor, known never actions) are denied by name too. A dry run gets no service (MCP)
        tools at all."""
        config = profile["phases"][phase]
        tools = [t for t in config["allowed_tools"] if not (dry and t.startswith("mcp__"))]
        inbox = self.inbox(profile["name"])
        dirs = [inbox]  # dontAsk refuses heredocs, so items and drafts travel as inbox files
        if phase == "execute":
            dirs.append(self.profile_state(profile["name"]) / "work")  # a fix edits files in its scratch worktree only
            dirs[-1].mkdir(exist_ok=True)
        tools += [f"Edit(/{d}/**)" for d in dirs] + ([f"Read(/{d}/**)" for d in dirs] if phase == "execute" else [])
        workdir = self.workdir(profile)
        cmd = ["claude", "-p", prompt, "--model", config["model"], "--permission-mode", "dontAsk"]
        denied = NEVER_DENIED if phase == "execute" else DENIED_TOOLS
        cmd += ["--add-dir", *map(str, dirs), "--allowedTools", ",".join(tools), "--disallowedTools", ",".join(denied)]
        return self.run(cmd, cwd=workdir)

    def workdir(self, profile):
        return Path(profile["workdir"].replace("~", self.env["HOME"], 1) if profile["workdir"].startswith("~") else profile["workdir"])

    def cmd_profile(self, name):
        """Print one validated profile, path first, so a phase reads it without guessing XDG_CONFIG_HOME."""
        self.profile(name)
        path = self.profiles_dir() / f"{name}.md"
        self.out(f"{path}\ninbox: {self.inbox(name)}\n{path.read_text()}")

    def cmd_profiles(self):
        """List every profile, refusing if any is invalid or grants a send tool to a sweep or triage phase."""
        for name, profile in self.profiles().items():
            self.out(f"{name}  {'active' if profile['active'] else 'inactive'}  {', '.join(profile['sources'])}")

    # -- sweeper side --

    def cmd_emit(self, profile, source, *args):
        """Store a sweeper's candidate items (JSONL on stdin, or --file) and advance its watermark."""
        opts = flags(args, "--watermark", "--file")
        path = opts.get("--file") and self.inbox_file(profile, opts["--file"])
        text = path.read_text() if path else self.stdin.read()
        pdir = self.profile_state(profile)
        known = {i["id"] for i in self.read_jsonl(pdir / "items.jsonl")}
        fresh, problems = [], []
        for n, candidate in enumerate(self.parse_jsonl(text, "item"), 1):
            candidate.pop("id", None)
            record = {"id": f"{candidate.get('source')}:{candidate.get('native_id')}@{digest(str(candidate.get('version')))}", "profile": profile, **candidate}
            errors = validate(record, "item")
            if not errors and record["source"] != source:
                errors = [f"$.source: {record['source']!r} emitted by the {source} sweeper"]
            problems += [f"item {n}: {e}" for e in errors]
            if record["id"] not in known:
                known.add(record["id"])
                fresh.append(record)
        if problems:
            raise SystemExit("process: nothing stored, invalid items:\n" + "\n".join(problems))
        self.append_jsonl(pdir / "items.jsonl", fresh)
        marks = self.read_json(pdir / "watermarks.json", {})
        marks[source] = opts.get("--watermark") or iso(self.clock())
        self.write_json(pdir / "watermarks.json", marks)
        if path:
            path.unlink()  # consumed, so a later sweep can never re-send it
        self.out(f"stored {len(fresh)} new item(s) for {profile}/{source}; watermark {marks[source]}")

    def cmd_watermark(self, profile, source):
        """Print the source's watermark for the profile; nothing when it has never been swept."""
        mark = self.read_json(self.state_root() / "profiles" / profile / "watermarks.json", {}).get(source)
        if mark:
            self.out(mark)

    def cmd_validate(self, kind, path=None):
        """Validate a JSON or JSONL file (or stdin) against one contract shape; silent when valid."""
        if kind not in SCHEMA["$defs"]:
            raise SystemExit(f"process: unknown shape {kind!r}")
        text = Path(path).read_text() if path else self.stdin.read()
        try:
            records = [json.loads(text)]
        except json.JSONDecodeError:
            records = self.parse_jsonl(text, kind)
        problems = [f"record {n}: {e}" for n, r in enumerate(records, 1) for e in validate(r, kind)]
        if problems:
            raise SystemExit("\n".join(problems))


    # -- triage side --

    def cmd_triage(self, step, profile, *args):
        """plan → draft/silent → commit. The triage skill drives it; plan and commit hold every rule."""
        steps = {"plan": self.triage_plan, "draft": self.triage_draft, "silent": self.triage_silent, "commit": self.triage_commit}
        if step not in steps:
            raise SystemExit(f"process: triage step must be one of {', '.join(steps)}")
        return call(steps[step], (profile, *args), f"triage {step}")

    def triage_plan(self, name):
        """Turn every unhandled item into proposed queue entries in plan.json, and print what needs drafting."""
        profile = self.profile(name)
        pdir = self.profile_state(name)
        items = self.unhandled(name)
        # The one-time clear-out happens on the first triage that sees Linear notifications, not merely the first triage.
        first_run = "bankrupt_at" not in self.read_json(pdir / "state.json", {}) and any(notification(i) for i in items)
        cutoff = iso(self.clock() - BANKRUPTCY_AGE)
        stale = [i for i in items if first_run and i["source"] == "linear" and notification(i) and i["timestamp"] < cutoff]
        dropped = [i for i in items if i["actor"]["role"] == "me" and i["kind"] != "pr_status"]
        live = [i for i in items if i not in stale and i not in dropped]
        groups = {}
        for i in sorted(live, key=lambda i: (i["source"], i["timestamp"])):  # github first: its item leads the entry
            groups.setdefault((i["kind"], canonical(i["subject"])), []).append(i)
        entries = [entry(name, profile, group) for group in groups.values()]
        if stale:
            entries.append(mark_read(name, stale, f"Cleared {len(stale)} Linear notification(s) older than 7 days (first run)"))
        read = [i for i in live + dropped if notification(i)]  # my own get marked read too
        if read:
            entries.append(mark_read(name, read, f"Marked {len(read)} processed Linear notification(s) read"))
        plan = {"profile": name, "first_run": first_run, "dropped": [i["id"] for i in dropped], "entries": entries}
        self.write_json(pdir / "plan.json", plan)
        by_id = {i["id"]: i for i in items}
        for e in entries:
            todo = "draft" if needs_draft(e) and not e["action"].get("draft") else "do, then report" if e["action"]["type"] == "linear_mark_read" else e["status"]
            self.out(f"{e['id']}  [{todo}]  {e['action']['type']} {json.dumps(e['action']['params'])}  {e['summary']}  {e['link']}")
            if todo == "draft":
                for item_id in e["item_ids"]:
                    self.out(f"    {item_id}: {by_id[item_id]['summary']}\n" + indent(by_id[item_id]["context"], "      | "))
        self.out(f"{len(entries)} entr(ies) planned in {pdir / 'plan.json'}; {len(dropped)} item(s) of my own dropped")

    def plan_entry(self, name, entry_id):
        plan = self.read_json(self.profile_state(name) / "plan.json", None)
        if plan is None:
            raise SystemExit(f"process: no triage plan for {name}; run `process triage plan {name}` first")
        found = [e for e in plan["entries"] if e["id"] == entry_id]
        if not found:
            raise SystemExit(f"process: no entry {entry_id} in the {name} plan")
        return plan, found[0]

    def triage_draft(self, name, entry_id, *args):
        """Set an entry's draft from stdin, or from --file (consumed)."""
        opts = flags(args, "--file")
        path = opts.get("--file") and self.inbox_file(name, opts["--file"])
        plan, e = self.plan_entry(name, entry_id)
        e["action"]["draft"] = (path.read_text() if path else self.stdin.read()).strip()
        if path:
            path.unlink()
        self.write_json(self.profile_state(name) / "plan.json", plan)

    def triage_silent(self, name, entry_id, outcome, note=None):
        """Record how a silent action triage performed itself went: done, failed or skipped (dry run)."""
        if outcome not in ("done", "failed", "skipped"):
            raise SystemExit("process: a silent outcome is done, failed or skipped")
        plan, e = self.plan_entry(name, entry_id)
        if e["action"]["type"] != "linear_mark_read":
            raise SystemExit(f"process: {entry_id} is {e['action']['type']}, not a silent action triage performs")
        e["status"] = outcome
        if note:
            e["note"] = note
        self.write_json(self.profile_state(name) / "plan.json", plan)

    def triage_commit(self, name):
        """Validate the finished plan, then append it to the queue and ledger. All or nothing."""
        pdir = self.profile_state(name)
        plan = self.read_json(pdir / "plan.json", None)
        if plan is None:
            raise SystemExit(f"process: no triage plan for {name}; run `process triage plan {name}` first")
        run = self.read_json(self.state_root() / "run.json", {}).get("id") or iso(self.clock())
        entries = [{**e, "run": run, "created": iso(self.clock())} for e in plan["entries"]]
        problems = []
        for e in entries:
            problems += [f"{e['id']}: {p}" for p in validate(e, "queue_entry")]
            if needs_draft(e) and not e["action"].get("draft"):
                problems.append(f"{e['id']}: no draft (process triage draft {name} {e['id']})")
            if e["action"]["type"] == "linear_mark_read" and e["status"] == "approved":
                problems.append(f"{e['id']}: no outcome (process triage silent {name} {e['id']} done|failed|skipped)")
        if problems:
            raise SystemExit("process: nothing committed:\n" + "\n".join(f"  {p}" for p in problems))
        keys = {(e["kind"], e["subject"]) for e in entries}
        superseded = [
            {**old, "status": "superseded"}
            for old in self.queue()
            if old["project"] == name and old["status"] in ("pending", "info") and (old["kind"], old["subject"]) in keys
        ]
        self.append_jsonl(self.state_root() / "queue.jsonl", superseded + entries)
        handled = list(dict.fromkeys([i for e in entries for i in e["item_ids"]] + plan["dropped"]))
        self.append_jsonl(pdir / "ledger.jsonl", [{"item_id": i, "at": iso(self.clock())} for i in handled])
        if plan["first_run"]:
            self.write_json(pdir / "state.json", {**self.read_json(pdir / "state.json", {}), "bankrupt_at": iso(self.clock())})
        (pdir / "plan.json").unlink()
        self.out(f"committed {len(entries)} entr(ies) for {name}; superseded {len(superseded)}; {len(handled)} item(s) handled")

    # -- next work --

    def cmd_next(self, name, *args):
        """Rank the next-work skill's candidate tickets (JSONL on stdin, or --file) into up to 3 recommendations, each
        with its reason, and an AFK list, in next.json. Promotions and AFK launches are queued for my approval, once
        each: a rerun keeps a proposal still waiting, supersedes one it no longer makes, and never re-proposes one I
        decided. All or nothing."""
        opts = flags(args, "--file")
        profile = self.profile(name)
        path = opts.get("--file") and self.inbox_file(name, opts["--file"])
        tickets = self.parse_jsonl(path.read_text() if path else self.stdin.read(), "ticket")
        problems = [f"ticket {n}: {e}" for n, t in enumerate(tickets, 1) for e in validate(t, "ticket")]
        if problems:
            raise SystemExit("process: nothing ranked, invalid tickets:\n" + "\n".join(problems))
        now = self.clock()
        proposals = [e for e in self.queue() if e["project"] == name and e["kind"] == "next_work"]
        decided = {(e["action"]["type"], e["subject"]): e["status"] for e in proposals if e["status"] not in ("pending", "superseded")}
        waiting = {(e["action"]["type"], e["subject"]): e for e in proposals if e["status"] == "pending"}
        status = lambda action_type, t: decided.get((action_type, canonical(t["url"])))
        # An AFK launch I approved has the ticket; one I skipped (or that failed) hands it back to me.
        free = [t for t in tickets if not t["blocked"] and status("afk_launch", t) not in ("approved", "done")]
        todo = [t for t in free if t["state"] == "todo" and t["assignee"] != "other"]
        afk = [t for t in todo if trivial(profile, t) and not status("afk_launch", t)]  # a separate list, not one of my 3
        todo = [t for t in todo if t not in afk]
        unblocked = [t for t in todo if t.get("unblocked_at", "") >= iso(now - UNBLOCKED_AGE)]
        tiers = [
            ("in_progress", [t for t in free if t["state"] == "in_progress" and t["assignee"] == "me"]),
            ("unblocked", unblocked),
            ("todo", [t for t in todo if t not in unblocked]),
        ]
        ranked = [(tier, t) for tier, group in tiers for t in sorted(group, key=ticket_order)]
        picks = (self.pr_fixes(name) + [recommend(tier, t, now) for tier, t in ranked])[:NEXT_SLOTS]
        backlog = [t for t in free if t["state"] == "backlog" and t["assignee"] != "other" and not status("linear_promote", t)]
        proposed = []

        def propose(action, summary, t):
            e = waiting.get((action["type"], canonical(t["url"]))) or self.next_entry(profile, action, summary, t)
            proposed.append(e)
            return e["id"]

        for t in sorted(backlog, key=ticket_order)[: NEXT_SLOTS - len(picks)]:  # Todo is thin: Backlog fills the slots it leaves
            pick = recommend("promote", t, now)
            action = {"type": "linear_promote", "params": ticket_ref(t), "draft": f"Move {t['identifier']} from Backlog to Todo. {pick['reason']}."}
            picks.append({**pick, "entry_id": propose(action, f"Promote {t['identifier']} {t['title']} from Backlog to Todo", t)})
        afk_list = []
        for t in sorted(afk, key=ticket_order):
            action = {"type": "afk_launch", "params": ticket_ref(t), "draft": (t.get("description") or "").strip() or f"No description: see {t['url']}."}
            entry_id = propose(action, f"Hand {t['identifier']} {t['title']} to an AFK worker", t)
            afk_list.append({"tier": "afk", "ref": t["identifier"], "title": t["title"], "link": t["url"], "reason": trivial(profile, t), "entry_id": entry_id})
        result = {"profile": name, "at": iso(now), "recommendations": picks, "afk": afk_list}
        fresh = [e for e in proposed if e not in waiting.values()]
        problems = validate(result, "next_work") + [f"{e['id']}: {p}" for e in fresh for p in validate(e, "queue_entry")]
        if problems:
            raise SystemExit("process: nothing written:\n" + "\n".join(f"  {p}" for p in problems))
        stale = [{**e, "status": "superseded"} for e in waiting.values() if e not in proposed]
        self.append_jsonl(self.state_root() / "queue.jsonl", stale + fresh)
        self.write_json(self.profile_state(name) / "next.json", result)
        if path:
            path.unlink()
        for n, r in enumerate(picks, 1):
            self.out(f"{n}. {r['ref']}  {r['title']}  — {r['reason']}  {r['link']}")
        for r in afk_list:
            self.out(f"AFK {r['ref']}  {r['title']}  — {r['reason']}  {r['link']}")

    def next_entry(self, profile, action, summary, t):
        """A queue entry for a promotion or AFK launch: drafted for my approval, unless I promoted its type to silent."""
        silent = action["type"] in profile["autonomy"]["silent"]
        return {
            "id": "q-" + digest(profile["name"] + "next_work" + action["type"] + canonical(t["url"])),
            "project": profile["name"],
            "item_ids": [f"linear:{t['identifier']}"],
            "sources": ["linear"],
            "kind": "next_work",
            "subject": canonical(t["url"]),
            "summary": summary,
            "link": t["url"],
            "urgency": "normal",
            "autonomy": "silent" if silent else "draft",
            "action": action,
            "status": "approved" if silent else "pending",
            "run": self.read_json(self.state_root() / "run.json", {}).get("id") or iso(self.clock()),
            "created": iso(self.clock()),
        }

    def pr_fixes(self, name):
        """One recommendation per PR of mine with fixes waiting on my approval, most urgent first. Silent fixes are the
        executor's, not mine."""
        prs = {}
        for e in sorted(self.queue(), key=lambda e: (URGENCY.index(e["urgency"]), e["created"])):
            if e["project"] == name and e["status"] == "pending" and e["action"]["type"] in ("gh_fix_pr", "gh_thread_reply"):
                prs.setdefault((e["action"]["params"]["repo"], e["action"]["params"]["number"]), []).append(e)
        return [
            {
                "tier": "pr_fix",
                "ref": f"{repo}#{number}",
                "title": fixes[0]["summary"],
                "link": f"https://github.com/{repo}/pull/{number}",
                "reason": f"My own PR: {len(fixes)} fix{'es' if len(fixes) > 1 else ''} waiting on me, first {fixes[0]['summary']}",
            }
            for (repo, number), fixes in prs.items()
        ]

    # -- executor side --

    def cmd_execute(self, *args):
        """Apply proc's decisions to the queue, then run the executor headless for each profile (or --project) that
        has approved actions waiting. proc runs this when I quit it; `process run` runs it after triage. The executor
        skill drives the steps: `process execute plan|review|reply|checkout|git|push|result <profile> ...`."""
        steps = {
            "plan": self.execute_plan,
            "result": self.execute_result,
            "review": self.execute_review,
            "reply": self.execute_reply,
            "checkout": self.execute_checkout,
            "git": self.execute_git,
            "push": self.execute_push,
        }
        if args and args[0] in steps:
            return call(steps[args[0]], args[1:], f"execute {args[0]}")
        opts = flags(args, "--project")
        profiles = self.profiles()
        if opts.get("--project") and opts["--project"] not in profiles:
            raise SystemExit(f"process: no profile {opts['--project']!r} in {self.profiles_dir()}")
        lock = self.executor_lock()
        if lock is None:
            self.out("process: another executor is running; these decisions apply on the next run")
            return 0
        with lock:
            self.apply_decisions(profiles)
            dry = (self.state_root() / "dry-run").exists()
            failed = False
            for name in [opts["--project"]] if opts.get("--project") else list(profiles):
                try:
                    problem = self.execute(profiles[name], dry)
                except (subprocess.CalledProcessError, OSError) as error:  # one profile's executor must not stop the rest
                    problem, failed = failure("execute", error), True
                if problem:
                    self.out(f"process: {name}: {problem}")
        return 1 if failed else 0

    def executor_lock(self):
        """One executor at a time (proc's on quit, or a run's after triage), or both could send the same approved
        review. Returns the held lock, released when closed or when this process exits; None if another holds it."""
        self.state_root().mkdir(parents=True, exist_ok=True)
        lock = (self.state_root() / "execute.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return None
        return lock

    def execute(self, profile, dry):
        """Start the profile's approved AFK launches, then run the executor phase when it has other approved entries;
        return what went wrong, if anything. A dry run (a dry-run state directory) gets --dry-run and no service tools."""
        self.launch_afk(profile, dry)
        name, waiting = profile["name"], len(self.approved(profile["name"]))
        if not waiting:
            return None
        if "execute" not in profile["phases"]:
            return f"{name} has no [phases.execute]; {waiting} approved entr(ies) wait"
        self.out(f"{name}: execute")
        self.headless(profile, "execute", f"/execute {name}" + (" --dry-run" if dry else ""), dry)
        if self.approved(name):
            return f"execute left {len(self.approved(name))} approved entr(ies) without a result"

    def approved(self, name, action_type=None):
        """Entries waiting on the executor phase: approved by me, or promoted to silent, with an action that carries
        text. AFK launches are not the phase's: `execute` starts them itself, and only they are listed for afk_launch."""
        return [
            e for e in self.queue()
            if e["project"] == name and e["status"] == "approved" and needs_draft(e) and (e["action"]["type"] == "afk_launch") == (action_type == "afk_launch")
        ]

    # An approved AFK launch needs no headless phase: its draft becomes a ticket in a local afk spec in the profile's
    # workdir, and afk starts the worker, in a tmux session of the spec's own that runs afk's watcher.

    def launch_afk(self, profile, dry):
        """Start a worker for each approved afk_launch, oldest first, and record how each went."""
        name, limit, running = profile["name"], profile.get("afk", {}).get("max_workers", AFK_MAX_WORKERS), None
        for e in self.approved(name, "afk_launch"):
            if dry:
                self.record(name, e, "skipped")
                continue
            try:
                project, spec = self.afk_project(profile)
                if running is None:
                    running = self.afk_running(project, profile)
                if running >= limit:  # still approved: the next executor run tries again
                    self.out(f"{e['id']}: waits for a free AFK slot ({running} of {limit} running)")
                    continue
                number = self.afk_ticket(spec, e)
                self.run(["env", f"AFK_PROJECT={project}", "afk", "start", number], cwd=self.workdir(profile))
            except (subprocess.CalledProcessError, OSError, ValueError) as error:  # ValueError: afk printed no JSON
                self.record(name, e, "failure", error=failure("afk", error).removeprefix("afk failed: "))
                continue
            running += 1
            self.record(name, e, "success", link=e["action"]["params"]["url"])

    def afk_running(self, project, profile):
        """Workers running in the project, counted as afk counts them: one in review is parked on me, a hitl one is mine."""
        workers = json.loads(self.run(["env", f"AFK_PROJECT={project}", "afk", "next"], cwd=self.workdir(profile)))["workers"]
        return sum(w["phase"] not in ("review", "hitl") for w in workers)

    def afk_project(self, profile):
        """The profile's afk project and its spec directory, bound to a tmux session of the same name on first use."""
        project, workdir = f"process-{profile['name']}", self.workdir(profile)
        spec = workdir / ".scratch" / project  # afk's local spec layout: <repo>/.scratch/<project>/issues/
        if not workdir.is_dir():
            raise FileNotFoundError(f"no workdir {workdir}")
        (spec / "issues").mkdir(parents=True, exist_ok=True)
        try:
            self.run(["tmux", "has-session", "-t", f"={project}"])
        except subprocess.CalledProcessError:
            pane = self.run(["tmux", "new-session", "-d", "-s", project, "-c", str(workdir), "-P", "-F", "#{pane_id}"]).strip()
            self.run(["env", f"TMUX_PANE={pane}", "afk", "init", str(spec)], cwd=workdir)  # binds the pane's session
            self.run(["tmux", "send-keys", "-t", pane, "afk watch", "Enter"])  # drives the workers through their phases
        return project, spec

    def afk_ticket(self, spec, e):
        """Write the entry's approved draft as the spec's next ticket, once: a retry finds the file it wrote before."""
        issues = sorted((spec / "issues").glob("*.md"))
        for path in issues:
            if re.search(rf"^\*\*Queue entry:\*\* {e['id']}$", path.read_text(), re.M):
                return path.name.split("-", 1)[0]
        number = str(max([int(p.name.split("-", 1)[0]) for p in issues if p.name.split("-", 1)[0].isdigit()], default=0) + 1).zfill(2)
        params = e["action"]["params"]
        title = " ".join(f"{params['ticket']} {params['title']}".split())
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50].rstrip("-")
        # afk reads a ticket's fields from their first match, so these come before the draft, which quotes Linear.
        (spec / "issues" / f"{number}-{slug}.md").write_text(
            f"# {number} — {title}\n\n**Status:** ready-for-agent\n\n**Blocked by:** None — can start immediately\n\n"
            f"**Linear:** {params['url']}\n\n**Queue entry:** {e['id']}\n\n{e['action']['draft'].strip()}\n\n{AFK_PR.format(ticket=params['ticket'])}\n"
        )
        return number

    def apply_decisions(self, profiles):
        """Turn each decision on a still-pending entry into its new state. A decision on anything else is stale (the
        entry was superseded or already decided) and changes nothing, so applying twice is a no-op."""
        queue = {e["id"]: e for e in self.queue()}
        path = self.state_root() / "decisions.jsonl"
        for n, line in enumerate(path.read_text().splitlines() if path.exists() else [], 1):
            if not line.strip():
                continue
            try:  # proc appends; a line it never finished must not block every decision after it
                decision = json.loads(line)
            except json.JSONDecodeError:
                self.out(f"process: decision {n} ignored: not JSON")
                continue
            problems = validate(decision, "decision")
            if problems:
                self.out(f"process: decision {n} ignored: {problems[0]}")
                continue
            e = queue.get(decision["entry_id"])
            if not e or e["status"] != "pending" or e["project"] not in profiles:
                continue
            if decision["verdict"] == "approve":
                draft = decision.get("draft") or e["action"].get("draft", "")
                did = "edited" if draft != e["action"].get("draft", "") else "approved"
                e = {**e, "status": "approved", "action": {**e["action"], "draft": draft}}
            else:
                did, e = "skipped", {**e, "status": "skipped"}
            # The entry leaves pending first, so a crash below can never teach the same decision twice.
            self.append_jsonl(self.state_root() / "queue.jsonl", [e])
            queue[e["id"]] = e
            if e["status"] == "skipped":
                self.handle(e["project"], [e], "skipped")
            if (decision.get("promote_to_silent") or decision.get("reason", "").strip()) and self.real_profiles_in_dry_run():
                self.out(f"process: dry run: {e['project']}'s profile is real, so no lesson or promotion is written")
                continue
            try:  # the profile is restored on a failed rewrite; the rest of the decisions still apply
                if decision.get("promote_to_silent"):
                    self.promote(e["project"], e["action"]["type"])
                if decision.get("reason", "").strip():
                    self.learn(e, did, decision["reason"])
            except SystemExit as error:
                self.out(f"{error.code}; edit the profile by hand")

    def real_profiles_in_dry_run(self):
        """A dry run may teach only scratch profiles: an XDG_CONFIG_HOME other than ~/.config."""
        real = Path(self.env["HOME"]) / ".config" / "process" / "profiles"
        return (self.state_root() / "dry-run").exists() and self.profiles_dir().resolve() == real.resolve()

    def learn(self, e, did, reason):
        """A lesson is my reason, word for word, never a paraphrase: the profile is read by every phase, so nothing
        a phase read from a PR or a ticket may reach it."""
        when = f"{datetime.fromtimestamp(self.clock()).astimezone():%Y-%m-%d}"
        lesson = f"Lesson ({when}, I {did} a {e['action']['type']}): {' '.join(reason.split())}"
        self.edit_profile(e["project"], lesson=lesson, changelog=f"Lesson from my reason on {e['id']}.")

    def promote(self, name, action_type):
        """Add an action type to the profile's autonomy overrides, so triage approves it without asking next time."""
        silent = self.profile(name)["autonomy"]["silent"]
        if action_type not in SCHEMA["$defs"]["profile"]["properties"]["autonomy"]["properties"]["silent"]["items"]["enum"]:
            self.out(f"process: {name}: {action_type} cannot be silent; not promoted")
        elif action_type not in silent:
            self.edit_profile(name, silent=silent + [action_type], changelog=f"Promoted {action_type} to silent.")

    def edit_profile(self, name, changelog, silent=None, lesson=None):
        """Rewrite a profile in place: the autonomy overrides, a lesson at the end of How to sweep, and always a
        Changelog line. The result must load cleanly, or the profile is left as it was."""
        path = self.profiles_dir() / f"{name}.md"
        text = path.read_text()
        match = re.match(r"(\+\+\+\n.*?\n\+\+\+\n)(.*)", text, re.S)
        front, body = match[1], match[2]
        if silent is not None:
            front = re.sub(r"^silent\s*=\s*\[[^\]]*\]", lambda _: "silent = " + json.dumps(silent), front, count=1, flags=re.M)
        if lesson:
            body = insert_in_section(body, "## How to sweep", f"- {lesson}")
        body = insert_in_section(body, "## Changelog", f"- {datetime.fromtimestamp(self.clock()).astimezone():%Y-%m-%d} — {changelog}")
        path.write_text(front + body)
        try:
            profile = load_profile(path)
            if silent is not None and profile["autonomy"]["silent"] != silent:
                raise SystemExit(f"process: {path}: could not rewrite autonomy.silent")
        except SystemExit:
            path.write_text(text)
            raise

    def execute_plan(self, name):
        """Print every approved entry for the executor: its action, params, link and the exact text to send."""
        self.profile(name)
        entries = self.approved(name)
        for e in entries:
            self.out(f"{e['id']}  {e['action']['type']} {json.dumps(e['action']['params'])}  {e['summary']}  {e['link']}")
            self.out(indent(e["action"]["draft"], "    | ", limit=None))
        self.out(f"{len(entries)} approved entr(ies) for {name}")

    def execute_result(self, name, entry_id, outcome, *args):
        """Record what the executor did with one approved entry: success (--link), failure (--error), or skipped in a
        dry run."""
        opts = flags(args, "--link", "--error")
        found = [e for e in self.approved(name) if e["id"] == entry_id]
        if not found:
            raise SystemExit(f"process: {entry_id} is not approved for {name}; the executor acts only on approved entries")
        dry = (self.state_root() / "dry-run").exists()
        if dry != (outcome == "skipped"):
            raise SystemExit("process: a dry run records skipped" if dry else "process: only a dry run records skipped")
        self.record(name, found[0], outcome, **{k[2:]: v for k, v in opts.items() if isinstance(v, str)})

    def record(self, name, e, outcome, **details):
        result = {"entry_id": e["id"], "outcome": outcome, **details, "at": iso(self.clock())}
        problems = validate(result, "result")
        if problems:
            raise SystemExit("process: result not recorded:\n" + "\n".join(f"  {p}" for p in problems))
        status = {"success": "done", "failure": "failed", "skipped": "skipped"}[outcome]
        note = result.get("link") or result.get("error")
        e = {**e, "status": status, **({"note": note} if note else {})}
        self.append_jsonl(self.state_root() / "results.jsonl", [result])
        self.append_jsonl(self.state_root() / "queue.jsonl", [e])
        self.handle(name, [e], status)
        worktree = self.profile_state(name) / "work" / e["id"]
        if worktree.exists():
            self.run(["git", "-C", str(self.workdir(self.profile(name))), "worktree", "remove", "--force", str(worktree)])
        self.out(f"{e['id']}: {status}{': ' + note if note else ''}")

    # Reviews and replies are sent here, not by a GitHub tool in the executor's allowlist: the text is exactly what I
    # approved, the event is pinned, and a review tool could otherwise approve.

    def send_entry(self, name, entry_id, action_type):
        found = [e for e in self.approved(name) if e["id"] == entry_id and e["action"]["type"] == action_type]
        if not found:
            raise SystemExit(f"process: {entry_id} is not an approved {action_type} for {name}")
        if (self.state_root() / "dry-run").exists():
            raise SystemExit("process: a dry run touches no PR; record the entry as skipped")
        return found[0]

    def send(self, name, e, endpoint, body):
        """POST one review or comment, then record the result: its URL, or the error."""
        try:
            response = self.run(["gh", "api", "-X", "POST", endpoint, "--input", "-"], input=json.dumps(body), cwd=self.workdir(self.profile(name)))
        except (subprocess.CalledProcessError, OSError) as error:
            problem = failure("send", error)
            self.record(name, e, "failure", error=problem.removeprefix("send failed: "))
            raise SystemExit(f"process: {problem}")
        self.record(name, e, "success", link=json.loads(response)["html_url"])

    def execute_review(self, name, entry_id):
        """Submit an approved gh_review: its approved draft, as a COMMENT or REQUEST_CHANGES review."""
        e = self.send_entry(name, entry_id, "gh_review")
        params = e["action"]["params"]
        if params.get("event") not in ("COMMENT", "REQUEST_CHANGES"):
            raise SystemExit(f"process: {entry_id} asks for a {params.get('event')!r} review; the executor will never APPROVE")
        self.send(name, e, f"repos/{params['repo']}/pulls/{params['number']}/reviews", {"event": params["event"], "body": e["action"]["draft"]})

    def execute_reply(self, name, entry_id):
        """Post an approved gh_thread_reply: the draft above its `## Fix` section, in the comment's own thread."""
        e = self.send_entry(name, entry_id, "gh_thread_reply")
        params = e["action"]["params"]
        body = re.split(r"^## Fix\s*$", e["action"]["draft"], maxsplit=1, flags=re.M)[0].strip()
        inline = re.search(r"#discussion_r(\d+)$", params.get("comment", ""))
        endpoint = f"repos/{params['repo']}/pulls/{params['number']}/comments/{inline[1]}/replies" if inline else f"repos/{params['repo']}/issues/{params['number']}/comments"
        self.send(name, e, endpoint, {"body": body})

    # The executor fixes my PRs in a scratch worktree per entry, never in my own checkout, and pushes only here.

    def fix_entry(self, name, entry_id):
        found = [e for e in self.approved(name) if e["id"] == entry_id and e["action"]["type"] in ("gh_fix_pr", "gh_thread_reply")]
        if not found:
            raise SystemExit(f"process: {entry_id} is not an approved fix for {name}")
        if (self.state_root() / "dry-run").exists():
            raise SystemExit("process: a dry run touches no PR; record the entry as skipped")
        return self.profile(name), found[0], self.profile_state(name) / "work" / entry_id

    def my_open_pr(self, profile, e):
        """The PR's branches, after checking it is mine, open and on the repo itself (a fork's branch is not mine)."""
        repo, number = e["action"]["params"]["repo"], e["action"]["params"]["number"]
        fields = "headRefName,baseRefName,author,isCrossRepository,state"
        pr = json.loads(self.run(["gh", "pr", "view", str(number), "--repo", repo, "--json", fields], cwd=self.workdir(profile)))
        me = self.run(["gh", "api", "user", "--jq", ".login"], cwd=self.workdir(profile)).strip()
        if pr["author"]["login"] != me or pr["state"] != "OPEN" or pr["isCrossRepository"]:
            raise SystemExit(f"process: {repo}#{number} is not my open PR on {repo}; the executor never touches it")
        origin = self.run(["git", "-C", str(self.workdir(profile)), "remote", "get-url", "origin"]).strip()
        if not re.fullmatch(rf"(https://|ssh://git@|git@)github\.com[:/]{re.escape(repo)}(\.git)?/?", origin, re.I):
            raise SystemExit(f"process: the workdir's origin is not {repo} ({origin}); fetching or pushing there would miss the PR")
        return pr

    def execute_checkout(self, name, entry_id):
        """Check out my PR's head in the entry's scratch worktree and print its path."""
        profile, e, worktree = self.fix_entry(name, entry_id)
        pr = self.my_open_pr(profile, e)
        repo = str(self.workdir(profile))
        self.run(["git", "-C", repo, "fetch", "origin", pr["headRefName"], pr["baseRefName"]])
        if worktree.exists():  # left by a run that crashed before its result: start again from the PR's head
            self.run(["git", "-C", repo, "worktree", "remove", "--force", str(worktree)])
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self.run(["git", "-C", repo, "worktree", "add", "--detach", str(worktree), f"origin/{pr['headRefName']}"])
        self.out(f"worktree: {worktree}\nhead: {pr['headRefName']}\nbase: origin/{pr['baseRefName']}")

    def execute_git(self, name, entry_id, subcommand, *args):
        """Run one git step of a fix in the entry's worktree. Pushing goes through `execute push` instead."""
        _, _, worktree = self.fix_entry(name, entry_id)
        if subcommand not in FIX_GIT:
            raise SystemExit(f"process: git {subcommand} is not a fix step; use one of {', '.join(sorted(FIX_GIT))}")
        refused = [a for a in args if fix_git_refused(a)]
        if refused:
            raise SystemExit(f"process: git {refused[0]} is not allowed in a fix: it reads or writes files outside the worktree")
        try:
            self.out(self.run(["git", "-C", str(worktree), subcommand, *args]).rstrip())
        except subprocess.CalledProcessError as error:  # a merge conflict says what conflicts on stdout
            raise SystemExit(f"process: git {subcommand} failed:\n{(error.stdout or '').strip()}\n{(error.stderr or '').strip()}".rstrip())

    def execute_push(self, name, entry_id):
        """Push the worktree's commits to my PR's branch: no force, no other branch. Prints the commit's link."""
        profile, e, worktree = self.fix_entry(name, entry_id)
        pr = self.my_open_pr(profile, e)
        self.run(["git", "-C", str(worktree), "push", "origin", f"HEAD:refs/heads/{pr['headRefName']}"])
        sha = self.run(["git", "-C", str(worktree), "rev-parse", "--short", "HEAD"]).strip()
        self.out(f"pushed: https://github.com/{e['action']['params']['repo']}/pull/{e['action']['params']['number']}/commits/{sha}")

    def handle(self, name, entries, outcome):
        """Record entries' items in the ledger with how they ended, so the same version never comes back."""
        at = iso(self.clock())
        self.append_jsonl(self.profile_state(name) / "ledger.jsonl", [{"item_id": i, "at": at, "entry_id": e["id"], "outcome": outcome} for e in entries for i in e["item_ids"]])

    # -- the brief --

    def cmd_brief(self, *args):
        """Render brief.md from the queue, grouped by project, and append it to today's archive."""
        opts = flags(args, "--delta", "--project")
        profiles = self.profiles()
        names = [opts["--project"]] if opts.get("--project") else [n for n, p in profiles.items() if p["active"]]
        unknown = [n for n in names if n not in profiles]
        if unknown:
            raise SystemExit(f"process: no profile {unknown[0]!r} in {self.profiles_dir()}")
        queue = self.queue()
        run = self.read_json(self.state_root() / "run.json", {})
        latest = run.get("id") or max((e["run"] for e in queue), default=None)
        now = datetime.fromtimestamp(self.clock()).astimezone()
        midnight = iso(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        # An executor result shows until a brief of its own project has shown it; a full brief also shows today's.
        last = self.read_json(self.state_root() / "brief.json", {})
        results = self.read_jsonl(self.state_root() / "results.jsonl")
        mode = "delta" if opts.get("--delta") else "full"
        lines = [f"# Brief — {now:%a %Y-%m-%d %H:%M} · {mode}", ""]
        for name in names:
            acted = {r["entry_id"] for r in results if r["at"] > last.get(name, "") or (mode == "full" and r["at"] >= midnight)}
            if mode == "delta":
                shown = [e for e in queue if e["run"] == latest or e["id"] in acted]
            else:
                shown = [e for e in queue if e["status"] in ("pending", "approved") or e["created"] >= midnight or e["id"] in acted]
            entries = [e for e in shown if e["project"] == name and e["status"] != "superseded"]
            lines += [f"## {name}", ""] + [f"> ⚠ {p}" for p in run.get("problems", {}).get(name, [])]
            lines += render_project(entries, acted) or ["Nothing new.", ""]
        brief = "\n".join(lines).rstrip() + "\n"
        self.state_root().mkdir(parents=True, exist_ok=True)
        (self.state_root() / "brief.md").write_text(brief)
        self.write_json(self.state_root() / "brief.json", {**last, **{name: iso(self.clock()) for name in names}})
        archive = self.state_root() / "briefs" / f"{now:%Y-%m-%d}.md"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with archive.open("a") as f:
            f.write(("\n---\n\n" if archive.stat().st_size else "") + brief)
        self.out(str(self.state_root() / "brief.md"))

    def queue(self):
        """The queue's current state: the last line for each entry id, in first-seen order."""
        return list({e["id"]: e for e in self.read_jsonl(self.state_root() / "queue.jsonl")}.values())


BANKRUPTCY_AGE = 7 * 24 * 3600
# A reviewer's nit, by the usual convention: the comment opens with "nit" (nit:, Nit —, [nit], nitpick).
NIT = re.compile(r"\s*\[?nit(pick)?\b", re.I)
# What a fix may do in its worktree: look, stage, commit, and merge the base branch in (never rebase, as open-pr).
FIX_GIT = {"status", "diff", "log", "show", "add", "rm", "mv", "restore", "commit", "merge"}
# Options that reach files outside the worktree (--output writes anywhere, -F/--file reads anywhere into a commit I then
# push) or run external programs.
FIX_GIT_REFUSED = {"output", "file", "template", "pathspec-from-file", "no-index", "ext-diff", "exec"}


def fix_git_refused(arg):
    """git takes any unambiguous prefix of a long option (--fil is --file), so refuse every prefix of a refused one.
    Short -F and -t also match stuck forms like -Fpath."""
    if arg.startswith("--"):
        name = arg[2:].split("=", 1)[0]
        return bool(name) and any(option.startswith(name) for option in FIX_GIT_REFUSED)
    return bool(re.match(r"-[a-zA-Z]*[Ft]", arg))
MERGE_BASE = (
    "Merge the base branch into the PR branch (never rebase) and resolve the conflicts. Push only if every conflict is "
    "mechanical: both sides kept, nothing about behaviour decided. Otherwise abort and report the conflicting files."
)
AFK_MAX_WORKERS = 2
# How an AFK worker's PR differs from afk's default: it is a draft, so it reads as unreviewed.
AFK_PR = """## Pull request

Open the PR as a draft. In the pr phase, push the branch and run `gh pr create --draft`, with a Conventional Commits
title and {ticket} in the body so Linear links it, before `/open-pr`: it finds the open draft and clears its gates.
Leave it a draft. Marking it ready for review is mine."""
PULL = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")


def needs_draft(e):
    """Every action that carries text needs a draft, even one promoted to silent: the executor sends that text."""
    return e["action"]["type"] not in ("none", "linear_mark_read")


def canonical(url):
    """Same thing, same key: lower-case host, no query, no trailing slash. Fragments stay (they name a comment)."""
    match = re.match(r"(https?://)([^/]+)([^?#]*)(\?[^#]*)?(#.*)?$", url)
    return match[1] + match[2].lower() + match[3].rstrip("/") + (match[5] or "") if match else url


def notification(i):
    return (i.get("facts") or {}).get("notification_id")


def insert_in_section(body, heading, line):
    """Add a line at the end of a markdown section: before the next ## heading, or at the end of the body."""
    start = body.index(heading) + len(heading)
    end = body.find("\n## ", start)
    end = len(body) if end == -1 else end + 1
    return body[:end].rstrip("\n") + f"\n{line}\n" + ("\n" + body[end:] if end < len(body) else "")


def indent(text, prefix, limit=20):
    return "\n".join(prefix + line for line in (text or "").splitlines()[:limit])


def mark_read(name, items, summary):
    ids = [i["id"] for i in items]
    return {
        "id": "q-" + digest(name + "linear_mark_read" + "".join(ids)),
        "project": name,
        "item_ids": ids,
        "sources": ["linear"],
        "kind": "housekeeping",
        "subject": "",
        "summary": summary,
        "link": "",
        "urgency": "low",
        "autonomy": "silent",
        "action": {"type": "linear_mark_read", "params": {"notification_ids": [notification(i) for i in items]}},
        "status": "approved",
    }


URGENCY = ["urgent", "normal", "low"]


def render_project(entries, acted=()):
    """One project's brief sections: Needs approval, Approved (what the executor did with what I approved), Done
    silently, FYI (collapsed). Empty sections are left out."""
    entries = sorted(entries, key=lambda e: (URGENCY.index(e["urgency"]), e["created"]))
    needs = [e for e in entries if e["status"] == "pending"]
    approved = [e for e in entries if e["autonomy"] == "draft" and (e["status"] == "approved" or e["id"] in acted)]
    silent = [e for e in entries if e["autonomy"] == "silent" and e["status"] in ("approved", "done", "failed", "skipped")]
    fyi = [e for e in entries if e["status"] == "info"]
    lines = []
    if needs:
        lines += [f"### Needs approval ({len(needs)})", ""]
        for e in needs:
            params = e["action"]["params"]
            ref = f" #{params['number']}" if "number" in params else ""
            lines.append(f"- {'**urgent** · ' if e['urgency'] == 'urgent' else ''}{e['summary']} · {' + '.join(e['sources'])} · {e['link']}")
            lines.append(f"  `{e['action']['type']}`{ref}{' ' + params['event'] if 'event' in params else ''} · `{e['id']}`")
            lines += [f"  > {line}" for line in (e["action"].get("draft") or "").splitlines()] + [""]
    words = {"approved": "approved, waits on the executor", "done": "done", "failed": "FAILED", "skipped": "skipped (dry run)"}
    words_for = lambda e: "approved, waits for a free AFK slot" if e["status"] == "approved" and e["action"]["type"] == "afk_launch" else words[e["status"]]
    outcome = lambda e: f"{words_for(e)}{': ' + e['note'] if e.get('note') else ''}"
    if approved:
        lines += [f"### Approved ({len(approved)})", ""]
        lines += [f"- {e['summary']} · {e['link']}\n  `{e['action']['type']}` — {outcome(e)}" for e in approved] + [""]
    if silent:
        lines += [f"### Done silently ({len(silent)})", ""]
        lines += [f"- {e['summary']} — {outcome(e)}" for e in silent] + [""]
    if fyi:
        lines += [f"### FYI ({len(fyi)})", "", "<details>", f"<summary>{len(fyi)} item(s)</summary>", ""]
        lines += [f"- {'**urgent** · ' if e['urgency'] == 'urgent' else ''}{e['summary']} · {e['link']}" for e in fyi]
        lines += ["", "</details>", ""]
    return lines


def entry(name, profile, group):
    """Apply the needs-me rules to one deduped ask: what to do about it, who decides, how urgent."""
    lead = group[0]
    facts = {k: v for i in group for k, v in (i.get("facts") or {}).items()}
    pr = PULL.search(lead["subject"]) or PULL.search(lead["link"])
    ref = {"repo": pr[1], "number": int(pr[2])} if pr else None
    action, summary, urgency, silent = {"type": "none", "params": {}}, lead["summary"], "low", False
    kind = lead["kind"]
    if kind == "review_request" and ref:
        action, urgency = {"type": "gh_review", "params": {**ref, "event": "COMMENT"}}, "normal"
    elif kind == "pr_status" and facts.get("mine") and ref:
        broken = facts.get("mergeable") == "conflicting" or facts.get("checks") == "failing" or facts.get("review") == "changes_requested"
        ready = facts.get("review") == "approved" and facts.get("checks") in ("passing", "none") and facts.get("mergeable") == "mergeable"
        conflict_only = facts.get("mergeable") == "conflicting" and facts.get("checks") != "failing" and facts.get("review") != "changes_requested"
        if conflict_only:  # mechanical: brought up to date silently, the way open-pr does it
            action, urgency, silent = {"type": "gh_fix_pr", "params": {**ref, "fix": "merge_base"}, "draft": MERGE_BASE}, "normal", True
        elif broken:
            action, urgency = {"type": "gh_fix_pr", "params": ref}, "normal"
        elif ready:
            summary += " — approved and green; merging is yours"  # merge is a never action: say so, never queue it
    elif kind == "pr_comment" and ref:
        # Fixed and pushed without asking: a bot's finding, or a nit from someone with a say in the repo. Anyone else's
        # "nit" could ask for any code at all, so it waits for me like any other comment.
        trivial = lead["actor"]["role"] == "bot" or (NIT.match(lead["context"]) and facts.get("collaborator"))
        action = {"type": "gh_fix_pr" if trivial else "gh_thread_reply", "params": {**ref, "comment": canonical(lead["subject"])}}
        urgency, silent = "normal", bool(trivial)
    elif kind in ("mention", "ticket_change"):
        urgency = "normal"
    rules = profile.get("urgency", {})
    if {i["actor"]["name"] for i in group} & set(rules.get("people", [])) or {l for i in group for l in i.get("labels", [])} & set(rules.get("labels", [])):
        urgency = "urgent"
    autonomy, status = "draft", "pending"
    if action["type"] == "none":
        autonomy, status = "silent", "info"
    elif silent or action["type"] in profile["autonomy"]["silent"]:
        autonomy, status = "silent", "approved"
    ids = [i["id"] for i in group]
    return {
        "id": "q-" + digest(name + kind + canonical(lead["subject"]) + "".join(ids)),
        "project": name,
        "item_ids": ids,
        "sources": sorted({i["source"] for i in group}),
        "kind": kind,
        "subject": canonical(lead["subject"]),
        "summary": summary,
        "link": lead["link"],
        "urgency": urgency,
        "autonomy": autonomy,
        "action": action,
        "status": status,
    }


NEXT_SLOTS = 3
PRIORITY = {1: "urgent", 2: "high", 3: "medium", 4: "low"}
# A Todo ticket stays newly unblocked this long after its last blocker cleared, then it is just another Todo ticket.
UNBLOCKED_AGE = 3 * 24 * 3600
TIER_LEADS = {"in_progress": "Already in progress", "unblocked": "Unblocked {unblocked}", "todo": "Next in Todo", "promote": "Todo is thin, so promote from Backlog"}


def ticket_order(t):
    """Priority (none last), then the project's target date (none last), then age (oldest first)."""
    return (t["priority"] or 5, t.get("target_date") or "9999-99-99", t["created"])


def trivial(profile, t):
    """Why a ticket meets the profile's trivial rules, or None: an estimate or label they name, none they exclude."""
    rules = profile.get("afk", {})
    if set(t["labels"]) & set(rules.get("exclude_labels", [])):
        return None
    if t.get("estimate") in rules.get("estimates", []):
        return f"Trivial: estimate {t['estimate']}"
    labels = [l for l in t["labels"] if l in rules.get("labels", [])]
    return f"Trivial: labeled {', '.join(labels)}" if labels else None


def ticket_ref(t):
    return {"ticket": t["identifier"], "title": t["title"], "url": t["url"]}


def recommend(tier, t, now):
    created = datetime.fromisoformat(t["created"].replace("Z", "+00:00")).timestamp()
    facts = [f"{PRIORITY[t['priority']]} priority"] if t["priority"] else []
    facts += [f"project due {t['target_date']}"] if t.get("target_date") else []
    facts.append(f"opened {max(0, int((now - created) // 86400))} days ago")
    return {"tier": tier, "ref": t["identifier"], "title": t["title"], "link": t["url"], "reason": f"{TIER_LEADS[tier].format(unblocked=t.get('unblocked_at', '')[:10])}: {', '.join(facts)}"}


def failure(phase, error):
    stderr = getattr(error, "stderr", None) or ""
    return f"{phase} failed: {(stderr.strip().splitlines() or [str(error)])[-1]}"


class Usage(Exception):
    pass


def call(handler, args, name):
    """Call a command handler, turning a wrong argument count into its usage line instead of a traceback."""
    signature = inspect.signature(handler)
    try:
        signature.bind(*args)
    except TypeError:
        words = [f"[{p.name}]" if p.default is not p.empty else "[options]" if p.kind is p.VAR_POSITIONAL else f"<{p.name}>" for p in signature.parameters.values()]
        raise Usage(f"usage: process {name} {' '.join(words)}".replace("entry_id", "entry-id").replace("<name>", "<profile>"))
    return handler(*args)


def flags(args, *names):
    """--name value pairs; bare --name (no value) is True."""
    opts, args = {}, list(args)
    while args:
        arg = args.pop(0)
        if arg not in names:
            raise SystemExit(f"process: unexpected argument {arg!r}")
        opts[arg] = args.pop(0) if args and not args[0].startswith("--") else True
    return opts


def main(argv, run=None, env=None, stdin=None, stdout=None, clock=None):
    app = Process(
        run or default_run,
        os.environ if env is None else env,
        stdin or sys.stdin,
        stdout or sys.stdout,
        clock or time.time,
    )
    if not argv:
        app.out("usage: process <run|emit|watermark|triage|execute|brief|profile|profiles|validate> ...")
        return 2
    command, args = argv[0], argv[1:]
    handler = getattr(app, "cmd_" + command.replace("-", "_"), None)
    if handler is None:
        app.out(f"process: unknown command '{command}'")
        return 2
    try:
        return call(handler, args, command) or 0
    except Usage as usage:
        app.out(str(usage))
        return 2
    except subprocess.CalledProcessError as error:
        app.out(f"process: `{shlex.join(error.cmd)}` failed: {(error.stderr or '').strip()}")
        return 1
    except SystemExit as exit:
        if isinstance(exit.code, str):
            app.out(exit.code)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
