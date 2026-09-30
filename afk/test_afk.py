import io
import json
import os
import shlex
import subprocess
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path

import afk


class FakeRun:
    """Stands in for the run() seam: records every command and replays scripted stdout.

    Responses are keyed by a command prefix; the longest matching prefix wins.
    """

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []
        self.inputs = []
        self.cwds = []

    def __call__(self, cmd, input=None, cwd=None):
        self.calls.append(list(cmd))
        self.inputs.append(input)
        self.cwds.append(cwd and str(cwd))
        matches = [p for p in self.responses if tuple(cmd[: len(p)]) == p]
        if not matches:
            return ""
        response = self.responses[max(matches, key=len)]
        return response(cmd) if callable(response) else response

    def find(self, *prefix):
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]


class AfkTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state_home = self.tmp / "state"
        self.repo = self.tmp / "repo"
        self.scratch = self.repo / ".scratch" / "widgets"
        (self.scratch / "issues").mkdir(parents=True)
        (self.scratch / "spec.md").write_text("# Widgets spec\n")
        self.run_fake = FakeRun(
            {
                ("tmux", "display-message"): "work\n",
                ("tmux", "show-options"): "widgets\n",
                ("git", "-C"): "",
                ("tmux", "new-window"): "%5\n",
                ("tmux", "display-message", "-p", "-t"): "claude\n",  # the pane's foreground command
            }
        )
        self.env = {"XDG_STATE_HOME": str(self.state_home), "HOME": str(self.tmp)}
        self.now = 1_000_000.0

    def afk(self, *argv, stdin="", env=None):
        out = io.StringIO()
        code = afk.main(
            list(argv),
            run=self.run_fake,
            env={**self.env, **(env or {})},
            stdin=io.StringIO(stdin),
            stdout=out,
            clock=lambda: self.now,
        )
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue()

    def ticket(self, name, title, status="ready-for-agent", blocked_by="None — can start immediately", type=None):
        number = name.split("-", 1)[0]
        (self.scratch / "issues" / f"{name}.md").write_text(
            f"# {number} — {title}\n\n"
            f"**What to build:** {title}.\n\n"
            f"**Blocked by:** {blocked_by}\n\n"
            f"**Status:** {status}\n\n"
            + (f"**Type:** {type}\n\n" if type else "")
            + "- [ ] It works\n"
        )

    @property
    def project_dir(self):
        return self.state_home / "afk" / "widgets"

    def repo_config(self, frontmatter, name="afk.md", body="# AFK\n\nRun `npm run dev`.\n"):
        """Write the repo's AFK config doc: TOML frontmatter between +++ lines, then prose."""
        path = self.repo / "docs" / "agents" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"+++\n{frontmatter.strip()}\n+++\n\n{body}")


class InitTest(AfkTestCase):
    def test_init_creates_project_state_and_tags_tmux_session(self):
        self.afk("init", str(self.scratch))

        config = tomllib.loads((self.project_dir / "config.toml").read_text())
        self.assertEqual(config["spec"], str(self.scratch))
        self.assertEqual(config["tracker"], "local")
        self.assertEqual(config["repo"], str(self.repo))
        self.assertEqual(config["session"], "work")
        self.assertEqual(
            self.run_fake.find("tmux", "set-option"),
            [["tmux", "set-option", "-t", "work", "@afk_project", "widgets"]],
        )


class GithubTestCase(AfkTestCase):
    """A project whose spec is GitHub issue acme/widgets#2 and whose tickets are its sub-issues, all via faked `gh`."""

    SPEC = "https://github.com/acme/widgets/issues/2"

    def setUp(self):
        super().setUp()
        self.issues = {}
        self.native_blockers = {}
        self.run_fake.responses[("git", "rev-parse", "--show-toplevel")] = f"{self.repo}\n"
        self.run_fake.responses[("tmux", "show-options")] = "widgets-2\n"
        self.run_fake.responses[("gh", "api")] = self.gh_api

    def gh_api(self, cmd):
        path = next(arg for arg in cmd[2:] if arg.startswith("repos/"))
        if path == "repos/acme/widgets/issues/2/sub_issues":
            result = list(self.issues.values())
        elif path.endswith("/dependencies/blocked_by"):
            result = [self.issues[n] for n in self.native_blockers.get(int(path.split("/")[4]), [])]
        else:
            result = self.issues[int(path.split("/")[4])]
        return json.dumps([result] if "--slurp" in cmd else result)  # --slurp wraps each page in one array

    def issue(self, number, title, state="open", assignees=(), body=""):
        self.issues[number] = {
            "number": number,
            "title": title,
            "state": state,
            "assignees": [{"login": login} for login in assignees],
            "body": body,
            "html_url": f"https://github.com/acme/widgets/issues/{number}",
        }

    @property
    def project_dir(self):
        return self.state_home / "afk" / "widgets-2"


class GithubInitTest(GithubTestCase):
    def test_init_from_a_spec_issue_url_records_the_github_tracker_and_the_local_checkout(self):
        self.afk("init", self.SPEC)

        config = tomllib.loads((self.project_dir / "config.toml").read_text())
        self.assertEqual(config["spec"], self.SPEC)
        self.assertEqual(config["tracker"], "github")
        self.assertEqual(config["repo"], str(self.repo))
        self.assertEqual(
            self.run_fake.find("tmux", "set-option"),
            [["tmux", "set-option", "-t", "work", "@afk_project", "widgets-2"]],
        )


class GithubFrontierTest(GithubTestCase):
    def test_frontier_lists_open_sub_issues_nobody_has_claimed(self):
        self.issue(3, "Widget schema", state="closed")
        self.issue(4, "Widget API")
        self.issue(5, "Widget UI", assignees=["someone-else"])
        self.issue(6, "Widget export")
        self.afk("init", self.SPEC)

        output = self.afk("frontier")

        self.assertEqual(output.splitlines(), ["4  Widget API", "6  Widget export"])

    def test_frontier_skips_sub_issues_with_an_open_native_blocker(self):
        self.issue(3, "Widget schema", state="closed")
        self.issue(4, "Widget API")
        self.issue(5, "Widget UI")
        self.issue(6, "Widget search")
        self.native_blockers = {5: [3], 6: [3, 4]}
        self.afk("init", self.SPEC)

        output = self.afk("frontier")

        self.assertEqual(output.splitlines(), ["4  Widget API", "5  Widget UI"])

    def test_without_native_blockers_the_body_blocked_by_section_is_used(self):
        self.issue(3, "Widget schema", state="closed")
        self.issue(4, "Widget API", body="## What to build\n\nNeeds 2 endpoints.\n\n## Blocked by\n\n- #3\n")
        self.issue(5, "Widget UI", body="## Blocked by\n\n- #3\n- #4\n\n## Notes\n\nSee #9.\n")
        self.issue(6, "Widget export", body="## Blocked by\n\nNone — can start immediately\n")
        self.afk("init", self.SPEC)

        output = self.afk("frontier")

        self.assertEqual(output.splitlines(), ["4  Widget API", "6  Widget export"])

    def test_a_repo_line_resolves_the_tickets_repo_and_its_absence_leaves_it_unresolved(self):
        self.issue(4, "Widget API", body="## What to build\n\nThe API.\n\n**Repo:** acme/widgets-api\n")
        self.issue(5, "Widget UI", body="Repo: acme/widgets-web\n")
        self.issue(6, "Widget export", body="## What to build\n\nExport to the repo's CSV format.\n")
        self.afk("init", self.SPEC)

        output = self.afk("frontier")

        self.assertEqual(
            output.splitlines(),
            ["4  Widget API  (acme/widgets-api)", "5  Widget UI  (acme/widgets-web)", "6  Widget export"],
        )


