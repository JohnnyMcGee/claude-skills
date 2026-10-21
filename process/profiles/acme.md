+++
# An example profile. Copy it to ~/.config/process/profiles/<name>.md, rename `name` to match, and fill in your
# project's values. Real profiles are personal: keep them out of this repo.
name = "acme"
active = true
# Headless phases run here, so project-scoped MCP servers (e.g. a Linear server in the repo's .mcp.json) load.
workdir = "~/code/widgets"
timezone = "America/New_York"
sources = ["github", "linear", "slack", "gmail", "calendar", "meetings"]

[github]
repo = "acme/widgets"
review_skill = "/review"

[linear]
team = "WID"

[slack]
# Profiles naming the same workspace share one Slack sweep; emit attributes each item with [match].
workspace = "acme"
# Channels swept even without a mention.
watchlist = ["#incidents", "#deploys"]

[google]
# Gmail, Calendar and Meetings read this account. Profiles naming the same account share one sweep of each.
account = "me@acme.example"
# Declining a meeting my manager organizes or attends is never proposed.
manager = "mo@acme.example"
# Noise mail is filed under this label as it is archived. Create it in Gmail first: triage cannot create labels.
noise_label = "Noise"

[match]
# How items from a shared Slack workspace or Google account are attributed to this project, besides the repo, the
# Linear team's ticket prefix and the watchlist. Items no profile claims land in the brief's Unsorted section, except
# Gmail noise, which stays with the profile that swept it so its triage archives it.
keywords = ["widgets"]
channels = ["#widgets"]
senders = ["@acme.example"]

[urgency]
# The only things that interrupt me (proc notify). Everything else waits for the brief.
# A DM or a mention from these people, by the name each source shows (Slack and Linear name, GitHub login), any case.
people = ["Mo", "mo-acme"]
# A mention of me in these Slack channels (glob patterns allowed). Other messages there are not urgent.
channels = ["#incidents", "#incident-*"]
# A review request whose PR carries one of these labels.
labels = ["deploy-blocking"]

[autonomy]
# Action types promoted from draft to silent, one at a time, once they have been right 10+ times in a row.
silent = []

[afk]
# A Todo ticket is trivial enough for an AFK worker when it has one of these estimates or labels, none of the excluded
# labels, and nothing blocking it. Approved AFK launches run at most max_workers at a time, each opening a draft PR.
estimates = ["XS", "S"]
labels = ["Cleanup", "Technical Debt"]
exclude_labels = ["Decompose", "Needs Design"]
max_workers = 2

[phases.sweep-github]
model = "sonnet"
allowed_tools = [
  "Read",
  "Bash(date -u:*)",
  "Bash(process profile:*)",
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
  "Bash(process profile:*)",
  "Bash(process watermark:*)",
  "Bash(process emit:*)",
  "mcp__linear-server__get_notifications",
  "mcp__linear-server__list_issues",
  "mcp__linear-server__get_issue",
  "mcp__linear-server__list_comments",
  "mcp__linear-server__get_user",
]

[phases.sweep-slack]
model = "sonnet"
allowed_tools = [
  "Read",
  "Bash(date -u:*)",
  "Bash(process profile:*)",
  "Bash(process watermark:*)",
  "Bash(process emit:*)",
  "mcp__slack__slack_read_user_profile",
  "mcp__slack__slack_search_public_and_private",
  "mcp__slack__slack_read_channel",
  "mcp__slack__slack_read_thread",
  "mcp__slack__slack_get_reactions",
]

[phases.sweep-gmail]
model = "sonnet"
allowed_tools = [
  "Read",
  "Bash(date -u:*)",
  "Bash(process profile:*)",
  "Bash(process watermark:*)",
  "Bash(process emit:*)",
  "mcp__claude_ai_Gmail__search_threads",
  "mcp__claude_ai_Gmail__get_thread",
]

[phases.sweep-calendar]
model = "sonnet"
allowed_tools = [
  "Read",
  "Bash(date -u:*)",
  "Bash(process profile:*)",
  "Bash(process watermark:*)",
  "Bash(process emit:*)",
  "mcp__claude_ai_Google_Calendar__list_events",
  "mcp__claude_ai_Google_Calendar__get_event",
]

