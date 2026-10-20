#!/usr/bin/env python3
"""process — the state directory behind the daily process pipeline: items, queue, ledger, watermarks and brief.

stdlib only. Every external command (claude) goes through run().
"""

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
from collections import Counter
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
    r"|write|delete|push|create|update|add_|reaction|assign|trash|spam"
)
READ_ONLY = re.compile(r"__(slack_)?(get|list|search|read|fetch)_|_read$")
# The silent writes the PRD allows: triage performs them on my own things. A sweeper only reads, so never holds one.
SILENT_WRITES = {
    "mcp__linear-server__mark_notification",
    "mcp__linear-server__save_issue",
    "mcp__linear-server__create_issue",
    "mcp__claude_ai_Gmail__label_thread",
    "mcp__claude_ai_Gmail__unlabel_thread",
    "mcp__claude_ai_Gmail__label_message",
    "mcp__claude_ai_Gmail__unlabel_message",
}
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
    if tool in SILENT_WRITES:
        return False
    return not READ_ONLY.search(tool) and bool(SEND_WORDS.search(tool.lower()))


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
    "mcp__claude_ai_Gmail__trash_thread",
    "mcp__claude_ai_Gmail__trash_message",
    "mcp__claude_ai_Google_Calendar__respond_to_event",
    "mcp__claude_ai_Google_Calendar__update_event",
    "mcp__claude_ai_Google_Calendar__delete_event",
    "mcp__linear-server__save_comment",
]


# The profile table each source reads its settings from, when it is not named after the source.
SOURCE_TABLE = {"gmail": "google", "calendar": "google", "meetings": "google"}


# Where a shared source's items go when no profile's match rules claim them.
UNSORTED = "unsorted"


def share_key(profile, source):
    """What makes two profiles' source the same feed, swept once for both; None for a source never shared."""
    if source == "slack":
        return ("slack", profile["slack"]["workspace"].lower())
    if SOURCE_TABLE.get(source) == "google":
        return (source, profile["google"]["account"].lower())
    return None


