# AFK

AFK runs agent workers on a project's tickets, each in its own worktree, and gets their work into a branch with the human's approval.

## Landing

**Landing mode**:
How a project's workers get their work into the target branch: `pr`, through a GitHub pull request the human merges, or `local`, landed by the worker itself once the human approves it in the worker's session.
_Avoid_: local mode alone, direct mode, offline mode

**Target branch**:
The branch a project's work lands in and its worktrees branch from: the repo's base branch, or an integration branch.
_Avoid_: destination, merge branch

**Integration branch**:
A target branch other than the repo's base branch, that collects a project's tickets until the human takes it to base.
_Avoid_: feature branch, staging branch

**Land**:
To squash a worker's branch into the target branch as one conventional commit, after the human approves it. A landed ticket has ended, as a merged one has.
_Avoid_: merge (for local mode), ship

**Approval**:
The human's go-ahead, typed into the worker's session, to land its branch as it stands. Any later change of the worker's own makes it stale.
_Avoid_: sign-off, LGTM

**Drop**:
To abandon a local-mode worker's unlanded work, releasing its ticket back onto the frontier.
_Avoid_: abort, discard, close

**Critique**:
Local adversarial review of a worker's diff by reviewer CLIs, standing in for the bot reviewers on a PR.
_Avoid_: local review, bot review

## Queue

**Queue**:
A standing, machine-wide line of standalone tickets that afk works through unattended, one at a time, each ending in a draft PR. It belongs to no project and never closes.
_Avoid_: backlog, background project, inbox

**Enqueue**:
To add a ticket to the end of the queue.
_Avoid_: submit, schedule, push

**Abandon**:
To give up on the queue's running ticket, after its wrap-up or on the human's word, keeping whatever it pushed and moving on to the next. Unlike a drop, nothing pushed is deleted.
_Avoid_: drop (for the queue), kill, fail
