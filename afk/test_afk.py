import io
import json
import os
import shlex
import subprocess
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
        self.inputs = []

    def __call__(self, cmd, input=None):
        self.calls.append(list(cmd))
        self.inputs.append(input)
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


if __name__ == "__main__":
    unittest.main()
