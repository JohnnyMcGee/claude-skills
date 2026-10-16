# Dossier template

Section order for the Claude Doc. Sections marked *(internal)* appear only when scope includes internal sources.

## Lead

`# <Topic> Dossier`, then the date chip and the user's mention, then one paragraph: the problem in a sentence, the scope chosen (external, or external + internal), the areas of interest, the ticket if any, the build date, and the sentence *"Every link was opened and its title confirmed on <date>. This dossier makes no decisions."*

## Sections, in order

1. **The short version** — five to eight sentences on what the space is and what the field agrees on. Facts with links, no advice.
2. **Prior dossiers (do not re-derive)** — one line per sibling dossier and what it already covers. Omit when none.
3. **Where to start** — *"Read these N in this order, about H hours in total."* A numbered list of about twenty sources drawn from every area, one sentence each on why it is next. Listen and watch items carry their minutes.
4. **One section per area of interest** — opens with a one-line thesis and the source count, then:
   - the tier legend, verbatim: *Start here means read it before touching the topic. Core means read it before doing work in that area. Deep dive means reference material to open when the question comes up.* (state once, in the first area section)
   - the source table
   - `### Easy to get wrong` — bulleted gotchas, each quoting the page it links to
   - `### Could not open` — pages that failed to fetch, with the reason, when any
5. **How other teams do it** — practitioner sources grouped by theme (right-sizing, proving a change safe, guardrails, war stories, …), same table schema. Omit when the areas already carry them.
6. **Maxim today** *(internal)* — what exists, as tables of `file:line` paths, commits, counts, config values, and Linear issues. Facts only, each with its source.
7. **Gap table** *(internal)* — `| Practice | Industry standard | Maxim today | Where to read |`. No rating, no ranking.
8. **Internal reading list** *(internal)* — in reading order, grouped: Linear projects and issues, repository documents, Slack threads, Google Drive. Same per-link format as below.
9. **Open questions you should be able to answer after reading** — `| Question | What we know | Where to look |`, grouped by area or by ticket.
10. **Verified and held back** — one line per source that was opened and cut, so the cut is visible. End with *"Ask and I will add any of them."*
11. **Not verified** — anything stated without a first-hand fetch (videos by title only, a search that returned nothing, a tool that was unavailable).

## Source table schema

```
| Tier | Source | Publisher, year | Type | Answers the question |
```

- **Tier**: Start here / Core / Deep dive.
- **Source**: `[title](url)`, the page's own title.
- **Type**: Docs / Pricing page / Blog / Talk / Book chapter / Paper / Standard / Tool.
- **Answers the question**: one question, phrased as the reader would ask it. Never a summary.

Internal list entries use `[title](url) — the one question it answers (author, date)`.
