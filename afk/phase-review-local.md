AFK phase: review — ticket {{ticket}} ({{ticket_path}})

Your branch is with the human for review, here in this session. There is nothing to do now: reply "Standing by." and stop. Don't run `afk report`.

From now on these standing instructions apply to each message the human types in this pane. Never push: in local mode the branch stays local, and only `afk land` moves the target.

When it's review feedback:

1. Address it and commit.
2. Re-run the prepr steps: the repo's pre-PR skill, then a merge-conflict check against the target, `git merge {{target}}` (merge, never rebase). Fix and commit what they find.
3. Critique the changed diff as in the critique phase: run each of the repo's reviewers, listed below, over `git diff {{target}}...HEAD`, skipping one whose binary is missing, or a code-review subagent when none runs. Sort the findings on code you changed for this feedback as before: fix and commit the valid, in-scope ones, and reject the rest with a one-line reason. Repeat until a round has nothing to fix.
4. Run `afk report done "<one-line summary of what changed, with rejected findings counted>"`.

When it's approval of the branch as it stands ("approve", "land it", "lgtm"):

1. Write a conventional commit message for the whole branch: a `type(scope): summary` line, a body summing up the change, and a `Ticket: {{ticket}}` trailer.
2. Run `afk land "<message>"`. It squashes the branch onto `{{target}}`. If it refuses, do what it says, or ask the human.

When it's unclear which it is, ask, and don't land.

The repo's reviewers, from `reviewers` in `docs/agents/afk.md`:

{{reviewers}}
