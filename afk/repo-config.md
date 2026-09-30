# Repo config

Each repo describes what AFK workers need in `docs/agents/afk.md`, committed. The file is TOML frontmatter between `+++` lines, which the `afk` CLI reads, followed by prose that workers read.

```markdown
+++
base = "main"
copy = [".env", "config/local.yml"]
bootstrap = ["npm ci", "make db"]
port_base = 4000
verify_concurrency = 1
default_type = "backend"

[task_types.backend]
agent = "claude"
model = "opus"
effort = "high"
skill = "/tdd"

[task_types.frontend]
model = "sonnet"
effort = "medium"
prompt = "docs/agents/prompts/frontend.md"
+++

# AFK

## Pre-PR skill
Run `/pre-pr` …

## Dev server
`PORT=$AFK_PORT_BASE npm run dev`; the database is `app_$AFK_SLOT` …

## Verification recipes
…
```

`/afk-setup` writes this file. `afk check [<repo>]` validates it as `afk start` will read it (with `afk.local.md` merged in) and warns about missing prose sections; `afk trial [<repo>]` proves it by creating a scratch worktree on branch `afk/trial`, copying files and running bootstrap on slot 9, and `afk trial --teardown` removes it.

## Frontmatter keys

Every key is optional.

| Key | Default | Meaning |
| --- | --- | --- |
| `base` | `"main"` | Branch worktrees are created from and PRs target. Workers are denied pushing to it. |
| `copy` | `[]` | Gitignored files, relative to the repo root, copied into each new worktree. |
| `bootstrap` | `[]` | Shell commands run in order in each new worktree, after `copy`. A failure rolls the start back. |
| `port_base` | `4000` | Worker slot `n` gets `AFK_PORT_BASE = port_base + 100·n`. |
| `verify_concurrency` | unlimited | Most workers in the verify phase at once. Set `1` when dev environments can't be isolated. Extra workers wait with state `queued`, first come first served. |
| `default_type` | none | Task type for tickets without a `Type:` line. |
| `task_types.<name>` | | How a ticket of this type is launched: see below. |

A task type's keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `agent` | `"claude"` | Agent CLI. Only `claude` is supported. |
| `model` | the agent's own | Passed as `--model`. |
| `effort` | the agent's own | Passed as `--effort`. |
| `skill` | none | Skill invoked with the first prompt, such as `/tdd`. |
| `prompt` | none | Repo-relative template appended to the implement prompt. `{{ticket}}` and `{{ticket_path}}` are filled in, so it can point at links in the ticket, such as Figma designs. |

`hitl` is built in: a ticket of that type gets a guide-mode worker that isn't driven through phases (see [SKILL.md](SKILL.md#hitl-tickets)). It needs no entry, but `task_types.hitl` can still set its model, effort, skill or prompt.

A ticket's type is its `Type:` line, else `default_type`. Override it at Gate 1, or with `afk start <ticket> --type <name>`. An unknown type or agent fails the start before anything is created.

## Prose

Workers are told to read the rest of the file. Cover the following:

- **Pre-PR skill**: which skill runs the repo's checks in the prepr phase.
- **Dev server and isolation**: how to start it on `$AFK_PORT_BASE` and up, and how to keep databases or other shared state apart per `$AFK_SLOT`.
- **Verification recipes**: how to exercise the app, by browser for UI and by HTTP for APIs.

## Environment

Bootstrap commands and the worker get `AFK_SLOT` and `AFK_PORT_BASE`. The worker also gets `AFK_PROJECT` and `AFK_TICKET`. Slots start at 1, so the base ports stay free for your own dev server. A slot is held while the worker exists and freed when it is cleaned up.

## Overrides

Later layers win, key by key, so an override only needs the fields it changes:

1. `docs/agents/afk.md`, committed.
2. `docs/agents/afk.local.md`, gitignored, in the same `+++` format, for personal preferences such as model choice.
3. The `[overrides]` table in the project's `config.toml` (`$XDG_STATE_HOME/afk/<project>/config.toml`), for example:

   ```toml
   [overrides.task_types.frontend]
   model = "haiku"
   ```

## Repos

A project can span repos. The `[repos]` table in the project's `config.toml` lists their clones:

```toml
[repos]
"acme/widgets-api" = "/home/me/code/widgets-api"
```

`[repos]` maps a ticket's `Repo: owner/name` to its local clone; each clone brings its own `docs/agents/afk.md`. A ticket without a `Repo:` line, or naming the spec's own repo, uses the clone the project was created from.
