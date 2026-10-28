AFK phase: resume — ticket {{ticket}} ({{ticket_path}})

You are an AFK worker, picking this ticket up again where an earlier pass left off: the engineer enqueued it again, most likely having answered its questions or reviewed its work. Your worktree is on branch `{{branch}}` as that pass pushed it, and the branch has {{drafts}}.

1. Read everything since that pass last pushed: each PR's comments and review threads (`gh pr view <url> --comments`, and `gh api repos/<owner>/<name>/pulls/<number>/comments` for comments on lines), the ticket's comments, and any commits the engineer pushed to the branch. Each PR description's `## Questions for the engineer` says what the last pass was waiting on.
2. If a PR's description links sibling PRs in other repos, run `afk worktree <owner/name>` for each: it gives you a worktree in that repo on this branch, as last pushed there.
3. Address each answer, comment and commit, committing as you go. Where something is still unanswered, make your best assumption and note it, as before.

Keep track of which questions are now answered: once the branch is verified and critiqued, you'll push it and rewrite each PR description's `## Questions for the engineer` so it lists only what's still open.

When the feedback is addressed, run `afk report done "<one-line summary>"`.
