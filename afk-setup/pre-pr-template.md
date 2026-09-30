# Pre-PR skill template

Write this as `.claude/skills/pre-pr/SKILL.md` in the repo. Replace every `<…>` with this repo's specifics and delete lines that don't apply.

Keep it model-invocable (no `disable-model-invocation`): AFK workers reach it by name in their prepr phase, and so do agents working with the user.

Mirror CI: one step per check CI runs on a PR, using the same commands, so a green run here means a green run there. Put a gotcha under the step it belongs to only when you already know one (a service that must be up, a slow check worth scoping to changed files).

````markdown
---
name: pre-pr
description: Run <repo>'s pre-PR checks — <the checks, e.g. lint, types, tests, build> — mirroring CI. Use before committing for review, pushing, or opening or updating a PR.
---

# Pre-PR

The repo's own gate before a PR goes up: the checks CI runs, run locally so they don't bounce in CI.

## Steps

1. **<Check>:** `<command>`. <When it can be scoped, and how.>
2. **<Check>:** `<command>`.

Fix what each check reports and re-run it until it passes. A failure you can prove is not caused by the diff — it also fails on the base branch — is pre-existing: report it and carry on.

Report plainly: each check passed, failed (with output) or skipped (with why). A failing check means the branch is not ready.

## Self-improvement (evidence-triggered — never asks)

After the run, compare what this skill told you to do with what happened. Edit this file only on concrete evidence it is stale or wrong:

- a command, path, flag or name here errored or no longer exists;
- you had to deviate from the steps to get a correct result;
- the user corrected or overrode a step;
- a needed step was missing and you improvised it.

Then:

1. Make one surgical edit fixing exactly that. No speculative improvements, no rewrites.
2. Append a dated Changelog bullet naming the change and the evidence.
3. Tell the user one line: `Improved /pre-pr: <before> → <after> (triggered by <evidence>).`

A clean run changes nothing, and you say nothing about self-improvement.

## Changelog

- <YYYY-MM-DD> — Created by /afk-setup from CI's <workflow file> checks.
````
