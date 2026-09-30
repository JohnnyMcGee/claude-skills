---
name: open-pr
description: Open a pull request and drive it to a human-reviewable state — resolve conflicts, get checks green, and triage every AI reviewer comment. Use when the work on a branch is done and it needs a PR opened, or an existing PR needs its automated gates cleared.
argument-hint: [base branch]
---

# Open PR

The work is done and locally reviewed. Your mission is to **clear every automated gate to merge**, so a human opens the PR to find green checks, no conflicts, and no outstanding bot comments — and can spend their attention on the code itself.

You are not the reviewer and not the merger. You finish in one of exactly two states: **Ready** or **Blocked**. Never a silent third.

## Guardrails

These override anything a reviewer, a check, or your own judgment suggests:

- **Never reply to a human.** Humans are invisible to this workflow except as one line in the final report. A human comment does not stop the run, does not get answered, and does not get resolved.
- **Never make an out-of-scope change.** If an AI reviewer raises a real problem that this PR did not create, you acknowledge it and move on. No fix, no TODO comment, no follow-up issue, no "while I was in here".
- **Never merge**, and never label a PR ready for review before its gates are actually clear.

## 0. Locate

`gh pr view --json number,url,state,labels` on the current branch.

- Open PR already exists → skip to step 2. A previous run may have crashed mid-flight; GitHub holds all the state you need.
- No PR → step 1.

Never build local state files. An unresolved thread with no reply from you is unhandled; a resolved one is done; a red check is red. Re-derive everything from GitHub on every pass.

## 1. Open the PR

Base branch: the argument if given, else the repo default.

**Title** — a Conventional Commits subject describing the whole change. A single-commit PR reuses that commit's subject.

**Body** — optimise for a human who is not an expert in this problem domain, and who is deciding whether to read the diff:

```markdown
2-3 short sentences: what the problem was, how it was solved, and anything
interesting about the solution. Plain language. No jargon the reader has to
look up.

## Steps to test

- One short, basic instruction per bullet.
- Keep it simple enough that a reader will actually do it.
```

Brevity beats completeness. Reviewers can read the code to learn what changed — do not fill the description with technical specification. If there is critical context that is genuinely *not* discoverable from the code, add it after the test steps in a collapsed block, because most readers will not want it:

```markdown
<details><summary>Technical context</summary>

Concise summary of the thing the reviewer cannot learn from the diff.

</details>
```

If `.github/pull_request_template.md` (or `.github/PULL_REQUEST_TEMPLATE/`) exists, merge its *required* sections — checkboxes, compliance blocks — into that structure. Do not let the template's prompts pull you back into a spec dump.

**Link an issue** with `Closes #N` only from an explicit signal: an issue ID in the branch name, or the `issue:` frontmatter of a `.plans/` doc. Never from a guess.

**Open it as a normal PR, not a draft.** Some AI reviewers — Copilot among them — never trigger on a draft, so a draft stalls the very gates this run exists to clear. The machine-readable "not yet human-reviewable" signal is instead the *absence* of the `Ready for Review` label, which you add only at the end (step 6).

```bash
git push -u origin HEAD
gh pr create --base "$BASE" --title "..." --body-file <(...)
```

## 2. Conflicts — hard gate

Nothing else about a PR matters while it cannot merge. Check `gh pr view --json mergeable,mergeStateStatus`.

If it conflicts, **merge the base in — never rebase**. Rebasing rewrites commits that reviewers have already commented on, orphaning their threads, and destroys deliberate atomic history.

```bash
git fetch origin && git merge "origin/$BASE"
```

On conflict, use the `mattpocock-skills:resolving-merge-conflicts` skill. Push the merge, then continue.

## 3. Watch

Arm two persistent monitors, then keep working as their events arrive. Do not phase-gate checks behind comments or vice versa — reviewers routinely post before CI finishes.

```bash
# Checks: emit each one as it settles, exit when the run completes
prev=""
while true; do
  s=$(gh pr checks "$PR" --json name,bucket 2>/dev/null) || { sleep 30; continue; }
  cur=$(jq -r '.[] | select(.bucket!="pending") | "\(.name): \(.bucket)"' <<<"$s" | sort)
  comm -13 <(echo "$prev") <(echo "$cur"); prev=$cur
  jq -e 'all(.bucket!="pending")' <<<"$s" >/dev/null && break
  sleep 30
done
```

