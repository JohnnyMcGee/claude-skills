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
from datetime import datetime
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

    def cmd_init(self, spec=None):
        """Create the project, or refresh it: re-running keeps its limits, overrides and hand-added repos."""
        if spec is None:
            spec = self.config()["spec"]  # re-init the current session's project
        issue = GITHUB_ISSUE.match(spec)
        if issue:
            tracker, project = "github", f"{issue['repo']}-{issue['number']}"
            repo = Path(self.run(["git", "rev-parse", "--show-toplevel"]).strip())  # run from inside the clone
            tickets = GithubTracker(self.run, spec)
        else:
            spec = Path(spec).resolve()
            if spec.is_file():
                spec = spec.parent
            tracker, project = "local", spec.name
            repo = spec.parent.parent  # <repo>/.scratch/<slug>
            tickets = LocalTracker(spec)
        session = self.run(["tmux", "display-message", "-p", "#{session_name}"]).strip()
        pdir = self.project_dir(project)
        pdir.mkdir(parents=True, exist_ok=True)
        path = pdir / "config.toml"
        config = tomllib.loads(path.read_text()) if path.exists() else {}
        own = getattr(tickets, "repo", None)
        wanted = [r for r in tickets.repos() if not (own and same(r, own))]
        found = self.find_clones(repo, wanted)
        kept = {r: c for r, c in config.get("repos", {}).items() if Path(c).is_dir()}
        repos = {**kept, **found}
        config.update({"spec": str(spec), "tracker": tracker, "repo": str(repo), "session": session})
        config.pop("repos", None)
        path.write_text(to_toml({**config, **({"repos": repos} if repos else {})}))
        self.run(["tmux", "set-option", "-t", session, "@afk_project", project])
        self.out(f"afk project '{project}' bound to tmux session '{session}'")
        for name, clone in repos.items():
            self.out(f"  {name}  {clone}")
        missing = [r for r in wanted if not any(same(r, k) for k in repos)]
        if missing:
            self.out(f"no local clone of {', '.join(missing)}: clone it next to {repo} (or as a submodule) and run "
                     f"`afk init` again, or add it under [repos] in {path}")

    def find_clones(self, repo, wanted):
        """Clones of the wanted owner/name repos: the project clone itself, then its submodules, its subdirectories and
        its siblings, the first found winning; matched by any of each clone's remotes."""
        gitmodules = repo / ".gitmodules"
        submodules = re.findall(r"^\s*path\s*=\s*(.+?)\s*$", gitmodules.read_text(), re.MULTILINE) if gitmodules.is_file() else []
        candidates = [repo, *(repo / p for p in submodules)] + sorted(repo.iterdir()) + sorted(repo.parent.iterdir())
        found = {}
        for candidate in candidates:
            if not (candidate / ".git").exists():
                continue
            try:
                remotes = self.run(["git", "-C", str(candidate), "remote", "-v"])
            except subprocess.CalledProcessError:
                continue  # a broken checkout isn't a clone we can use
            for url in re.findall(r"^\S+\s+(\S+)", remotes, re.MULTILINE):
                named = REMOTE_REPO.search(url)
                for want in wanted:
                    if named and same(f"{named['owner']}/{named['name']}", want):
                        found.setdefault(want, str(candidate))
        return found

    def cmd_frontier(self):
        for ticket in self.tracker().frontier():
            self.out(f"{ticket.id}  {ticket.title}" + (f"  ({ticket.repo})" if ticket.repo else ""))

    def cmd_next(self):
        """What the /afk next skill needs to propose a batch, as JSON: free capacity and the unblocked tickets."""
        workers = self.worker_statuses()
        started = {w["ticket"] for w in workers}
        tickets = []
        for ticket in self.tracker().frontier():
            if ticket.id in started:
                continue  # a local ticket stays open while its worker runs
            clone = self.find_clone(ticket.repo)  # None flags a repo the project has no clone of
            repo_config = self.repo_config(clone) if clone else {}
            choices = launch_choices(ticket, repo_config.get("default_type"), repo_config.get("task_types", {}), {})
            tickets.append({"id": ticket.id, "title": ticket.title, "path": str(ticket.path), "repo": ticket.repo,
                            "clone": clone and str(clone), **choices})
        self.out(json.dumps({
            "capacity": self.capacity(workers),
            "workers": [{k: w.get(k) for k in ("ticket", "title", "phase", "state", "type")} for w in workers],
            "tickets": tickets,
        }, indent=2))

    def capacity(self, workers):
        """Free worker slots: as for `afk start`, workers in review are parked on the human and hitl workers are the
        human's, so neither counts."""
        return max(0, self.max_workers() - sum(w["phase"] not in ("review", "hitl") for w in workers))

    def max_workers(self):
        return {**LIMITS, **self.config().get("limits", {})}["max_workers"]

    def worker_statuses(self):
        return [read_json(p) for p in sorted(self.workers_dir().glob("*/status.json"))]

    def cmd_split(self, ticket_id):
        """Replace a ticket spanning repos with one sibling per repo; stdin is a JSON list of {repo, title, body}."""
        parts = json.loads(self.stdin.read())
        # Checked whole before any write: trackers write part by part, so a bad late part would leave a partial split.
        if not isinstance(parts, list) or not parts or not all(
            isinstance(part, dict)
            and all(isinstance(part.get(key), str) and part[key].strip() for key in ("repo", "title", "body"))
            and not any(c in part[key] for key in ("repo", "title") for c in "\r\n")  # one line each: no forged fields
            for part in parts
        ):
            raise SystemExit("afk: split needs a non-empty JSON list of {repo, title, body} on stdin")
        tracker = self.tracker()
        ticket = tracker.get(ticket_id)
        if not ticket.open:
            raise SystemExit(f"afk: ticket {ticket.id} is not open (it is {ticket.status})")
        ids = tracker.split(ticket, parts)
        self.out(f"split {ticket.id} into {', '.join(ids)}")

    def cmd_start(self, ticket_id, *options):
        project = self.current_project()
        config = self.config()
        corrections = dict(zip(options[::2], options[1::2]))
        if len(options) % 2 or set(corrections) - set(START_OPTIONS) or len(corrections) != len(options) // 2:
            raise SystemExit("afk: usage: afk start <ticket> " + " ".join(f"[{o} <{o[2:]}>]" for o in START_OPTIONS))
        corrections = {option[2:]: value for option, value in corrections.items()}
        tracker = self.tracker()
        ticket = tracker.get(ticket_id)
        pdir = self.project_dir(project)
        worktree = pdir / "worktrees" / ticket.id
        worker_dir = pdir / "workers" / ticket.id
        # Checked before openness: a ticket this orchestrator started is now claimed. claim_worker settles races.
        if worker_dir.exists():
            raise SystemExit(f"afk: ticket {ticket.id} is already started; see `afk status`")
        if not ticket.open:
            raise SystemExit(f"afk: ticket {ticket.id} is not open (it is {ticket.status})")
        repo = corrections.pop("repo", None) or ticket.repo
        clone = self.clone(repo)
        repo_config = self.repo_config(clone)
        task_types = repo_config.get("task_types", {})
        choices = launch_choices(ticket, repo_config.get("default_type"), task_types, corrections)
        type_name = choices["type"]
        if type_name and type_name not in task_types and type_name != HITL:
            raise SystemExit(f"afk: unknown task type '{type_name}'; docs/agents/afk.md defines: {', '.join(task_types) or 'none'}")
        if choices["agent"] not in AGENTS:
            raise SystemExit(f"afk: ticket {ticket.id} would use agent '{choices['agent']}'; supported: {', '.join(AGENTS)}")
        task_type = task_types.get(type_name, {})
        base = repo_config.get("base", "main")
        # A hitl ticket is the human's: its worker guides them and is never driven through the phases.
        phase, state = ("hitl", "yours") if type_name == HITL else ("implement", "working")
        slot = self.claim_worker(worker_dir, counted=phase != "hitl")
        slot_env = [f"AFK_SLOT={slot}", f"AFK_PORT_BASE={repo_config.get('port_base', 4000) + 100 * slot}"]
        branch = f"afk/{project}-{ticket.id}"
        undo = [lambda: shutil.rmtree(worker_dir, ignore_errors=True)]
        try:
            tracker.claim(ticket)
            undo.append(lambda: tracker.release(ticket))
            self.run(["git", "-C", str(clone), "worktree", "add", "-b", branch, str(worktree), base])
            undo.append(lambda: self.run(["git", "-C", str(clone), "branch", "-D", branch]))
            undo.append(lambda: self.run(["git", "-C", str(clone), "worktree", "remove", "--force", str(worktree)]))
            self.prepare_worktree(clone, worktree, repo_config, slot_env)

            pane = self.run(
                ["tmux", "new-window", "-d", "-t", config["session"] + ":", "-n", window_name(ticket),
                 "-c", str(worktree), "-P", "-F", "#{pane_id}"]
            ).strip()
            undo.append(lambda: self.run(["tmux", "kill-window", "-t", pane]))
            shell = self.run(["tmux", "split-window", "-h", "-d", "-t", pane, "-c", str(worktree), "-P", "-F", "#{pane_id}"]).strip()

            settings = worker_dir / "settings.json"
            settings.write_text(json.dumps(worker_settings(base), indent=2) + "\n")
            launch = [
                "env", f"AFK_PROJECT={project}", f"AFK_TICKET={ticket.id}", *slot_env,
                "claude", "--permission-mode", "auto", "--settings", str(settings),
                *[arg for key in ("model", "effort") if choices[key] for arg in (f"--{key}", choices[key])],
                self.first_prompt(ticket, phase, task_type, clone),
            ]
            write_json(
                worker_dir / "status.json",
                {"ticket": ticket.id, "title": ticket.title, "phase": phase, "state": state, "message": "",
                 "pane": pane, "shell_pane": shell, "window": window_name(ticket), "slot": slot, "repo": repo, **choices,
                 "started_at": self.clock(), "phase_started_at": self.clock()},
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

    def prepare_worktree(self, repo, worktree, repo_config, slot_env, step=lambda label: None):
        """Copy the repo's gitignored files into a new worktree, then run its bootstrap; step(label) announces each."""
        for name in repo_config.get("copy", []):
            step(f"copy {name}")
            target = worktree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo / name, target)
        for command in repo_config.get("bootstrap", []):
            step(f"bootstrap `{command}`")
            self.run(["env", *slot_env, "sh", "-c", command], cwd=worktree)

    def cmd_check(self, repo=None):
        """Validate a repo's docs/agents/afk.md (plus afk.local.md) against what `afk start` reads."""
        repo = Path(repo or self.run(["git", "rev-parse", "--show-toplevel"]).strip()).resolve()
        errors, warnings = check_repo_config(repo, self.branch_exists)
        for warning in warnings:
            self.out(f"warning: {warning}")
        for error in errors:
            self.out(f"error: {error}")
        if errors:
            return 1
        self.out(f"{repo / 'docs' / 'agents' / 'afk.md'} is valid")

    def branch_exists(self, repo, branch):
        try:
            self.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"])
            return True
        except subprocess.CalledProcessError:
            return False

    def cmd_trial(self, *args):
        """Prove a repo's config as `afk start` would use it: a scratch worktree, copied files and bootstrap.

        The worktree is left for the caller to start the dev server in; `afk trial --teardown` removes it.
        """
        teardown = "--teardown" in args
        rest = [a for a in args if a != "--teardown"]
        if len(rest) > 1:
            raise SystemExit("afk: usage: afk trial [<repo>] [--teardown]")
        repo = Path(rest[0] if rest else self.run(["git", "rev-parse", "--show-toplevel"]).strip()).resolve()
        worktree = self.state_root() / ".trial" / repo.name  # dot-named: no project dir can be it
        self.remove_trial(repo, worktree)
        if teardown:
            self.out(f"removed trial worktree {worktree} and branch {TRIAL_BRANCH}")
            return
        if self.cmd_check(str(repo)):
            return 1
        repo_config = reduce(deep_merge, repo_config_layers(repo), {})
        base = repo_config.get("base", "main")
        slot_env = [f"AFK_SLOT={TRIAL_SLOT}", f"AFK_PORT_BASE={repo_config.get('port_base', 4000) + 100 * TRIAL_SLOT}"]
        label = f"worktree {worktree} on {TRIAL_BRANCH} from {base}"
        try:
            worktree.parent.mkdir(parents=True, exist_ok=True)
            self.run(["git", "-C", str(repo), "worktree", "add", "-B", TRIAL_BRANCH, str(worktree), base])
            self.out(f"ok   {label}")

            def step(next_label):
                nonlocal label
                label = next_label
                self.out(f"...  {label}")

            self.prepare_worktree(repo, worktree, repo_config, slot_env, step)
        except (OSError, subprocess.CalledProcessError) as error:
            detail = (getattr(error, "stderr", None) or getattr(error, "stdout", None) or str(error)).strip()
            code = f" (exit {error.returncode})" if isinstance(error, subprocess.CalledProcessError) else ""
            self.out(f"FAIL {label}{code}: {detail}")
            self.remove_trial(repo, worktree)
            return 1
        self.out(f"worktree ready: cd {shlex.quote(str(worktree))} && export {' '.join(slot_env)}")

    def remove_trial(self, repo, worktree):
        self.best_effort(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)])
        shutil.rmtree(worktree, ignore_errors=True)
        self.best_effort(["git", "-C", str(repo), "worktree", "prune"])
        self.best_effort(["git", "-C", str(repo), "branch", "-D", TRIAL_BRANCH])

    def claim_worker(self, worker_dir, counted=True):
        """Create the worker's dir and claim the lowest slot no active worker holds; removing the dir frees it.

        An uncounted worker (a hitl ticket's) is neither held to max_workers nor counted toward it.
        """
        workers = worker_dir.parent
        workers.mkdir(parents=True, exist_ok=True)
        with locked(workers):
            if worker_dir.exists():
                raise SystemExit(f"afk: ticket {worker_dir.name} is already started; see `afk status`")
            max_workers = self.max_workers()
            # Workers in review are parked on the human and hitl workers are the human's, so neither counts;
            # one still starting has no status yet.
            active = [d for d in workers.glob("*/slot") if counts_toward_max(d.parent)]
            if counted and len(active) >= max_workers:
                raise SystemExit(f"afk: {len(active)} workers already active (max_workers = {max_workers}); see `afk status`")
            worker_dir.mkdir()
            taken = {int(p.read_text()) for p in workers.glob("*/slot")}
            slot = next(n for n in range(1, len(taken) + 2) if n not in taken)
            (worker_dir / "slot").write_text(f"{slot}\n")
            if not counted:
                (worker_dir / "uncounted").touch()  # before it has a status, so a start racing its setup sees it
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
                # Not a stall by itself: a worker ends its turn to wait on a background task (a review subagent,
                # CI checks) and resumes when it lands. The idle limit catches a worker that never resumes.
                changes["idle"] = True
            elif event == "activity":
                # About to use a tool, so no longer stopped: it resumed, say when a background task it waited on
                # landed. Nothing may be pasted into it until it stops again.
                changes["idle"] = False
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
                try:
                    pr_state = pr_url = None
                    pr_ready = ticket_closed = False
                    if before["phase"] in ("pr", "review", "hitl") and now - before.get("pr_polled_at", 0) >= PR_POLL_SECONDS:
                        pr_state, pr_url, pr_ready = self.pr_state(before)
                        ticket_closed = self.tracker().get(before["ticket"]).done
                    given.update(pr_state=pr_state, pr_url=pr_url, pr_ready=pr_ready, ticket_closed=ticket_closed)
                    blocker = self.cleanup_blocker(before) if ending(before, pr_state, ticket_closed) else None
                    status, effects = tick(path.parent.name, before, now, cleanup_blocker=blocker, **given)
                    if any(kind == "prompt" for kind, *_ in effects) and not self.agent_running(before["pane"]):
                        # Pasting into a bare shell would run the prompt's markdown as commands.
                        status, effects = tick(path.parent.name, before, now, agent_running=False, **given)
                    for effect in effects:
                        self.perform(*effect)
                except subprocess.CalledProcessError as error:
                    failures.append(error)
                    shown.append(before)
                    continue
                if status is None:
                    continue  # cleaned up: the worker is gone
                if status != before:
                    write_json(path, status)
                verify.update(before, status)
                shown.append(status)
        for line in dashboard(shown, now):
            self.out(line)
        self.surface_unblocked(shown, now)
        if failures:
            raise failures[0]

    def surface_unblocked(self, statuses, now):
        """The Gate 1 judgment point: unblocked tickets and a free worker slot. The user decides with /afk next.

        The tracker is polled at most every FRONTIER_POLL seconds, as a GitHub frontier costs a request per ticket,
        and the user is notified once per newly unblocked ticket, not every tick.
        """
        path = self.project_dir(self.current_project()) / "frontier.json"
        seen = read_json(path) if path.exists() else {}
        before = dict(seen)
        started = {s["ticket"] for s in statuses}
        if now - seen.get("polled_at", float("-inf")) >= FRONTIER_POLL:
            seen.update(polled_at=now, unblocked=[t.id for t in self.tracker().frontier()])
        candidates = [t for t in seen["unblocked"] if t not in started]
        # A ticket that drops out, say blocked again, counts as newly unblocked when it returns.
        seen["notified"] = [t for t in seen.get("notified", []) if t in candidates]
        if candidates and self.capacity(statuses):
            message = f"{len(candidates)} ticket{'s' * (len(candidates) != 1)} unblocked — run /afk next"
            self.out(message)
            if set(candidates) - set(seen.get("notified", [])):
                self.notify(message)
                seen["notified"] = candidates
        if seen != before:
            write_json(path, seen)

    def perform(self, kind, *args):
        if kind == "prompt":
            ticket_id, pane, phase = args
            repo = read_json(self.workers_dir() / ticket_id / "status.json").get("repo")
            self.send_prompt(ticket_id, pane, self.phase_prompt(ticket_id, phase, self.clone(repo)))
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
        elif kind == "cleanup":
            self.clean_up(*args)

    def cleanup_blocker(self, status):
        """Why cleaning up this worker could lose the human's work, or None if it's safe."""
        if "shell_pane" not in status:
            return "its shell pane is unknown (it was started by an older afk)"
        command = self.pane_command(status["shell_pane"])
        if command is None:
            return "its shell pane is gone"
        if command not in SHELLS:
            return f"its shell pane is running {command}"
        if self.run(["git", "-C", str(self.worktree(status["ticket"])), "status", "--porcelain"]).strip():
            return "its worktree has uncommitted changes"
        return None

    def clean_up(self, ticket_id, pane, ended):
        """Remove an ended worker's window, worktree, local branch and state; removing its dir frees its slot.

        A merged ticket is completed. One whose PR was closed unmerged is released, back onto the frontier. A closed
        ticket is left as it is.
        """
        project = self.current_project()
        repo = str(self.clone(read_json(self.workers_dir() / ticket_id / "status.json").get("repo")))
        tracker = self.tracker()
        if ended == "merged":
            tracker.complete(tracker.get(ticket_id))
        elif ended == "closed":
            tracker.release(tracker.get(ticket_id))
        self.run(["tmux", "kill-window", "-t", pane])
        self.run(["git", "-C", repo, "worktree", "remove", str(self.worktree(ticket_id))])
        self.run(["git", "-C", repo, "branch", "-D", f"afk/{project}-{ticket_id}"])
        shutil.rmtree(self.workers_dir() / ticket_id)

    def worktree(self, ticket_id):
        return self.project_dir(self.current_project()) / "worktrees" / ticket_id

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

    def phase_prompt(self, ticket_id, phase, clone):
        ticket = self.tracker().get(ticket_id)
        return render(
            SKILL_DIR / f"phase-{phase}.md",
            ticket=ticket.id,
            ticket_path=ticket.path,
            base=self.repo_config(clone).get("base", "main"),
        )

    def first_prompt(self, ticket, phase, task_type, clone):
        """The first phase prompt, invoking the task type's skill and followed by its prompt template."""
        prompt = self.phase_prompt(ticket.id, phase, clone)
        if "prompt" in task_type:
            template = clone / task_type["prompt"]
            prompt += "\n" + render(template, ticket=ticket.id, ticket_path=ticket.path)
        if "skill" in task_type:
            prompt = f"{task_type['skill']} {prompt}"
        return prompt

    def pr_state(self, status):
        """GitHub's state for the worker's PR, OPEN, CLOSED, MERGED or NONE, its URL (None when there's no PR), and
        whether it carries /open-pr's READY_LABEL.

        afk only ever reads PRs; merging is the human's call. Whoever opened the PR, the worker or the human, it is
        found by the worker's branch, so afk never depends on a URL being reported. A PR opened before the worker
        started, from an earlier attempt at the ticket on the same branch, is none of this worker's.
        """
        branch = f"afk/{self.current_project()}-{status['ticket']}"
        try:
            pr = json.loads(self.run(["gh", "pr", "view", branch, "--json", "state,url,labels,createdAt"], cwd=self.worktree(status["ticket"])))
        except subprocess.CalledProcessError as error:
            if "no pull requests found" not in (error.stderr or ""):
                raise
            return "NONE", None, False
        if datetime.fromisoformat(pr["createdAt"]).timestamp() < status.get("started_at", float("-inf")):
            return "NONE", None, False
        return pr["state"], pr["url"], any(label["name"] == READY_LABEL for label in pr.get("labels", []))

    def agent_running(self, pane):
        return self.pane_command(pane) == "claude"

    def pane_command(self, pane):
        """The pane's foreground command, or None if the pane is gone."""
        try:
            return self.run(["tmux", "display-message", "-p", "-t", pane, "#{pane_current_command}"]).strip()
        except subprocess.CalledProcessError:
            return None

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

    def clone(self, repo):
        clone = self.find_clone(repo)
        if clone is None:
            repos = self.config().get("repos", {})
            raise SystemExit(f"afk: no clone of {repo} in this project; its [repos] are: {', '.join(repos) or 'none'}")
        return clone

    def find_clone(self, repo):
        """The local clone of an owner/name repo, the project's own when there is none, or None if it has no clone."""
        config = self.config()
        if not repo or repo == getattr(self.tracker(), "repo", None):
            return Path(config["repo"])
        clone = next((c for r, c in config.get("repos", {}).items() if same(r, repo)), None)
        return clone and Path(clone)

    def repo_config(self, clone=None):
        """A repo's AFK config: docs/agents/afk.md, then gitignored afk.local.md, then the project's [overrides]."""
        config = self.config()
        return reduce(deep_merge, [*repo_config_layers(Path(clone or config["repo"])), config.get("overrides", {})], {})

    def tracker(self):
        config = self.config()
        if config["tracker"] == "github":
            return GithubTracker(self.run, config["spec"])
        return LocalTracker(Path(config["spec"]))


