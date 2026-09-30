# AFK end-to-end checklist

A manual run of the whole loop with real tmux and real `claude`: init → Gate 1 → phases → Ready → merge → cleanup → next batch. The unit tests (`python3 -m unittest` in this folder) fake tmux, git and GitHub. This checklist is the only check that the pieces work together. Run it after any change to the watcher, the phase prompts or worker launch.

It uses a throwaway repo with three trivial tickets in a local `.scratch` spec:

| Ticket | Type | Blocked by | Exercises |
| --- | --- | --- | --- |
| 01 — `/health` endpoint | default | — | an agent worker through every phase to Ready, merge, cleanup |
| 02 — `/version` endpoint | default | 01 | the next batch unblocked by a merge |
| 03 — change the greeting | `hitl` | — | a guide-mode worker, `cleanup-pending` on a dirty worktree |

Tick each box as you go. Where the result doesn't match **Expect**, note it in [Defects](#defects) and carry on if you can.

## Prerequisites

- [ ] `afk` on PATH, pointing at this checkout's `afk.py`: `readlink -f "$(command -v afk)"`
- [ ] `tmux`, `git`, `gh` (logged in: `gh auth status`), `claude`, Python 3.11+
- [ ] The `afk`, `afk-setup` and `open-pr` skills installed from this checkout
- [ ] No leftover state: `ls ${XDG_STATE_HOME:-~/.local/state}/afk/` has no `afk-e2e` directory

## 1. Throwaway repo

PRs and merge detection go through GitHub, so the repo needs a remote. Create a private one:

```bash
mkdir -p ~/code/afk-e2e && cd ~/code/afk-e2e && git init -b main
printf '__pycache__/\n.scratch/\n' > .gitignore

cat > app.py <<'EOF'
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.reply(200, "hello\n")
        else:
            self.reply(404, "not found\n")

    def reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(os.environ.get("PORT", 8000))), Handler).serve_forever()
EOF

cat > test_app.py <<'EOF'
import threading
import unittest
from http.server import HTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

from app import Handler


class AppTest(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()

    def test_root_says_hello(self):
        self.assertEqual(urlopen(self.url + "/").read(), b"hello\n")

    def test_unknown_path_is_404(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.url + "/nope")
        self.assertEqual(caught.exception.code, 404)
EOF

git add -A && git commit -m "Initial app"
gh repo create afk-e2e --private --source . --push
```

- [ ] `python3 -m unittest` passes and `gh repo view` shows the repo

The tickets are gitignored: the local tracker marks a merged ticket `Status: done` in the main checkout, and a tracked file would leave it dirty.

```bash
mkdir -p .scratch/afk-e2e/issues && cd .scratch/afk-e2e
printf '# AFK E2E\n\nThree trivial tickets for the afk end-to-end checklist.\n' > spec.md

cat > issues/01-health-endpoint.md <<'EOF'
# 01 — Health endpoint

**Blocked by:** None — can start immediately

**Status:** ready-for-agent

`GET /health` returns 200 with body `ok\n`.

## Acceptance criteria

- [ ] `GET /health` returns 200 and `ok\n`
- [ ] A unit test covers it
EOF

cat > issues/02-version-endpoint.md <<'EOF'
# 02 — Version endpoint

**Blocked by:** 01

**Status:** ready-for-agent

`GET /version` returns 200 with body `0.1.0\n`.

## Acceptance criteria

- [ ] `GET /version` returns 200 and `0.1.0\n`
- [ ] A unit test covers it
EOF

cat > issues/03-greeting.md <<'EOF'
# 03 — Friendlier greeting

**Type:** hitl

**Blocked by:** None — can start immediately

**Status:** ready-for-agent

`GET /` says `hello, afk\n` instead of `hello\n`. Done by hand.

## Acceptance criteria

- [ ] `GET /` returns `hello, afk\n`
- [ ] The existing test is updated
EOF
cd ../..
```

## 2. Onboard with `/afk-setup`

In the repo, run `claude` and `/afk-setup`. Accept its proposals: base `main`, no files to copy, no bootstrap, dev server `PORT=$AFK_PORT_BASE python3 app.py`, nothing shared (no `verify_concurrency`), HTTP verification with `curl`, pre-PR checks `python3 -m unittest`, one task type with a `default_type`.

- [ ] Step 1 reports each machine item as already done, fixed, or needs you. A second run changes nothing.
- [ ] It writes `docs/agents/afk.md`, and `afk check` prints `is valid` with no warnings
- [ ] It creates `.claude/skills/pre-pr/SKILL.md`, and afk.md names it
- [ ] The proof passes all four steps in one run. Afterwards `git worktree list` and `git branch` show no `afk/trial`.
- [ ] It shows the diff and commits only once you approve. Push the commit to `main`.

## 3. Init

```bash
tmux new -s afk-e2e -c ~/code/afk-e2e
```

Split the window: `claude` on the left, a shell on the right. From here on, every step runs inside this session.

