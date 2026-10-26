---
name: pre-pr
description: Run claude-skills' pre-PR checks — every Python test suite, skill frontmatter, and README coverage of each skill. Use before committing for review, pushing, or opening or updating a PR.
---

# Pre-PR

The repo's own gate before a PR goes up. There is no CI, so these checks are the only gate.

## Steps

1. **Tests:** for each folder holding `test_*.py` files, run `python3 -m unittest` from inside that folder:
   `export TMPDIR=$(python3 -c 'import os,tempfile;print(os.path.realpath(tempfile.gettempdir()))'); for d in $(git ls-files '*test_*.py' | xargs -n1 dirname | sort -u); do (cd "$d" && python3 -m unittest) || echo "FAILED: $d"; done`
   Tests import their module by its bare name, so they must run from their own folder. TMPDIR is resolved first because on macOS it sits under the `/var` → `/private/var` symlink, and afk's tests compare temp paths against resolved ones.
2. **Skill frontmatter:** every top-level folder with a `SKILL.md` (and `.claude/skills/*/SKILL.md`) opens with `---` frontmatter that has a `name` matching the folder and a non-empty `description`:
   `for f in */SKILL.md .claude/skills/*/SKILL.md; do d=$(basename "$(dirname "$f")"); head -1 "$f" | grep -qx -- '---' && grep -qx "name: $d" "$f" && grep -q '^description: .' "$f" || echo "BAD FRONTMATTER: $f"; done`
3. **README coverage:** every top-level skill folder has a `### <folder>` section in `README.md` and appears in the Installation `cp -r` lines:
   `for f in */SKILL.md; do d=$(dirname "$f"); grep -qx "### $d" README.md || echo "README missing section: $d"; grep -q "cp -r .*\b$d\b" README.md || echo "README install missing: $d"; done`

Fix what each check reports and re-run it until it passes. A failure you can prove is not caused by the diff (it also fails on `main`) is pre-existing: report it and carry on.

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

- 2026-10-20 — Created by /afk-setup. The repo has no CI; checks mirror the README's test instructions and its skill-listing convention.
- 2026-10-25 — Step 1 resolves TMPDIR first: on macOS the plain command failed afk with 22 failures and 6 errors (on `main` too), which all pass once `/var` is resolved to `/private/var`.
