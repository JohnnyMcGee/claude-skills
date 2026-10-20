---
name: brief
description: Run the process pipeline by hand — sweep every active project profile's sources, triage, and show the markdown brief.
disable-model-invocation: true
argument-hint: [--delta] [--project <name>] [--fixtures <dir>]
---

# Brief

You run `process run`, which does all the work, then show me the brief.

1. **Run**: `process run <arguments as given>`, in the background, and wait for it to finish. It runs each phase (one sweeper per source, then triage) as headless `claude -p` under that phase's model and tool allowlist from the profile, then writes the brief. A full run can take several minutes.
   - `--delta`: sweep from each source's watermark and show only what this run added.
   - `--project <name>`: one profile only.
   - `--fixtures <dir>`: a dry run. Sweepers read `<dir>/<source>.json` instead of the services, and triage touches nothing. It checks the fixture tickets before it would create one. Run it as `XDG_STATE_HOME=$(mktemp -d) process run …`, a fresh directory every time. `process` refuses a dry run into any directory that already holds real state.
2. **Show**: print the brief file it names (`$XDG_STATE_HOME/process/brief.md`) as it is. Done when the brief is on screen.

Items from a Slack workspace or Google account that several profiles share, and that none of them claims, show under **Unsorted** at the end.

If `process` refuses a profile, show its message as it is. The profile is mine to fix, and no phase ran. A `⚠` line in the brief is a phase that failed: say which, and point at the phase's prompt (e.g. `/sweep-linear <profile>`) to rerun it by hand.
