#!/usr/bin/env python3
"""afk — deterministic orchestrator for AFK agent workers.

stdlib only. Every external command (tmux, git, gh, claude) goes through run().
"""

import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


AFK_BIN = os.path.realpath(__file__)


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
        config = {"spec": str(spec), "tracker": "local", "repo": str(repo), "session": session, "base": "main"}
        (pdir / "config.toml").write_text(to_toml(config))
        self.run(["tmux", "set-option", "-t", session, "@afk_project", project])
        self.out(f"afk project '{project}' bound to tmux session '{session}'")

    def cmd_frontier(self):
        for ticket in self.tracker().frontier():
            self.out(f"{ticket.id}  {ticket.title}")

    def cmd_start(self, ticket_id):
        project = self.current_project()
        config = self.config()
        ticket = self.tracker().get(ticket_id)
        pdir = self.project_dir(project)
        worktree = pdir / "worktrees" / ticket.id
        worker_dir = pdir / "workers" / ticket.id
        worker_dir.mkdir(parents=True, exist_ok=True)

        branch = f"afk/{project}-{ticket.id}"
        self.run(["git", "-C", config["repo"], "worktree", "add", "-b", branch, str(worktree)])

        pane = self.run(
            ["tmux", "new-window", "-d", "-t", config["session"] + ":", "-n", window_name(ticket),
             "-c", str(worktree), "-P", "-F", "#{pane_id}"]
        ).strip()
        self.run(["tmux", "split-window", "-h", "-d", "-t", pane, "-c", str(worktree)])

        settings = worker_dir / "settings.json"
        settings.write_text(json.dumps(worker_settings(config.get("base", "main")), indent=2) + "\n")
        launch = [
            "env", f"AFK_PROJECT={project}", f"AFK_TICKET={ticket.id}",
            "claude", "--permission-mode", "auto", "--settings", str(settings),
            worker_prompt(ticket),
        ]
        self.run(["tmux", "send-keys", "-t", pane, shlex.join(launch), "Enter"])
        self.out(f"started {ticket.id} in {worktree} on {branch}")

    def current_project(self):
        project = self.env.get("AFK_PROJECT") or self.run(
            ["tmux", "show-options", "-v", "@afk_project"]
        ).strip()
        if not project:
            raise SystemExit("afk: no project for this tmux session; run `afk init <spec>` first")
        return project

    def config(self):
        return tomllib.loads((self.project_dir(self.current_project()) / "config.toml").read_text())

    def tracker(self):
        return LocalTracker(Path(self.config()["spec"]))


DONE_STATUSES = {"resolved", "done", "closed"}


@dataclass
class Ticket:
    id: str
    title: str
    status: str
    blocked_by: list
    path: Path

    @property
    def done(self):
        return self.status in DONE_STATUSES

    @property
    def open(self):
        return not self.done and self.status != "claimed"


class LocalTracker:
    """Tickets as `.scratch/<slug>/issues/<NN>-<slug>.md` files, as written by /to-tickets."""

    def __init__(self, spec_dir):
        self.issues_dir = spec_dir / "issues"

    def tickets(self):
        return [parse_ticket(p) for p in sorted(self.issues_dir.glob("*.md"))]

    def get(self, ticket_id):
        for ticket in self.tickets():
            if ticket.id == ticket_id:
                return ticket
        raise SystemExit(f"afk: no ticket '{ticket_id}'")

    def frontier(self):
        tickets = self.tickets()
        done = {t.id for t in tickets if t.done}
        return [t for t in tickets if t.open and all(b in done for b in t.blocked_by)]


def parse_ticket(path):
    number = path.name.split("-", 1)[0]
    text = path.read_text()
    title = path.stem
    heading = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    if heading:
        title = re.sub(r"^\d+\s*[—–-]\s*", "", heading.group(1)).strip()
    return Ticket(
        id=number,
        title=title,
        status=field(text, "Status").lower(),
        blocked_by=[n.zfill(len(number)) for n in re.findall(r"\b(\d+)\b", field(text, "Blocked by"))],
        path=path,
    )


def window_name(ticket):
    abbrev = re.sub(r"[^a-z0-9]+", "-", ticket.title.lower()).strip("-")[:10].rstrip("-")
    return f"{ticket.id}-{abbrev}"


def worker_settings(base):
    """Per-session Claude settings: status hooks plus a deny list against irreversible damage."""
    hook = lambda event: [{"hooks": [{"type": "command", "command": f"{shlex.quote(AFK_BIN)} hook {event}"}]}]
    return {
        "permissions": {
            "deny": [
                "Bash(git push *--force*)",
                "Bash(git push * -f)",
                "Bash(git push * -f *)",
                "Bash(git push * +*)",
                f"Bash(git push * {base})",
                f"Bash(git push * HEAD:{base})",
                f"Bash(git push * *:{base})",
                "Bash(gh pr merge *)",
                "Bash(git worktree remove *)",
                "Bash(git worktree prune *)",
            ]
        },
        "hooks": {
            "SessionStart": hook("session-start"),
            "Stop": hook("stop"),
            "Notification": hook("notification"),
        },
    }


def worker_prompt(ticket):
    return (
        f"You are an AFK worker. Implement the ticket at {ticket.path}. "
        'When you finish, or need the human, end by running `afk report <done|blocked|question> "<message>"`.'
    )


def field(text, name):
    """Value of a `Name: value` line, tolerating markdown bold around the label."""
    match = re.search(rf"^\W*{re.escape(name)}\W*:\**\s*(.*)$", text, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else ""


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
