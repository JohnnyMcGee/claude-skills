AFK phase: review — ticket {{ticket}} ({{ticket_path}})

Your branch is with the human for review, here in this pane. There is nothing to do now: reply "Standing by." and stop. Don't run `afk report`.

From now on these standing instructions apply to each message the human types in this pane:

- **Feedback** (changes asked for):
  1. Address it and commit.
  2. Re-run the repo's pre-PR skill, then check for merge conflicts with the target: `git merge {{target}}` (merge, never rebase). Resolve any conflicts, and commit.
  3. Run `afk report done "<one-line summary of what changed>"`.
- **Approval** ("approve", "land it", "lgtm"):
  1. Write a conventional commit message for the whole branch: a `type(scope): summary` subject, a body summing up the change, and a `Ticket: {{ticket}}` trailer.
  2. Run `afk land "<message>"`. It squashes the branch onto `{{target}}` as that one commit, and afk cleans up this worker once it has landed. Don't run `afk report`.
  3. If it refuses because the target moved, merge `{{target}}`, re-run the pre-PR skill, commit, and run `afk land` again. If it refuses for want of approval, run `afk report question "<why it needs approving again>"`. Never move `{{target}}` yourself.
- **Unclear** which it is: ask the human, and don't land.