class GithubStartTest(GithubTestCase):
    def setUp(self):
        super().setUp()
        self.issue(4, "Widget API")
        self.afk("init", self.SPEC)

    def test_start_claims_the_ticket_by_assigning_it_before_setting_up_the_worker(self):
        self.afk("start", "4")

        claim = ["gh", "issue", "edit", "4", "--repo", "acme/widgets", "--add-assignee", "@me"]
        self.assertEqual(self.run_fake.find("gh", "issue", "edit"), [claim])
        [add] = self.run_fake.find("git", "-C", str(self.repo), "worktree", "add")
        self.assertLess(self.run_fake.calls.index(claim), self.run_fake.calls.index(add))

    def test_a_claimed_or_closed_ticket_cannot_be_started(self):
        self.issue(5, "Widget UI", assignees=["someone-else"])
        self.issue(6, "Widget export", state="closed")

        for number in ("5", "6"):
            out = io.StringIO()
            code = afk.main(["start", number], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)

            self.assertNotEqual(code, 0)
            self.assertIn("not open", out.getvalue())
        self.assertEqual(self.run_fake.find("gh", "issue", "edit"), [])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "worktree", "add"), [])

    def test_failed_start_releases_the_claim(self):
        def split_fails(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="no space for new pane")

        self.run_fake.responses[("tmux", "split-window")] = split_fails
        code = afk.main(["start", "4"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=io.StringIO())

        self.assertNotEqual(code, 0)
        self.assertEqual(
            self.run_fake.find("gh", "issue", "edit"),
            [
                ["gh", "issue", "edit", "4", "--repo", "acme/widgets", "--add-assignee", "@me"],
                ["gh", "issue", "edit", "4", "--repo", "acme/widgets", "--remove-assignee", "@me"],
            ],
        )

    def test_worker_is_pointed_at_the_issue_url(self):
        self.afk("start", "4")

        [send] = self.run_fake.find("tmux", "send-keys")
        self.assertIn("https://github.com/acme/widgets/issues/4", shlex.split(send[4])[-1])


class FrontierTest(AfkTestCase):
    def test_frontier_lists_open_tickets_whose_blockers_are_all_done(self):
        self.ticket("01-schema", "Widget schema", status="resolved")
        self.ticket("02-api", "Widget API", status="done", blocked_by="01")
        self.ticket("03-ui", "Widget UI", blocked_by="01, 02")
        self.ticket("04-search", "Widget search", blocked_by="03")
        self.ticket("05-export", "Widget export")
        self.ticket("06-import", "Widget import", status="claimed")
        self.afk("init", str(self.scratch))

        output = self.afk("frontier")

        self.assertEqual(output.splitlines(), ["03  Widget UI", "05  Widget export"])

    def test_numbers_in_blocker_prose_are_not_treated_as_tickets(self):
        self.ticket("01-auth", "Add 2FA login", status="done")
        self.ticket("02-keys", "Key rotation", blocked_by="None — needs 2 API keys")
        self.ticket("03-audit", "Audit log", blocked_by="01 — Add 2FA login")
        self.afk("init", str(self.scratch))

        output = self.afk("frontier")

        self.assertEqual(output.splitlines(), ["02  Key rotation", "03  Audit log"])


class StartTest(AfkTestCase):
    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI")
        self.afk("init", str(self.scratch))

    def launch_command(self):
        [send] = self.run_fake.find("tmux", "send-keys")
        self.assertEqual(send[:4], ["tmux", "send-keys", "-t", "%5"])
        self.assertEqual(send[-1], "Enter")
        return shlex.split(send[4])

    def test_start_creates_worktree_and_branch_named_from_ticket(self):
        self.afk("start", "03")

        [add] = self.run_fake.find("git", "-C", str(self.repo), "worktree", "add")
        self.assertEqual(add[5:7], ["-b", "afk/widgets-03"])
        self.assertEqual(add[7], str(self.project_dir / "worktrees" / "03"))

    def test_start_opens_named_window_split_agent_left_shell_right(self):
        self.afk("start", "03")

        worktree = str(self.project_dir / "worktrees" / "03")
        [window] = self.run_fake.find("tmux", "new-window")
        self.assertIn("-d", window)
        self.assertEqual(window[window.index("-t") + 1], "work:")
        self.assertEqual(window[window.index("-n") + 1], "03-widget-ui")
        self.assertEqual(window[window.index("-c") + 1], worktree)
        self.assertEqual(
            self.run_fake.find("tmux", "split-window"),
            [["tmux", "split-window", "-h", "-d", "-t", "%5", "-c", worktree, "-P", "-F", "#{pane_id}"]],
        )

    def test_start_launches_interactive_claude_in_auto_mode_with_generated_settings(self):
        self.afk("start", "03")

        launch = self.launch_command()
        self.assertIn("AFK_PROJECT=widgets", launch)
        self.assertIn("AFK_TICKET=03", launch)
        claude = launch[launch.index("claude") :]
        self.assertEqual(claude[1:3], ["--permission-mode", "auto"])
        self.assertEqual(claude[3], "--settings")
        self.assertTrue(Path(claude[4]).is_file())
        self.assertNotIn("--dangerously-skip-permissions", claude)
        self.assertNotIn("-p", claude)
        self.assertIn(str(self.scratch / "issues" / "03-ui.md"), claude[-1])
        self.assertIn('afk report', claude[-1])

    def test_worker_session_start_hook_firing_immediately_on_launch_is_recorded(self):
        def worker_starts(cmd):
            payload = {"session_id": "early", "transcript_path": "/t.jsonl", "hook_event_name": "SessionStart"}
            self.afk("hook", "session-start", stdin=json.dumps(payload), env={"AFK_PROJECT": "widgets", "AFK_TICKET": "03"})
            return ""

        self.run_fake.responses[("tmux", "send-keys")] = worker_starts
        self.afk("start", "03")

        status = json.loads((self.project_dir / "workers" / "03" / "status.json").read_text())
        self.assertEqual(status["session_id"], "early")
        self.assertEqual(status["state"], "working")

    def test_failed_start_rolls_back_window_worktree_and_branch(self):
        def split_fails(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="no space for new pane")

        self.run_fake.responses[("tmux", "split-window")] = split_fails
        out = io.StringIO()
        code = afk.main(["start", "03"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)

        self.assertNotEqual(code, 0)
        self.assertIn("no space for new pane", out.getvalue())
        worktree = str(self.project_dir / "worktrees" / "03")
        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [["tmux", "kill-window", "-t", "%5"]])
        self.assertEqual(
            self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove"),
            [["git", "-C", str(self.repo), "worktree", "remove", "--force", worktree]],
        )
        self.assertEqual(
            self.run_fake.find("git", "-C", str(self.repo), "branch"),
            [["git", "-C", str(self.repo), "branch", "-D", "afk/widgets-03"]],
        )
        self.assertFalse((self.project_dir / "workers" / "03").exists())

    def test_interrupted_start_rolls_back_and_can_be_retried(self):
        def interrupted(cmd):
            raise KeyboardInterrupt

        self.run_fake.responses[("tmux", "split-window")] = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.afk("start", "03")

        self.assertEqual(len(self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove")), 1)
        del self.run_fake.responses[("tmux", "split-window")]
        self.afk("start", "03")
        self.assertEqual(self.worker_state("03"), "working")

    def worker_state(self, ticket):
        return json.loads((self.project_dir / "workers" / ticket / "status.json").read_text())["state"]

    def test_starting_an_already_started_ticket_leaves_the_running_worker_alone(self):
        self.afk("start", "03")
        calls_before = len(self.run_fake.calls)

        out = io.StringIO()
        code = afk.main(["start", "03"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)

        self.assertNotEqual(code, 0)
        self.assertIn("already started", out.getvalue())
        later = self.run_fake.calls[calls_before:]
        self.assertFalse([c for c in later if c[0] == "git" or c[:2] in (["tmux", "new-window"], ["tmux", "kill-window"])])
        self.assertTrue((self.project_dir / "workers" / "03" / "status.json").is_file())

    def settings(self):
        self.afk("start", "03")
        launch = self.launch_command()
        return json.loads(Path(launch[launch.index("--settings") + 1]).read_text())

    def test_settings_deny_force_push_push_to_base_pr_merge_and_worktree_removal(self):
        deny = self.settings()["permissions"]["deny"]

        for rule in [
            "Bash(git push *--force*)",
            "Bash(git push * main)",
            "Bash(git push * HEAD:main)",
            "Bash(gh pr merge *)",
            "Bash(git worktree remove *)",
        ]:
            self.assertIn(rule, deny)

    def test_settings_wire_status_hooks_to_afk_hook(self):
        hooks = self.settings()["hooks"]

        for event, arg in [("SessionStart", "session-start"), ("Stop", "stop"), ("Notification", "notification")]:
            command = shlex.split(hooks[event][0]["hooks"][0]["command"])
            self.assertEqual(command[-2:], ["hook", arg])
            self.assertTrue(os.access(command[0], os.X_OK), command[0])


class RepoConfigStartTest(AfkTestCase):
    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI")
        self.afk("init", str(self.scratch))
        self.worktree = self.project_dir / "worktrees" / "03"

    def test_start_copies_configured_gitignored_files_into_the_worktree(self):
        (self.repo / ".env").write_text("SECRET=1\n")
        (self.repo / "config").mkdir()
        (self.repo / "config" / "local.yml").write_text("db: dev\n")
        self.repo_config('copy = [".env", "config/local.yml"]')

        self.afk("start", "03")

        self.assertEqual((self.worktree / ".env").read_text(), "SECRET=1\n")
        self.assertEqual((self.worktree / "config" / "local.yml").read_text(), "db: dev\n")

    def test_worktree_branches_from_the_repos_base_branch_which_workers_may_not_push_to(self):
        self.repo_config('base = "develop"')

        self.afk("start", "03")

        [add] = self.run_fake.find("git", "-C", str(self.repo), "worktree", "add")
        self.assertEqual(add[-1], "develop")
        [send] = self.run_fake.find("tmux", "send-keys")
        launch = shlex.split(send[4])
        deny = json.loads(Path(launch[launch.index("--settings") + 1]).read_text())["permissions"]["deny"]
        self.assertIn("Bash(git push * develop)", deny)
        self.assertNotIn("Bash(git push * main)", deny)

    def bootstraps(self):
        """(shell command, cwd) of each bootstrap command run so far."""
        return [(cmd[-1], cwd) for cmd, cwd in zip(self.run_fake.calls, self.run_fake.cwds) if cmd[-3:-1] == ["sh", "-c"]]

    def test_start_runs_bootstrap_commands_in_order_in_the_worktree(self):
        self.repo_config('bootstrap = ["npm ci", "make db"]')

        self.afk("start", "03")

        self.assertEqual(self.bootstraps(), [("npm ci", str(self.worktree)), ("make db", str(self.worktree))])

    def test_failed_bootstrap_rolls_back_the_start(self):
        def npm_fails(cmd):
            if cmd[-1] == "npm ci":
                raise subprocess.CalledProcessError(1, cmd, stderr="npm ERR! missing lockfile")
            return ""

        self.run_fake.responses[("sh",)] = npm_fails
        self.run_fake.responses[("env",)] = npm_fails
        self.repo_config('bootstrap = ["npm ci"]')
        out = io.StringIO()

        code = afk.main(["start", "03"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)

        self.assertNotEqual(code, 0)
        self.assertIn("missing lockfile", out.getvalue())
        self.assertEqual(len(self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove")), 1)
        self.assertEqual(self.run_fake.find("tmux", "send-keys"), [])
        self.assertFalse((self.project_dir / "workers" / "03").exists())


TASK_TYPES = """
default_type = "backend"

[task_types.backend]
agent = "claude"
model = "opus"
effort = "high"
skill = "/tdd"

[task_types.frontend]
agent = "claude"
model = "sonnet"
effort = "medium"
prompt = "docs/agents/prompts/frontend.md"
"""


class TaskTypeTest(AfkTestCase):
    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI", type="frontend")
        self.ticket("05-export", "Widget export")
        self.afk("init", str(self.scratch))
        self.repo_config(TASK_TYPES)
        prompts = self.repo / "docs" / "agents" / "prompts"
        prompts.mkdir()
        (prompts / "frontend.md").write_text("Match the Figma design linked in {{ticket_path}}.\n")

    def claude(self):
        [send] = self.run_fake.find("tmux", "send-keys")
        launch = shlex.split(send[4])
        return launch[launch.index("claude") :]

    def flag(self, claude, name):
        return claude[claude.index(name) + 1]

    def test_tickets_type_selects_model_effort_and_prompt_template(self):
        self.afk("start", "03")

        claude = self.claude()
        self.assertEqual(self.flag(claude, "--model"), "sonnet")
        self.assertEqual(self.flag(claude, "--effort"), "medium")
        prompt = claude[-1]
        self.assertTrue(prompt.startswith("AFK phase: implement"), prompt)
        self.assertIn(f"Match the Figma design linked in {self.scratch / 'issues' / '03-ui.md'}.", prompt)

    def test_untyped_ticket_uses_the_default_type_and_invokes_its_skill(self):
        self.afk("start", "05")

        claude = self.claude()
        self.assertEqual(self.flag(claude, "--model"), "opus")
        self.assertEqual(self.flag(claude, "--effort"), "high")
        self.assertTrue(claude[-1].startswith("/tdd AFK phase: implement"), claude[-1])

    def test_type_flag_overrides_the_tickets_type(self):
        self.afk("start", "03", "--type", "backend")

        self.assertEqual(self.flag(self.claude(), "--model"), "opus")

    def test_local_override_file_replaces_just_the_fields_it_sets(self):
        self.repo_config('[task_types.frontend]\nmodel = "opus"', name="afk.local.md")

        self.afk("start", "03")

        claude = self.claude()
        self.assertEqual(self.flag(claude, "--model"), "opus")
        self.assertEqual(self.flag(claude, "--effort"), "medium")

    def test_project_config_overrides_win_over_the_local_override_file(self):
        self.repo_config('[task_types.frontend]\nmodel = "opus"\neffort = "low"', name="afk.local.md")
        with open(self.project_dir / "config.toml", "a") as config:
            config.write('\n[overrides.task_types.frontend]\nmodel = "haiku"\n')

        self.afk("start", "03")

        claude = self.claude()
        self.assertEqual(self.flag(claude, "--model"), "haiku")
        self.assertEqual(self.flag(claude, "--effort"), "low")

    def start_fails(self, *argv):
        out = io.StringIO()
        code = afk.main(["start", *argv], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.run_fake.find("git"), [])
        self.assertEqual(self.run_fake.find("tmux", "new-window"), [])
        self.assertFalse((self.project_dir / "workers" / argv[0]).exists())
        return out.getvalue()

    def test_unknown_task_type_fails_before_creating_anything(self):
        out = self.start_fails("03", "--type", "infra")

        self.assertIn("infra", out)
        self.assertIn("backend, frontend", out)

    def test_malformed_options_fail_with_usage_before_creating_anything(self):
        for options in (["--type"], ["--typ", "backend"], ["--type", "backend", "extra"]):
            with self.subTest(options=options):
                self.assertIn("usage: afk start <ticket> [--type <type>]", self.start_fails("03", *options))

    def test_unsupported_agent_fails_before_creating_anything(self):
        self.repo_config('[task_types.frontend]\nagent = "grok"', name="afk.local.md")

        out = self.start_fails("03")

        self.assertIn("grok", out)

    def test_without_task_types_claude_launches_with_its_own_defaults(self):
        self.repo_config("")

        self.afk("start", "05")

        claude = self.claude()
        self.assertNotIn("--model", claude)
        self.assertNotIn("--effort", claude)
        self.assertTrue(claude[-1].startswith("AFK phase: implement"))


class SlotTest(AfkTestCase):
    def setUp(self):
        super().setUp()
        for name, title in [("03-ui", "Widget UI"), ("05-export", "Widget export"), ("07-import", "Widget import")]:
            self.ticket(name, title)
        self.afk("init", str(self.scratch))
        self.repo_config('port_base = 5000\nbootstrap = ["make db"]')
        panes = iter(["%5", "%6", "%7", "%8"])
        self.run_fake.responses[("tmux", "new-window")] = lambda cmd: next(panes) + "\n"

    def slot_env(self, cmd):
        return [arg for arg in cmd if arg.startswith(("AFK_SLOT=", "AFK_PORT_BASE="))]

    def launches(self):
        return [self.slot_env(shlex.split(c[4])) for c in self.run_fake.find("tmux", "send-keys") if len(c) > 5]

    def test_each_worker_gets_its_own_slot_and_port_base(self):
        self.afk("start", "03")
        self.afk("start", "05")

        self.assertEqual(
            self.launches(),
            [["AFK_SLOT=1", "AFK_PORT_BASE=5100"], ["AFK_SLOT=2", "AFK_PORT_BASE=5200"]],
        )

    def test_bootstrap_sees_the_workers_slot(self):
        self.afk("start", "03")

        [bootstrap] = [c for c in self.run_fake.calls if c[-1] == "make db"]
        self.assertEqual(self.slot_env(bootstrap), ["AFK_SLOT=1", "AFK_PORT_BASE=5100"])

    def test_slot_of_a_rolled_back_start_is_reused(self):
        self.afk("start", "03")
        self.run_fake.responses[("tmux", "split-window")] = lambda cmd: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, cmd, stderr="no space for new pane")
        )
        afk.main(["start", "05"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=io.StringIO())
        del self.run_fake.responses[("tmux", "split-window")]

        self.afk("start", "07")

        self.assertEqual(self.launches()[-1], ["AFK_SLOT=2", "AFK_PORT_BASE=5200"])

    def test_port_base_defaults_to_4000(self):
        self.repo_config("")

        self.afk("start", "03")

        self.assertEqual(self.launches(), [["AFK_SLOT=1", "AFK_PORT_BASE=4100"]])


class VerifyQueueTest(AfkTestCase):
    PANES = {"03": "%5", "05": "%6", "07": "%7"}

    def setUp(self):
        super().setUp()
        for name, title in [("03-ui", "Widget UI"), ("05-export", "Widget export"), ("07-import", "Widget import")]:
            self.ticket(name, title)
        self.afk("init", str(self.scratch))
        self.repo_config("verify_concurrency = 1")
        panes = iter(self.PANES.values())
        self.run_fake.responses[("tmux", "new-window")] = lambda cmd: next(panes) + "\n"
        for ticket in self.PANES:
            self.afk("start", ticket)

    def finish(self, ticket):
        env = {"AFK_PROJECT": "widgets", "AFK_TICKET": ticket}
        self.afk("report", "done", "Done", env=env)
        self.afk("hook", "stop", stdin=json.dumps({"hook_event_name": "Stop"}), env=env)
        self.now += 60

    def state(self, ticket):
        status = json.loads((self.project_dir / "workers" / ticket / "status.json").read_text())
        return status["phase"], status["state"]

    def verify_prompted(self, ticket):
        pane = self.PANES[ticket]
        pastes = [c for c in self.run_fake.find("tmux", "paste-buffer") if c[c.index("-t") + 1] == pane]
        buffers = [i for c, i in zip(self.run_fake.calls, self.run_fake.inputs) if c[:2] == ["tmux", "load-buffer"]]
        return len(pastes) > 0 and any(b.startswith(f"AFK phase: verify — ticket {ticket}") for b in buffers)

    def test_second_worker_is_queued_while_another_verifies(self):
        self.finish("03")
        self.afk("tick")
        self.finish("05")

        self.afk("tick")

        self.assertEqual(self.state("03"), ("verify", "working"))
        self.assertEqual(self.state("05"), ("implement", "queued"))
        self.assertFalse(self.verify_prompted("05"))

    def test_queued_worker_starts_verifying_once_the_slot_frees(self):
        self.finish("03")
        self.afk("tick")
        self.finish("05")
        self.afk("tick")

        self.finish("03")
        self.afk("tick")
        self.afk("tick")

        self.assertEqual(self.state("03"), ("prepr", "working"))
        self.assertEqual(self.state("05"), ("verify", "working"))
        self.assertTrue(self.verify_prompted("05"))

    def test_workers_finishing_in_the_same_tick_verify_one_at_a_time(self):
        self.finish("03")
        self.finish("05")

        self.afk("tick")

        self.assertEqual([self.state("03")[1], self.state("05")[1]], ["working", "queued"])

    def test_queue_is_first_come_first_served(self):
        self.finish("03")
        self.afk("tick")
        self.finish("07")
        self.afk("tick")
        self.finish("05")
        self.afk("tick")

        self.finish("03")
        self.afk("tick")
        self.afk("tick")

        self.assertEqual(self.state("07"), ("verify", "working"))
        self.assertEqual(self.state("05"), ("implement", "queued"))

    def test_queued_worker_whose_agent_exited_needs_attention_without_skipping_verify(self):
        self.finish("03")
        self.afk("tick")
        self.finish("05")
        self.afk("tick")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t")] = lambda cmd: "claude\n" if cmd[4] == "%5" else "bash\n"

        self.finish("03")
        self.afk("tick")

        self.assertEqual(self.state("05"), ("implement", "attention"))
        self.assertFalse(self.verify_prompted("05"))

    def test_overlapping_ticks_still_admit_one_verifier(self):
        self.finish("05")
        self.finish("07")
        results = []

        def second_tick():
            out = io.StringIO()
            try:
                results.append(afk.main(["tick"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out, clock=lambda: self.now))
            except Exception as error:
                results.append(error)

        other = threading.Thread(target=second_tick)

        def another_tick_starts_mid_paste(cmd):
            if cmd[cmd.index("-t") + 1] == "%6" and not other.is_alive() and not results:
                other.start()
                other.join(timeout=0.2)  # without a tick-wide lock it snapshots the state before this tick writes it
            return ""

        self.run_fake.responses[("tmux", "paste-buffer")] = another_tick_starts_mid_paste
        self.afk("tick")
        other.join()

        self.assertEqual(results, [0])
        self.assertEqual([self.state("05"), self.state("07")], [("verify", "working"), ("implement", "queued")])
        self.assertFalse(self.verify_prompted("07"))

    def test_without_verify_concurrency_workers_verify_in_parallel(self):
        self.repo_config("")
        self.finish("03")
        self.finish("05")

        self.afk("tick")

        self.assertEqual([self.state("03"), self.state("05")], [("verify", "working")] * 2)


class WorkerStatusTest(AfkTestCase):
    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI")
        self.ticket("05-export", "Widget export")
        self.afk("init", str(self.scratch))
        self.afk("start", "03")
        self.afk("start", "05")

    def worker_env(self, ticket):
        return {"AFK_PROJECT": "widgets", "AFK_TICKET": ticket}

    def status_rows(self):
        return [line.split(None, 3) for line in self.afk("status").splitlines()[1:]]

    def test_status_shows_each_started_worker_implementing(self):
        self.assertEqual(
            self.status_rows(),
            [["03", "implement", "working"], ["05", "implement", "working"]],
        )

    def test_report_from_worker_shows_in_status(self):
        self.afk("report", "question", "Which database?", env=self.worker_env("03"))
        self.afk("report", "done", "Export works", env=self.worker_env("05"))

        self.assertEqual(
            self.status_rows(),
            [["03", "implement", "question", "Which database?"], ["05", "implement", "done", "Export works"]],
        )

    def test_report_rejects_unknown_state(self):
        out = io.StringIO()
        code = afk.main(
            ["report", "finished", "hi"],
            run=self.run_fake,
            env={**self.env, **self.worker_env("03")},
            stdin=io.StringIO(),
            stdout=out,
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(self.status_rows()[0], ["03", "implement", "working"])

    def status_file(self, ticket):
        return json.loads((self.project_dir / "workers" / ticket / "status.json").read_text())

    def test_hooks_record_session_id_transcript_and_event_in_status_file(self):
        for event, name in [("session-start", "SessionStart"), ("notification", "Notification"), ("stop", "Stop")]:
            payload = {
                "session_id": "sess-123",
                "transcript_path": "/home/u/.claude/projects/x/sess-123.jsonl",
                "hook_event_name": name,
            }
            self.afk("hook", event, stdin=json.dumps(payload), env=self.worker_env("03"))

            status = self.status_file("03")
            self.assertEqual(status["session_id"], "sess-123")
            self.assertEqual(status["transcript_path"], "/home/u/.claude/projects/x/sess-123.jsonl")
            self.assertEqual(status["last_event"], name)
        self.assertNotIn("session_id", self.status_file("05"))

    def test_hook_keeps_reported_state(self):
        self.afk("report", "done", "Finished", env=self.worker_env("03"))
        self.afk("hook", "stop", stdin=json.dumps({"session_id": "s", "transcript_path": "/t"}), env=self.worker_env("03"))

        self.assertEqual(self.status_rows()[0], ["03", "implement", "done", "Finished"])


class WatchTest(AfkTestCase):
    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI")
        self.afk("init", str(self.scratch))
        self.run_fake.responses[("tmux", "split-window")] = "%6\n"  # the worker's shell pane
        self.afk("start", "03")
        self.calls_before = len(self.run_fake.calls)

    def worker_env(self, ticket="03"):
        return {"AFK_PROJECT": "widgets", "AFK_TICKET": ticket}

    def report(self, state, message="", ticket="03"):
        self.afk("report", state, message, env=self.worker_env(ticket))

    def stop(self, ticket="03"):
        payload = {"session_id": "s", "transcript_path": "/t", "hook_event_name": "Stop"}
        self.afk("hook", "stop", stdin=json.dumps(payload), env=self.worker_env(ticket))

    def prompts_sent(self, pane="%5"):
        """Prompts pasted into a pane and submitted since setUp, in order."""
        calls = list(zip(self.run_fake.calls, self.run_fake.inputs))[self.calls_before :]
        prompts, buffers = [], {}
        for cmd, stdin in calls:
            if cmd[:2] == ["tmux", "load-buffer"]:
                buffers[cmd[cmd.index("-b") + 1]] = stdin
            elif cmd[:2] == ["tmux", "paste-buffer"] and cmd[cmd.index("-t") + 1] == pane:
                self.assertIn("-p", cmd)  # bracketed paste, so newlines don't submit early
                prompts.append(buffers[cmd[cmd.index("-b") + 1]])
            elif cmd[:2] == ["tmux", "send-keys"] and cmd[cmd.index("-t") + 1] == pane:
                self.assertEqual(cmd[-1], "Enter")
        return prompts

    def phase(self, ticket="03"):
        return json.loads((self.project_dir / "workers" / ticket / "status.json").read_text())["phase"]

    def status(self, ticket="03"):
        return json.loads((self.project_dir / "workers" / ticket / "status.json").read_text())

    def finish_phase(self, message="Done"):
        self.report("done", message)
        self.stop()
        self.afk("tick")

    def test_done_report_then_stop_advances_to_verify_and_sends_its_prompt(self):
        self.report("done", "Implemented")
        self.stop()

        self.afk("tick")

        self.assertEqual(self.phase(), "verify")
        [prompt] = self.prompts_sent()
        self.assertTrue(prompt.startswith("AFK phase: verify"), prompt)
        self.assertIn(str(self.scratch / "issues" / "03-ui.md"), prompt)

    def test_done_report_while_worker_is_still_busy_sends_nothing(self):
        self.report("done", "Implemented")

        self.afk("tick")

        self.assertEqual(self.phase(), "implement")
        self.assertEqual(self.prompts_sent(), [])

    def test_stop_left_over_from_before_the_report_does_not_count_as_idle(self):
        self.stop()
        self.report("done", "Implemented")

        self.afk("tick")

        self.assertEqual(self.prompts_sent(), [])

    def test_each_done_phase_advances_to_the_next_until_review(self):
        for _ in range(4):
            self.finish_phase()

        self.assertEqual(self.phase(), "review")
        self.assertEqual(self.status()["state"], "review")
        self.assertEqual(
            [p.splitlines()[0].split(" — ")[0] for p in self.prompts_sent()],
            ["AFK phase: verify", "AFK phase: prepr", "AFK phase: pr", "AFK phase: review"],
        )

    def desktop_notifications(self):
        return [" ".join(c[1:]) for c in self.run_fake.calls[self.calls_before :] if c[0] == "notify-send"]

    def test_question_and_blocked_reports_notify_once_without_advancing(self):
        for state, message in [("question", "Which database?"), ("blocked", "CI is down")]:
            with self.subTest(state):
                self.report(state, message)
                self.stop()

                self.afk("tick")
                self.afk("tick")

                self.assertEqual(self.phase(), "implement")
                self.assertEqual(self.prompts_sent(), [])
                [notification] = [n for n in self.desktop_notifications() if message in n]
                self.assertIn("03", notification)
                self.assertIn(state, notification)

    def test_same_question_asked_again_after_an_answer_notifies_again(self):
        self.report("question", "Which database?")
        self.stop()
        self.afk("tick")
        self.report("question", "Which database?")
        self.stop()

        self.afk("tick")

        self.assertEqual(len(self.desktop_notifications()), 2)

    def reach_review(self, pr="https://github.com/acme/widgets/pull/42"):
        for message in ["Implemented", "Verified", "Checks pass", pr]:
            self.finish_phase(message)

    def test_reaching_review_notifies_with_the_pr(self):
        self.reach_review()

        [notification] = self.desktop_notifications()
        self.assertIn("03", notification)
        self.assertIn("https://github.com/acme/widgets/pull/42", notification)

    def test_done_after_review_feedback_stays_in_review_and_notifies(self):
        self.reach_review()
        prompts = len(self.prompts_sent())

        self.finish_phase("Renamed the endpoint as asked")
        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("review", "review"))
        self.assertEqual(len(self.prompts_sent()), prompts)
        self.assertEqual(len(self.desktop_notifications()), 2)
        self.assertIn("Renamed the endpoint as asked", self.desktop_notifications()[-1])

    def test_stop_without_a_report_needs_attention_and_notifies_once(self):
        self.stop()

        self.afk("tick")
        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("implement", "attention"))
        [notification] = self.desktop_notifications()
        self.assertIn("03", notification)
        self.assertIn("attention", notification)

    def test_stop_without_a_report_after_a_new_phase_prompt_needs_attention(self):
        self.finish_phase()

        self.stop()
        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("verify", "attention"))

    def test_standing_by_in_review_does_not_need_attention(self):
        self.reach_review()

        self.stop()
        self.afk("tick")

        self.assertEqual(self.status()["state"], "review")

    def test_worker_that_needed_attention_can_report_done_and_move_on(self):
        self.stop()
        self.afk("tick")

        self.finish_phase()

        self.assertEqual((self.phase(), self.status()["state"]), ("verify", "working"))

    def test_tick_renders_dashboard_of_each_tickets_phase_state_time_in_phase_and_pr(self):
        self.ticket("05-export", "Widget export")
        self.afk("start", "05")
        self.now += 60
        self.reach_review()
        self.now += 3600 + 300

        dashboard = self.afk("tick").splitlines()

        self.assertEqual(dashboard[0].split(), ["TICKET", "PHASE", "STATE", "ELAPSED", "PR"])
        self.assertEqual(
            [line.split() for line in dashboard[1:]],
            [
                ["03", "review", "review", "1h05m", "https://github.com/acme/widgets/pull/42"],
                ["05", "implement", "working", "1h06m", "-"],
            ],
        )

    def test_dashboard_shows_short_elapsed_times_in_minutes_and_seconds(self):
        self.now += 45
        self.assertEqual(self.afk("tick").splitlines()[1].split()[3], "45s")
        self.now += 12 * 60
        self.assertEqual(self.afk("tick").splitlines()[1].split()[3], "12m")

    def window(self, pane="%5"):
        """The worker window's last applied name and @afk_state since setUp."""
        name = state = None
        for cmd in self.run_fake.calls[self.calls_before :]:
            if cmd[:2] == ["tmux", "rename-window"] and cmd[cmd.index("-t") + 1] == pane:
                name = cmd[-1]
            if cmd[:2] == ["tmux", "set-option"] and "@afk_state" in cmd and cmd[cmd.index("-t") + 1] == pane:
                self.assertIn("-w", cmd)
                state = cmd[-1]
        return name, state

    def test_window_name_and_state_option_follow_the_worker(self):
        self.report("question", "Which database?")
        self.stop()
        self.afk("tick")
        self.assertEqual(self.window(), ("03-widget-ui?", "question"))

        self.finish_phase()
        self.assertEqual(self.window(), ("03-widget-ui", "working"))

        self.stop()
        self.afk("tick")
        self.assertEqual(self.window(), ("03-widget-ui!", "attention"))

        for message in ["Verified", "Checks pass", "https://github.com/acme/widgets/pull/42"]:
            self.finish_phase(message)
        self.assertEqual(self.window(), ("03-widget-ui✓", "review"))

    def test_window_is_only_updated_when_the_state_changes(self):
        self.report("blocked", "CI is down")
        self.stop()
        self.afk("tick")
        renames = len(self.run_fake.find("tmux", "rename-window"))

        self.afk("tick")

        self.assertEqual(len(self.run_fake.find("tmux", "rename-window")), renames)

    def test_notify_rings_the_bell_and_shows_a_tmux_message_as_well_as_a_desktop_notification(self):
        self.report("question", "Which database?")
        self.stop()

        output = self.afk("tick")

        self.assertIn("\a", output)
        [message] = [c for c in self.run_fake.find("tmux", "display-message") if "Which database?" in c[-1]]
        self.assertEqual(message[message.index("-t") + 1], "work:")
        self.assertEqual(len(self.desktop_notifications()), 1)

    def test_quiet_ticks_do_not_ring_the_bell(self):
        self.assertNotIn("\a", self.afk("tick"))

    def test_a_failing_notification_backend_does_not_stop_the_watcher(self):
        def missing(cmd):
            raise FileNotFoundError(2, "No such file or directory", "notify-send")

        self.run_fake.responses[("notify-send",)] = missing
        self.report("question", "Which database?")
        self.stop()

        self.afk("tick")

        self.assertTrue([c for c in self.run_fake.find("tmux", "display-message") if "Which database?" in c[-1]])
        self.afk("tick")
        self.assertEqual(len(self.desktop_notifications()), 1)

    def test_a_prompt_that_fails_to_send_does_not_resend_other_workers_prompts(self):
        self.ticket("05-export", "Widget export")
        self.run_fake.responses[("tmux", "new-window")] = "%7\n"
        self.afk("start", "05")
        self.calls_before = len(self.run_fake.calls)
        for ticket in ("03", "05"):
            self.report("done", "Implemented", ticket=ticket)
            self.stop(ticket=ticket)

        def pane_gone(cmd):
            if "%7" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr="can't find pane: %7")
            return ""

        self.run_fake.responses[("tmux", "paste-buffer")] = pane_gone
        out = io.StringIO()
        afk.main(["tick"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out, clock=lambda: self.now)
        del self.run_fake.responses[("tmux", "paste-buffer")]

        self.afk("tick")

        self.assertEqual(len(self.prompts_sent("%5")), 1)
        self.assertEqual(len(self.prompts_sent("%7")), 2)  # the failed paste, then its retry
        self.assertEqual((self.phase("03"), self.phase("05")), ("verify", "verify"))

    def test_a_worker_whose_pane_is_gone_does_not_hold_up_the_others(self):
        self.ticket("05-export", "Widget export")
        self.run_fake.responses[("tmux", "new-window")] = "%7\n"
        self.afk("start", "05")
        for ticket in ("03", "05"):
            self.report("done", "Implemented", ticket=ticket)
            self.stop(ticket=ticket)

        def pane_gone(cmd):
            if "%5" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr="can't find pane: %5")
            return ""

        self.run_fake.responses[("tmux", "paste-buffer")] = pane_gone
        out = io.StringIO()
        code = afk.main(["tick"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out, clock=lambda: self.now)

        self.assertNotEqual(code, 0)
        self.assertIn("can't find pane: %5", out.getvalue())
        self.assertEqual((self.phase("03"), self.phase("05")), ("implement", "verify"))

    def test_a_report_arriving_while_the_tick_prompts_that_worker_is_not_lost(self):
        self.report("done", "Implemented")
        self.stop()
        reporter = threading.Thread(target=self.report, args=("question", "Which database?"))

        def worker_reports_mid_paste(cmd):
            reporter.start()
            reporter.join(timeout=0.2)  # without a lock it lands now, and the tick's write would clobber it
            return ""

        self.run_fake.responses[("tmux", "paste-buffer")] = worker_reports_mid_paste
        self.afk("tick")
        reporter.join()

        self.assertEqual((self.phase(), self.status()["state"]), ("verify", "question"))

    def test_a_failing_window_update_does_not_resend_the_phase_prompt(self):
        def rename_fails(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="server busy")

        self.run_fake.responses[("tmux", "rename-window")] = rename_fails
        self.report("done", "Implemented")
        self.stop()

        self.afk("tick")
        self.afk("tick")

        self.assertEqual(self.phase(), "verify")
        self.assertEqual(len(self.prompts_sent()), 1)

    def test_next_phase_is_not_pasted_into_a_pane_where_claude_has_exited(self):
        self.run_fake.responses[("tmux", "display-message", "-p", "-t")] = "bash\n"
        self.report("done", "Implemented")
        self.stop()

        self.afk("tick")

        self.assertEqual(self.prompts_sent(), [])
        self.assertEqual((self.phase(), self.status()["state"]), ("implement", "attention"))
        [notification] = self.desktop_notifications()
        self.assertIn("03", notification)
        self.assertIn("claude", notification)

    def test_a_killed_agent_pane_needs_attention_instead_of_failing_every_tick(self):
        def pane_gone(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="can't find pane: %5")

        self.run_fake.responses[("tmux", "display-message", "-p", "-t")] = pane_gone
        self.report("done", "Implemented")
        self.stop()

        self.afk("tick")
        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("implement", "attention"))
        self.assertEqual(len(self.desktop_notifications()), 1)


    # Merge detection and cleanup

    def pr_polls(self):
        return self.run_fake.find("gh", "pr", "view")

    def test_an_open_pr_in_review_is_polled_with_gh_and_stays_in_review(self):
        self.reach_review()
        self.run_fake.responses[("gh", "pr", "view")] = "OPEN\n"

        self.afk("tick")

        self.assertEqual(self.pr_polls(), [["gh", "pr", "view", "https://github.com/acme/widgets/pull/42", "--json", "state", "-q", ".state"]])
        self.assertEqual((self.phase(), self.status()["state"]), ("review", "review"))

    def test_a_pr_is_polled_at_most_once_a_minute(self):
        self.reach_review()

        self.afk("tick")
        self.now += 30
        self.afk("tick")
        self.assertEqual(len(self.pr_polls()), 1)

        self.now += 30
        self.afk("tick")
        self.assertEqual(len(self.pr_polls()), 2)

    def merge(self, shell="bash", worktree_status=""):
        """GitHub reports the PR merged; the shell pane runs `shell` and the worktree has `worktree_status` changes."""
        worktree = str(self.project_dir / "worktrees" / "03")
        self.run_fake.responses[("gh", "pr", "view")] = "MERGED\n"
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = shell + "\n"
        self.run_fake.responses[("git", "-C", worktree, "status")] = worktree_status
        self.now += 60

    def cleanup_commands(self):
        worktree = str(self.project_dir / "worktrees" / "03")
        return (
            self.run_fake.find("tmux", "kill-window"),
            self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove"),
            self.run_fake.find("git", "-C", str(self.repo), "branch", "-D"),
        )

    def test_merged_pr_with_idle_shell_and_clean_worktree_removes_window_worktree_and_branch(self):
        self.reach_review()
        self.merge()

        dashboard = self.afk("tick").splitlines()

        worktree = str(self.project_dir / "worktrees" / "03")
        self.assertEqual(
            self.cleanup_commands(),
            (
                [["tmux", "kill-window", "-t", "%5"]],
                [["git", "-C", str(self.repo), "worktree", "remove", worktree]],
                [["git", "-C", str(self.repo), "branch", "-D", "afk/widgets-03"]],
            ),
        )
        self.assertEqual(dashboard[1:], [])
        self.assertEqual(self.afk("status").splitlines()[1:], [])
        # afk only reads PRs: it never merges, requests reviewers or otherwise acts on GitHub.
        self.assertEqual({tuple(c[:3]) for c in self.run_fake.find("gh")}, {("gh", "pr", "view")})

    def test_cleaning_up_frees_the_workers_slot_for_the_next_ticket(self):
        self.reach_review()
        self.merge()
        self.afk("tick")
        self.ticket("05-export", "Widget export")

        self.afk("start", "05")

        launch = shlex.split(self.run_fake.find("tmux", "send-keys")[-1][4])
        self.assertIn("AFK_SLOT=1", launch)

    def test_merged_pr_with_a_busy_shell_pane_is_left_cleanup_pending_and_notifies_once(self):
        self.reach_review()
        self.merge(shell="vim")

        self.afk("tick")
        self.afk("tick")

        self.assertEqual(self.cleanup_commands(), ([], [], []))
        self.assertEqual(self.status()["state"], "cleanup-pending")
        notification = self.desktop_notifications()[-1]
        self.assertIn("03", notification)
        self.assertIn("vim", notification)
        self.assertEqual(len(self.desktop_notifications()), 2)  # PR ready, then cleanup pending

    def test_merged_pr_with_uncommitted_changes_is_left_cleanup_pending_and_notifies(self):
        self.reach_review()
        self.merge(worktree_status=" M src/widget.py\n")

        self.afk("tick")

        self.assertEqual(self.cleanup_commands(), ([], [], []))
        self.assertEqual(self.status()["state"], "cleanup-pending")
        self.assertIn("uncommitted", self.desktop_notifications()[-1])

    def test_merged_pr_whose_shell_pane_is_gone_is_left_cleanup_pending(self):
        self.reach_review()
        self.merge()

        def pane_gone(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="can't find pane: %6")

        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = pane_gone

        self.afk("tick")

        self.assertEqual(self.cleanup_commands(), ([], [], []))
        self.assertEqual(self.status()["state"], "cleanup-pending")
        self.assertIn("shell pane is gone", self.desktop_notifications()[-1])

    def test_cleanup_pending_worker_is_cleaned_up_once_the_shell_is_idle_again(self):
        self.reach_review()
        self.merge(shell="vim")
        self.afk("tick")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"

        self.afk("tick")

        self.assertEqual([len(c) for c in self.cleanup_commands()], [1, 1, 1])
        self.assertEqual(self.afk("status").splitlines()[1:], [])

    def test_cleaning_up_a_merged_worker_prompts_for_the_next_batch(self):
        self.reach_review()
        self.merge()

        self.afk("tick")

        notification = self.desktop_notifications()[-1]
        self.assertIn("03 merged", notification)
        self.assertIn("/afk next", notification)

    def test_a_failing_pr_poll_does_not_hold_up_the_other_workers(self):
        self.reach_review()
        self.ticket("05-export", "Widget export")
        self.run_fake.responses[("tmux", "new-window")] = "%7\n"
        self.afk("start", "05")
        self.report("done", "Implemented", ticket="05")
        self.stop(ticket="05")

        def offline(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="error connecting to api.github.com")

        self.run_fake.responses[("gh", "pr", "view")] = offline
        self.now += 60
        out = io.StringIO()
        code = afk.main(["tick"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out, clock=lambda: self.now)

        self.assertNotEqual(code, 0)
        self.assertIn("api.github.com", out.getvalue())
        self.assertEqual((self.phase("03"), self.phase("05")), ("review", "verify"))

    def test_merged_worker_started_before_shell_panes_were_recorded_is_left_cleanup_pending(self):
        self.reach_review()
        path = self.project_dir / "workers" / "03" / "status.json"
        status = self.status()
        del status["shell_pane"]
        path.write_text(json.dumps(status))
        self.merge()

        self.afk("tick")

        self.assertEqual(self.cleanup_commands(), ([], [], []))
        self.assertEqual(self.status()["state"], "cleanup-pending")
        self.assertIn("shell pane is unknown", self.desktop_notifications()[-1])

if __name__ == "__main__":
    unittest.main()
