#!/usr/bin/env python3
"""afk — deterministic orchestrator for AFK agent workers.

stdlib only. Every external command (tmux, git, gh, claude) goes through run().
"""

import bisect
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from functools import reduce
from pathlib import Path


AFK_BIN = os.path.realpath(__file__)
SKILL_DIR = Path(AFK_BIN).parent


def default_run(cmd, input=None, cwd=None):
    return subprocess.run(cmd, input=input, cwd=cwd, capture_output=True, text=True, check=True).stdout


class Afk:
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
        return Path(base) / "afk"

    def project_dir(self, project):
        return self.state_root() / project

    def cmd_init(self, spec):
        issue = GITHUB_ISSUE.match(spec)
        if issue:
            tracker, project = "github", f"{issue['repo']}-{issue['number']}"
            repo = self.run(["git", "rev-parse", "--show-toplevel"]).strip()  # run from inside the clone
        else:
            spec = Path(spec).resolve()
            if spec.is_file():
                spec = spec.parent
            tracker, project = "local", spec.name
            repo = spec.parent.parent  # <repo>/.scratch/<slug>
        session = self.run(["tmux", "display-message", "-p", "#{session_name}"]).strip()
        pdir = self.project_dir(project)
        pdir.mkdir(parents=True, exist_ok=True)
        config = {"spec": str(spec), "tracker": tracker, "repo": str(repo), "session": session}
        (pdir / "config.toml").write_text(to_toml(config))
        self.run(["tmux", "set-option", "-t", session, "@afk_project", project])
        self.out(f"afk project '{project}' bound to tmux session '{session}'")

    def cmd_frontier(self):
        for ticket in self.tracker().frontier():
            self.out(f"{ticket.id}  {ticket.title}" + (f"  ({ticket.repo})" if ticket.repo else ""))

    def cmd_start(self, ticket_id, *options):
        project = self.current_project()
        config = self.config()
        if options and (len(options) != 2 or options[0] != "--type"):
            raise SystemExit("afk: usage: afk start <ticket> [--type <type>]")
        tracker = self.tracker()
        ticket = tracker.get(ticket_id)
        type_name = options[1] if options else ticket.type
        pdir = self.project_dir(project)
        worktree = pdir / "worktrees" / ticket.id
        worker_dir = pdir / "workers" / ticket.id
        # Checked before openness: a ticket this orchestrator started is now claimed. claim_worker settles races.
        if worker_dir.exists():
            raise SystemExit(f"afk: ticket {ticket.id} is already started; see `afk status`")
        if not ticket.open:
            raise SystemExit(f"afk: ticket {ticket.id} is not open (it is {ticket.status})")
        repo_config = self.repo_config()
        type_name = type_name or repo_config.get("default_type")
        task_types = repo_config.get("task_types", {})
        if type_name and type_name not in task_types:
            raise SystemExit(f"afk: unknown task type '{type_name}'; docs/agents/afk.md defines: {', '.join(task_types) or 'none'}")
        task_type = task_types.get(type_name, {})
        if task_type.get("agent", "claude") not in AGENTS:
            raise SystemExit(f"afk: task type '{type_name}' uses agent '{task_type['agent']}'; supported: {', '.join(AGENTS)}")
        base = repo_config.get("base", "main")
        slot = self.claim_worker(worker_dir)
        slot_env = [f"AFK_SLOT={slot}", f"AFK_PORT_BASE={repo_config.get('port_base', 4000) + 100 * slot}"]
        branch = f"afk/{project}-{ticket.id}"
        undo = [lambda: shutil.rmtree(worker_dir, ignore_errors=True)]
        try:
            tracker.claim(ticket)
            undo.append(lambda: tracker.release(ticket))
            self.run(["git", "-C", config["repo"], "worktree", "add", "-b", branch, str(worktree), base])
            undo.append(lambda: self.run(["git", "-C", config["repo"], "branch", "-D", branch]))
            undo.append(lambda: self.run(["git", "-C", config["repo"], "worktree", "remove", "--force", str(worktree)]))
            for name in repo_config.get("copy", []):
                target = worktree / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(config["repo"]) / name, target)
            for command in repo_config.get("bootstrap", []):
                self.run(["env", *slot_env, "sh", "-c", command], cwd=worktree)

            pane = self.run(
                ["tmux", "new-window", "-d", "-t", config["session"] + ":", "-n", window_name(ticket),
                 "-c", str(worktree), "-P", "-F", "#{pane_id}"]
            ).strip()
            undo.append(lambda: self.run(["tmux", "kill-window", "-t", pane]))
            self.run(["tmux", "split-window", "-h", "-d", "-t", pane, "-c", str(worktree)])

            settings = worker_dir / "settings.json"
            settings.write_text(json.dumps(worker_settings(base), indent=2) + "\n")
            launch = [
                "env", f"AFK_PROJECT={project}", f"AFK_TICKET={ticket.id}", *slot_env,
                "claude", "--permission-mode", "auto", "--settings", str(settings),
                *[arg for key in ("model", "effort") if key in task_type for arg in (f"--{key}", task_type[key])],
                self.implement_prompt(ticket, task_type),
            ]
            write_json(
                worker_dir / "status.json",
                {"ticket": ticket.id, "phase": "implement", "state": "working", "message": "",
                 "pane": pane, "window": window_name(ticket), "slot": slot, "type": type_name, "phase_started_at": self.clock()},
            )
            self.run(["tmux", "send-keys", "-t", pane, shlex.join(launch), "Enter"])
        except BaseException:
            # Roll back, even on Ctrl-C, so a plain retry of `afk start` works.
            for step in reversed(undo):
                try:
                    step()
                except Exception:
                    pass
            raise
        self.out(f"started {ticket.id} in {worktree} on {branch}")

    def claim_worker(self, worker_dir):
        """Create the worker's dir and claim the lowest slot no active worker holds; removing the dir frees it."""
        workers = worker_dir.parent
        workers.mkdir(parents=True, exist_ok=True)
        with locked(workers):
            if worker_dir.exists():
                raise SystemExit(f"afk: ticket {worker_dir.name} is already started; see `afk status`")
            max_workers = {**LIMITS, **self.config().get("limits", {})}["max_workers"]
            # Workers in review are parked on the human, so they don't count; one still starting has no status yet.
            active = [d for d in workers.glob("*/slot") if not is_reviewing(d.parent / "status.json")]
            if len(active) >= max_workers:
                raise SystemExit(f"afk: {len(active)} workers already active (max_workers = {max_workers}); see `afk status`")
            worker_dir.mkdir()
            taken = {int(p.read_text()) for p in workers.glob("*/slot")}
            slot = next(n for n in range(1, len(taken) + 2) if n not in taken)
            (worker_dir / "slot").write_text(f"{slot}\n")
        return slot

    def cmd_report(self, state, message=""):
        if state not in REPORT_STATES:
            self.out(f"afk: report state must be one of {', '.join(REPORT_STATES)}")
            return 2
        self.update_status(lambda status: dict(state=state, message=message, idle=False, reports=status.get("reports", 0) + 1))

    def cmd_hook(self, event):
        payload = json.loads(self.stdin.read() or "{}")

        def changes(status):
            changes = dict(
                session_id=payload.get("session_id"),
                transcript_path=payload.get("transcript_path"),
                last_event=payload.get("hook_event_name", event),
            )
            if event == "stop":
                changes["idle"] = True
                if status["state"] == "working":
                    # Stopped without reporting since its last phase prompt: a silent stall.
                    changes.update(state="attention", message="stopped without reporting", reports=status.get("reports", 0) + 1)
            return changes

        self.update_status(changes)

    def cmd_watch(self, interval="2"):
        """Loop around tick for the orchestrator window's right pane. Needs no LLM, so waiting is free."""
        try:
            while True:
                self.stdout.write("\033[H\033[2J")  # redraw the dashboard in place
                try:
                    self.cmd_tick()
                except subprocess.CalledProcessError as error:
                    # The failed worker's status wasn't persisted, so the next tick retries it.
                    self.out(f"afk: `{shlex.join(error.cmd)}` failed: {(error.stderr or '').strip()}")
                self.stdout.flush()
                time.sleep(float(interval))
        except KeyboardInterrupt:
            return 0

    def cmd_tick(self):
        # One tick at a time, so an overlapping tick (say `afk tick` beside `afk watch`) can't act on a stale snapshot.
        self.workers_dir().mkdir(parents=True, exist_ok=True)
        with locked(self.workers_dir()):
            return self.tick_workers()

    def tick_workers(self):
        now = self.clock()
        limits = self.config().get("limits", {})
        shown, failures = [], []
        paths = sorted(self.workers_dir().glob("*/status.json"))
        # Only ticks change phases and ticks don't overlap, so a snapshot of them stays true for the whole tick.
        verify = VerifyQueue(self.repo_config().get("verify_concurrency"), [read_json(p) for p in paths])
        # Each worker is read, acted on and persisted under its lock, so a report or hook can't land in between,
        # a failure can't make a later tick repeat another worker's effects, and one failing worker fails alone.
        for path in paths:
            with locked(path.parent):
                before = read_json(path)
                given = dict(may_verify=verify.may_start(before), limits=limits,
                             last_activity=last_write(before.get("transcript_path")))
                status, effects = tick(path.parent.name, before, now, **given)
                if any(kind == "prompt" for kind, *_ in effects) and not self.agent_running(before["pane"]):
                    # Pasting into a bare shell would run the prompt's markdown as commands.
                    status, effects = tick(path.parent.name, before, now, agent_running=False, **given)
                try:
                    for effect in effects:
                        self.perform(*effect)
                except subprocess.CalledProcessError as error:
                    failures.append(error)
                    shown.append(before)
                    continue
                if status != before:
                    write_json(path, status)
                verify.update(before, status)
                shown.append(status)
        for line in dashboard(shown, now):
            self.out(line)
        if failures:
            raise failures[0]

    def perform(self, kind, *args):
        if kind == "prompt":
            ticket_id, pane, phase = args
            self.send_prompt(ticket_id, pane, self.phase_prompt(ticket_id, phase))
        elif kind == "window":
            # Cosmetic, so best-effort: a failure here must not make the next tick resend a prompt.
            pane, name, state = args
            self.best_effort(["tmux", "rename-window", "-t", pane, name + WINDOW_MARKS.get(state, "")])
            self.best_effort(["tmux", "set-option", "-w", "-t", pane, "@afk_state", state])
        elif kind == "interrupt":
            # Best-effort: the worker is marked stuck and the human notified even if its pane is gone.
            self.best_effort(["tmux", "send-keys", "-t", args[0], "Escape"])
        elif kind == "notify":
            self.notify(*args)

    def notify(self, message):
        """The only way afk gets the human's attention. Backends are best-effort: none may stop the watcher."""
        self.stdout.write("\a")  # the watcher's pane rings, flagging the orchestrator window
        self.best_effort(["tmux", "display-message", "-t", self.config()["session"] + ":", f"afk: {message}"])
        self.best_effort(["notify-send", f"afk: {self.current_project()}", message])

    def best_effort(self, cmd):
        try:
            self.run(cmd)
        except (OSError, subprocess.CalledProcessError):
            pass

    def phase_prompt(self, ticket_id, phase):
        ticket = self.tracker().get(ticket_id)
        return render(
            SKILL_DIR / f"phase-{phase}.md",
            ticket=ticket.id,
            ticket_path=ticket.path,
            base=self.repo_config().get("base", "main"),
        )

    def implement_prompt(self, ticket, task_type):
        """The first phase prompt, invoking the task type's skill and followed by its prompt template."""
        prompt = self.phase_prompt(ticket.id, "implement")
        if "prompt" in task_type:
            template = Path(self.config()["repo"]) / task_type["prompt"]
            prompt += "\n" + render(template, ticket=ticket.id, ticket_path=ticket.path)
        if "skill" in task_type:
            prompt = f"{task_type['skill']} {prompt}"
        return prompt

    def agent_running(self, pane):
        try:
            return self.run(["tmux", "display-message", "-p", "-t", pane, "#{pane_current_command}"]).strip() == "claude"
        except subprocess.CalledProcessError:
            return False

    def send_prompt(self, ticket_id, pane, text):
        """Paste as one bracketed paste, so the prompt's newlines don't submit it early, then submit."""
        buffer = f"afk-{ticket_id}"
        self.run(["tmux", "load-buffer", "-b", buffer, "-"], input=text)
        self.run(["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", pane])
        self.run(["tmux", "send-keys", "-t", pane, "Enter"])

    def workers_dir(self):
        return self.project_dir(self.current_project()) / "workers"

    def cmd_status(self):
        rows = [read_json(p) for p in sorted((self.project_dir(self.current_project()) / "workers").glob("*/status.json"))]
        self.out(f"{'TICKET':<8}{'PHASE':<11}{'STATE':<10}MESSAGE")
        for s in rows:
            self.out(f"{s['ticket']:<8}{s['phase']:<11}{s['state']:<10}{s.get('message', '')}".rstrip())

    def worker_status_path(self):
        ticket = self.env.get("AFK_TICKET")
        if not ticket:
            raise SystemExit("afk: AFK_TICKET is not set; run this from an afk worker")
        return self.project_dir(self.current_project()) / "workers" / ticket / "status.json"

    def update_status(self, changes):
        """Merge changes(current status) into this worker's status, under its lock."""
        path = self.worker_status_path()
        with locked(path.parent):
            status = read_json(path)
            write_json(path, {**status, **changes(status)})

    def current_project(self):
        project = self.env.get("AFK_PROJECT") or self.run(
            ["tmux", "show-options", "-v", "@afk_project"]
        ).strip()
        if not project:
            raise SystemExit("afk: no project for this tmux session; run `afk init <spec>` first")
        return project

    def config(self):
        return tomllib.loads((self.project_dir(self.current_project()) / "config.toml").read_text())

    def repo_config(self):
        """The repo's AFK config: docs/agents/afk.md, then gitignored afk.local.md, then the project's [overrides]."""
        config = self.config()
        docs = Path(config["repo"]) / "docs" / "agents"
        layers = [frontmatter(p.read_text()) for p in (docs / "afk.md", docs / "afk.local.md") if p.is_file()]
        return reduce(deep_merge, [*layers, config.get("overrides", {})], {})

    def tracker(self):
        config = self.config()
        if config["tracker"] == "github":
            return GithubTracker(self.run, config["spec"])
        return LocalTracker(Path(config["spec"]))


