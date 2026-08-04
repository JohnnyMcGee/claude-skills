# Research briefs

Four subagents, one per dossier section, launched in parallel. Every prompt starts from the same template, then adds its section brief.

## Shared prompt template

Give every agent:

- The target: PR number + title, ticket ID + title, or the task description.
- The shared material pre-fetched in step 1 (diff, descriptions, comments, linked resources) — pasted in, so the agent never re-fetches it.
- The instruction: "Your final message is the dossier section itself, in markdown, starting with a `##` heading. Write for a capable engineer new to this area. Include mermaid diagrams where they genuinely clarify. End with an `### Unresolved questions` list — every ambiguity or missing fact you hit; write `None` only if you hit none. Report facts with their source (file path, ticket, URL). Never pad: a short accurate section beats a long speculative one."

Agents reach MCP tools (GitHub, Linear, etc.), the codebase, and web search as their section requires.

## Business Context

Answer the who, what, where, when, why: What problem does this solve, and why now? Who asked for it, who decided, who is affected? What conversations, tickets, projects, or initiatives surround it?

Sources, in order of trust: the ticket and its comments; linked documents and parent projects/initiatives in the tracker; PR discussion; repo docs (README, docs/, ONTOLOGY-style domain files); web search for domain concepts the internal sources assume.

Cover: the problem in plain language; why it matters to the business and why it surfaced now; the people involved and their roles in the decision; where the surrounding discussion lives (links).

## Technical Context

Deep-dive the parts of the codebase this task touches. From the diff or ticket, identify the touched domains/modules, then map each: its responsibilities, key files, data models, entry points, and how data flows through it.

Cover: the relevant stack and libraries (only what this task actually meets); the architecture of the touched area — with a mermaid diagram of components or data flow; the local coding conventions and patterns (how neighboring code does it); any project rules that constrain the work (from CLAUDE.md, STANDARDS-style docs, lint config).

## Possible Approaches

Present 2–3 approaches to the problem — building intuition for the solution space, not picking a winner.

For a PR: approach #1 is always "what this PR actually does," explained mechanically and honestly; the others are the plausible alternatives the author didn't take, with the trade-offs that likely drove the choice.

For an issue or open-ended task: 2–3 genuinely distinct approaches. Ground each in precedent — an existing pattern in this codebase that does something similar (cite files), or an established pattern in the wild (cite sources).

For each approach: how it works in a paragraph, what it costs, what it risks, where the codebase already leans that way.

## Risks

For a PR: run a full code-review pass over the diff — correctness (trace the failure scenario before claiming a bug), security, test coverage, adherence to the repo's documented standards. Report findings ranked by severity, each with file:line and a concrete failure scenario. Plausible-but-unverified findings are labeled as such.

For an issue or open-ended task: enumerate the footguns, gotchas, and failure modes of solving it here — migration hazards, backwards compatibility, performance cliffs, security surfaces, deploy/infra coupling (crons, queues, multi-cloud parity), and the places where this codebase has tripped before (search git history and docs for scars).

Either way, end with the two or three risks the user should hold in mind while working — the ones that would hurt most if forgotten.
