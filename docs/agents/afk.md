+++
base = "main"
default_type = "skill"

[task_types.skill]
model = "opus"
effort = "high"
skill = "/tdd"

[task_types.docs]
model = "sonnet"
effort = "medium"
+++

# AFK

This repo is a collection of Claude Code skills: one folder per skill with a `SKILL.md`, plus any supporting files and Python tests (stdlib only, Python 3.11+, no install step). There is no app and no dev server.

## Pre-PR skill

Run `/pre-pr` in the prepr phase, and again after any merge from `main`. It runs every test suite and checks that skill folders and the README are consistent.

## Dev server

None. What stands in for one is a skill's state directory. Skills that keep state under `$XDG_STATE_HOME` (afk, process) must be pointed at a per-slot directory during verification, e.g. `/tmp/afk-state-$AFK_SLOT`.

Set `XDG_STATE_HOME` **on the command only**, never `export` it in your shell: `afk report` reads the same variable to find this worker's status, so an exported override hides your reports from the watcher. Remove the directory when you are done: `rm -rf /tmp/afk-state-$AFK_SLOT`.

## Verification recipes

1. **Tests.** In each folder that has `test_*.py`, run `python3 -m unittest` from that folder (e.g. `cd afk && python3 -m unittest`). Tests import their module by its bare name, so running from the repo root fails.
2. **Skill dry run.** For a skill with a fixture or dry-run mode, run it headless against its fixtures with no network, with state isolated per slot:
   `XDG_STATE_HOME=/tmp/afk-state-$AFK_SLOT claude -p "/<skill> <args>" --allowedTools "<the phase's allowlist>"`
   Then inspect the files it wrote under `/tmp/afk-state-$AFK_SLOT`: schema validity, which items exist, ledger and watermark changes. Do this once per acceptance criterion the dry run can show.
3. **Live runs are the human's.** Never run a skill against real GitHub, Linear, Slack, Gmail or Calendar accounts. That includes "read-only" sweeps, because they can mark notifications read. For an acceptance criterion that needs a live run (e.g. "a live run on Maxim produces a readable brief"), verify everything you can with fixtures, then list the exact live-run command and what to look for under **Manual verification** in the PR description. Say in your `afk report done` message that a live run is still pending.
4. **afk itself.** Changes to the afk loop also have the manual end-to-end run in `afk/e2e-checklist.md`. Don't run it from inside a worker; note in the PR which steps it affects.