REPORT_STATES = ("done", "blocked", "question")

AGENTS = ("claude",)

# Runaway limits; a project's config.toml [limits] table overrides any of them.
LIMITS = {"max_workers": 3, "phase_minutes": 90, "idle_minutes": 20, "fix_loops": 3}

PHASES = ("implement", "verify", "prepr", "pr", "review")

# Appended to a worker's window name so its state shows even without afk's tmux status format.
WINDOW_MARKS = {"question": "?", "blocked": "!", "attention": "!", "stuck": "!", "review": "✓"}


def tick(ticket, status, now, agent_running=True, may_verify=True, limits=None, last_activity=None):
    """One watcher step for one worker, pure: its status and the time in; its updated status and effects out.

    agent_running=False says the worker's pane no longer runs its agent, so it can't be sent a prompt.
    may_verify=False says the repo's verify slots are full, so a worker due to verify must queue.
    """
    limits = {**LIMITS, **(limits or {})}
    status, effects = advance(ticket, status, now, agent_running, may_verify, limits)
    if not effects:
        status, effects = enforce(ticket, status, now, limits, last_activity)
    if status["state"] != status.get("shown"):
        status = {**status, "shown": status["state"]}
        effects.append(("window", status["pane"], status["window"], status["state"]))
    return status, effects


