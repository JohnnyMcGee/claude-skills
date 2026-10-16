---
name: dossier
description: Build a dossier — a prioritized, verified reading list that makes the user an expert in a problem space before any planning starts. Use when the user asks for a dossier or a reading list, says they don't understand a problem well enough yet, or wants to learn how other engineers have solved something before designing their own solution.
argument-hint: <problem and direction, or a Linear issue id>
---

# Dossier

The user has a problem and a rough direction, and does not yet know the space well enough to plan. Your mission is a **dossier**: a Claude Doc whose weight is in **external links** the user reads themselves — docs, blog posts, talks, books, and (when the problem is specific to this company) internal sources — prioritized and grouped so a few hours of reading leaves them able to plan on their own.

The dossier is the reading you do *before* writing a plan. It is finished when it carries a **where-to-start path**, one **verified source table per area**, and an **open-questions** list the reading answers.

## Guardrails

- **No planning decisions.** The user cannot choose well yet; that is why they asked. The dossier contains no rankings, no recommendations, no plan, and ends with the doc link. Every section states facts and where answers live. The one question you may ask is the scope question in step 1.
- **Every link opened.** A source goes in the doc only after you (or the sub-agent) fetched it on the build date and confirmed its title. A page that would not open goes under *Could not open*, never in a source table. Videos are confirmed by title and description and typed `Talk`.
- **One question per source, no summaries.** The annotation says what question the source answers, so the user decides whether to open it. Content farms and AI-generated roundups never qualify; cite the page that owns the claim.

## 1. Scope

Parse the argument. A ticket pattern like `ENG-123` → fetch its description and comments (Linear MCP, else the repo's script) and treat them as the problem statement; anything else is the problem statement as written; nothing → ask for the problem and direction.

Decide the **scope**:

- **External** — general best practice and how other engineers solved it. The user wants the field, not the codebase. ("How do teams run safe Postgres migrations?")
- **External + internal** — the problem is this system. The user must understand what exists before reading how to change it. ("Why is our AlloyDB bill what it is?")

Infer it from the statement. Ask one question only when the statement genuinely reads both ways.

Then fix the **areas of interest**: four to eight sub-topics that partition the space, each worth its own source table. Honor any the user named.

Check for **prior dossiers**: the memory index and the Claude Docs gallery (`list`). Any that overlap are linked under *Prior dossiers (do not re-derive)* and their ground is left out of this one.

**Done when:** scope, areas, and prior dossiers are written down in one short block you will reuse as the doc's lead paragraph.

## 2. Open the doc

Create the Claude Doc first, before research, so the user can watch it fill. Load the docs connector's guide if it is not already in context. Title it `<Topic> Dossier`, byline today's date and the user, and lay down one pending block per section in [template.md](template.md) — the internal sections only when scope includes internal.

**Done when:** the user has the link.

## 3. Research

Launch one background sub-agent per area, in parallel. Each prompt carries the problem statement, the scope, the area, the build date, and this brief:

> Find the sources that make an engineer expert in *<area>* for *<problem>*. Primary sources for mechanics (vendor docs, specs, standards, source code); practitioner writing for how other teams do it (engineering blogs, conference talks, book chapters, papers). Open every candidate and confirm its title on <date>; report pages that would not open separately. For each kept source give: tier (Start here / Core / Deep dive), title, URL, publisher and year, type (Docs / Pricing page / Blog / Talk / Book chapter / Paper / Standard / Tool), and the one question it answers. Then list, as *Easy to get wrong*, the gotchas the sources state, each quoting the page it came from. Keep candidates you verified but cut as *Verified and held back*, one line each. Your final message is this material in markdown, nothing else.

When scope includes internal, one more sub-agent builds *Maxim today* and the internal reading list: repo docs and code paths with `file:line`, Linear projects and issues, and Slack, Drive, Betterstack, or GCP only where the problem statement points there. Facts only, each with its source.

**Done when:** every sub-agent has returned. A failed area is reported to the user as a gap, never filled from memory.

## 4. Assemble

Fill the doc section by section from the template. The *Where to start* path is the editorial step that research cannot do: pick about twenty sources across areas, order them so each one prepares for the next, and total the reading time. *Open questions* are the questions the user should be able to answer after reading, each with where the answer lives. The *gap table* (internal scope only) sets industry practice beside what the repo does today, no rating column.

**Done when:** every section in the template is filled or explicitly marked as a gap, and the lead paragraph states scope, areas, build date, and the no-decisions disclaimer.

## 5. Hand off

Write a memory pointer silently: slug `<ticket-or-topic>-dossier`, holding the doc link, build date, ticket, scope, and the *Could not open* gaps. When a ticket seeded the run, offer to post the link as a comment on it. Then end with one line and the link.
