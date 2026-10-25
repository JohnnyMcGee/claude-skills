---
name: afk-setup
description: Onboard this repo for AFK workers — write docs/agents/afk.md, create a pre-PR skill if missing, configure reviewer CLIs, and prove the config in a scratch worktree. First run on a machine also sets up tmux, notifications and the `afk` CLI.
disable-model-invocation: true
---

# AFK setup

Onboard the current repo so `/afk` can run workers in it. The `afk` skill lives beside this one at `${CLAUDE_SKILL_DIR}/../afk`; its [repo-config.md](../afk/repo-config.md) is the schema for everything you write here — read it before step 3.

Work through the steps in order. Each ends on a check; move on only when it passes.

## 1. Machine

Check each item and fix only the ones that fail, so a second run changes nothing:

- **`afk` on PATH.** Done when `readlink -f "$(command -v afk)"` equals `readlink -f ${CLAUDE_SKILL_DIR}/../afk/afk.py`. Otherwise `mkdir -p ~/.local/bin && ln -sf "$(readlink -f ${CLAUDE_SKILL_DIR}/../afk/afk.py)" ~/.local/bin/afk`, and tell the user if `~/.local/bin` is not on their `PATH`.
- **tmux window states.** Done when the user's tmux config (`~/.tmux.conf`, else `~/.config/tmux/tmux.conf`) has a `source-file` line for `afk.tmux.conf` from this folder. Otherwise append `source-file <absolute path of ${CLAUDE_SKILL_DIR}/afk.tmux.conf>` to it, then run `tmux source-file` on that config if a server is running. The snippet colours worker windows by state and keeps the user's own style for every other window.
- **Desktop notifications.** Done when `command -v notify-send` succeeds. Otherwise tell the user which package provides it on their system (`libnotify-bin` on Debian/Ubuntu, `libnotify` elsewhere) and carry on: `afk` still rings tmux without it.

Report one line per item: already done, fixed, or needs the user.

## 2. Inspect the repo

Everything afk.md needs has a default you can read from the repo. Find evidence for each before asking the user anything:

- **Existing config.** If `docs/agents/afk.md` exists, this is a re-run: run `afk check`, fix what it reports with the user, and skip to step 4.
- **Base branch**: `git symbolic-ref refs/remotes/origin/HEAD`, else the branch PRs merge into.
- **Files to copy**: gitignored files a fresh clone needs to run (`.env`, local config, credentials files) — `git ls-files --others --ignored --exclude-standard --directory` in the main checkout, minus build output, dependencies and caches.
- **Bootstrap**: the install and setup commands from the README, CI workflows, `Makefile`, `package.json` scripts or equivalent — what a fresh worktree needs before tests and the dev server run.
- **Dev server and isolation**: the command, how it takes a port (flag, `PORT`, config file), every other port it binds, and any shared state (database, cache, queue, file paths). Each is either isolatable per slot (port from `$AFK_PORT_BASE` and up, database named with `$AFK_SLOT`) or shared, which means `verify_concurrency = 1`.
- **Verification**: UI (browser checks) or API (HTTP checks), and any seed data or login a check needs.
- **Pre-PR checks**: the lint, type, test and build jobs CI runs on a PR, and an existing pre-PR skill (`.claude/skills/*/SKILL.md`, or one named in `AGENTS.md`/`CLAUDE.md`).
- **Task types**: the kinds of tickets this repo gets (backend, frontend, infra…), judged from its layout and recent PRs.

Done when every bullet has a proposed answer with its source, or is marked unknown.

## 3. Interview and write afk.md

Show the user your proposals as one list — answer and source per item — then ask about the unknowns and anything you inferred rather than read. For task types, propose `model`, `effort` and `skill` (`/tdd` for code with tests) per type and a `default_type`. Ask one question at a time; take the user's answer over the evidence.

Write `docs/agents/afk.md`: TOML frontmatter for the keys, then prose with these sections, which workers are pointed at:

- `## Pre-PR skill` — the skill's name (step 4) and when to run it.
- `## Dev server` — the exact start command using `$AFK_PORT_BASE`, how shared state is kept apart per `$AFK_SLOT`, and how to stop it.
- `## Verification recipes` — how to exercise the app: URLs and flows for a browser, or requests for an API.

Cache only what a worker can't find by looking: the gotchas, the reasons, the non-obvious commands. Add `docs/agents/afk.local.md` to `.gitignore` for personal overrides.

Done when `afk check` prints `is valid` with no warnings.

## 4. Pre-PR skill

