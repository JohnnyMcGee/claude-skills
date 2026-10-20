import io
import json
import tempfile
import unittest
from pathlib import Path

import process


class FakeRun:
    """Stands in for the run() seam: records every command (and its cwd) and replays scripted stdout."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []
        self.cwds = []

    def __call__(self, cmd, input=None, cwd=None):
        self.calls.append(list(cmd))
        self.cwds.append(cwd and str(cwd))
        matches = [p for p in self.responses if tuple(cmd[: len(p)]) == p]
        if not matches:
            return ""
        response = self.responses[max(matches, key=len)]
        return response(cmd) if callable(response) else response


# 2026-10-20T13:00:00Z, 08:00 in Chicago.
NOW = 1_792_501_200.0


def item(**over):
    """A candidate item as a sweeper hands it to `process emit`: everything but the id and profile."""
    base = {
        "source": "github",
        "native_id": "pr/101",
        "version": "v1",
        "kind": "review_request",
        "subject": "https://github.com/acme/widgets/pull/101",
        "actor": {"name": "ana", "role": "person"},
        "timestamp": "2026-10-20T12:00:00Z",
        "link": "https://github.com/acme/widgets/pull/101",
        "summary": "Ana requested your review on #101 Add CSV export",
        "context": "Adds a CSV export of orders.",
    }
    return {**base, **over}


SHIPPED_PROFILES = sorted((Path(__file__).parent / "profiles").glob("*.md"))

PROFILE = """+++
name = "{name}"
active = {active}
workdir = "{workdir}"
timezone = "America/Chicago"
sources = ["github", "linear"]

[github]
repo = "acme/widgets"

[linear]
team = "WID"

[urgency]
people = ["boss"]
labels = ["deploy-blocking"]

[autonomy]
silent = {silent}

[phases.sweep-github]
model = "sonnet"
allowed_tools = ["Read", "Bash(process emit:*)", "Bash(gh pr list:*)", "mcp__github__pull_request_read"]

[phases.sweep-linear]
model = "sonnet"
allowed_tools = ["Read", "Bash(process emit:*)", "mcp__linear-server__get_notifications"]

[phases.triage]
model = "opus"
allowed_tools = {triage_tools}
+++

# {name}

## How to sweep

Nothing special.

## Changelog

