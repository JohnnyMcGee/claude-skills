---
name: atomic-commits
description: Turn-based coding loop — plan a feature as a checklist of atomic commits, then build, stage, and land them one at a time with user review between each.
disable-model-invocation: true
argument-hint: <issue id, or a description of the work>
---

# Atomic Commits

A turn-based loop: you build one **atomic** commit at a time — the smallest complete change that leaves the repo working — and the user reviews every one before it lands. The user is the gate: you stop only when there is something for them to review, and start the next item the moment they approve.

## Branch guardrail

All work happens on a feature branch the user checked out for this specific feature. If the current branch is the default branch or belongs to other work, stop and ask the user to check out a feature branch, then wait. Branch creation and switching belong to the user; every commit you make goes to the plan's branch.

## 1. Plan

Input: a ticket/issue (fetch its full description and acceptance criteria) or a description of the work.

**Resume branch:** if `.plans/` already holds a plan for the current branch, read it — Notes included — and pick up the first unchecked item at step 2.

Otherwise, explore the code enough to break the work down, then write `.plans/<feature-slug>.md`:

```markdown
---
issue: ENG-1234
url: https://linear.app/...
---

One-paragraph description of what's being built.

## Commits

- [ ] feat: first atomic commit
- [ ] feat: second atomic commit
- [ ] 🔍 Smoke test: what the user can try at this point

## Notes
```

Each checklist item is one **atomic** commit: it compiles, passes tests, and could be reverted on its own. Order items by dependency and slice vertically — a thin end-to-end sliver first, then widen — so the feature is smoke-testable early and often. Interleave `🔍 Smoke test` checkpoints wherever the user can meaningfully try the feature.

Stop and ask the user to review the plan. This step is complete when the doc is written and you are waiting. The user may edit the file directly, ask you for changes, or say **"go"**.

## 2. Build one commit

Write only the code for the first unchecked item — later items stay untouched, even when the code is nearby. Use /tdd for backend code and pure frontend logic; UI components are verified by the user's smoke tests instead.

Stage the changes (`git add`) and stop: tell the user what's staged and ask them to review. This step is complete when the item's changes are staged, nothing is committed, and you are waiting. The user may edit the code directly (restage after), ask you for changes, or say **"commit"** / **"next"**.

## 3. Commit and roll on

On the user's go-ahead:

1. Commit with a [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) message, following any repo-specific commit conventions on top.
2. In the plan doc, check the item off and append the short sha: `- [x] feat: ... (abc1234)`.
3. Immediately begin step 2 on the next unchecked item — the user should only ever be waiting on a review, never on you.

When the next item is a `🔍 Smoke test` checkpoint, remind the user to run it; if they skip it, check it off and move on.

## Throughout

- A newly discovered subtask — found by you or raised by the user — becomes its own checklist item, placed by dependency order.
- Anything non-obvious you learn goes under **Notes** in the plan doc. Doc plus branch must be enough for a fresh agent to resume mid-feature.
- Every few commits, run the repo's lint and type-check as a sanity check; fix what they surface as part of the current item.

## 4. Finish

When every item is checked, re-read the issue's requirements and acceptance criteria and account for each one explicitly — a gap becomes a new checklist item and you return to step 2. This step is complete only when every criterion maps to landed work. Then ask: **"Would you like me to open a PR?"** — the user may open it themselves, keep editing, or say "open a PR", in which case you do.