If step 2 found a pre-PR skill, check it runs the repo's checks and nothing else, name it in afk.md, and move on.

Otherwise create `.claude/skills/pre-pr/SKILL.md` from [pre-pr-template.md](pre-pr-template.md), filled with this repo's CI checks. Keep `/afk`'s generic steps (merge-conflict check, code review) out of it: the prepr phase runs those itself.

Done when the skill exists, afk.md names it, and each command in it exits 0 on the base branch (or fails for a reason the user confirms is pre-existing).

## 5. Reviewers

A project in local landing mode opens no PR, so its critique phase runs the repo's `reviewers` commands instead of bot reviews (see [repo-config.md](../afk/repo-config.md#reviewers)). Configure them from the reviewer CLIs installed here. Never install one yourself.

1. **Detect.** `command -v coderabbit` and `command -v copilot`. For each one missing, give the user its install hint and leave it out:
   - CodeRabbit: `curl -fsSL https://cli.coderabbit.ai/install.sh | sh`, then `coderabbit auth login`.
   - Copilot: `npm install -g @github/copilot` (or `brew install copilot-cli`), then `copilot login`.
2. **Propose** a command for each one found. Each runs headless, prints plain-text findings, and reviews `git diff {{target}}...HEAD` with nothing that can write: no file edits, no pushes, no PR or issue comments.
   - CodeRabbit: `NO_COLOR=1 coderabbit review --committed --base {{target}}`. Its default mode is plain text, and `NO_COLOR` guards against colour codes. Each finding's `file:line` still comes wrapped in a terminal-hyperlink escape, which nothing turns off; readers can ignore it. Its `--agent` mode prints JSON lines instead, so it isn't the plain text critique expects.
   - Copilot: `copilot -p "Review the changes in git diff {{target}}...HEAD as a code reviewer. Print each finding as plain text: file:line, severity, the problem in one line, the fix in one line. Print 'No findings.' if there are none. Don't change any file." -s --no-color --no-ask-user --disable-builtin-mcps --allow-tool 'shell(git diff:*)' --allow-tool 'shell(git log:*)' --allow-tool 'shell(git show:*)' --deny-tool write`. `--disable-builtin-mcps` drops the GitHub MCP server, so it can't write to GitHub; the allow-list lets it read the diff without prompting.

   Keep any `reviewers` afk.md already has; show the user the proposals, with the missing CLIs and their hints, and take their edits.
3. **Write** the approved commands to `reviewers` in afk.md's frontmatter. With none approved, leave the key out: critique falls back to a code-review subagent.

Done when `afk check` prints `is valid` with no reviewer warnings and afk.md holds the approved commands. They are proven in step 6.

## 6. Prove it

Run the proof in one pass, recording each step as ok or failed:

1. `afk trial` — creates a scratch worktree from base on branch `afk/trial`, copies the files and runs bootstrap on trial slot 9, printing each step. Its last line gives the worktree and the `AFK_SLOT`/`AFK_PORT_BASE` to export.
2. **Dev server up.** In that worktree with those variables exported, start the server exactly as afk.md's `## Dev server` says, in the background with its output to a log file. Done when its port answers (`curl -s -o /dev/null -w '%{http_code}' localhost:<port>`) within two minutes. Skip this step and the next when afk.md says there is no dev server.
3. **Dev server down.** Stop it as afk.md says. Done when the port no longer answers and no process from the worktree is left.
4. **Reviewers.** Skip when afk.md has no `reviewers`. In the worktree, commit a small trial diff on `afk/trial`: a new file of a few lines with an obvious bug, such as an off-by-one. Run each command with `{{target}}` replaced by base, its stdin from `/dev/null`, under a 15-minute timeout (CodeRabbit says a review can take that long; most take one or two minutes). Done for a command when it exits 0 without prompting, prints its findings or says it found none, and leaves `git status` clean with HEAD unchanged; nothing in it pushes or comments, by construction of step 5.
5. `afk trial --teardown` — removes the worktree and branch, with the trial commit.

On a failure, stop the server if it is up, run step 5, and report exactly which step failed with the last lines of its output. Fix the cause in afk.md, bootstrap or the repo with the user, then rerun the whole proof. A reviewer that can't be made to pass comes out of `reviewers`, with the user's agreement. The proof passes when every step is ok or skipped in a single run.

## 7. Hand off

Show the user `git status` and the diff of afk.md, `.gitignore` and the pre-PR skill, with the proof result and any reviewer CLIs left out for not being installed. Commit only when they approve. Then they can start a project with `/afk init <spec>`.