def claims(profile, item):
    """True when a profile's match rules (plus its repo, Linear team and Slack watchlist) claim a shared-source item."""
    rules = profile.get("match", {})
    text = " ".join([item["summary"], item["subject"], item["link"], item["context"]]).lower()
    channels = {c.lower() for c in rules.get("channels", []) + profile.get("slack", {}).get("watchlist", [])}
    keywords = rules.get("keywords", []) + ([profile["github"]["repo"]] if "github" in profile else [])
    actor = item["actor"]["name"].lower()
    team = profile.get("linear", {}).get("team")
    return (
        (item.get("facts") or {}).get("channel", "").lower() in channels
        or any(actor == s.lower() or (s.startswith("@") and actor.endswith(s.lower())) for s in rules.get("senders", []))
        or any(k.lower() in text for k in keywords)
        or bool(team and re.search(rf"\b{re.escape(team)}-\d+\b", text, re.I))
    )


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
    if profile.get("name") == UNSORTED:
        problems.append(f"the name {UNSORTED!r} is reserved for items no profile claims")
    if profile.get("name") != path.stem:
        problems.append(f"name {profile.get('name')!r} does not match the file name {path.stem!r}")
    for heading in ("## How to sweep", "## Changelog"):
        if heading not in match[2]:
            problems.append(f"the body has no {heading!r} section")
    phases = profile.get("phases", {})
    for source in profile.get("sources", []):
        if SOURCE_TABLE.get(source, source) not in profile:
            problems.append(f"source {source!r} has no [{SOURCE_TABLE.get(source, source)}] table")
        if f"sweep-{source}" not in phases:
            problems.append(f"source {source!r} has no [phases.sweep-{source}]")
    for name, phase in phases.items():
        problems += [f"phases.{name} grants {t!r}, which can send, review, RSVP or merge" for t in phase.get("allowed_tools", []) if forbidden_tool(t)]
        if name.startswith("sweep-"):
            problems += [f"phases.{name} grants {t!r}, a silent write only triage may hold" for t in phase.get("allowed_tools", []) if t in SILENT_WRITES]
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
        """Sweep every source, then triage, for each active profile (or --project), each phase headless under its
        profile's model and allowlist; then render the brief. --fixtures points sweepers at fixture data and makes
        triage a dry run."""
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
        swept = set()  # shared feeds already swept this run, for every profile that shares them
        try:
            for name in names:
                profile = profiles[name]
                sources = [s for s in profile["sources"] if share_key(profile, s) not in swept]
                swept |= {share_key(profile, s) for s in sources} - {None}
                phases = [(f"sweep-{s}", f"/sweep-{s} {name}{sweep_args}") for s in sources]
                phases.append(("triage", f"/triage {name}" + (f" --dry-run --fixtures {opts['--fixtures']}" if dry else "")))
                for phase, prompt in phases:
                    self.out(f"{name}: {phase}")
                    before = self.watermarks(name)
                    try:
                        self.headless(profile, phase, prompt, dry)
                        problem = self.phase_problem(name, phase, before)
                    except (subprocess.CalledProcessError, OSError) as error:  # OSError: no workdir, no claude
                        stderr = getattr(error, "stderr", None) or ""
                        problem = f"{phase} failed: {(stderr.strip().splitlines() or [str(error)])[-1]}"
                    if problem:
                        run["problems"].setdefault(name, []).append(problem)
                        self.write_json(self.state_root() / "run.json", run)
            self.file_unsorted()
            self.cmd_brief(*(["--delta"] if opts.get("--delta") else []), *(["--project", opts["--project"]] if opts.get("--project") else []))
        finally:
            (self.state_root() / "run.json").unlink()

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
        known send tools are denied by name too. A dry run gets no service (MCP) tools at all."""
        config = profile["phases"][phase]
        tools = [t for t in config["allowed_tools"] if not (dry and t.startswith("mcp__"))]
        inbox = self.inbox(profile["name"])
        tools.append(f"Edit(/{inbox}/**)")  # dontAsk refuses heredocs, so items and drafts travel as inbox files
        workdir = Path(profile["workdir"].replace("~", self.env["HOME"], 1) if profile["workdir"].startswith("~") else profile["workdir"])
        cmd = ["claude", "-p", prompt, "--model", config["model"], "--permission-mode", "dontAsk"]
        cmd += ["--add-dir", str(inbox), "--allowedTools", ",".join(tools), "--disallowedTools", ",".join(DENIED_TOOLS)]
        return self.run(cmd, cwd=workdir)

    def cmd_profile(self, name):
        """Print one validated profile, path first, so a phase reads it without guessing XDG_CONFIG_HOME."""
        profile = self.profile(name)
        path = self.profiles_dir() / f"{name}.md"
        lines = [str(path), f"inbox: {self.inbox(name)}"]
        for source in profile["sources"]:
            group = self.share_group(name, source)
            if len(group) > 1:
                lines.append(f"shared {source}: {', '.join(group)} (one sweep serves all; emit attributes each item)")
            if source == "slack":
                lines.append("watchlist: " + ", ".join(dict.fromkeys(c for p in group.values() for c in p["slack"].get("watchlist", []))))
        self.out("\n".join(lines) + "\n" + path.read_text())

    def cmd_profiles(self):
        """List every profile, refusing if any is invalid or grants a send tool to a sweep or triage phase."""
        for name, profile in self.profiles().items():
            self.out(f"{name}  {'active' if profile['active'] else 'inactive'}  {', '.join(profile['sources'])}")

    # -- sweeper side --

    def share_group(self, name, source):
        """The profiles one sweep of this source serves: the sweeping profile plus every active one that shares its feed."""
        if source in ("github", "linear"):
            return {name: None}
        profiles = self.profiles()
        if source not in self.profile(name)["sources"]:
            raise SystemExit(f"process: profile {name!r} does not sweep {source}")
        key = share_key(profiles[name], source)
        return {n: p for n, p in sorted(profiles.items()) if n == name or (p["active"] and source in p["sources"] and share_key(p, source) == key)}

    def cmd_emit(self, profile, source, *args):
        """Store a sweeper's candidate items (JSONL on stdin, or --file) and advance its watermark. A source shared with
        other profiles is attributed item by item to the first profile whose match rules claim it, or to unsorted."""
        opts = flags(args, "--watermark", "--file")
        path = opts.get("--file") and self.inbox_file(profile, opts["--file"])
        text = path.read_text() if path else self.stdin.read()
        group = self.share_group(profile, source)
        known = {}
        fresh, problems, answered = [], [], 0
        for n, candidate in enumerate(self.parse_jsonl(text, "item"), 1):
            candidate.pop("id", None)
            record = {"id": f"{candidate.get('source')}:{candidate.get('native_id')}@{digest(str(candidate.get('version')))}", "profile": profile, **candidate}
            errors = validate(record, "item")
            if not errors and record["source"] != source:
                errors = [f"$.source: {record['source']!r} emitted by the {source} sweeper"]
            problems += [f"item {n}: {e}" for e in errors]
            if errors:
                continue
            if len(group) > 1:
                record["profile"] = next((n for n, p in group.items() if claims(p, record)), UNSORTED)
            if record["profile"] not in known:
                known[record["profile"]] = {i["id"] for i in self.read_jsonl(self.profile_state(record["profile"]) / "items.jsonl")}
            if (record.get("facts") or {}).get("answered"):  # I already replied or reacted: not an ask any more
                answered += 1
            elif record["id"] not in known[record["profile"]]:
                known[record["profile"]].add(record["id"])
                fresh.append(record)
        if problems:
            raise SystemExit("process: nothing stored, invalid items:\n" + "\n".join(problems))
        counts = Counter(r["profile"] for r in fresh)
        for name in counts:
            self.append_jsonl(self.profile_state(name) / "items.jsonl", [r for r in fresh if r["profile"] == name])
        mark = opts.get("--watermark") or iso(self.clock())
        for name in group:
            pdir = self.profile_state(name)
            self.write_json(pdir / "watermarks.json", {**self.read_json(pdir / "watermarks.json", {}), source: mark})
        if path:
            path.unlink()  # consumed, so a later sweep can never re-send it
        split = f" ({', '.join(f'{n} {c}' for n, c in counts.items())})" if len(group) > 1 and counts else ""
        self.out(f"stored {len(fresh)} new item(s) for {profile}/{source}{split}; watermark {mark}" + (f"; skipped {answered} already answered" if answered else ""))

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
        noise = [i for i in items if i["source"] == "gmail" and (i.get("facts") or {}).get("noise") and i not in dropped]
        live = [i for i in items if i not in stale and i not in dropped and i not in noise]
        queued = {(e["kind"], e["subject"]) for e in self.queue() if e["project"] == name}
        groups = asks(live)
        # Linear's mirror or GitHub's Slack digest of a review request already queued: the GitHub item was the ask.
        echoes = [g for g in groups if g[0]["kind"] == "review_request" and g[0]["source"] != "github" and (g[0]["kind"], canonical(g[0]["subject"])) in queued]
        dropped += [i for g in echoes for i in g]
        entries = [entry(name, profile, group) for group in groups if group not in echoes]
        ticketed = {e["subject"]: e.get("note", "") for e in self.queue() if e["project"] == name and e["action"]["type"] == "linear_create_issue" and e["status"] == "done"}
        for e in entries:
            if e["action"]["type"] == "linear_create_issue" and e["subject"] in ticketed:  # a changed ask I already made a ticket for
                e.update(action={"type": "none", "params": {}}, autonomy="silent", status="info", summary=f"{e['summary']} — already ticketed: {ticketed[e['subject']]}")
        if stale:
            entries.append(mark_read(name, stale, f"Cleared {len(stale)} Linear notification(s) older than 7 days (first run)"))
        if noise:
            entries.append(archive(name, profile, noise))
        read = [i for i in live + dropped if notification(i)]  # my own get marked read too
        if read:
            entries.append(mark_read(name, read, f"Marked {len(read)} processed Linear notification(s) read"))
        plan = {"profile": name, "first_run": first_run, "dropped": [i["id"] for i in dropped], "entries": entries}
        self.write_json(pdir / "plan.json", plan)
        by_id = {i["id"]: i for i in items}
        for e in entries:
            todo = "draft" if needs_draft(e) else "do, then report" if e["action"]["type"] in TRIAGE_ACTIONS else e["status"]
            self.out(f"{e['id']}  [{todo}]  {e['action']['type']} {json.dumps(e['action']['params'])}  {e['summary']}  {e['link']}")
            if todo == "draft" or e["action"]["type"] == "linear_create_issue":  # a ticket is written from its ask
                for item_id in e["item_ids"]:
                    self.out(f"    {item_id}: {by_id[item_id]['summary']}\n" + indent(by_id[item_id]["context"], "      | "))
        self.out(f"{len(entries)} entr(ies) planned in {pdir / 'plan.json'}; {len(dropped)} item(s) dropped (my own, or echoes of a review already queued)")

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
        if e["action"]["type"] not in TRIAGE_ACTIONS:
            raise SystemExit(f"process: {entry_id} is {e['action']['type']}, not a silent action triage performs")
        e["status"] = outcome
        if note:
            e["note"] = note
        self.write_json(self.profile_state(name) / "plan.json", plan)

    def file_unsorted(self):
        """Shared-source items no profile claimed become FYI entries of their own: no project's rules apply to them."""
        items = self.unhandled(UNSORTED)
        bare = {"autonomy": {"silent": []}}
        entries = [{**entry(UNSORTED, bare, g), "autonomy": "silent", "action": {"type": "none", "params": {}}, "status": "info"} for g in asks(items)]
        self.record(UNSORTED, self.stamp(entries), [i["id"] for i in items])

    def stamp(self, entries):
        """Give planned entries the run that made them and their creation time."""
        run = self.read_json(self.state_root() / "run.json", {}).get("id") or iso(self.clock())
        return [{**e, "run": run, "created": iso(self.clock())} for e in entries]

    def record(self, name, entries, handled):
        """Append stamped entries to the queue (superseding older versions of the same ask) and their items to the ledger."""
        keys = {(e["kind"], e["subject"]) for e in entries}
        superseded = [
            {**old, "status": "superseded"}
            for old in self.queue()
            if old["project"] == name and old["status"] in ("pending", "info") and (old["kind"], old["subject"]) in keys
        ]
        self.append_jsonl(self.state_root() / "queue.jsonl", superseded + entries)
        self.append_jsonl(self.profile_state(name) / "ledger.jsonl", [{"item_id": i, "at": iso(self.clock())} for i in handled])
        return superseded

    def triage_commit(self, name):
        """Validate the finished plan, then append it to the queue and ledger. All or nothing."""
        pdir = self.profile_state(name)
        plan = self.read_json(pdir / "plan.json", None)
        if plan is None:
            raise SystemExit(f"process: no triage plan for {name}; run `process triage plan {name}` first")
        entries = self.stamp(plan["entries"])
        items = {i["id"]: i for i in self.read_jsonl(pdir / "items.jsonl")}
        problems = []
        for e in entries:
            problems += [f"{e['id']}: {p}" for p in validate(e, "queue_entry")]
            if needs_draft(e) and not e["action"].get("draft"):
                problems.append(f"{e['id']}: no draft (process triage draft {name} {e['id']})")
            if e["action"]["type"] in TRIAGE_ACTIONS and e["status"] == "approved":
                problems.append(f"{e['id']}: no outcome (process triage silent {name} {e['id']} done|failed|skipped)")
            declined = e["action"]["type"] == "rsvp" and e["action"]["params"].get("response") == "declined"
            if declined and protected_meeting(self.profile(name), [items[i] for i in e["item_ids"] if i in items]):
                problems.append(f"{e['id']}: never decline a meeting with external guests or my manager")
        if problems:
            raise SystemExit("process: nothing committed:\n" + "\n".join(f"  {p}" for p in problems))
        handled = list(dict.fromkeys([i for e in entries for i in e["item_ids"]] + plan["dropped"]))
        superseded = self.record(name, entries, handled)
        if plan["first_run"]:
            self.write_json(pdir / "state.json", {**self.read_json(pdir / "state.json", {}), "bankrupt_at": iso(self.clock())})
        (pdir / "plan.json").unlink()
        self.out(f"committed {len(entries)} entr(ies) for {name}; superseded {len(superseded)}; {len(handled)} item(s) handled")

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
        if opts.get("--delta"):
            shown = lambda e: e["run"] == latest
        else:
            shown = lambda e: e["status"] == "pending" or e["created"] >= midnight
        mode = "delta" if opts.get("--delta") else "full"
        lines = [f"# Brief — {now:%a %Y-%m-%d %H:%M} · {mode}", ""]
        for name in names:
            entries = [e for e in queue if e["project"] == name and e["status"] != "superseded" and shown(e)]
            lines += [f"## {name}", ""] + [f"> ⚠ {p}" for p in run.get("problems", {}).get(name, [])]
            lines += render_project(entries) or ["Nothing new.", ""]
        unsorted = [e for e in queue if e["project"] == UNSORTED and e["status"] != "superseded" and shown(e)]
        if unsorted and not opts.get("--project"):
            lines += ["## Unsorted", "", "Items from a shared source that no profile's match rules claimed.", ""] + render_project(unsorted)
        brief = "\n".join(lines).rstrip() + "\n"
        self.state_root().mkdir(parents=True, exist_ok=True)
        (self.state_root() / "brief.md").write_text(brief)
        archive = self.state_root() / "briefs" / f"{now:%Y-%m-%d}.md"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with archive.open("a") as f:
            f.write(("\n---\n\n" if archive.stat().st_size else "") + brief)
        self.out(str(self.state_root() / "brief.md"))

    def queue(self):
        """The queue's current state: the last line for each entry id, in first-seen order."""
        return list({e["id"]: e for e in self.read_jsonl(self.state_root() / "queue.jsonl")}.values())