REPORT_STATES = ("done", "blocked", "question")

AGENTS = ("claude",)

# The built-in task type for human-in-the-loop tickets; the repo may still configure it under task_types.
HITL = "hitl"

FRONTIER_POLL = 60  # seconds between the watcher's reads of the tracker

# Corrections the user can make at Gate 1, each a `afk start` option.
START_OPTIONS = ("--type", "--agent", "--model", "--effort", "--repo")

# Runaway limits; a project's config.toml [limits] table overrides any of them.
LIMITS = {"max_workers": 3, "phase_minutes": 90, "idle_minutes": 20, "fix_loops": 3}

PHASES = ("implement", "verify", "prepr", "pr", "review")

PR_POLL_SECONDS = 60
NO_PR = "no PR found for its branch"
NO_REPORT = "stopped without reporting"
# /open-pr adds this label only once the PR's automated gates are clear.
READY_LABEL = "Ready for Review"

# A shell pane running one of these at its prompt is idle, so closing it loses nothing.
SHELLS = {"bash", "zsh", "fish", "sh", "dash", "ksh"}

# `afk trial` works in a scratch worktree on this branch, on a slot clear of the low ones real workers take.
TRIAL_BRANCH = "afk/trial"
TRIAL_SLOT = 9

# The frontmatter `afk start` reads, key -> (type, description of the type).
REPO_KEYS = {
    "base": (str, "a string"),
    "copy": (list, "a list of strings"),
    "bootstrap": (list, "a list of strings"),
    "port_base": (int, "an integer"),
    "verify_concurrency": (int, "an integer of at least 1"),
    "default_type": (str, "a string"),
    "task_types": (dict, "a table of task types"),
}
TASK_TYPE_KEYS = ("agent", "model", "effort", "skill", "prompt")