def advance(ticket, status, now, agent_running, may_verify, limits):
    state, phase, message = status["state"], status["phase"], status.get("message", "")
    if state == "queued" and may_verify:
        state = "done"  # its turn to verify: advance as if it had just finished
    if state == "done" and status.get("idle") and not agent_running:
        state, message = "attention", "claude is no longer running in its pane"
        status = {**status, "state": state, "message": message, "reports": status.get("reports", 0) + 1}
    # Only act on done once the worker has also stopped: never type into a busy session.
    if state == "done" and status.get("idle"):
        if phase == "review":
            loops = status.get("fix_loops", 0) + 1
            if loops > limits["fix_loops"]:
                # The worker has already stopped, so there is nothing to interrupt. Counting restarts, so the
                # human, having looked, gets another fix_loops rounds.
                reason = f"over {limits['fix_loops']} PR fix loops"
                return {**status, "state": "stuck", "message": reason, "fix_loops": 0}, [("notify", f"{ticket} stuck: {reason}")]
            status = {**status, "state": "review", "fix_loops": loops}
            return status, [("notify", f"{ticket} addressed review feedback: {message}")]
        phase = PHASES[PHASES.index(phase) + 1]
        if phase == "verify" and not may_verify:
            # Stays in its finished phase, so if it needs attention while queued, done still leads to verify.
            return {**status, "state": "queued", "queued_at": now}, []
        status = {**status, "phase": phase, "state": "working", "message": "", "idle": False, "phase_started_at": now}
        effects = [("prompt", ticket, status["pane"], phase)]
        if phase == "review":
            pr = re.search(r"https?://\S+", message)
            status = {**status, "state": "review", "pr": pr.group(0) if pr else message}
            effects.append(("notify", f"{ticket} PR ready for review: {status['pr']}"))
        return status, effects
    if state in ("question", "blocked", "attention") and status.get("reports") != status.get("notified"):
        label = "needs attention" if state == "attention" else state
        return {**status, "notified": status.get("reports")}, [("notify", f"{ticket} {label}: {message}")]
    return status, []