BANKRUPTCY_AGE = 7 * 24 * 3600
PULL = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")


def asks(items):
    """Group items into asks: the same kind about the same subject, whichever source saw it. A github item leads its
    entry."""
    groups = {}
    for i in sorted(items, key=lambda i: (i["source"] != "github", i["source"], i["timestamp"])):
        groups.setdefault((i["kind"], canonical(i["subject"])), []).append(i)
    return list(groups.values())


# Silent actions triage performs itself under its allowlist, then reports. They carry no text for me to approve.
TRIAGE_ACTIONS = ("linear_mark_read", "linear_create_issue", "gmail_archive")


def needs_draft(e):
    """Every action that carries text needs a draft, even one promoted to silent: the executor sends that text."""
    return e["action"]["type"] not in ("none",) + TRIAGE_ACTIONS


def canonical(url):
    """Same thing, same key: lower-case host, no trailing slash, no tracking parameters. The rest of the query stays,
    sorted (a calendar event or Slack thread is named by it), and so do fragments (they name a comment)."""
    match = re.match(r"(https?://)([^/]+)([^?#]*)(\?[^#]*)?(#.*)?$", url)
    if not match:
        return url
    params = sorted(p for p in (match[4] or "?")[1:].split("&") if p and not p.startswith("utm_"))
    return match[1] + match[2].lower() + match[3].rstrip("/") + ("?" + "&".join(params) if params else "") + (match[5] or "")