- 2026-10-20 — Created.
"""


class ProcessTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = {"HOME": str(self.tmp), "XDG_STATE_HOME": str(self.tmp / "state"), "XDG_CONFIG_HOME": str(self.tmp / "config")}
        self.run_fake = FakeRun()
        self.now = NOW

    def process(self, *argv, stdin="", expect=0):
        out = io.StringIO()
        code = process.main(
            list(argv),
            run=self.run_fake,
            env=self.env,
            stdin=io.StringIO(stdin),
            stdout=out,
            clock=lambda: self.now,
        )
        self.assertEqual(code, expect, out.getvalue())
        return out.getvalue()

    @property
    def state(self):
        return self.tmp / "state" / "process"

    def write_profile(self, name="acme", active=True, silent=(), triage_tools=("Read", "Bash(process triage:*)", "mcp__linear-server__mark_notification"), text=None):
        path = self.tmp / "config" / "process" / "profiles" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            text
            or PROFILE.format(
                name=name,
                active=str(active).lower(),
                workdir=self.tmp / "work" / name,
                silent=json.dumps(list(silent)),
                triage_tools=json.dumps(list(triage_tools)),
            )
        )
        return path

    def profile_dir(self, name="acme"):
        return self.state / "profiles" / name

    def jsonl(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def emit(self, *items, profile="acme", source="github", watermark=None):
        args = ["emit", profile, source] + (["--watermark", watermark] if watermark else [])
        return self.process(*args, stdin="".join(json.dumps(i) + "\n" for i in items))


class EmitTest(ProcessTestCase):
    def test_emit_stores_valid_items_with_versioned_ids_and_advances_the_watermark(self):
        self.emit(item(), watermark="2026-10-20T13:00:00Z")

        [stored] = self.jsonl(self.profile_dir() / "items.jsonl")
        self.assertRegex(stored["id"], r"^github:pr/101@[0-9a-f]{12}$")
        self.assertEqual(stored["profile"], "acme")
        self.assertEqual(self.process("validate", "item", str(self.profile_dir() / "items.jsonl")), "")
        self.assertEqual(self.process("watermark", "acme", "github").strip(), "2026-10-20T13:00:00Z")

    def test_a_missing_argument_prints_the_commands_usage(self):
        self.assertIn("usage: process emit <profile> <source>", self.process("emit", "acme", expect=2))
        self.assertIn("usage: process triage <step> <profile>", self.process("triage", "plan", expect=2))

    def test_an_invalid_item_stores_nothing_and_leaves_the_watermark_alone(self):
        out = self.process(
            "emit", "acme", "github", stdin=json.dumps(item()) + "\n" + json.dumps(item(kind="gossip")) + "\n", expect=1
        )

        self.assertIn("item 2", out)
        self.assertEqual(self.jsonl(self.profile_dir() / "items.jsonl"), [])
        self.assertEqual(self.process("watermark", "acme", "github"), "")

    def test_re_emitting_an_unchanged_item_is_a_no_op_but_a_new_version_is_a_new_item(self):
        self.emit(item())
        self.emit(item())
        self.emit(item(version="v2", summary="Ana re-requested your review on #101"))

        stored = self.jsonl(self.profile_dir() / "items.jsonl")
        self.assertEqual(len(stored), 2)
        self.assertNotEqual(stored[0]["id"], stored[1]["id"])


def linear(**over):
    """A Linear notification item; by default Linear's mirror of Ana's review request on #101."""
    base = item(
        source="linear",
        native_id="notif-1",
        version="2026-10-20T12:01:00Z",
        summary="Review requested on #101 (linked to WID-500)",
        link="https://linear.app/acme/issue/WID-500",
        timestamp="2026-10-20T12:01:00Z",
        facts={"notification_id": "notif-1"},
    )
    return {**base, **over}


def my_pr(number=7, version="v1", **facts):
    return item(
        native_id=f"pr/{number}",
        version=version,
        kind="pr_status",
        subject=f"https://github.com/acme/widgets/pull/{number}",
        link=f"https://github.com/acme/widgets/pull/{number}",
        actor={"name": "me", "role": "me"},
        summary=f"Your PR #{number}",
        facts={"mine": True, **facts},
    )


class TriageTestCase(ProcessTestCase):
    def setUp(self):
        super().setUp()
        self.write_profile()

    def plan(self, profile="acme"):
        return json.loads((self.profile_dir(profile) / "plan.json").read_text())

    def queue(self):
        """The queue's current state: the last line for each entry id."""
        return list({e["id"]: e for e in self.jsonl(self.state / "queue.jsonl")}.values())

    def triage(self, profile="acme", outcome="done"):
        """Run triage the way the triage skill does: plan, draft every draft entry, report each silent action, commit."""
        self.process("triage", "plan", profile)
        for entry in self.plan(profile)["entries"]:
            if entry["autonomy"] == "draft":
                self.process("triage", "draft", profile, entry["id"], stdin=f"Draft for {entry['summary']}")
            if entry["action"]["type"] == "linear_mark_read":
                self.process("triage", "silent", profile, entry["id"], outcome)
        self.process("triage", "commit", profile)
        return self.queue()


