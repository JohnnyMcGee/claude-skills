
This ticket spans repos, so you have a worktree in each, all on branch `{{branch}}`:

{{worktrees}}

Do this phase in every one of them, running its commands from that worktree's root, reading each repo's own `docs/agents/afk.md`, and starting each repo's dev server with its own `AFK_PORT_BASE`, listed above, in place of your session's. Where this prompt names `{{base}}`, use each repo's own base: verify, pre-PR and critique each repo's part of the change, with each critique over the diff against `origin/<that repo's base>`. When you open drafts, push each worktree's branch and open one draft PR per repo, against its own base, or update the one it already has. Once all are open, edit each one's description so it links the ticket and every sibling PR, with its own `## Questions for the engineer` still last. afk finds each PR by its branch.{{reviewers}}