def notification(i):
    return (i.get("facts") or {}).get("notification_id")


def indent(text, prefix):
    return "\n".join(prefix + line for line in (text or "").splitlines()[:20])


def mark_read(name, items, summary):
    return housekeeping(name, items, summary, {"type": "linear_mark_read", "params": {"notification_ids": [notification(i) for i in items]}})


def archive(name, profile, items):
    """One silent entry that labels and archives every noise email of the run."""
    label = profile["google"].get("noise_label", "Noise")
    summary = f"Labeled {label} and archived {len(items)} email(s): " + "; ".join(i["summary"] for i in items)
    return housekeeping(name, items, summary, {"type": "gmail_archive", "params": {"thread_ids": [i["native_id"] for i in items], "label": label}})


def housekeeping(name, items, summary, action):
    """A batch silent action triage performs itself over many items: it waits on triage's report."""
    ids = [i["id"] for i in items]
    return {
        "id": "q-" + digest(name + action["type"] + "".join(ids)),
        "project": name,
        "item_ids": ids,
        "sources": sorted({i["source"] for i in items}),
        "kind": "housekeeping",
        "subject": "",
        "summary": summary,
        "link": "",
        "urgency": "low",
        "autonomy": "silent",
        "action": action,
        "status": "approved",
    }


URGENCY = ["urgent", "normal", "low"]