class TriageTest(TriageTestCase):
    def test_the_same_review_request_from_github_and_linears_mirror_becomes_one_entry(self):
        self.emit(item())
        self.emit(linear(), source="linear")

        queue = self.triage()

        [review] = [e for e in queue if e["kind"] == "review_request"]
        self.assertEqual(len(review["item_ids"]), 2)
        self.assertEqual(review["sources"], ["github", "linear"])
        self.assertEqual(review["action"]["type"], "gh_review")
        self.assertEqual(review["action"]["params"], {"repo": "acme/widgets", "number": 101, "event": "COMMENT"})
        self.assertEqual((review["autonomy"], review["status"]), ("draft", "pending"))
        self.assertEqual(self.process("validate", "queue_entry", str(self.state / "queue.jsonl")), "")

    def test_a_handled_item_stays_handled_until_it_changes_then_it_resurfaces_and_replaces_its_entry(self):
        self.emit(item(kind="pr_comment", subject="https://github.com/acme/widgets/pull/7#issuecomment-1", version="c1"))
        [first] = self.triage()

        self.emit(item(kind="pr_comment", subject="https://github.com/acme/widgets/pull/7#issuecomment-1", version="c1"))
        self.assertEqual(self.triage(), [first])  # same version: nothing new

        self.emit(item(kind="pr_comment", subject="https://github.com/acme/widgets/pull/7#issuecomment-1", version="c2"))
        queue = self.triage()

        self.assertEqual([e["status"] for e in queue], ["superseded", "pending"])
        self.assertEqual(queue[1]["action"]["type"], "gh_thread_reply")
        self.assertNotEqual(queue[1]["item_ids"], first["item_ids"])


class TriageRulesTest(TriageTestCase):
    def test_asks_from_urgent_people_or_with_urgent_labels_are_urgent(self):
        self.emit(item(actor={"name": "boss", "role": "person"}), item(native_id="pr/102", subject="https://github.com/acme/widgets/pull/102", labels=["deploy-blocking"]), item(native_id="pr/103", subject="https://github.com/acme/widgets/pull/103"))

        self.assertEqual([e["urgency"] for e in self.triage()], ["urgent", "urgent", "normal"])

    def test_an_action_type_promoted_to_silent_skips_approval(self):
        self.write_profile(silent=["gh_thread_reply"])
        self.emit(item(kind="pr_comment", subject="https://github.com/acme/widgets/pull/7#issuecomment-9"))

        [reply] = self.triage()

        self.assertEqual((reply["autonomy"], reply["status"]), ("silent", "approved"))

    def test_my_own_comments_are_handled_without_an_entry(self):
        mine = item(kind="pr_comment", subject="https://github.com/acme/widgets/pull/7#issuecomment-2", actor={"name": "me", "role": "me"})
        self.emit(mine)

        self.assertEqual(self.triage(), [])
        self.emit(mine)
        self.process("triage", "plan", "acme")
        self.assertEqual(self.plan()["entries"], [])


class LinearInboxTest(TriageTestCase):
    def notification(self, n, days_old):
        stamp = process.iso(NOW - days_old * 86400)
        return linear(
            native_id=f"notif-{n}",
            version=stamp,
            kind="mention",
            subject=f"https://linear.app/acme/issue/WID-{n}#comment-{n}",
            timestamp=stamp,
            summary=f"Mentioned on WID-{n}",
            facts={"notification_id": f"notif-{n}"},
        )

    def mark_read(self, queue):
        return [e for e in queue if e["action"]["type"] == "linear_mark_read"]

    def test_the_first_run_clears_notifications_older_than_7_days_and_marks_the_rest_read_as_processed(self):
        self.emit(self.notification(1, days_old=30), self.notification(2, days_old=8), self.notification(3, days_old=1), source="linear")

        queue = self.triage()

        cleared, processed = self.mark_read(queue)
        self.assertEqual(cleared["action"]["params"]["notification_ids"], ["notif-1", "notif-2"])
        self.assertIn("older than 7 days", cleared["summary"])
        self.assertEqual(processed["action"]["params"]["notification_ids"], ["notif-3"])
        self.assertEqual({(e["status"], e["autonomy"]) for e in (cleared, processed)}, {("done", "silent")})
        self.assertEqual([e["summary"] for e in queue if e["kind"] == "mention"], ["Mentioned on WID-3"])

    def test_later_runs_mark_only_the_items_they_processed_however_old(self):
        self.emit(self.notification(1, days_old=1), source="linear")
        self.triage()

        self.emit(self.notification(4, days_old=9), source="linear")
        queue = self.triage()

        self.assertEqual([e["action"]["params"]["notification_ids"] for e in self.mark_read(queue)], [["notif-1"], ["notif-4"]])
        self.assertIn("Mentioned on WID-4", [e["summary"] for e in queue])

    def test_a_failed_mark_read_is_recorded_as_failed(self):
        self.emit(self.notification(1, days_old=1), source="linear")

        [failed] = self.mark_read(self.triage(outcome="failed"))

        self.assertEqual(failed["status"], "failed")


