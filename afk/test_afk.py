import io
import json
import os
import shlex
import tempfile
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

    def __call__(self, cmd, input=None):
        self.calls.append(list(cmd))
        matches = [p for p in self.responses if tuple(cmd[: len(p)]) == p]
        return self.responses[max(matches, key=len)] if matches else ""

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
            }
        )
        self.env = {"XDG_STATE_HOME": str(self.state_home), "HOME": str(self.tmp)}

    def afk(self, *argv, stdin="", env=None):
        out = io.StringIO()
        code = afk.main(
            list(argv),
            run=self.run_fake,
            env={**self.env, **(env or {})},
            stdin=io.StringIO(stdin),
            stdout=out,
        )
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue()

    def ticket(self, name, title, status="ready-for-agent", blocked_by="None — can start immediately"):
        number = name.split("-", 1)[0]
        (self.scratch / "issues" / f"{name}.md").write_text(
            f"# {number} — {title}\n\n"
            f"**What to build:** {title}.\n\n"
            f"**Blocked by:** {blocked_by}\n\n"
            f"**Status:** {status}\n\n"
            "- [ ] It works\n"
        )

    @property
    def project_dir(self):
        return self.state_home / "afk" / "widgets"


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
            [["tmux", "split-window", "-h", "-d", "-t", "%5", "-c", worktree]],
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


if __name__ == "__main__":
    unittest.main()
