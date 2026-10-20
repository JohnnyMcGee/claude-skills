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
        "summary": "Ana requested your review on #101 Add vote export",
        "context": "Adds a CSV export of votes.",
    }
    return {**base, **over}


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


if __name__ == "__main__":
    unittest.main()