- [ ] `/afk init .scratch/afk-e2e` → `afk project 'afk-e2e' bound to tmux session 'afk-e2e'`
- [ ] `${XDG_STATE_HOME:-~/.local/state}/afk/afk-e2e/config.toml` has `tracker = "local"` and the repo path
- [ ] `afk frontier` lists 01 and 03, not 02
- [ ] Start `afk watch` in the right pane. The dashboard is empty and it says tickets are unblocked, pointing at `/afk next`.

## 4. Gate 1

- [ ] `/afk next`: no stuck workers to triage. It reads each ticket and presents a table: 01 with the default type, 03 with `hitl`, both in the spec's repo. 02 is absent.
- [ ] The conflict-risk reasoning is there. 01 and 03 both edit `app.py`, so expect a flag or a reason it's acceptable.
- [ ] Correct one field, e.g. the model for 01, and approve both. It runs `afk start 01 --model …` and `afk start 03`.
- [ ] Two windows, `01-…` and `03-…`, each split agent-left / shell-right, each in its own worktree (`git worktree list`) on branch `afk/afk-e2e-01` / `afk/afk-e2e-03`
- [ ] Each worker's `status.json` records the type, agent, model and effort it launched with, including the correction
- [ ] In the 01 shell pane, `echo $AFK_SLOT $AFK_PORT_BASE` shows a slot of 1 or more and its port base. 03's slot differs.
- [ ] In 01's agent pane, a denied command stays denied: ask it to `git push --force` and it refuses or is blocked

## 5. Agent worker through the phases (01)

Watch the dashboard, and don't type into 01's agent pane unless it asks.

- [ ] implement: it commits and reports `done`. The watcher waits until its session has stopped, then pastes the verify prompt.
- [ ] verify: it starts `app.py` on its `$AFK_PORT_BASE`, curls `/health` and an unhappy path, stops the server and reports `done`
- [ ] prepr: it runs `/pre-pr`, merges `origin/main`, runs a review subagent and reports `done`
- [ ] pr: it pushes and runs `/open-pr main`. With no CI or bot reviewers, `/open-pr` should reach Ready without waiting forever. It reports `done`, and the watcher finds the PR by its branch.
- [ ] review: the dashboard shows the PR, the window is marked Ready, and you get a "PR ready for review" notification (bell, tmux message and `notify-send`). The worker replies "Standing by."
- [ ] Review feedback: type a small request into 01's agent pane, such as "also return `Content-Length`". It fixes, re-runs the prepr steps, pushes, and reports `done`. You're notified, and it stays in review.
- [ ] `afk status` matches the dashboard at every step

## 6. HITL worker (03)

- [ ] The dashboard shows 03 as phase `hitl`, state `yours`. The watcher never pastes a phase prompt into it.
- [ ] Its agent summarises the ticket and asks where to start. It changes nothing until you ask.
- [ ] Make the change yourself in the 03 shell pane, ask the agent to review it, and commit. Push and open a PR with `gh pr create --base main`. The agent doesn't open it for you.
- [ ] Within about a minute, the dashboard shows 03's PR, found by branch
- [ ] Leave an untracked file in 03's worktree (`touch scratch.txt`) for step 8

## 7. Merge 01 → cleanup → next batch

- [ ] Merge 01's PR on GitHub. The watcher never merged it itself.
- [ ] Within about a minute, 01's window, worktree and local branch are gone, its slot is freed, and you're notified "01 merged and cleaned up; run `/afk next`"
- [ ] `.scratch/afk-e2e/issues/01-health-endpoint.md` now reads `Status: done`
- [ ] `afk frontier` now lists 02
- [ ] `/afk next` proposes 02, noting that 03 is running. On approval it starts, and 02 is created from a base that includes 01's merge (`git log` in its worktree).
- [ ] 02 runs through the phases to Ready as in step 5. Merge it and it's cleaned up the same way.

## 8. `cleanup-pending` (03)

- [ ] Merge 03's PR while `scratch.txt` is still in its worktree. 03 becomes `cleanup-pending`, you're notified once with the reason, and nothing is removed.
- [ ] Start something in 03's shell pane (`sleep 600`) and remove `scratch.txt` from another pane. It stays pending, because the shell pane is busy.
- [ ] Stop the `sleep`. On the next tick 03 is cleaned up.

## 9. Runaway limits (optional)

Set `[limits] idle_minutes = 1` in the project's `config.toml`, then start a fresh worker, e.g. re-open 02's ticket as `Status: ready-for-agent`. Interrupt it with Escape mid-phase and leave it.

- [ ] Within about a minute it's `stuck` with the reason, its window is suffixed `!`, and you're notified. Nothing is killed.
- [ ] `/afk next` triages it: it names the window and the limit, reads its agent pane, and proposes nudge, raise the limit, or take over
- [ ] Nudge it. When it reports `done`, it moves on normally.

Restore the limit after.

## 10. Tear down

- [ ] Stop `afk watch`. Remove `${XDG_STATE_HOME:-~/.local/state}/afk/afk-e2e`, the tmux session, `~/code/afk-e2e`, and the GitHub repo (`gh repo delete afk-e2e`).

## Defects

Record each mismatch here with the step, what happened, and the fix commit or filed issue. The run passes when every box is ticked, or its defect is fixed or filed.

| Step | What happened | Fixed in / filed as |
| --- | --- | --- |
| | | |
