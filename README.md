# claude-skills

A small collection of [Claude Code skills](https://code.claude.com/docs/en/skills).

## Skills

### atomic-commits

A turn-based coding loop: plan a feature as a checklist of atomic commits, then build, stage, and land them one at a time with user review between each. The user is the gate — the agent stops only when there's something to review, and starts the next item the moment it's approved.

Invoke with `/atomic-commits <issue id or description of the work>`.

### brief-me

Builds a four-section dossier (Business Context, Technical Context, Possible Approaches, Risks) on a PR, issue, or open-ended task using parallel research subagents, then walks you through it as a user-paced tour. Useful for getting up to speed before reviewing or starting work on something.

Invoke with `/brief-me <PR number, ticket id, or task description>`.

## Installation

Copy the skill folders into your skills directory:

```bash
# Personal (all projects)
cp -r atomic-commits brief-me ~/.claude/skills/

# Or per-project
cp -r atomic-commits brief-me <project>/.claude/skills/
```

Each skill is a folder containing a `SKILL.md` (plus any supporting files) — see the [skills documentation](https://code.claude.com/docs/en/skills) for details.
