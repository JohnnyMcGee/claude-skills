AFK phase: pr — ticket {{ticket}} ({{ticket_path}})

The branch is ready. Push it and run `/open-pr {{base}}` until the PR is **Ready**: checks green, no conflicts, bot threads resolved. Re-run it as many times as it takes. Never merge and never request a human reviewer.

When the PR is Ready, run `afk report done "<one-line summary>"`. If `/open-pr` finishes Blocked, run `afk report blocked "<why>"`. afk finds the PR by your branch, so you don't need to report its URL.
