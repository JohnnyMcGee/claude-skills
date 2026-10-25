AFK phase: critique — ticket {{ticket}} ({{ticket_path}})

The branch passes pre-PR. No PR will be opened, so critique it locally the way bot reviewers would on a PR. This prompt is one critique round: afk sends it again for the next.

The repo's reviewers, from the `reviewers` key in `docs/agents/afk.md`:

{{reviewers}}

1. Run each reviewer command above from the worktree root, over `git diff {{target}}...HEAD`. Skip one whose binary isn't on PATH (`command -v <binary>`) and name it in your report; a missing reviewer doesn't fail the phase. With no reviewers configured, run a code-review subagent over `git diff {{target}}...HEAD` instead, against the ticket's acceptance criteria and this repo's coding standards.
2. Reviewers don't share your context: judge every finding against the code, not the reviewer's confidence. Sort each into exactly one bucket, as `/open-pr` sorts bot comments:
   - **Valid, in scope**: fix it, one atomic commit per fix.
   - **Invalid**: no change. It is wrong about the code, wrong about the requirements, or invented.
   - **Valid, out of scope**: no change. Real, but not something this branch touches.
   Note every rejected finding (invalid or out of scope) with a one-line reason.
3. Never push.

Then end the round:

- If you fixed anything, run `afk report done "round: <the fixes, in a line>"`. afk sends this prompt again, so the fixes get critiqued too. Don't start the next round yourself.
- If nothing needed fixing, the critique is finished: run `afk report done "<one-line summary, with the rejected findings of every round counted and any skipped reviewer named>"`.

If you can't proceed, run `afk report blocked "<why>"`; if you need a decision, run `afk report question "<question>"`.
