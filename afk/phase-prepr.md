AFK phase: prepr — ticket {{ticket}} ({{ticket_path}})

The work is verified. Get the branch ready to open a PR against `{{base}}`, so the PR passes checks and pre-empts reviewer comments.

1. Run this repo's pre-PR skill, named in `docs/agents/afk.md`. Fix everything it reports and commit.
2. Check for merge conflicts with the base: `git fetch origin {{base}}` then merge `origin/{{base}}` into your branch. Resolve any conflicts, re-run step 1 if anything changed, and commit. Never rebase or force-push.
3. Run a code review in a subagent over `git diff origin/{{base}}...HEAD`, against the ticket's acceptance criteria and this repo's coding standards. Fix every real finding in scope and commit. Note out-of-scope findings in your report; don't fix them.

When the branch is clean, committed and conflict-free, run `afk report done "<one-line summary>"`. If you can't proceed, run `afk report blocked "<why>"`; if you need a decision, run `afk report question "<question>"`.