[phases.sweep-meetings]
model = "sonnet"
allowed_tools = [
  "Read",
  "Bash(date -u:*)",
  "Bash(process profile:*)",
  "Bash(process watermark:*)",
  "Bash(process emit:*)",
  "mcp__claude_ai_Google_Calendar__list_events",
  "mcp__claude_ai_Google_Drive__search_files",
  "mcp__claude_ai_Google_Drive__get_file_metadata",
  "mcp__claude_ai_Google_Drive__read_file_content",
]

[phases.triage]
model = "opus"
# The silent writes live here and nowhere else: marking Linear notifications read, creating tickets assigned to me,
# and labeling and archiving Gmail noise. No send, reply, RSVP, review or merge tool: drafts wait for my approval.
allowed_tools = [
  "Read",
  "Bash(process profile:*)",
  "Bash(process triage:*)",
  "mcp__linear-server__mark_notification",
  "mcp__linear-server__create_issue",
  "mcp__linear-server__list_issues",
  "mcp__linear-server__get_issue",
  "mcp__linear-server__list_comments",
  "mcp__github__pull_request_read",
  "mcp__slack__slack_read_thread",
  "mcp__claude_ai_Gmail__get_thread",
  "mcp__claude_ai_Gmail__list_labels",
  "mcp__claude_ai_Gmail__label_thread",
  "mcp__claude_ai_Gmail__unlabel_thread",
  "mcp__claude_ai_Google_Calendar__get_event",
]

# Next work: read-only, like a sweeper. `process next` ranks what it reads and queues promotions and AFK launches.
[phases.next-work]
model = "opus"
allowed_tools = [
  "Read",
  "Bash(date -u:*)",
  "Bash(process profile:*)",
  "Bash(process next:*)",
  "mcp__linear-server__get_user",
  "mcp__linear-server__list_issues",
  "mcp__linear-server__get_issue",
  "mcp__linear-server__get_project",
]

# The executor: the only phase that may send, and only what I approved (or promoted to silent). It sends and pushes
# through `process execute` steps, which post exactly the approved text and check the PR is mine, so it holds no GitHub
# write tool. Each step is named: a bare `process execute` would start a second executor. It reads only its worktree.
# Its one Linear write moves a ticket whose promotion I approved from Backlog to Todo. AFK launches need no tool here:
# `process execute` writes the afk spec and starts the worker itself.
[phases.execute]
model = "opus"
allowed_tools = [
  "Bash(process profile:*)",
  "Bash(process execute plan:*)",
  "Bash(process execute review:*)",
  "Bash(process execute reply:*)",
  "Bash(process execute checkout:*)",
  "Bash(process execute git:*)",
  "Bash(process execute push:*)",
  "Bash(process execute result:*)",
  "mcp__github__pull_request_read",
  "mcp__linear-server__get_issue",
  "mcp__linear-server__list_issue_statuses",
  "mcp__linear-server__save_issue",
]
+++

# Acme widgets

An example project: GitHub `acme/widgets`, Linear team `WID`, Slack workspace `acme`, Google account `me@acme.example`.

## How to sweep

Free text the sweepers and triage read before every run: what matters on this project and what is noise. The executor appends lessons here only from reasons you typed, and logs each one in the Changelog.

- **Review requests.** Draft each review with the repo's review skill (`review_skill` above). A review draft is never an approving verdict.
- **Linear mirrors GitHub.** Linear's GitHub integration posts a notification for review requests and PR comments on linked tickets. Give those items the PR URL as their subject so triage merges them with the GitHub sweeper's item.
- **Slack asks.** An immediate ask (do X today, can you look at Y) becomes a ticket; a question gets a drafted reply. `#incidents` matters even without a mention.
- **Gmail noise.** Vendor newsletters and CI notification mail are noise. Mail from people at acme.example never is.
- **Meetings.** Only the Widgets standup and planning meetings have Gemini notes worth reading.
- **Next work.** Note here which Linear projects or labels matter most this cycle, so ties in the ranking read right.
- **Volume.** Note the usual daily volume here, so a sweep that finds far more than that is a sign it is re-reading old items.

## Changelog

- 2026-10-20 — Created with the GitHub and Linear sources.
- 2026-10-20 — Added the Slack, Gmail, Calendar and Meetings sources.
- 2026-10-20 — Added next-work and AFK rules.