# Prose sections workers are pointed at.
REPO_SECTIONS = ("Pre-PR skill", "Dev server", "Verification recipes")

# Appended to a worker's window name so its state shows even without afk's tmux status format.
WINDOW_MARKS = {"question": "?", "blocked": "!", "attention": "!", "stuck": "!", "review": "✓"}


def ending(status, pr_state, ticket_closed=False):
    """How the worker's work ended: its PR "merged" or "closed", or its "ticket closed"; None while it goes on.

    A closed ticket outranks a closed PR, so cleanup never releases a ticket that is already closed.
    """
    if status["state"] == "cleanup-pending":
        return status.get("ended", "merged")  # workers left pending before closed PRs were cleaned up had merged
    if pr_state == "MERGED":
        return "merged"
    if ticket_closed:
        return "ticket closed"
    return "closed" if pr_state == "CLOSED" else None


def tick(ticket, status, now, agent_running=True, may_verify=True, limits=None, last_activity=None,
         pr_state=None, pr_url=None, pr_ready=False, ticket_closed=False, cleanup_blocker=None):
    """One watcher step for one worker, pure: its status and the time in; its updated status and effects out.

    agent_running=False says the worker's pane no longer runs its agent, so it can't be sent a prompt.
    may_verify=False says the repo's verify slots are full, so a worker due to verify must queue.
    pr_state is GitHub's state for the worker's PR when this tick polled it, else None; pr_url is the PR's URL, if any.
    pr_ready says the polled PR carries READY_LABEL.
    ticket_closed says the tracker, polled with the PR, has the worker's ticket closed.
    cleanup_blocker, for a merged or closed PR or a closed ticket, says why cleaning up now could lose the human's work.
    A status of None out means the worker is cleaned up and gone.
    """
    ended = ending(status, pr_state, ticket_closed)
    if ended and cleanup_blocker is None:
        return None, [
            ("cleanup", ticket, status["pane"], ended),
            ("notify", f"{ticket} {ended} and cleaned up; run `/afk next` to propose the next batch"),
        ]
    if pr_state is not None:
        status = {**status, "pr_polled_at": now}
    if pr_url:
        status = {**status, "pr": pr_url}
    if ended:
        # Checked again every tick, but the human hears about it once.
        effects = [] if status["state"] == "cleanup-pending" else [("notify", f"{ticket} {ended}; cleanup pending: {cleanup_blocker}")]
        status = {**status, "state": "cleanup-pending", "ended": ended, "message": cleanup_blocker}
    elif status["phase"] == "hitl":
        # The human drives it, so there's nothing to advance, no limit to hold it to, and no report to act on.
        status, effects = {**status, "state": "yours"}, []
    else:
        limits = {**LIMITS, **(limits or {})}
        status, effects = advance(ticket, status, now, agent_running, may_verify, limits, pr_state, pr_ready)
        if not effects:
            status, effects = enforce(ticket, status, now, limits, last_activity)
    if status["state"] != status.get("shown"):
        status = {**status, "shown": status["state"]}
        effects.append(("window", status["pane"], status["window"], status["state"]))
    return status, effects