class NeverActionTest(TriageTestCase):
    def test_my_approved_green_pr_is_an_fyi_never_a_merge(self):
        self.emit(my_pr(mergeable="mergeable", checks="passing", review="approved"))

        [ready] = self.triage()

        self.assertEqual((ready["status"], ready["action"]["type"]), ("info", "none"))
        self.assertIn("merging is yours", ready["summary"])

    def test_my_broken_pr_is_a_fix_for_approval(self):
        self.emit(my_pr(mergeable="conflicting", checks="failing", review="review_required"))

        [broken] = self.triage()

        self.assertEqual((broken["autonomy"], broken["action"]["type"]), ("draft", "gh_fix_pr"))

    def test_a_plan_edited_into_a_merge_or_an_approving_review_is_refused_and_nothing_is_queued(self):
        for action in ({"type": "gh_merge", "params": {"repo": "acme/widgets", "number": 101}},
                       {"type": "gh_review", "params": {"repo": "acme/widgets", "number": 101, "event": "APPROVE"}, "draft": "LGTM"},
                       {"type": "prod_deploy", "params": {}}):
            with self.subTest(action=action["type"]):
                self.emit(item(version=action["type"]))
                self.process("triage", "plan", "acme")
                path = self.profile_dir() / "plan.json"
                plan = json.loads(path.read_text())
                plan["entries"][0]["action"] = action
                path.write_text(json.dumps(plan))

                out = self.process("triage", "commit", "acme", expect=1)
                self.assertIn("nothing committed", out)
                if action["type"] == "gh_review":
                    self.assertIn("'APPROVE' is not one of", out)
                self.assertEqual(self.jsonl(self.state / "queue.jsonl"), [])
                self.assertEqual(self.jsonl(self.profile_dir() / "ledger.jsonl"), [])

    def test_commit_refuses_a_draft_entry_without_a_draft(self):
        self.emit(item())
        self.process("triage", "plan", "acme")

        self.assertIn("no draft", self.process("triage", "commit", "acme", expect=1))


class BriefTest(TriageTestCase):
    def section(self, brief, project, heading):
        """The lines under one project's ### heading, up to the next heading."""
        lines = brief.split(f"## {project}\n", 1)[1].split(f"### {heading}", 1)[1].split("\n#", 1)[0]
        return lines

    def test_the_brief_groups_each_project_into_needs_approval_done_silently_and_fyi(self):
        self.emit(item(), my_pr(mergeable="mergeable", checks="passing", review="approved"))
        self.emit(linear(native_id="notif-9", kind="mention", subject="https://linear.app/acme/issue/WID-9", summary="Mentioned on WID-9", facts={"notification_id": "notif-9"}), source="linear")
        self.triage()

        self.process("brief")

        brief = (self.state / "brief.md").read_text()
        self.assertIn("Ana requested your review on #101", self.section(brief, "acme", "Needs approval (1)"))
        self.assertIn("> Draft for Ana requested your review on #101", self.section(brief, "acme", "Needs approval (1)"))
        self.assertIn("Marked 1 processed Linear notification(s) read", self.section(brief, "acme", "Done silently (1)"))
        fyi = self.section(brief, "acme", "FYI (2)")
        self.assertIn("<details>", fyi)
        self.assertIn("Mentioned on WID-9", fyi)
        self.assertIn("merging is yours", fyi)
        self.assertEqual((self.state / "briefs" / "2026-10-20.md").read_text(), brief)

    def test_delta_shows_only_what_the_latest_run_added_and_the_archive_keeps_every_brief_of_the_day(self):
        self.emit(item())
        self.triage()
        self.process("brief")
        self.now += 4 * 3600
        self.emit(item(native_id="pr/102", subject="https://github.com/acme/widgets/pull/102", summary="Ben requested your review on #102"))
        self.triage()

        self.process("brief", "--delta")

        delta = (self.state / "brief.md").read_text()
        self.assertIn("#102", delta)
        self.assertNotIn("#101", delta)
        archive = (self.state / "briefs" / "2026-10-20.md").read_text()
        self.assertIn("#101", archive)
        self.assertIn("#102", archive)

    def test_a_full_brief_keeps_showing_drafts_still_waiting_on_me(self):
        self.emit(item())
        self.triage()
        self.now += 4 * 3600
        self.triage()

        self.process("brief")

        self.assertIn("#101", (self.state / "brief.md").read_text())

    def test_project_narrows_the_brief_to_one_profile(self):
        self.write_profile("side")
        self.emit(item())
        self.emit(item(summary="Side review"), profile="side")
        self.triage()
        self.triage("side")

        self.process("brief", "--project", "side")

        brief = (self.state / "brief.md").read_text()
        self.assertIn("## side", brief)
        self.assertNotIn("## acme", brief)


