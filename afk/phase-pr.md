AFK phase: pr — ticket {{ticket}} ({{ticket_path}})

The branch is ready. Push it and run `/open-pr {{base}}` until the PR is **Ready**: checks green, no conflicts, bot threads resolved. Re-run it as many times as it takes. Never merge and never request a human reviewer.

When the PR is Ready, run `afk report done "<PR url>"`. If `/open-pr` finishes Blocked, run `afk report blocked "<PR url>: <why>"`.
