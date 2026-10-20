+++
# An example profile. Copy it to ~/.config/process/profiles/<name>.md, rename `name` to match, and fill in your
# project's values. Real profiles are personal: keep them out of this repo.
name = "acme"
active = true
# Headless phases run here, so project-scoped MCP servers (e.g. a Linear server in the repo's .mcp.json) load.
workdir = "~/code/widgets"
timezone = "America/New_York"
sources = ["github", "linear"]

[github]
repo = "acme/widgets"
review_skill = "/review"

[linear]
team = "WID"

[google]
account = "me@acme.example"

[urgency]
people = ["my-manager"]
labels = ["deploy-blocking"]

[autonomy]
# Action types promoted from draft to silent, one at a time, once they have been right 10+ times in a row.
silent = []

[phases.sweep-github]
model = "sonnet"
allowed_tools = [
  "Read",
  "Bash(date -u:*)",
  "Bash(process watermark:*)",
  "Bash(process emit:*)",
  "Bash(gh pr list:*)",
  "Bash(gh pr view:*)",
  "Bash(gh pr checks:*)",
  "mcp__github__get_me",
  "mcp__github__pull_request_read",
  "mcp__github__search_pull_requests",
]

[phases.sweep-linear]
model = "sonnet"
allowed_tools = [
  "Read",
  "Bash(date -u:*)",
  "Bash(process watermark:*)",
  "Bash(process emit:*)",
  "mcp__linear-server__get_notifications",
  "mcp__linear-server__list_issues",
  "mcp__linear-server__get_issue",
  "mcp__linear-server__list_comments",
  "mcp__linear-server__get_user",
]

[phases.triage]
model = "opus"
allowed_tools = [
  "Read",
  "Bash(process triage:*)",
  "mcp__linear-server__mark_notification",
  "mcp__linear-server__get_issue",
  "mcp__linear-server__list_comments",
  "mcp__github__pull_request_read",
]
+++

# Acme widgets

An example project: GitHub `acme/widgets`, Linear team `WID`, Google account `me@acme.example`.

## How to sweep

Free text the sweepers and triage read before every run: what matters on this project and what is noise. The executor appends lessons here only from reasons you typed, and logs each one in the Changelog.

- **Review requests.** Draft each review with the repo's review skill (`review_skill` above). A review draft is never an approving verdict.
- **Linear mirrors GitHub.** Linear's GitHub integration posts a notification for review requests and PR comments on linked tickets. Give those items the PR URL as their subject so triage merges them with the GitHub sweeper's item.
- **Volume.** Note the usual daily volume here, so a sweep that finds far more than that is a sign it is re-reading old items.

## Changelog

- 2026-10-20 — Created with the GitHub and Linear sources.