def advance(ticket, status, now, agent_running, may_verify, limits, pr_state=None, pr_ready=False):
    state, phase, message = status["state"], status["phase"], status.get("message", "")
    if phase == "pr" and pr_state == "OPEN" and pr_ready and state in ("working", "blocked", "attention", "stuck") and status.get("idle"):
        # The worker stopped without reporting done, but its PR carries the label /open-pr adds once the gates are
        # clear, whoever cleared them: it's up for review. An open PR alone isn't enough, since /open-pr stops
        # between turns while it waits on checks and reviewers. Like any phase prompt, the review prompt waits
        # until the worker's session has stopped.
        effects = [("prompt", ticket, status["pane"], "review")] if agent_running else []
        return enter_review(ticket, status, now, effects)
    if phase == "review":
        # Only a worker otherwise just waiting on review is flagged, and only this attention clears itself: any
        # other state the worker is in still needs the human to look.
        missing = state == "attention" and message == NO_PR
        if pr_state == "NONE" and state == "review":
            status = {**status, "state": "attention", "message": NO_PR, "notified": status.get("reports")}
            return status, [("notify", f"{ticket} needs attention: {NO_PR}")]
        if pr_state == "OPEN" and missing:
            return {**status, "state": "review", "message": ""}, []
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
        effects = [("prompt", ticket, status["pane"], phase)]
        if phase == "review":
            return enter_review(ticket, status, now, effects)
        return {**status, "phase": phase, "state": "working", "message": "", "idle": False, "phase_started_at": now}, effects
    if state in ("question", "blocked", "attention") and status.get("reports") != status.get("notified"):
        label = "needs attention" if state == "attention" else state
        return {**status, "notified": status.get("reports")}, [("notify", f"{ticket} {label}: {message}")]
    return status, []


