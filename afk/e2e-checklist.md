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
- [ ] Step 5 finds the reviewer CLIs on your PATH (`coderabbit`, `copilot`): it proposes a `reviewers` command for each one found, and gives an install hint for each one missing without installing it. Accept its proposals.
- [ ] The proof passes every step in one run, each reviewer exiting 0 over the trial diff without prompting, printing its findings or that it found none, and leaving the worktree clean and HEAD unchanged. With no reviewer installed, the reviewers step is skipped. Afterwards `git worktree list` and `git branch` show no `afk/trial`.
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
- [ ] pr: it pushes and runs `/open-pr main`. With no CI or bot reviewers, `/open-pr` should reach Ready without waiting forever. While it waits on checks or reviewers between turns, the dashboard keeps it in `pr` `working`, with no attention notification and no review prompt. It adds the `Ready for Review` label, reports `done`, and the watcher finds the PR by its branch.
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

## 10. Closed without merging (optional)

Re-open 02's ticket as `Status: ready-for-agent` again and start it.

- [ ] Once it has a PR, close the PR on GitHub without merging. Within about a minute 02 is cleaned up, you're notified "02 closed and cleaned up", its ticket still reads `Status: ready-for-agent`, and `afk frontier` lists it.
- [ ] Start 02 once more as a guide-mode worker (`afk start 02 --type hitl`), since only a worker in `pr`, `review` or `hitl` is polled. It isn't cleaned up for the PR the last attempt closed. With no PR, set its ticket to `Status: closed`. Within about a minute it's cleaned up, you're notified "02 ticket closed and cleaned up", and its ticket still reads `Status: closed`.

## 11. Local landing mode

Re-uses the throwaway repo, with the PR-mode run above finished and torn down from the afk side (stop its `afk watch`; keep the repo and its remote). This section checks that workers land without a PR, that the integration branch is pushed and base never is, and that approval gates the land. It needs the reviewer CLIs from step 2 (or accept the code-review subagent fallback). Tickets in a second spec:

| Ticket | Blocked by | Exercises |
| --- | --- | --- |
| 01 — `/ping` endpoint | — | critique, stale approval, `afk land`, cleanup |
| 02 — `/pong` endpoint | 01 | starting from the landed work |
| 03 — `/echo` endpoint | — | `afk drop --discard`, then landing on a restart |

```bash
cd ~/code/afk-e2e && git checkout main && git pull
mkdir -p .scratch/afk-e2e-local/issues && cd .scratch/afk-e2e-local
printf '# AFK E2E local\n\nThree trivial tickets for the local landing section.\n' > spec.md
for t in "01 ping none" "02 pong 01" "03 echo none"; do
  set -- $t
  blocked=$([ "$3" = none ] && echo "None — can start immediately" || echo "$3")
  printf '# %s — /%s endpoint\n\n**Blocked by:** %s\n\n**Status:** ready-for-agent\n\n`GET /%s` returns 200 with body `%s\\n`.\n\n## Acceptance criteria\n\n- [ ] `GET /%s` returns 200\n- [ ] A unit test covers it\n' "$1" "$2" "$blocked" "$2" "$2" "$2" > "issues/$1-$2-endpoint.md"
done
cd ../.. && tmux new -s afk-e2e-local -c ~/code/afk-e2e
```

Split the window as in step 3. Everything below runs in this session.

**Init**

- [ ] `/afk init .scratch/afk-e2e-local --land local --target afk-e2e-int` → binds the project and prints `created branch afk-e2e-int from main in …`
- [ ] `config.toml` has `[landing]` with `mode = "local"` and `target = "afk-e2e-int"`. `git branch` in the repo lists `afk-e2e-int`, and nothing is pushed yet.
- [ ] Re-running `afk init` with no argument changes nothing: `[landing]` is kept and the branch isn't recreated.
- [ ] Start `afk watch`. `afk frontier` lists 01 and 03, not 02.

**A worker through critique to review (01)**

- [ ] `/afk next`, approve 01 and 03. Each worktree branches from `afk-e2e-int` (`git log --oneline -1` in it matches `git rev-parse afk-e2e-int`), and each status records `landing` `local` and `target` `afk-e2e-int`.
- [ ] 01 runs implement → verify → prepr as in step 5. Its prepr merges `afk-e2e-int`, not `origin/main`, and it never pushes or opens a PR.
- [ ] critique: 01 runs each reviewer from `reviewers` over `git diff afk-e2e-int...HEAD` (or a code-review subagent if none is installed), names any it skipped, and fixes or rejects each finding with a one-line reason. A round that fixed anything ends `done "round: …"` and the critique prompt comes again. A round with nothing to fix moves it on. The dashboard shows phase `critique` throughout.
- [ ] review: the dashboard shows phase `review` with no PR, the window is marked Ready, and you get a notification that the branch is ready for review. No `gh pr view` lookup happens for it. The worker replies "Standing by."

