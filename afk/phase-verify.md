AFK phase: verify — ticket {{ticket}} ({{ticket_path}})

Implementation is done. Now test it by hand, the way a reviewer would, before anyone sees a PR.

1. Read this repo's verification recipe in `docs/agents/afk.md` (dev server command, how to apply `AFK_PORT_BASE`, browser or HTTP checks). If there is none, work out the smallest real way to run the app.
2. Start the dev server on your slot's ports (`$AFK_PORT_BASE` and up) so you don't collide with other workers.
3. Exercise every acceptance criterion in the ticket through the running app: drive a browser for UI work, send real HTTP requests for APIs. Check the unhappy paths too.
4. Fix what you find in scope, commit, and re-test.
5. Stop the dev server when you are done.

If you find a problem that is out of scope for this ticket, don't fix it. Finish what is in scope, then raise it with `afk report question "<what you found; proposed follow-up ticket(s)>"` so the human decides what gets filed.

When every acceptance criterion is verified in the running app, run `afk report done "<one-line summary of what you checked>"`. If you can't proceed, run `afk report blocked "<why>"`.