def enter_review(ticket, status, now, effects):
    # The PR's URL is whatever the last poll found by branch, never taken from a report.
    status = {**status, "phase": "review", "state": "review", "message": "", "idle": False, "phase_started_at": now}
    pr = status.get("pr")
    return status, effects + [("notify", f"{ticket} PR ready for review" + (f": {pr}" if pr else ""))]


def enforce(ticket, status, now, limits, last_activity):
    """Trip a runaway limit: interrupt the worker, mark it stuck and notify. Never kill it. A worker that stopped
    without reporting and stayed quiet is instead marked attention, with nothing to interrupt.

    last_activity is when the worker's session last wrote anything; a phase prompt counts as activity.
    """
    # A worker that reported done but hasn't stopped is still running. Review has no phase clock: it waits on the human.
    running = status["state"] == "working" or (status["state"] == "done" and not status.get("idle"))
    # A limit trips once per phase: after the human has looked, a recovered worker isn't interrupted again.
    tripped = status.get("tripped") == status["phase_started_at"]
    if not running or tripped or status["phase"] == "review":
        return status, []
    quiet = now - max(last_activity or 0, status["phase_started_at"]) > limits["idle_minutes"] * 60
    if status["state"] == "working" and status.get("idle"):
        # It stopped without reporting, likely to wait on a background task, so only the idle limit applies: once
        # nothing has woken it for that long, it's a silent stall. It's already stopped, so there is nothing to
        # interrupt.
        if not quiet:
            return status, []
        reports = status.get("reports", 0) + 1
        status = {**status, "state": "attention", "message": NO_REPORT, "reports": reports, "notified": reports}
        return status, [("notify", f"{ticket} needs attention: {NO_REPORT}")]
    if now - status["phase_started_at"] > limits["phase_minutes"] * 60:
        reason = f"over {limits['phase_minutes']}m in {status['phase']}"
    elif quiet:
        reason = f"idle for over {limits['idle_minutes']}m"
    else:
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