class ContractTest(ProcessTestCase):
    def test_decisions_and_results_validate_against_the_shared_schema(self):
        approve = {"entry_id": "q-0123456789ab", "verdict": "approve", "draft": "Edited.", "reason": "too formal", "promote_to_silent": False, "at": "2026-10-20T13:05:00Z"}
        success = {"entry_id": "q-0123456789ab", "outcome": "success", "link": "https://github.com/acme/widgets/pull/101#pullrequestreview-1", "at": "2026-10-20T13:06:00Z"}

        self.assertEqual(self.process("validate", "decision", stdin=json.dumps(approve)), "")
        self.assertEqual(self.process("validate", "result", stdin=json.dumps(success)), "")
        self.assertIn("'merge' is not one of", self.process("validate", "decision", stdin=json.dumps({**approve, "verdict": "merge"}), expect=1))
        self.assertIn("missing 'outcome'", self.process("validate", "result", stdin=json.dumps({"entry_id": "q-0123456789ab", "at": "2026-10-20T13:06:00Z"}), expect=1))


class RunTest(ProcessTestCase):
    def claude_calls(self):
        return [(c, cwd) for c, cwd in zip(self.run_fake.calls, self.run_fake.cwds) if c[0] == "claude"]

    def opt(self, cmd, name):
        return cmd[cmd.index(name) + 1]

    def test_run_sweeps_each_source_then_triages_each_active_profile_headless_under_its_allowlist(self):
        self.write_profile("acme")
        self.write_profile("side", active=False)

        self.process("run")

        calls = self.claude_calls()
        self.assertEqual([c[:3] for c, _ in calls], [["claude", "-p", "/sweep-github acme"], ["claude", "-p", "/sweep-linear acme"], ["claude", "-p", "/triage acme"]])
        profile = process.load_profile(self.tmp / "config" / "process" / "profiles" / "acme.md")
        for (cmd, cwd), phase in zip(calls, ["sweep-github", "sweep-linear", "triage"]):
            self.assertEqual(self.opt(cmd, "--model"), profile["phases"][phase]["model"])
            self.assertEqual(self.opt(cmd, "--allowedTools").split(","), profile["phases"][phase]["allowed_tools"])
            self.assertIn("mcp__github__merge_pull_request", self.opt(cmd, "--disallowedTools").split(","))
            self.assertEqual(cwd, str(self.tmp / "work" / "acme"))
        self.assertTrue((self.state / "brief.md").exists())

    def test_delta_project_and_fixtures_reach_every_phase_and_the_brief(self):
        self.write_profile("acme")
        self.write_profile("side")

        self.process("run", "--delta", "--project", "side", "--fixtures", "/fx")

        prompts = [c[2] for c, _ in self.claude_calls()]
        self.assertEqual(prompts, ["/sweep-github side --delta --fixtures /fx", "/sweep-linear side --delta --fixtures /fx", "/triage side --dry-run"])
        brief = (self.state / "brief.md").read_text()
        self.assertIn("· delta", brief)
        self.assertNotIn("## acme", brief)

    def test_a_dry_run_refuses_to_write_into_the_real_state_directory(self):
        self.write_profile("acme")
        del self.env["XDG_STATE_HOME"]

        self.assertIn("scratch XDG_STATE_HOME", self.process("run", "--fixtures", "/fx", expect=1))
        self.assertEqual(self.claude_calls(), [])
        self.assertFalse((self.tmp / ".local" / "state" / "process").exists())

    def test_run_refuses_to_start_any_phase_when_a_profile_grants_a_send_tool(self):
        self.write_profile("acme")
        self.write_profile("side", triage_tools=["mcp__slack__slack_send_message"])

        self.assertIn("slack_send_message", self.process("run", expect=1))
        self.assertEqual(self.claude_calls(), [])

    def test_a_failed_sweep_is_reported_in_the_brief_and_triage_still_runs(self):
        self.write_profile("acme")

        def fail(cmd):
            raise process.subprocess.CalledProcessError(1, cmd, stderr="linear-server: not connected\n")

        self.run_fake.responses[("claude", "-p", "/sweep-linear acme")] = fail

        self.process("run")

        self.assertEqual(self.claude_calls()[-1][0][2], "/triage acme")
        self.assertIn("> ⚠ sweep-linear failed: linear-server: not connected", (self.state / "brief.md").read_text())


