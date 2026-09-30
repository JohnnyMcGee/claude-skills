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


if __name__ == "__main__":
    unittest.main()
