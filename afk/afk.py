#!/usr/bin/env python3
"""afk — deterministic orchestrator for AFK agent workers.

stdlib only. Every external command (tmux, git, gh, claude) goes through run().
"""

import os
import subprocess
import sys
from pathlib import Path


def default_run(cmd, input=None):
    return subprocess.run(cmd, input=input, capture_output=True, text=True, check=True).stdout


class Afk:
    def __init__(self, run, env, stdin, stdout):
        self.run = run
        self.env = env
        self.stdin = stdin
        self.stdout = stdout

    def out(self, line):
        print(line, file=self.stdout)

    def state_root(self):
        base = self.env.get("XDG_STATE_HOME") or os.path.join(self.env["HOME"], ".local", "state")
        return Path(base) / "afk"

    def project_dir(self, project):
        return self.state_root() / project

    def cmd_init(self, spec):
        spec = Path(spec).resolve()
        if spec.is_file():
            spec = spec.parent
        project = spec.name
        repo = spec.parent.parent  # <repo>/.scratch/<slug>
        session = self.run(["tmux", "display-message", "-p", "#{session_name}"]).strip()
        pdir = self.project_dir(project)
        pdir.mkdir(parents=True, exist_ok=True)
        config = {"spec": str(spec), "tracker": "local", "repo": str(repo), "session": session}
        (pdir / "config.toml").write_text(to_toml(config))
        self.run(["tmux", "set-option", "-t", session, "@afk_project", project])
        self.out(f"afk project '{project}' bound to tmux session '{session}'")


def to_toml(flat):
    return "".join(f"{k} = {toml_string(v)}\n" for k, v in flat.items())


def toml_string(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def main(argv, run=None, env=None, stdin=None, stdout=None):
    app = Afk(
        run or default_run,
        os.environ if env is None else env,
        stdin or sys.stdin,
        stdout or sys.stdout,
    )
    if not argv:
        app.out("usage: afk <init|frontier|start|report|status|hook> ...")
        return 2
    command, args = argv[0], argv[1:]
    handler = getattr(app, "cmd_" + command, None)
    if handler is None:
        app.out(f"afk: unknown command '{command}'")
        return 2
    return handler(*args) or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
