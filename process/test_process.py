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
sources = {sources}

[github]
repo = "{repo}"

[linear]
team = "{team}"
{extra}
[urgency]
people = ["boss"]
labels = ["deploy-blocking"]

[autonomy]
silent = {silent}

[phases.sweep-github]
model = "sonnet"
allowed_tools = {sweep_tools}

[phases.sweep-linear]
model = "sonnet"
allowed_tools = ["Read", "Bash(process emit:*)", "mcp__linear-server__get_notifications"]
{phases}
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

ALL_SOURCES = ["github", "linear", "slack", "gmail", "calendar", "meetings"]

# The tables the Slack and Google sources read, keyed by what turns them on.
SOURCE_TABLES = {
    "slack": '[slack]\nworkspace = "{workspace}"\nwatchlist = {watchlist}\n',
    "google": '[google]\naccount = "{account}"\nmanager = "boss@acme.example"\nnoise_label = "Noise"\n',
}



def slack(**over):
    """A Slack item; by default Dee asking me something in #deploys."""
    base = item(
        source="slack",
        native_id="C100/1792500000.000100",
        version="1792500000.000100",
        kind="mention",
        subject="https://acme.slack.com/archives/C100/p1792500000000100",
        link="https://acme.slack.com/archives/C100/p1792500000000100",
        actor={"name": "Dee", "role": "person"},
        summary="Dee asked in #deploys whether the export query uses the new index",
        context="@me-dev does the export query use the new index?",
        facts={"channel": "#deploys", "ask": "question"},
    )
    return {**base, **over}


def email(**over):
    """A Gmail item; by default Cal asking me for a review date."""
    base = item(
        source="gmail",
        native_id="thread-1",
        version="msg-1",
        kind="email",
        subject="https://mail.google.com/mail/u/0/#all/thread-1",
        link="https://mail.google.com/mail/u/0/#all/thread-1",
        actor={"name": "cal@acme.example", "role": "person"},
        summary="Cal asked when the security review can happen",
        context="When can we do the security review?",
        facts={"ask": "question"},
    )
    return {**base, **over}


def invite(**over):
    """A pending Calendar invite; by default an internal one with no conflict."""
    base = item(
        source="calendar",
        native_id="event-1",
        version="2026-10-20T11:00:00Z",
        kind="invite",
        subject="https://calendar.google.com/calendar/event?eid=event-1",
        link="https://calendar.google.com/calendar/event?eid=event-1",
        actor={"name": "dee@acme.example", "role": "person"},
        summary="Dee invited you to Export design review, Wed 14:00",
        context="Walk through the CSV export design.",
        facts={"external_guests": False, "with_manager": False, "conflicts": []},
    )
    return {**base, **over}


def action_item(**over):
    """An action item of mine from a Gemini notes doc."""
    base = item(
        source="meetings",
        native_id="doc-1#benchmark-the-export-query",
        version="2026-10-19T16:00:00Z",
        kind="action_item",
        subject="https://docs.google.com/document/d/doc-1#benchmark-the-export-query",
        link="https://docs.google.com/document/d/doc-1",
        actor={"name": "Gemini", "role": "bot"},
        summary="Benchmark the export query against the new index",
        context="Me Dev will benchmark the export query against the new index before Friday.",
    )
    return {**base, **over}