**Feedback leaves the approval stale**

- [ ] Type "approve" in 01's agent pane, but while it is still busy (before it runs `afk land`) interrupt with Escape, then type "also return `Content-Length`". It fixes, commits, re-runs the pre-PR skill, merges `afk-e2e-int`, critiques the changed diff and reports `done`. Expect it not to land: the approval predates its new commit. If it runs `afk land` anyway, expect a refusal "no approval since the branch's last commit", with `afk-e2e-int` unmoved.

**Approval, then `afk land`**

- [ ] Type "approve" in 01's agent pane after its last commit (this is the approval the stale step above made necessary). It writes a conventional commit message with a `Ticket: 01` trailer and runs `afk land "…"`.
- [ ] Expect `afk land` to print the new commit on `afk-e2e-int`. `git log --oneline afk-e2e-int` shows one new commit, not 01's branch history, and the message matches.
- [ ] A watcher paste or a background-task notice in 01's pane is not taken for approval: confirm that a land attempted right after a phase prompt, with no human typing since the last commit, is refused.

**Target pushed, base untouched**

- [ ] `git rev-parse origin/afk-e2e-int` equals `afk-e2e-int` after `git fetch`: the integration branch was pushed.
- [ ] `git rev-parse main` and `origin/main` are unchanged from before the run: base was not moved or pushed.
- [ ] In 01's agent pane, `git push origin afk-e2e-int`, `git checkout afk-e2e-int` and `git update-ref refs/heads/afk-e2e-int HEAD` are each denied.

**Cleanup**

- [ ] Within about a minute 01's window, worktree and local branch are gone, its slot is freed, and you're notified "01 landed and cleaned up; run `/afk next`". No PR lookup was needed.
- [ ] `.scratch/afk-e2e-local/issues/01-ping-endpoint.md` reads `Status: done`
- [ ] `afk frontier` now lists 02

**A dependent ticket starts from the landed work (02)**

- [ ] `/afk next` proposes 02, noting that 03 is running. On approval, 02's worktree branches from `afk-e2e-int` and `git log` there includes 01's landed commit. `/ping` and its test are in the worktree.
- [ ] Run 02 through critique to review. Approve it so it lands on top of 01 without a conflict. Leave it for the all-landed check below.

**`afk drop --discard`**

- [ ] 03 is not worth landing. In the right pane (not 03's agent pane) run `afk drop 03`. Expect a dry run that changes nothing, listing the unlanded commits, any uncommitted changes and anything running in the shell pane.
- [ ] Ask 03's agent to run `afk drop 03 --discard`. Expect it to be denied. Then run it yourself: 03's window, worktree and branch go, its slot is freed, and it says what it deleted. `afk frontier` lists 03 again, and its ticket still reads `Status: ready-for-agent`.
- [ ] `afk drop 01 --discard` refuses, as 01 has no worker. `afk drop` on a worker that has landed refuses and says the watcher will clean it up.

**The all-landed notification**

- [ ] While 03 is on the frontier, there is no "all tickets landed" line on the dashboard or in `afk status`, and no notification.
- [ ] Start 03 again, take it through to review, approve and land it. Once the last worker is cleaned up and every ticket is done, the dashboard and `afk status` say `all tickets landed on afk-e2e-int; open a PR to main when ready`, and you're notified once.
- [ ] A new ticket added to the spec reopens the project: the line goes away, and comes back with another notification once that ticket lands.
- [ ] The integration branch holds one commit per ticket: `git log --oneline main..afk-e2e-int` shows three. Open a PR from it to `main` with `gh pr create --base main --head afk-e2e-int`; that is the only PR in the run.

## 12. Tear down

- [ ] Stop `afk watch`. Remove `${XDG_STATE_HOME:-~/.local/state}/afk/afk-e2e` and `afk-e2e-local`, both tmux sessions, `~/code/afk-e2e`, and the GitHub repo (`gh repo delete afk-e2e`).

## Defects

Record each mismatch here with the step, what happened, and the fix commit or filed issue. The run passes when every box is ticked, or its defect is fixed or filed.

| Step | What happened | Fixed in / filed as |
| --- | --- | --- |
| | | |