# `#<number>` links an issue in the body's own repo, `<owner>/<name>#<number>` one in any repo.
BLOCKER_REF = re.compile(r"(?<![\w/.-])((?:[\w.-]+/[\w.-]+)?)#(\d+)\b")

def same(a, b):
    """GitHub owner and repo names match in any case."""
    return a.lower() == b.lower()


GITHUB_ISSUE = re.compile(r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/?$")

# The owner/name a git remote URL points at, over SSH or HTTPS.
REMOTE_REPO = re.compile(r"[:/](?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$")


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

    def repos(self):
        """The repos the tickets' Repo: lines name."""
        return list(dict.fromkeys(t.repo for t in self.tickets() if t.repo))

    def get(self, ticket_id):
        for ticket in self.tickets():
            if ticket.id == ticket_id:
                return ticket
        raise SystemExit(f"afk: no ticket '{ticket_id}'")

    def claim(self, ticket):
        """A local spec belongs to this orchestrator alone, so there is no one to claim it from."""

    def release(self, ticket):
        pass

    def complete(self, ticket):
        """Merged: mark it done, as GitHub closes an issue, so the frontier doesn't offer it again."""
        ticket.path.write_text(set_field(ticket.path.read_text(), "Status", "done"))

    def split(self, ticket, parts):
        numbers = [int(t.id) for t in self.tickets()]
        blocked_by = ", ".join(ticket.blocked_by) or "None — can start immediately"
        ids = []
        for number, part in enumerate(parts, start=max(numbers) + 1):
            id = str(number).zfill(len(ticket.id))
            # Fields before the body: the first match wins, so fields copied into the body can't override them.
            (self.issues_dir / f"{id}-{slug(part['title'])}.md").write_text(
                f"# {id} — {part['title']}\n\n**Repo:** {part['repo']}\n\n**Blocked by:** {blocked_by}\n\n"
                f"**Status:** ready-for-agent\n\n{part['body'].strip()}\n"
            )
            ids.append(id)
        for dependent in self.tickets():
            if ticket.id in dependent.blocked_by:
                blockers = [b for b in dependent.blocked_by if b != ticket.id] + ids
                dependent.path.write_text(set_field(dependent.path.read_text(), "Blocked by", ", ".join(blockers)))
        text = ticket.path.read_text()
        ticket.path.write_text(set_field(text, "Status", "closed") + f"\n**Split into:** {', '.join(ids)}\n")
        return ids

    def frontier(self):
        tickets = self.tickets()
        done = {t.id for t in tickets if t.done}
        return [t for t in tickets if t.open and all(b in done for b in t.blocked_by)]


class GithubTracker:
    """Tickets as the sub-issues of a GitHub spec issue, read through `gh`."""

    def __init__(self, run, spec_url):
        self.run = run
        issue = GITHUB_ISSUE.match(spec_url)
        self.owner = issue["owner"]
        self.repo = f"{issue['owner']}/{issue['repo']}"
        self.spec_number = issue["number"]

    def api(self, path, repo=None):
        pages = json.loads(self.run(["gh", "api", "--paginate", "--slurp", f"repos/{repo or self.repo}/{path}"]))
        return [item for page in pages for item in page]

    def name(self, issue):
        """A ticket's id: its number in the spec's repo, `<repo>#<number>` for a sub-issue in another of the owner's repos."""
        repo = self.repo_of(issue)
        if same(repo, self.repo):
            return str(issue["number"])
        owner, name = repo.split("/")
        if not same(owner, self.owner):
            raise SystemExit(f"afk: sub-issue {repo}#{issue['number']} is outside {self.owner}; move it to one of {self.owner}'s repos")
        return f"{name}#{issue['number']}"

    @staticmethod
    def repo_of(issue):
        return issue["repository_url"].removeprefix("https://api.github.com/repos/")

    def ref(self, issue, repo=None):
        """How a body in `repo` (the spec's by default) links to the issue: `#<number>` within its own repo."""
        own = self.repo_of(issue)
        return f"{'' if same(own, repo or self.repo) else own}#{issue['number']}"

    def locate(self, ticket_id):
        """The repo and number a ticket id names."""
        name, _, number = ticket_id.rpartition("#")
        return (f"{self.owner}/{name}" if name else self.repo), number

    def tickets(self):
        return [self.ticket(issue) for issue in self.api(f"issues/{self.spec_number}/sub_issues")]

    def repos(self):
        """Every repo a sub-issue lives in or names on its Repo: line, in any owner, so none is refused here."""
        repos = {}
        for issue in self.api(f"issues/{self.spec_number}/sub_issues"):
            named = re.fullmatch(r"[\w.-]+/[\w.-]+", field(issue.get("body") or "", "Repo"))
            for repo in (self.repo_of(issue), named and named.group(0)):
                if repo and not any(same(repo, r) for r in repos):
                    repos[repo] = None
        return list(repos)

    def ticket(self, issue):
        if issue["state"] == "closed":
            status = "closed"
        elif issue["assignees"]:
            status = "claimed"  # by another orchestrator or a human
        else:
            status = "open"
        body = issue.get("body") or ""
        id = self.name(issue)
        blocked_by = [f"{repo or self.repo_of(issue)}#{n}" for repo, n in BLOCKER_REF.findall(section(body, "Blocked by"))]
        repo = re.fullmatch(r"[\w.-]+/[\w.-]+", field(body, "Repo"))
        own = self.repo_of(issue)
        return Ticket(
            id=id, title=issue["title"], status=status, blocked_by=blocked_by, path=issue["html_url"],
            type=field(body, "Type"), repo=repo.group(0) if repo else None if same(own, self.repo) else own,
        )

    def get(self, ticket_id):
        try:
            repo, number = self.locate(ticket_id)
            return self.ticket(json.loads(self.run(["gh", "api", f"repos/{repo}/issues/{number}"])))
        except subprocess.CalledProcessError as error:
            if "Not Found" not in (error.stderr or ""):
                raise
            raise SystemExit(f"afk: no ticket '{ticket_id}'")

    def claim(self, ticket):
        """Assignment is the cross-orchestrator lock: a claimed ticket drops out of every frontier."""
        repo, number = self.locate(ticket.id)
        self.run(["gh", "issue", "edit", number, "--repo", repo, "--add-assignee", "@me"])

    def release(self, ticket):
        repo, number = self.locate(ticket.id)
        self.run(["gh", "issue", "edit", number, "--repo", repo, "--remove-assignee", "@me"])

    def complete(self, ticket):
        """Merged: its assignment keeps it off the frontier until its PR, or the user, closes it."""

    def blockers(self, ticket):
        """Native blocked-by dependencies; failing those, the issues the body's "Blocked by" section names."""
        repo, number = self.locate(ticket.id)
        native = self.api(f"issues/{number}/dependencies/blocked_by", repo)
        if native:
            return native
        refs = (ref.rpartition("#") for ref in ticket.blocked_by)
        return [json.loads(self.run(["gh", "api", f"repos/{repo}/issues/{n}"])) for repo, _, n in refs]

    def split(self, ticket, parts):
        """Create the parts as sub-issues of the spec, point the ticket's dependents at them, then close the ticket."""
        blocked_by = "\n".join(f"- {self.ref(b)}" for b in self.blockers(ticket)) or "None — can start immediately"
        created = []
        for part in parts:
            # Repo line and Blocked by section first, as the first match wins; the heading after them ends the section.
            body = f"Repo: {part['repo']}\n\n## Blocked by\n\n{blocked_by}\n\n## Details\n\n{part['body'].strip()}\n"
            created.append(json.loads(self.run(
                ["gh", "api", f"repos/{self.repo}/issues", "-X", "POST", "-f", f"title={part['title']}", "-f", f"body={body}"]
            )))
            self.run(["gh", "api", f"repos/{self.repo}/issues/{self.spec_number}/sub_issues", "-X", "POST",
                      "-F", f"sub_issue_id={created[-1]['id']}"])
        repo, number = self.locate(ticket.id)
        for dependent in self.api(f"issues/{number}/dependencies/blocking", repo):
            for issue in created:
                self.run(["gh", "api", f"repos/{self.repo_of(dependent)}/issues/{dependent['number']}/dependencies/blocked_by", "-X", "POST",
                          "-F", f"issue_id={issue['id']}"])
        for dependent in self.api(f"issues/{self.spec_number}/sub_issues"):
            body = dependent.get("body") or ""
            blockers = section(body, "Blocked by")
            own = self.repo_of(dependent)
            replacement = ", ".join(self.ref(issue, own) for issue in created)
            names_ticket = lambda ref: same(ref[1] or own, repo) and ref[2] == number
            rewired = BLOCKER_REF.sub(lambda ref: replacement if names_ticket(ref) else ref[0], blockers)
            if rewired != blockers:
                self.run(["gh", "api", f"repos/{own}/issues/{dependent['number']}", "-X", "PATCH",
                          "-f", f"body={body.replace(blockers, rewired, 1)}"])
        self.run(["gh", "issue", "close", number, "--repo", repo, "--reason", "not planned",
                  "--comment", f"Split into {', '.join(self.ref(issue, repo) for issue in created)}, one per repo."])
        return [str(issue["number"]) for issue in created]

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
        repo=field(text, "Repo") or None,
    )