NEW_SOURCE_ITEMS = {"slack": slack(), "gmail": email(), "calendar": invite(), "meetings": action_item()}

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

    def write_profile(self, name="acme", active=True, silent=(), triage_tools=("Read", "Bash(process triage:*)", "mcp__linear-server__mark_notification"), text=None, sources=("github", "linear"), workspace="acme", account="me@acme.example", watchlist=("#deploys",), match=None, repo="acme/widgets", team="WID", sweep_tools=("Read", "Bash(process emit:*)", "Bash(gh pr list:*)", "mcp__github__pull_request_read")):
        path = self.tmp / "config" / "process" / "profiles" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        extra = ""
        if "slack" in sources:
            extra += SOURCE_TABLES["slack"].format(workspace=workspace, watchlist=json.dumps(list(watchlist)))
        if {"gmail", "calendar", "meetings"} & set(sources):
            extra += SOURCE_TABLES["google"].format(account=account)
        if match:
            extra += "[match]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in match.items())
        phases = "".join(f'[phases.sweep-{s}]\nmodel = "sonnet"\nallowed_tools = ["Read", "Bash(process emit:*)"]\n' for s in sources if s not in ("github", "linear"))
        path.write_text(
            text
            or PROFILE.format(
                name=name,
                active=str(active).lower(),
                workdir=self.tmp / "work" / name,
                sources=json.dumps(list(sources)),
                repo=repo,
                team=team,
                extra=extra,
                phases=phases,
                silent=json.dumps(list(silent)),
                triage_tools=json.dumps(list(triage_tools)),
                sweep_tools=json.dumps(list(sweep_tools)),
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

    def test_emit_consumes_an_inbox_file(self):
        inbox = self.profile_dir() / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "github.jsonl").write_text(json.dumps(item()) + "\n")

        self.process("emit", "acme", "github", "--file", str(inbox / "github.jsonl"))

        self.assertEqual(len(self.jsonl(self.profile_dir() / "items.jsonl")), 1)
        self.assertFalse((inbox / "github.jsonl").exists())

    def test_a_file_outside_the_inbox_is_refused_and_left_alone(self):
        secret = self.tmp / "id_rsa"
        secret.write_text("")

        self.assertIn("outside", self.process("emit", "acme", "github", "--file", str(secret), expect=1))

        self.assertTrue(secret.exists())
        self.assertEqual(self.process("watermark", "acme", "github"), "")

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

    def test_each_new_sweeper_emits_valid_items_and_advances_its_own_watermark(self):
        self.write_profile(sources=ALL_SOURCES)
        for source, candidate in NEW_SOURCE_ITEMS.items():
            with self.subTest(source=source):
                self.emit(candidate, source=source, watermark="2026-10-20T13:00:00Z")

                self.assertEqual(self.process("watermark", "acme", source).strip(), "2026-10-20T13:00:00Z")
        stored = self.jsonl(self.profile_dir() / "items.jsonl")
        self.assertEqual([i["source"] for i in stored], list(NEW_SOURCE_ITEMS))
        self.assertEqual(self.process("validate", "item", str(self.profile_dir() / "items.jsonl")), "")

    def test_a_slack_thread_i_already_replied_or_reacted_to_does_not_become_an_item(self):
        self.write_profile(sources=ALL_SOURCES)

        out = self.emit(slack(facts={"channel": "#deploys", "ask": "question", "answered": True}), source="slack", watermark="2026-10-20T13:00:00Z")

        self.assertIn("stored 0 new item(s)", out)
        self.assertIn("1 already answered", out)
        self.assertEqual(self.jsonl(self.profile_dir() / "items.jsonl"), [])
        self.assertEqual(self.process("watermark", "acme", "slack").strip(), "2026-10-20T13:00:00Z")

    def test_a_shared_source_is_attributed_to_projects_by_match_rules_and_the_rest_is_unsorted(self):
        self.write_profile("acme", sources=ALL_SOURCES, watchlist=["#deploys"])
        self.write_profile("side", sources=ALL_SOURCES, watchlist=["#gadgets"], repo="acme/gadgets", team="GAD", match={"keywords": ["gizmo"], "senders": ["@gadgets.example"]})
        self.write_profile("old", active=False, sources=ALL_SOURCES, match={"keywords": ["lunch"]})

        out = self.emit(
            slack(native_id="C1/1", facts={"channel": "#deploys"}),  # acme's watchlist
            slack(native_id="C2/2", summary="Eve asked about the gizmo rollout", facts={"channel": "#general"}),  # side's keyword
            slack(native_id="C3/3", summary="Fay asked whether GAD-12 ships today", facts={"channel": "#general"}),  # side's team
            slack(native_id="C4/4", summary="Gus asked who is up for lunch", context="lunch?", facts={"channel": "#random"}),
            source="slack",
            watermark="2026-10-20T13:00:00Z",
        )

        attributed = {name: [i["native_id"] for i in self.jsonl(self.profile_dir(name) / "items.jsonl")] for name in ("acme", "side", "unsorted")}
        self.assertEqual(attributed, {"acme": ["C1/1"], "side": ["C2/2", "C3/3"], "unsorted": ["C4/4"]})
        self.assertEqual(self.jsonl(self.profile_dir("side") / "items.jsonl")[0]["profile"], "side")
        self.assertIn("stored 4 new item(s)", out)
        for name in ("acme", "side"):
            self.assertEqual(self.process("watermark", name, "slack").strip(), "2026-10-20T13:00:00Z")

    def test_a_source_no_other_active_profile_shares_takes_every_item(self):
        self.write_profile("acme", sources=ALL_SOURCES)
        self.write_profile("side", sources=ALL_SOURCES, workspace="gadgets", account="me@gadgets.example", match={"keywords": ["lunch"]})

        self.emit(slack(summary="Gus asked who is up for lunch", facts={"channel": "#random"}), source="slack")
        self.emit(email(summary="Lunch on Friday?", actor={"name": "gus@gadgets.example", "role": "person"}), source="gmail")

        self.assertEqual([i["source"] for i in self.jsonl(self.profile_dir("acme") / "items.jsonl")], ["slack", "gmail"])
        self.assertFalse((self.profile_dir("side") / "items.jsonl").exists())

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

    def triage(self, profile="acme", outcome="done", note=None):
        """Run triage the way the triage skill does: plan, draft every draft entry, report each silent action, commit."""
        self.process("triage", "plan", profile)
        for entry in self.plan(profile)["entries"]:
            if entry["action"]["type"] not in ("none",) + TRIAGE_ACTIONS:
                self.process("triage", "draft", profile, entry["id"], stdin=f"Draft for {entry['summary']}")
            if entry["action"]["type"] in TRIAGE_ACTIONS:
                self.process("triage", "silent", profile, entry["id"], outcome, *([note] if note else []))
        self.process("triage", "commit", profile)
        return self.queue()


# The silent actions triage performs itself, then reports.
TRIAGE_ACTIONS = ("linear_mark_read", "linear_create_issue", "gmail_archive")


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
        self.assertTrue(reply["action"]["draft"])

    def test_a_promoted_action_still_needs_its_draft_before_commit(self):
        self.write_profile(silent=["gh_thread_reply"])
        self.emit(item(kind="pr_comment", subject="https://github.com/acme/widgets/pull/7#issuecomment-9"))

        self.assertIn("[draft]", self.process("triage", "plan", "acme"))
        self.assertIn("no draft", self.process("triage", "commit", "acme", expect=1))

    def test_a_draft_can_come_from_an_inbox_file(self):
        self.emit(item())
        self.process("triage", "plan", "acme")
        [review] = self.plan()["entries"]
        draft = self.profile_dir() / "inbox" / f"{review['id']}.md"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text("Two questions on the export.\n")

        self.process("triage", "draft", "acme", review["id"], "--file", str(draft))

        self.assertEqual(self.plan()["entries"][0]["action"]["draft"], "Two questions on the export.")
        self.assertFalse(draft.exists())

    def test_a_draft_file_outside_the_inbox_is_refused_and_left_alone(self):
        self.emit(item())
        self.process("triage", "plan", "acme")
        [review] = self.plan()["entries"]
        secret = self.tmp / "id_rsa"
        secret.write_text("PRIVATE KEY")

        self.assertIn("outside", self.process("triage", "draft", "acme", review["id"], "--file", str(secret), expect=1))
        self.assertTrue(secret.exists())
        self.assertNotIn("draft", self.plan()["entries"][0]["action"])

    def test_silent_outcomes_are_only_for_actions_triage_performs_itself(self):
        self.emit(item())
        self.process("triage", "plan", "acme")
        [review] = self.plan()["entries"]

        self.assertIn("not a silent action", self.process("triage", "silent", "acme", review["id"], "done", expect=1))

    def test_my_own_comments_are_handled_without_an_entry(self):
        mine = item(kind="pr_comment", subject="https://github.com/acme/widgets/pull/7#issuecomment-2", actor={"name": "me", "role": "me"})
        self.emit(mine)

        self.assertEqual(self.triage(), [])
        self.emit(mine)
        self.process("triage", "plan", "acme")
        self.assertEqual(self.plan()["entries"], [])


class SlackTriageTest(TriageTestCase):
    def setUp(self):
        super().setUp()
        self.write_profile(sources=ALL_SOURCES)

    def test_a_slack_question_gets_a_drafted_reply_in_its_thread(self):
        self.emit(slack(), source="slack")

        [reply] = self.triage()

        self.assertEqual(reply["action"]["type"], "slack_reply")
        self.assertEqual(reply["action"]["params"], {"channel": "C100", "thread_ts": "1792500000.000100"})
        self.assertEqual((reply["autonomy"], reply["status"], reply["urgency"]), ("draft", "pending", "normal"))
        self.assertTrue(reply["action"]["draft"])

    def test_an_immediate_slack_ask_becomes_a_ticket_assigned_to_me_silently_linking_back(self):
        ask = slack(summary="Dee asked you to add an index on orders.created_at", facts={"channel": "#deploys", "ask": "task"})
        self.emit(ask, source="slack")

        plan = self.process("triage", "plan", "acme")
        self.assertIn("[do, then report]", plan)
        self.assertIn("| @me-dev does the export query use the new index?", plan)  # the ticket's description comes from it
        [ticket] = self.triage(note="https://linear.app/acme/issue/WID-30")

        self.assertEqual(ticket["action"]["type"], "linear_create_issue")
        self.assertEqual(ticket["action"]["params"], {"team": "WID", "assignee": "me", "link": ask["link"]})
        self.assertEqual((ticket["autonomy"], ticket["status"], ticket["note"]), ("silent", "done", "https://linear.app/acme/issue/WID-30"))
        self.assertEqual(self.process("validate", "queue_entry", str(self.state / "queue.jsonl")), "")

    def test_an_ask_already_ticketed_is_not_ticketed_again_when_its_message_changes(self):
        ask = slack(summary="Dee asked you to add an index on orders.created_at", facts={"channel": "#deploys", "ask": "task"})
        self.emit(ask, source="slack")
        self.triage(note="https://linear.app/acme/issue/WID-30")

        self.emit({**ask, "version": "1792500100.000100"}, source="slack")  # Dee edited the message
        queue = self.triage()

        self.assertEqual([e["action"]["type"] for e in queue], ["linear_create_issue", "none"])
        self.assertIn("already ticketed: https://linear.app/acme/issue/WID-30", queue[1]["summary"])

    def test_an_ask_triage_found_already_tracked_is_listed_under_done_silently_as_skipped(self):
        self.emit(slack(summary="Dee asked you to add an index on orders.created_at", facts={"channel": "#deploys", "ask": "task"}), source="slack")
        self.triage(outcome="skipped", note="already tracked: https://linear.app/acme/issue/WID-21")

        self.process("brief")

        self.assertIn(
            "- Dee asked you to add an index on orders.created_at — skipped: already tracked: https://linear.app/acme/issue/WID-21",
            (self.state / "brief.md").read_text(),
        )

    def digest_dm(self, number=101):
        """GitHub's daily review digest DM in Slack, as the sweeper maps it: one review request per PR it names."""
        return slack(
            native_id=f"D9/1792490000.000100#pr-{number}",
            kind="review_request",
            subject=f"https://github.com/acme/widgets/pull/{number}",
            actor={"name": "GitHub", "role": "bot"},
            summary=f"GitHub digest: review requested on #{number}",
            facts={"channel": "DM"},
        )

    def test_the_github_review_digest_dm_merges_into_the_github_review_request(self):
        self.emit(item())
        self.emit(self.digest_dm(), source="slack")

        [review] = self.triage()

        self.assertEqual((review["sources"], review["action"]["type"]), (["github", "slack"], "gh_review"))

    def test_a_review_digest_or_linear_mirror_naming_a_review_already_queued_adds_nothing(self):
        self.emit(item())
        [review] = self.triage()

        self.emit(self.digest_dm(), source="slack")
        self.emit(linear(), source="linear")
        queue = self.triage()

        self.assertEqual([e["kind"] for e in queue], ["review_request", "housekeeping"])  # the mirror's notification is still marked read
        self.assertEqual(self.process("triage", "plan", "acme").splitlines()[-1].split(";")[0], "0 entr(ies) planned in " + str(self.profile_dir() / "plan.json"))

    def test_a_slack_message_that_asks_nothing_is_an_fyi(self):
        self.emit(slack(kind="channel_message", summary="Deploy 412 finished", facts={"channel": "#deploys"}), source="slack")

        [fyi] = self.triage()

        self.assertEqual((fyi["status"], fyi["action"]["type"], fyi["urgency"]), ("info", "none", "low"))


class MeetingsTriageTest(TriageTestCase):
    def setUp(self):
        super().setUp()
        self.write_profile(sources=ALL_SOURCES)

    def test_my_meeting_action_item_becomes_a_ticket_in_triage_assigned_to_me_linking_to_the_notes(self):
        self.emit(action_item(), source="meetings")

        [ticket] = self.triage(note="https://linear.app/acme/issue/WID-31")

        self.assertEqual(ticket["action"]["type"], "linear_create_issue")
        self.assertEqual(ticket["action"]["params"], {"team": "WID", "state": "Triage", "assignee": "me", "link": "https://docs.google.com/document/d/doc-1"})
        self.assertEqual((ticket["autonomy"], ticket["status"]), ("silent", "done"))

    def test_an_action_item_already_ticketed_is_not_ticketed_again_when_the_notes_change(self):
        self.emit(action_item(), source="meetings")
        self.triage(note="https://linear.app/acme/issue/WID-31")

        self.emit(action_item(version="2026-10-20T09:00:00Z"), source="meetings")

        self.assertEqual([e["action"]["type"] for e in self.triage()], ["linear_create_issue", "none"])


class CalendarTriageTest(TriageTestCase):
    def setUp(self):
        super().setUp()
        self.write_profile(sources=ALL_SOURCES)

    def rsvp(self, **over):
        self.emit(invite(**over), source="calendar")
        return self.triage()[-1]

    def test_a_pending_invite_with_no_conflict_gets_a_drafted_accept(self):
        rsvp = self.rsvp()

        self.assertEqual(rsvp["action"]["type"], "rsvp")
        self.assertEqual(rsvp["action"]["params"], {"event_id": "event-1", "response": "accepted"})
        self.assertEqual((rsvp["autonomy"], rsvp["status"]), ("draft", "pending"))

    def test_a_conflicting_invite_is_proposed_declined_unless_external_guests_or_my_manager_are_in_it(self):
        cases = {
            "internal": ({"external_guests": False, "with_manager": False}, "dee@acme.example", "declined"),
            "external guests": ({"external_guests": True, "with_manager": False}, "dee@acme.example", "tentative"),
            "manager attends": ({"external_guests": False, "with_manager": True}, "dee@acme.example", "tentative"),
            "manager organizes": ({"external_guests": False, "with_manager": False}, "boss@acme.example", "tentative"),
        }
        for n, (case, (facts, organizer, response)) in enumerate(cases.items()):
            with self.subTest(case=case):
                rsvp = self.rsvp(native_id=f"event-{n}", subject=f"https://calendar.google.com/calendar/event?eid=event-{n}", actor={"name": organizer, "role": "person"}, facts={**facts, "conflicts": ["1:1 with Ana"]})

                self.assertEqual(rsvp["action"]["params"]["response"], response)
                self.assertIn("conflicts with 1:1 with Ana", rsvp["summary"])

    def test_a_plan_edited_into_declining_a_meeting_with_external_guests_or_my_manager_is_refused(self):
        for n, facts in enumerate(({"external_guests": True}, {"with_manager": True})):
            with self.subTest(facts=facts):
                self.emit(invite(native_id=f"event-{n}", facts={"conflicts": [], **facts}), source="calendar")
                self.process("triage", "plan", "acme")
                path = self.profile_dir() / "plan.json"
                plan = json.loads(path.read_text())
                plan["entries"][0]["action"]["params"]["response"] = "declined"
                plan["entries"][0]["action"]["draft"] = "Can't make it."
                path.write_text(json.dumps(plan))

                out = self.process("triage", "commit", "acme", expect=1)

                self.assertIn("never decline", out)
                self.assertEqual(self.jsonl(self.state / "queue.jsonl"), [])
                path.unlink()


class GmailTriageTest(TriageTestCase):
    def setUp(self):
        super().setUp()
        self.write_profile(sources=ALL_SOURCES)

    def noise(self, n):
        return email(native_id=f"thread-{n}", subject=f"https://mail.google.com/mail/u/0/#all/thread-{n}", actor={"name": "news@vendor.example", "role": "bot"}, summary=f"Vendor newsletter {n}", facts={"noise": True})

    def test_an_email_that_asks_something_of_me_gets_a_drafted_reply(self):
        self.emit(email(), source="gmail")

        [reply] = self.triage()

        self.assertEqual((reply["action"]["type"], reply["action"]["params"]), ("email_reply", {"thread_id": "thread-1"}))
        self.assertEqual((reply["autonomy"], reply["status"], reply["urgency"]), ("draft", "pending", "normal"))

    def test_noise_is_labeled_and_archived_silently_and_listed_under_done_silently(self):
        self.emit(self.noise(2), self.noise(3), source="gmail")

        [archive] = self.triage()

        self.assertEqual(archive["action"], {"type": "gmail_archive", "params": {"thread_ids": ["thread-2", "thread-3"], "label": "Noise"}})
        self.assertEqual((archive["autonomy"], archive["status"]), ("silent", "done"))
        self.assertEqual(self.process("validate", "queue_entry", str(self.state / "queue.jsonl")), "")
        self.process("brief")
        self.assertIn("- Labeled Noise and archived 2 email(s): Vendor newsletter 2; Vendor newsletter 3 — done", (self.state / "brief.md").read_text())

    def test_an_email_that_asks_nothing_is_an_fyi(self):
        self.emit(email(summary="Cal shared the Q4 roadmap", facts={}), source="gmail")

        [fyi] = self.triage()

        self.assertEqual((fyi["status"], fyi["action"]["type"], fyi["urgency"]), ("info", "none", "low"))


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

    def test_the_clear_out_waits_for_the_first_run_that_sees_linear(self):
        self.emit(item())
        self.triage()  # the Linear sweep failed or found nothing

        self.emit(self.notification(1, days_old=30), self.notification(2, days_old=1), source="linear")
        cleared, processed = self.mark_read(self.triage())

        self.assertEqual(cleared["action"]["params"]["notification_ids"], ["notif-1"])
        self.assertEqual(processed["action"]["params"]["notification_ids"], ["notif-2"])

    def test_my_own_notifications_are_marked_read_without_an_entry(self):
        self.emit(self.notification(1, days_old=1), {**self.notification(2, days_old=1), "actor": {"name": "me", "role": "me"}}, source="linear")

        queue = self.triage()

        [processed] = self.mark_read(queue)
        self.assertEqual(processed["action"]["params"]["notification_ids"], ["notif-1", "notif-2"])
        self.assertEqual([e["summary"] for e in queue if e["kind"] == "mention"], ["Mentioned on WID-1"])

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

    def test_a_brief_before_any_run_says_nothing_new(self):
        self.process("brief")

        self.assertIn("Nothing new.", (self.state / "brief.md").read_text())

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
            inbox = self.profile_dir("acme") / "inbox"
            self.assertEqual(self.opt(cmd, "--allowedTools").split(","), profile["phases"][phase]["allowed_tools"] + [f"Edit(/{inbox}/**)"])
            self.assertEqual(self.opt(cmd, "--add-dir"), str(inbox))  # the one place a phase may write files
            self.assertIn("mcp__github__merge_pull_request", self.opt(cmd, "--disallowedTools").split(","))
            self.assertEqual(self.opt(cmd, "--permission-mode"), "dontAsk")  # anything not allowlisted is denied
            self.assertEqual(cwd, str(self.tmp / "work" / "acme"))
        self.assertTrue((self.state / "brief.md").exists())

    def test_delta_project_and_fixtures_reach_every_phase_and_the_brief(self):
        self.write_profile("acme")
        self.write_profile("side")

        self.process("run", "--delta", "--project", "side", "--fixtures", "/fx")

        prompts = [c[2] for c, _ in self.claude_calls()]
        self.assertEqual(prompts, ["/sweep-github side --delta --fixtures /fx", "/sweep-linear side --delta --fixtures /fx", "/triage side --dry-run --fixtures /fx"])
        brief = (self.state / "brief.md").read_text()
        self.assertIn("· delta", brief)
        self.assertNotIn("## acme", brief)

    def test_a_source_shared_by_two_profiles_is_swept_once_for_both(self):
        self.write_profile("acme", sources=ALL_SOURCES)
        self.write_profile("side", sources=ALL_SOURCES)

        def sweep(source):  # a sweeper that does its job and finds nothing
            return lambda cmd: process.main(["emit", "acme", source], run=self.run_fake, env=self.env, stdin=io.StringIO(""), stdout=io.StringIO(), clock=lambda: self.now)

        for source in ALL_SOURCES[2:]:
            self.run_fake.responses[("claude", "-p", f"/sweep-{source} acme")] = sweep(source)

        self.process("run")

        prompts = [c[2] for c, _ in self.claude_calls()]
        shared = [f"/sweep-{s} acme" for s in ALL_SOURCES[2:]]
        self.assertEqual(prompts, ["/sweep-github acme", "/sweep-linear acme", *shared, "/triage acme", "/sweep-github side", "/sweep-linear side", "/triage side"])
        self.assertNotIn("slack", (self.state / "brief.md").read_text())  # side's watermark moved with acme's sweep

    def test_unsorted_items_get_their_own_section_of_the_brief_once(self):
        self.write_profile("acme", sources=["github", "linear", "slack"])
        self.write_profile("side", sources=["github", "linear", "slack"], repo="acme/gadgets", team="GAD", watchlist=[])
        lunch = slack(summary="Gus asked who is up for lunch", context="lunch?", facts={"channel": "#random", "ask": "question"})

        def sweep_slack(cmd):
            process.main(["emit", "acme", "slack"], run=self.run_fake, env=self.env, stdin=io.StringIO(json.dumps(lunch) + "\n"), stdout=io.StringIO(), clock=lambda: self.now)

        self.run_fake.responses[("claude", "-p", "/sweep-slack acme")] = sweep_slack

        self.process("run")
        self.process("run")

        brief = (self.state / "brief.md").read_text()
        self.assertIn("## Unsorted", brief)
        self.assertIn("Gus asked who is up for lunch", brief.split("## Unsorted", 1)[1])
        unsorted = [e for e in self.jsonl(self.state / "queue.jsonl") if e["project"] == "unsorted"]
        self.assertEqual([(e["status"], e["action"]["type"]) for e in unsorted], [("info", "none")])
        self.assertEqual(self.process("validate", "queue_entry", str(self.state / "queue.jsonl")), "")

    def test_a_dry_run_reads_fixtures_from_an_absolute_path_with_no_service_tools(self):
        self.write_profile("acme")

        self.process("run", "--fixtures", "fx")
        self.process("run", "--fixtures", "fx")  # a scratch directory takes any number of dry runs

        calls = self.claude_calls()
        self.assertEqual(calls[0][0][2], f"/sweep-github acme --fixtures {Path('fx').resolve()}")
        for cmd, _ in calls:
            self.assertEqual([t for t in self.opt(cmd, "--allowedTools").split(",") if t.startswith("mcp__")], [])

    def test_a_dry_run_refuses_a_state_directory_that_holds_a_real_queue(self):
        self.write_profile("acme")
        self.emit(item())
        out = self.process("triage", "plan", "acme")
        self.process("triage", "draft", "acme", out.split()[0], stdin="Looks fine.")
        self.process("triage", "commit", "acme")  # a real queue now lives here

        self.assertIn("scratch XDG_STATE_HOME", self.process("run", "--fixtures", "/fx", expect=1))
        self.assertEqual(self.claude_calls(), [])

    def test_a_dry_run_refuses_any_used_state_directory_even_before_its_first_queue(self):
        self.write_profile("acme")
        self.emit(item())  # a real sweep ran, triage has not yet

        self.assertIn("scratch XDG_STATE_HOME", self.process("run", "--fixtures", "/fx", expect=1))
        self.assertFalse((self.state / "dry-run").exists())

    def test_a_real_run_refuses_a_dry_run_state_directory(self):
        self.write_profile("acme")
        self.process("run", "--fixtures", "/fx")

        self.assertIn("holds dry-run data", self.process("run", expect=1))

    def test_a_missing_workdir_is_reported_and_the_run_carries_on(self):
        self.write_profile("acme")
        self.write_profile("side")

        def missing(cmd):
            raise FileNotFoundError(2, "No such file or directory", "/nowhere")

        self.run_fake.responses[("claude", "-p", "/sweep-github acme")] = missing
        self.run_fake.responses[("claude", "-p", "/sweep-linear acme")] = missing
        self.run_fake.responses[("claude", "-p", "/triage acme")] = missing

        self.process("run")

        self.assertIn("/triage side", [c[2] for c, _ in self.claude_calls()])
        self.assertIn("> ⚠ triage failed: [Errno 2] No such file or directory", (self.state / "brief.md").read_text())
        self.assertFalse((self.state / "run.json").exists())

    def test_a_phase_that_exits_cleanly_but_does_nothing_is_flagged_in_the_brief(self):
        self.write_profile("acme")

        def sweep_github(cmd):  # a sweeper that does its job
            process.main(["emit", "acme", "github"], run=self.run_fake, env=self.env, stdin=io.StringIO(json.dumps(item()) + "\n"), stdout=io.StringIO(), clock=lambda: self.now)

        self.run_fake.responses[("claude", "-p", "/sweep-github acme")] = sweep_github

        self.process("run")  # the Linear sweeper and triage do nothing and exit 0

        brief = (self.state / "brief.md").read_text()
        self.assertNotIn("sweep-github", brief)
        self.assertIn("> ⚠ sweep-linear stored nothing: its watermark did not move", brief)
        self.assertIn("> ⚠ triage left 1 item(s) unhandled", brief)

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
        "Bash(gh issue:*)",
        "Bash(gh pr:*)",
        "Bash(sh -c:*)",
        "Bash(python3 -c:*)",
        "Bash(env gh:*)",
        "Bash(xargs gh:*)",
        "mcp__github__push_files",
        "mcp__github__create_or_update_file",
        "mcp__github__issue_write",
        "mcp__github__update_pull_request",
        "mcp__github__delete_repository",
        "mcp__slack__slack_add_reaction",
        "mcp__linear-server__create_comment",
        "mcp__claude_ai_Google_Calendar__create_event",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "WebFetch",
        "WebSearch",
        "mcp__claude_ai_Gmail__trash_thread",
        "mcp__claude_ai_Gmail__mark_thread_spam",
        "mcp__claude_ai_Gmail__create_draft",
        "mcp__slack__slack_send_message_draft",
    ]
    # The silent writes: triage may perform them, a sweeper never.
    SILENT_WRITES = [
        "mcp__linear-server__mark_notification",
        "mcp__linear-server__save_issue",
        "mcp__linear-server__create_issue",
        "mcp__claude_ai_Gmail__label_thread",
        "mcp__claude_ai_Gmail__unlabel_thread",
        "mcp__claude_ai_Gmail__label_message",
        "mcp__claude_ai_Gmail__unlabel_message",
    ]

    def test_triage_may_grant_the_silent_writes_but_a_sweeper_may_not(self):
        self.write_profile(triage_tools=["Read", *self.SILENT_WRITES])
        self.assertIn("acme  active", self.process("profiles"))

        for tool in self.SILENT_WRITES:
            with self.subTest(tool=tool):
                self.write_profile(sweep_tools=["Read", tool])
                self.assertIn(f"phases.sweep-github grants {tool!r}", self.process("profiles", expect=1))

    def test_sweepers_may_read_slack_reactions_calendars_and_meeting_notes(self):
        reads = ["mcp__slack__slack_get_reactions", "mcp__slack__slack_read_thread", "mcp__claude_ai_Google_Calendar__list_events", "mcp__claude_ai_Google_Drive__read_file_content", "mcp__claude_ai_Gmail__search_threads"]
        self.write_profile(sweep_tools=["Read", *reads])

        self.assertIn("acme  active", self.process("profiles"))

    def test_profile_prints_one_profile_so_phases_never_guess_its_path(self):
        path = self.write_profile("acme")

        out = self.process("profile", "acme")

        self.assertTrue(out.startswith(f"{path}\ninbox: {self.profile_dir('acme') / 'inbox'}\n"))
        self.assertIn("## How to sweep", out)

    def test_profile_names_who_shares_each_feed_and_every_watchlist_its_sweep_covers(self):
        self.write_profile("acme", sources=ALL_SOURCES, watchlist=["#deploys"])
        self.write_profile("side", sources=["github", "linear", "slack"], watchlist=["#gadgets", "#deploys"])
        self.write_profile("solo", sources=["github", "linear", "gmail"], account="me@solo.example")

        out = self.process("profile", "acme")

        self.assertIn("shared slack: acme, side (one sweep serves all; emit attributes each item)\n", out)
        self.assertIn("watchlist: #deploys, #gadgets\n", out)
        self.assertNotIn("shared gmail", out)

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

    def test_a_profile_can_declare_all_six_sources(self):
        self.write_profile(sources=ALL_SOURCES, match={"keywords": ["widgets"], "channels": ["#widgets"], "senders": ["@acme.example"]})

        self.assertIn("acme  active  github, linear, slack, gmail, calendar, meetings", self.process("profiles"))

    def test_google_sources_need_the_google_table(self):
        text = self.write_profile(sources=["github", "linear", "gmail"]).read_text()
        self.write_profile(text=text.replace("[google]", "[elsewhere]").replace('account = "me@acme.example"\n', ""))

        self.assertIn("source 'gmail' has no [google] table", self.process("profiles", expect=1))

    def test_unsorted_is_reserved_for_items_no_profile_claims(self):
        self.write_profile("unsorted")

        self.assertIn("'unsorted' is reserved", self.process("profiles", expect=1))

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
        self.assertEqual(acme["sources"], ALL_SOURCES)
        self.assertEqual(acme["slack"]["watchlist"], ["#incidents", "#deploys"])
        self.assertEqual({p["model"] for p in acme["phases"].values()}, {"sonnet", "opus"})
        self.assertIn("## Changelog", acme["body"])


if __name__ == "__main__":
    unittest.main()