class ProfileTest(ProcessTestCase):
    # Tools that send, review, RSVP or merge on my behalf. None may ever be in a sweep or triage allowlist.
    SEND_TOOLS = [
        "mcp__slack__slack_send_message",
        "mcp__slack__slack_schedule_message",
        "mcp__claude_ai_Gmail__send_message",
        "mcp__claude_ai_Gmail__reply",
        "mcp__claude_ai_Gmail__forward",
        "mcp__claude_ai_Google_Calendar__respond_to_event",
        "mcp__github__pull_request_review_write",
        "mcp__github__add_issue_comment",
        "mcp__github__add_reply_to_pull_request_comment",
        "mcp__github__merge_pull_request",
        "mcp__linear-server__save_comment",
        "Bash(gh pr merge:*)",
        "Bash(gh pr review:*)",
        "Bash(gh pr comment:*)",
        "Bash(gh api:*)",
        "Bash",
        "Bash(gh:*)",
        "mcp__slack",
        "mcp__github__*",
    ]

    def test_profiles_lists_each_profile_with_its_sources(self):
        self.write_profile("acme")
        self.write_profile("side", active=False)

        out = self.process("profiles")

        self.assertIn("acme  active  github, linear", out)
        self.assertIn("side  inactive  github, linear", out)

    def test_a_profile_granting_any_send_review_rsvp_or_merge_tool_is_refused(self):
        for tool in self.SEND_TOOLS:
            with self.subTest(tool=tool):
                self.write_profile(triage_tools=["Read", tool])
                out = self.process("profiles", expect=1)
                self.assertIn(f"phases.triage grants {tool!r}", out)

    def test_a_profile_missing_its_how_to_sweep_section_is_refused(self):
        self.write_profile(text=self.write_profile().read_text().replace("## How to sweep", "## Notes"))

        self.assertIn("## How to sweep", self.process("profiles", expect=1))

    def test_every_shipped_profile_is_valid_and_its_sweep_and_triage_allowlists_hold_no_send_tool(self):
        self.assertTrue(SHIPPED_PROFILES, "no profiles shipped")
        for path in SHIPPED_PROFILES:
            with self.subTest(profile=path.name):
                profile = process.load_profile(path)  # raises SystemExit with every problem
                for phase in profile["phases"].values():
                    self.assertFalse(set(phase["allowed_tools"]) & set(self.SEND_TOOLS))
                    self.assertEqual([t for t in phase["allowed_tools"] if process.forbidden_tool(t)], [])

    def test_the_example_profile_declares_its_repo_team_account_models_and_how_to_sweep(self):
        acme = process.load_profile(Path(__file__).parent / "profiles" / "acme.md")

        self.assertEqual(acme["github"]["repo"], "acme/widgets")
        self.assertEqual(acme["linear"]["team"], "WID")
        self.assertEqual(acme["google"]["account"], "me@acme.example")
        self.assertEqual({p["model"] for p in acme["phases"].values()}, {"sonnet", "opus"})
        self.assertIn("## Changelog", acme["body"])


if __name__ == "__main__":
    unittest.main()
