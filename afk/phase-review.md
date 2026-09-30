AFK phase: review — ticket {{ticket}} ({{ticket_path}})

Your PR is with the human for review. There is nothing to do now: reply "Standing by." and stop. Don't run `afk report`.

From now on these standing instructions apply. When the human gives you review feedback in this pane:

1. Address it and commit.
2. Re-run the prepr steps: the repo's pre-PR skill, a merge-conflict check against `origin/{{base}}` (merge, never rebase), and a code-review subagent over the diff.
3. Push. Never force-push.
4. Run `afk report done "<one-line summary of what changed>"`.