def render_project(entries):
    """One project's brief sections: Needs approval, Done silently, FYI (collapsed). Empty sections are left out."""
    entries = sorted(entries, key=lambda e: (URGENCY.index(e["urgency"]), e["created"]))
    needs = [e for e in entries if e["status"] == "pending"]
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
    if silent:
        lines += [f"### Done silently ({len(silent)})", ""]
        words = {"approved": "approved, waits on the executor", "done": "done", "failed": "FAILED", "skipped": "skipped"}
        lines += [f"- {e['summary']} — {words[e['status']]}{': ' + e['note'] if e.get('note') else ''}" for e in silent] + [""]
    if fyi:
        lines += [f"### FYI ({len(fyi)})", "", "<details>", f"<summary>{len(fyi)} item(s)</summary>", ""]
        lines += [f"- {'**urgent** · ' if e['urgency'] == 'urgent' else ''}{e['summary']} · {e['link']}" for e in fyi]
        lines += ["", "</details>", ""]
    return lines


def protected_meeting(profile, items):
    """A meeting with external guests or my manager: declining it is a never action."""
    manager = profile.get("google", {}).get("manager", "").lower()
    return any(
        (i.get("facts") or {}).get("external_guests") or (i.get("facts") or {}).get("with_manager") or (manager and i["actor"]["name"].lower() == manager)
        for i in items
    )