def enforce(ticket, status, now, limits, last_activity):
    """Trip a runaway limit: interrupt the worker, mark it stuck and notify. Never kill it.

    last_activity is when the worker's session last wrote anything; a phase prompt counts as activity.
    """
    # A worker that reported done but hasn't stopped is still running. Review has no phase clock: it waits on the human.
    running = status["state"] == "working" or (status["state"] == "done" and not status.get("idle"))
    # A limit trips once per phase: after the human has looked, a recovered worker isn't interrupted again.
    tripped = status.get("tripped") == status["phase_started_at"]
    if not running or tripped or status["phase"] == "review":
        return status, []
    reason = None
    if now - status["phase_started_at"] > limits["phase_minutes"] * 60:
        reason = f"over {limits['phase_minutes']}m in {status['phase']}"
    elif now - max(last_activity or 0, status["phase_started_at"]) > limits["idle_minutes"] * 60:
        reason = f"idle for over {limits['idle_minutes']}m"
    if reason is None:
        return status, []
    status = {**status, "state": "stuck", "message": reason, "tripped": status["phase_started_at"]}
    return status, [("interrupt", status["pane"]), ("notify", f"{ticket} stuck: {reason}")]


class VerifyQueue:
    """Holds workers back from verify, first come first served, while the repo's verify_concurrency slots are full."""

    def __init__(self, limit, statuses):
        self.limit = limit
        self.verifying = sum(s["phase"] == "verify" for s in statuses)
        self.queue = sorted((s["queued_at"], s["ticket"]) for s in statuses if s["state"] == "queued")

    def may_start(self, status):
        if self.limit is None or self.verifying >= self.limit:
            return self.limit is None
        if status["state"] == "queued":
            return self.queue[0][1] == status["ticket"]
        return not self.queue

    def update(self, before, after):
        self.verifying += (after["phase"] == "verify") - (before["phase"] == "verify")
        if before["state"] == "queued":
            self.queue.remove((before["queued_at"], before["ticket"]))
        if after["state"] == "queued":
            bisect.insort(self.queue, (after["queued_at"], after["ticket"]))


