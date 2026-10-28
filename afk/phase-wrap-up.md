AFK phase: wrap-up — ticket {{ticket}} ({{ticket_path}})

Stop what you're doing: this ticket went {{reason}}, so it wraps up now, finished or not. You have {{grace}} minutes. Start nothing new.

1. Commit what you have, unfinished work included, as atomic commits where you can.
2. Push the branch: `git push -u origin HEAD`. Never force-push, and never push to `{{base}}`. Only what you push is kept.
3. Write the PR description with a shell heredoc to a file in your worktree's git directory: `cat > "$(git rev-parse --git-dir)/afk-pr-body.md" <<'EOF'`. Use the shell, here and whenever you change it, as your Edit and Write tools work only on the worktree's files. It says what was done and links the ticket ({{ticket_path}}). It ends with a `## Questions for the engineer` section listing what's unfinished, that afk stopped the ticket because it went {{reason}}, and every question, assumption and blocker you noted in any phase, each with the assumption you made and what's still needed. That section is always the last.
4. If your branch already has a PR (`gh pr view`), update its description: `gh pr edit --body-file "$(git rev-parse --git-dir)/afk-pr-body.md"`. Otherwise open a draft PR against `{{base}}`: `gh pr create --draft --base {{base}} --title "<conventional commit title>" --body-file "$(git rev-parse --git-dir)/afk-pr-body.md"`, or, if GitHub refuses a draft for this repo, the same with `--label Draft` in place of `--draft`, after `gh label create Draft --description "Not ready for review"`. Don't wait for CI. Never run `gh pr ready`, never request a reviewer and never merge.

Then run `afk report done "<one-line summary>"`. If you haven't within {{grace}} minutes, afk abandons the ticket: your window, worktree and local branch are removed, and only what you pushed is kept.