def entry(name, profile, group):
    """Apply the needs-me rules to one deduped ask: what to do about it, who decides, how urgent."""
    lead = group[0]
    facts = {k: v for i in group for k, v in (i.get("facts") or {}).items()}
    pr = PULL.search(lead["subject"]) or PULL.search(lead["link"])
    ref = {"repo": pr[1], "number": int(pr[2])} if pr else None
    action, summary, urgency = {"type": "none", "params": {}}, lead["summary"], "low"
    kind = lead["kind"]
    if kind == "review_request" and ref:
        action, urgency = {"type": "gh_review", "params": {**ref, "event": "COMMENT"}}, "normal"
    elif kind == "pr_status" and facts.get("mine") and ref:
        broken = facts.get("mergeable") == "conflicting" or facts.get("checks") == "failing" or facts.get("review") == "changes_requested"
        ready = facts.get("review") == "approved" and facts.get("checks") in ("passing", "none") and facts.get("mergeable") == "mergeable"
        if broken:
            action, urgency = {"type": "gh_fix_pr", "params": ref}, "normal"
        elif ready:
            summary += " — approved and green; merging is yours"  # merge is a never action: say so, never queue it
    elif kind == "pr_comment" and ref:
        action, urgency = {"type": "gh_fix_pr" if lead["actor"]["role"] == "bot" else "gh_thread_reply", "params": ref}, "normal"
    elif kind == "invite":
        conflicts = facts.get("conflicts") or []
        response = "tentative" if conflicts and protected_meeting(profile, group) else "declined" if conflicts else "accepted"
        action, urgency = {"type": "rsvp", "params": {"event_id": lead["native_id"], "response": response}}, "normal"
        summary += f" — conflicts with {', '.join(conflicts)}" if conflicts else ""
    elif kind == "action_item" and "linear" in profile:  # meeting commitments land in Triage so next-work sees them
        params = {"team": profile["linear"]["team"], "state": "Triage", "assignee": "me", "link": lead["link"]}
        action, urgency = {"type": "linear_create_issue", "params": params}, "normal"
    elif lead["source"] == "slack" and facts.get("ask") == "task" and "linear" in profile:  # an immediate ask: a ticket for me, linking back
        action, urgency = {"type": "linear_create_issue", "params": {"team": profile["linear"]["team"], "assignee": "me", "link": lead["link"]}}, "normal"
    elif lead["source"] == "gmail" and facts.get("ask"):
        action, urgency = {"type": "email_reply", "params": {"thread_id": lead["native_id"]}}, "normal"
    elif lead["source"] == "slack" and facts.get("ask"):
        channel, _, thread = lead["native_id"].partition("/")
        action, urgency = {"type": "slack_reply", "params": {"channel": channel, "thread_ts": thread}}, "normal"
    elif kind in ("mention", "ticket_change", "dm", "thread_question"):
        urgency = "normal"
    rules = profile.get("urgency", {})
    if {i["actor"]["name"] for i in group} & set(rules.get("people", [])) or {l for i in group for l in i.get("labels", [])} & set(rules.get("labels", [])):
        urgency = "urgent"
    autonomy, status = "draft", "pending"
    if action["type"] == "none":
        autonomy, status = "silent", "info"
    elif action["type"] in profile["autonomy"]["silent"] or action["type"] in TRIAGE_ACTIONS:
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
        app.out("usage: process <run|emit|watermark|triage|brief|profile|profiles|validate> ...")
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