DONE_STATUSES = {"resolved", "done", "closed"}

GITHUB_ISSUE = re.compile(r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/?$")


@dataclass
class Ticket:
    id: str
    title: str
    status: str
    blocked_by: list
    path: Path
    type: str
    repo: str | None = None  # owner/name; None leaves it for Gate 1 to resolve

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

    def claim(self, ticket):
        """A local spec belongs to this orchestrator alone, so there is no one to claim it from."""

    def release(self, ticket):
        pass

    def frontier(self):
        tickets = self.tickets()
        done = {t.id for t in tickets if t.done}
        return [t for t in tickets if t.open and all(b in done for b in t.blocked_by)]


class GithubTracker:
    """Tickets as the sub-issues of a GitHub spec issue, read through `gh`."""

    def __init__(self, run, spec_url):
        self.run = run
        issue = GITHUB_ISSUE.match(spec_url)
        self.repo = f"{issue['owner']}/{issue['repo']}"
        self.spec_number = issue["number"]

    def api(self, path):
        pages = json.loads(self.run(["gh", "api", "--paginate", "--slurp", f"repos/{self.repo}/{path}"]))
        return [item for page in pages for item in page]

    def tickets(self):
        return [self.ticket(issue) for issue in self.api(f"issues/{self.spec_number}/sub_issues")]

    def ticket(self, issue):
        if issue["state"] == "closed":
            status = "closed"
        elif issue["assignees"]:
            status = "claimed"  # by another orchestrator or a human
        else:
            status = "open"
        body = issue.get("body") or ""
        blocked_by = re.findall(r"(?<![\w/])#(\d+)\b", section(body, "Blocked by"))
        repo = re.fullmatch(r"[\w.-]+/[\w.-]+", field(body, "Repo"))
        return Ticket(
            id=str(issue["number"]), title=issue["title"], status=status, blocked_by=blocked_by, path=issue["html_url"],
            type=field(body, "Type"), repo=repo and repo.group(0),
        )

    def get(self, ticket_id):
        try:
            return self.ticket(json.loads(self.run(["gh", "api", f"repos/{self.repo}/issues/{ticket_id}"])))
        except subprocess.CalledProcessError as error:
            if "Not Found" not in (error.stderr or ""):
                raise
            raise SystemExit(f"afk: no ticket '{ticket_id}'")

    def claim(self, ticket):
        """Assignment is the cross-orchestrator lock: a claimed ticket drops out of every frontier."""
        self.run(["gh", "issue", "edit", ticket.id, "--repo", self.repo, "--add-assignee", "@me"])

    def release(self, ticket):
        self.run(["gh", "issue", "edit", ticket.id, "--repo", self.repo, "--remove-assignee", "@me"])

    def blockers(self, ticket):
        """Native blocked-by dependencies; failing those, the issues the body's "Blocked by" section names."""
        native = self.api(f"issues/{ticket.id}/dependencies/blocked_by")
        if native:
            return native
        return [json.loads(self.run(["gh", "api", f"repos/{self.repo}/issues/{n}"])) for n in ticket.blocked_by]

    def frontier(self):
        # Blockers carry their own state, so one outside this spec's sub-issues still counts.
        return [t for t in self.tickets() if t.open and all(b["state"] == "closed" for b in self.blockers(t))]


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
        blocked_by=[n.zfill(len(number)) for n in re.findall(r"(?:^|,)\s*#?(\d+)\b", field(text, "Blocked by"))],
        path=path,
        type=field(text, "Type"),
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


def dashboard(statuses, now):
    yield f"{'TICKET':<8}{'PHASE':<11}{'STATE':<11}{'ELAPSED':<9}PR"
    for s in statuses:
        elapsed = duration(now - s.get("phase_started_at", now))
        yield f"{s['ticket']:<8}{s['phase']:<11}{s['state']:<11}{elapsed:<9}{s.get('pr', '-')}"


def duration(seconds):
    minutes, hours = int(seconds) // 60, int(seconds) // 3600
    if hours:
        return f"{hours}h{minutes % 60:02d}m"
    return f"{minutes}m" if minutes else f"{int(seconds)}s"


def render(template, **values):
    text = template.read_text()
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def frontmatter(text):
    """TOML between a leading pair of +++ lines, as in Hugo and Zola."""
    match = re.match(r"\+\+\+\n(.*?)^\+\+\+$", text, re.DOTALL | re.MULTILINE)
    return tomllib.loads(match.group(1)) if match else {}


def deep_merge(base, override):
    merged = dict(base)
    for key, value in override.items():
        merged[key] = deep_merge(merged[key], value) if isinstance(value, dict) and isinstance(merged.get(key), dict) else value
    return merged


def field(text, name):
    """Value of a `Name: value` line, tolerating markdown bold around the label."""
    match = re.search(rf"^\W*{re.escape(name)}\W*:\**\s*(.*)$", text, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def section(markdown, heading):
    """Body of a `## Heading` section, up to the next heading of any level."""
    match = re.search(rf"^#+\s*{re.escape(heading)}\s*$(.*?)(?=^#+\s|\Z)", markdown, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return match.group(1) if match else ""


@contextmanager
def locked(worker_dir):
    """Serialise read-modify-writes of one worker's status between the watcher, its hooks and its reports."""
    with open(worker_dir / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def is_reviewing(status_path):
    try:
        return read_json(status_path)["phase"] == "review"
    except FileNotFoundError:
        return False


def last_write(path):
    """When a file was last written, or None if there's no such file."""
    try:
        return os.path.getmtime(path) if path else None
    except OSError:
        return None


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def to_toml(flat):
    return "".join(f"{k} = {toml_string(v)}\n" for k, v in flat.items())


def toml_string(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def main(argv, run=None, env=None, stdin=None, stdout=None, clock=None):
    app = Afk(
        run or default_run,
        os.environ if env is None else env,
        stdin or sys.stdin,
        stdout or sys.stdout,
        clock or time.time,
    )
    if not argv:
        app.out("usage: afk <init|frontier|start|watch|tick|report|status|hook> ...")
        return 2
    command, args = argv[0], argv[1:]
    handler = getattr(app, "cmd_" + command, None)
    if handler is None:
        app.out(f"afk: unknown command '{command}'")
        return 2
    try:
        return handler(*args) or 0
    except subprocess.CalledProcessError as error:
        app.out(f"afk: `{shlex.join(error.cmd)}` failed: {(error.stderr or '').strip()}")
        return 1
    except SystemExit as exit:
        if isinstance(exit.code, str):
            app.out(exit.code)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