def launch_choices(ticket, default_type, task_types, corrections):
    """A ticket's task type and the agent, model and effort it launches with, after the user's corrections."""
    type_name = corrections.get("type") or ticket.type or default_type
    task_type = task_types.get(type_name, {})
    choices = {"type": type_name, "agent": task_type.get("agent", "claude"), "model": task_type.get("model"),
               "effort": task_type.get("effort")}
    return {**choices, **{k: v for k, v in corrections.items() if k != "type"}}


def slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def window_name(ticket):
    abbrev = slug(ticket.title)[:10].rstrip("-")
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
            "PreToolUse": hook("activity"),
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


def repo_config_layers(repo):
    docs = repo / "docs" / "agents"
    return [frontmatter(p.read_text()) for p in (docs / "afk.md", docs / "afk.local.md") if p.is_file()]


def check_repo_config(repo, branch_exists):
    """(errors, warnings) for a repo's AFK config, as `afk start` would read it."""
    docs = repo / "docs" / "agents"
    main = docs / "afk.md"
    if not main.is_file():
        return [f"{main} not found"], []
    errors, warnings, layers = [], [], []
    for path in (main, docs / "afk.local.md"):
        if not path.is_file():
            continue
        text = path.read_text()
        if not re.match(r"\+\+\+\n(.*?)^\+\+\+$", text, re.DOTALL | re.MULTILINE):
            errors.append(f"{path.name}: no TOML frontmatter between leading +++ lines")
            continue
        try:
            layers.append(frontmatter(text))
        except tomllib.TOMLDecodeError as error:
            errors.append(f"{path.name}: invalid TOML: {error}")
    config = reduce(deep_merge, layers, {})

    for key, value in config.items():
        if key not in REPO_KEYS:
            errors.append(f"unknown key '{key}'; known keys: {', '.join(REPO_KEYS)}")
            continue
        kind, description = REPO_KEYS[key]
        if (
            not isinstance(value, kind)
            or isinstance(value, bool)
            or (kind is list and not all(isinstance(v, str) for v in value))
            or (key == "verify_concurrency" and value < 1)
        ):
            errors.append(f"'{key}' must be {description}")
    task_types = config.get("task_types", {}) if isinstance(config.get("task_types"), dict) else {}
    for name, task_type in task_types.items():
        if not isinstance(task_type, dict):
            errors.append(f"task_types.{name} must be a table")
            continue
        for key, value in task_type.items():
            if key not in TASK_TYPE_KEYS:
                errors.append(f"task_types.{name}: unknown key '{key}'; known keys: {', '.join(TASK_TYPE_KEYS)}")
            elif not isinstance(value, str):
                errors.append(f"task_types.{name}.{key} must be a string")
        if task_type.get("agent", "claude") not in AGENTS:
            errors.append(f"task_types.{name}: unsupported agent '{task_type['agent']}'; supported: {', '.join(AGENTS)}")
        prompt = task_type.get("prompt")
        if isinstance(prompt, str) and not (repo / prompt).is_file():
            errors.append(f"task_types.{name}: prompt template {prompt} not found in the repo")
    default_type = config.get("default_type")
    if isinstance(default_type, str) and default_type not in task_types and default_type != HITL:
        errors.append(f"default_type '{default_type}' is not a task type; defined: {', '.join(task_types) or 'none'}")
    if isinstance(config.get("copy"), list):
        for name in config["copy"]:
            if isinstance(name, str) and not (repo / name).is_file():
                errors.append(f"copy: {name} not found in {repo}")
    base = config.get("base", "main")
    if isinstance(base, str) and not branch_exists(repo, base):
        errors.append(f"base branch '{base}' does not exist in {repo}")

    prose = re.sub(r"\A\+\+\+\n.*?^\+\+\+$", "", main.read_text(), count=1, flags=re.DOTALL | re.MULTILINE)
    for heading in REPO_SECTIONS:
        if not re.search(rf"^#+\s*{re.escape(heading)}\s*$", prose, re.MULTILINE | re.IGNORECASE):
            warnings.append(f"afk.md has no '## {heading}' section; workers look for it")
    return errors, warnings


