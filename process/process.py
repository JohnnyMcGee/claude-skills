#!/usr/bin/env python3
"""process — the state directory behind the daily process pipeline: items, queue, ledger, watermarks and brief.

stdlib only. Every external command (claude) goes through run().
"""

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
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
            errors.append(f"{path}: matches no allowed shape ({min(branches, key=len)[0]})")
    return errors


def validate(value, kind):
    return schema_errors(value, {"$ref": f"#/$defs/{kind}"})


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

    # -- sweeper side --

    def cmd_emit(self, profile, source, *args):
        """Store a sweeper's candidate items (JSONL on stdin, or --file) and advance its watermark."""
        opts = flags(args, "--watermark", "--file")
        text = Path(opts["--file"]).read_text() if opts.get("--file") else self.stdin.read()
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
        app.out("usage: process <run|emit|watermark|triage|brief|profiles|validate> ...")
        return 2
    command, args = argv[0], argv[1:]
    handler = getattr(app, "cmd_" + command.replace("-", "_"), None)
    if handler is None:
        app.out(f"process: unknown command '{command}'")
        return 2
    try:
        return handler(*args) or 0
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