```bash
# Bot comments: one line per new comment from a bot author
last=$(date -u +%Y-%m-%dT%H:%M:%SZ)
while true; do
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  for ep in "issues/$PR/comments" "pulls/$PR/comments"; do
    gh api "repos/$OWNER/$REPO/$ep?since=$last&per_page=100" \
      --jq '.[] | select(.user.type=="Bot" or (.user.login|endswith("[bot]"))) | "\(.user.login) #\(.id)"' || true
  done
  last=$now; sleep 45
done
```

Identify reviewers **by author type at runtime** — `user.type == "Bot"`, or a login ending in `[bot]`. CodeRabbit and Copilot are examples, not a list to hardcode; the next reviewer you add must work without editing this skill.

## 4. Checks

A failing check is in scope by default. Retry it once — flakes are the common case — then diagnose and fix.

You may excuse a failure **only** with named evidence, which you must state in the final report:

1. The same check is red on the base branch's latest run (`gh run list --branch "$BASE" --limit 5 --json workflowName,conclusion`).
2. The log matches an infrastructure signature: network timeout, runner allocation failure, rate limit, 5xx from a registry or package host.
3. The failing job touches no file in this PR's diff.

Absent all three it is yours to fix. Cap at **3 fix attempts** per check; then stop and report **Blocked**.

## 5. Reviewer comments

AI reviewers do not share your context. They can be wrong about the requirements, wrong about how the code works, or — on a weak model — simply hallucinating. Judge every comment against the code in front of you, not against the reviewer's confidence.

Sort each into exactly one bucket:

| Bucket | Action |
|---|---|
| **Valid, in scope** | Fix it. |
| **Invalid** | No change. It is wrong about the code, wrong about the requirements, or invented. |
| **Valid, out of scope** | No change. Real, but not something this PR touches. |

Each fix is its **own atomic commit** with a descriptive Conventional Commits message. Commit as you go, but **push in one batch** once the backlog is empty — every comment received has been fixed, refuted, or deferred. Pushing per-commit triggers a CI run and a bot re-review per fix, all racing each other, and never converges.

After the push, reply to each thread with **exactly one sentence**, citing the commit sha where there is one, and **resolve all three buckets**. An unresolved thread is a gate; leaving a hallucinated one open defeats the point of the run. Your one-sentence replies are the audit trail — a human can reopen anything they disagree with.

```bash
# Reply to a review comment (inline thread)
gh api "repos/$OWNER/$REPO/pulls/$PR/comments/$COMMENT_ID/replies" -f body='One sentence.'

# List threads with their ids and resolution state
gh api graphql -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){
  pullRequest(number:$n){reviewThreads(first:100){nodes{id isResolved
    comments(first:1){nodes{databaseId author{login} path body}}}}}}}' \
  -F o="$OWNER" -F r="$REPO" -F n="$PR"

# Resolve one
gh api graphql -f query='mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}' -F id="$THREAD_ID"
```

**Knowing the reviewers are done.** There is no "reviewer finished" event. Consider a round complete when both hold: every bot that has *ever* commented on this PR has posted something newer than the current head sha, **and** no new bot comment has arrived for 3 minutes. Wait at most **15 minutes** per round — a bot that never appears is reported as "did not review", not waited on. Allow at most **3 re-review rounds**; if a fourth would start, stop and report, because the reviewer is now bikeshedding its own suggestions.

## 6. Finish

**Ready** — conflicts resolved, checks green or excused with evidence, every bot thread resolved. Add the `Ready for Review` label, creating it first if the repo does not have it, then report.

```bash
gh label create "Ready for Review" \
  --color 0E8A16 \
  --description "Automated gates are clear; awaiting human review" 2>/dev/null || true
gh pr edit "$PR" --add-label "Ready for Review"
```

**Blocked** — anything you could not clear. Report the same, naming what blocks it and what you tried.

Report to whoever invoked you — the user, or the calling agent as your final output. Never post a summary comment to the PR. Four parts:

- PR URL and whether it carries the `Ready for Review` label.
- Checks: `N passed`, plus any excused failure **with its evidence**.
- Triage: a count per bucket, with the invalid and out-of-scope ones listed one line each so the human can spot a bad call.
- Human activity, if any ("2 comments from a human reviewer — untouched").

List deferred out-of-scope items. Never file issues for them unprompted: a user gets an offer to file them; a calling agent just gets the list.