def deep_merge(base, override):
    merged = dict(base)
    for key, value in override.items():
        merged[key] = deep_merge(merged[key], value) if isinstance(value, dict) and isinstance(merged.get(key), dict) else value
    return merged


def field(text, name):
    """Value of a `Name: value` line, tolerating markdown bold around the label."""
    match = re.search(rf"^\W*{re.escape(name)}\W*:\**\s*(.*)$", text, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def set_field(text, name, value):
    """Replace the value of a `Name: value` line, keeping its label's markdown."""
    pattern = rf"^(\W*{re.escape(name)}\W*:\**[ \t]*).*$"
    return re.sub(pattern, lambda m: m.group(1) + value, text, count=1, flags=re.MULTILINE | re.IGNORECASE)


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


def counts_toward_max(worker_dir):
    if (worker_dir / "uncounted").exists():
        return False
    try:
        return read_json(worker_dir / "status.json")["phase"] not in ("review", "hitl")
    except FileNotFoundError:
        return True


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


def to_toml(table, prefix=()):
    """TOML for a table of strings, numbers, booleans, lists of those, and nested tables."""
    keys = "".join(f"{toml_key(k)} = {toml_value(v)}\n" for k, v in table.items() if not isinstance(v, dict))
    tables = "".join(
        f"\n[{'.'.join(map(toml_key, (*prefix, k)))}]\n" + to_toml(v, (*prefix, k))
        for k, v in table.items() if isinstance(v, dict)
    )
    return keys + tables


def toml_key(key):
    return key if re.fullmatch(r"[\w-]+", key) else toml_string(key)


def toml_value(value):
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(map(toml_value, value)) + "]"
    return toml_string(value)


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
        app.out("usage: afk <init|frontier|next|split|start|watch|tick|report|status|hook|check|trial> ...")
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
