# CLAUDE.md

Guidance for Claude (and other agentic coding tools) working in this repository.

## What this project is

An agentic platform that keeps a repository's documentation continuously in sync with its code. When code changes, the system detects which docs are affected, regenerates them with an LLM-driven agent, validates the output, and publishes the result back to the repo as a pull request. The same engine doubles as a Q&A agent over the codebase and its docs.

See `Architecture.md` for the full design. This file is the operating manual for working in the codebase.

## Current build frontier

v1 ships **single-tenant CLI mode first.** The entry point is `greenbean clone <url> [--token $PAT]` (`src/greenbean/cli.py`); credentials go through `TokenCredentials` (`connectors/github/token_credentials.py`); the working copy lands under `~/.greenbean/cache/<host>/<owner>/<name>/`. This is the path the agent and tool layers will operate on as those steps come online — `Architecture.md` §12 has the full ordering.

**Multi-tenant SaaS infra is deferred.** `GitHubCredentials` (App + JWT + installation-token mint) is scaffolded but inactive; webhooks, reconciliation cron, advisory locks, Postgres-backed working-copy cache, and per-tenant disk isolation are explicitly *not* built yet — they're step 6 of the build order, gated on the agent demonstrably producing docs that get merged. **Don't add tenant-aware plumbing in core paths until then.** If a change feels like it's "for when we go multi-tenant," push back on the timing and flag it on the PR.

## Design principles (non-negotiable)

These are the spine of the system. Don't violate them without an explicit conversation:

1. **The connector is *only* what's platform-specific.** v1 ships GitHub-hosted only, but the abstraction must support future local-agent and other-host connectors. What lives behind the connector: auth, change notifications, PR creation, branch HEAD lookups. What does *not* live behind the connector: file reads, diffs, tree listings, commit metadata — those are `git` operations on a working copy, and `git` is already a perfectly good abstraction for them. If you find yourself adding a connector method to do something `git` already does, that's the wrong layer.
2. **The repo is the published surface; Postgres is the operational substrate.** Customer-facing docs are Markdown committed to the customer's repo via PR. Everything *about* docs (doc plan, source-to-doc map, generation metadata, embeddings, sync state) lives in Postgres. Don't blur this line.
3. **Grep beats RAG for code.** Structured search (ripgrep, tree-sitter, git) is the primary retrieval mechanism. Embeddings are a supplementary navigation aid for when the agent doesn't yet know what to search for. Don't reach for vector search as a default.
4. **Generation is a function of change, not of code.** Steady-state runs are "main moved from SHA A to SHA B; what changes?" Avoid full-repo regenerations except on initial onboarding.
5. **No execution of customer code in v1.** Read-only static analysis only — `read`, `grep`, `git log`, tree-sitter. No `npm install`, no running scripts, no test execution. Crossing this line requires real sandboxing infra and an explicit decision.
6. **Show your work.** Every PR explains what changed, why, and what source drove the update. Reasoning artifacts are retained for debugging.
7. **Preserve human edits aggressively.** A developer's edit to a generated doc is sacred. Detect it, respect it, never silently overwrite.

## Architectural shape

```
Connection → Sync → Planning → Triage → Agent Runtime → Publishing
                                              │
                                              ▼
                                          Q&A API

(Git service is shared across Sync, Planning, and Agent Runtime)
```

- **Connection Layer** — `ChangeNotifier`, `RepoCredentials`, `RepoWriter`. Two `RepoCredentials` impls live side by side: `TokenCredentials` (BYO PAT, what the CLI uses today) and `GitHubCredentials` (GitHub App + JWT, scaffolded for SaaS). Only platform-specific things (auth, webhooks, PR creation) live here — *not* file reads, diffs, or commit lookups; those are Git operations on a working copy. Optional capabilities (`SupportsCheckRuns`, etc.) are feature-detected, not assumed.
- **Git service** — thin shell-out wrapper around `git` (or libgit2). Used by sync, planning, and the tool layer. Not part of the connector — same on every platform.
- **Sync Layer** — receives change events (webhook + reconciliation), manages the per-tenant working-copy cache, computes diffs, tracks `last_synced_sha`. Idempotent on `(repo_id, after_sha)`.
- **Planning** — maintains the doc plan and the source-to-doc map; resolves a diff to a candidate set of affected docs. Lightweight, mostly mechanical.
- **Triage Classifier** — cheap model that gates the expensive generator: `regenerate | targeted_edit | no_op` per candidate.
- **Agent Runtime** — tool layer (read/grep/find_symbol/git, on-demand discovery), generator (capable model, agentic loop), validator. Same runtime serves doc generation and Q&A with different prompts.
- **Publishing** — PR builder + writer, batched per generation cycle.

Schema, exact file layout, language choices, and prompt structure are deliberately not pinned here. They will evolve. The interfaces between layers are what matters.

**What's deliberately *not* a separate layer.** Earlier drafts had a "Comprehension Engine" doing symbol extraction and dep-graph construction over the entire repo on every push. That's been pushed down into the agent's tool layer as on-demand discovery via tree-sitter and ripgrep. If profiling later shows we need caching, it goes behind the tool interface — not as a separate upstream stage.

## Code organization (loose; refactor freely)

- Keep the **connector** code physically separated from the core. The dependency arrow points from connectors → core, never the reverse. If core code imports anything platform-specific, that's a bug in the abstraction.
- Keep the **tool layer** (read, grep, find_symbol, git, semantic_search) behind an interface. v1's implementation runs against a server-side working copy; future implementations may run on a customer's laptop. Agent logic must not depend on which.
- Keep **prompts and templates** in version-controlled files, not inline strings in code. They're product, not plumbing.

Beyond that: don't over-engineer the directory structure early. Move things when patterns emerge.

## Conventions

### Languages and tooling

We're not pinning a language stack here yet. When adding a new language or major dep, document the choice in a short ADR rather than burying it in a PR description.

### Testing

- Tool-layer functions are unit-testable without an LLM. Test them that way.
- Agent loops are tested with recorded fixtures (real diffs, real repos) and snapshot outputs. Don't write tests that depend on LLM nondeterminism passing/failing.
- Validators (code parsing, symbol resolution, link checks) are pure functions. Cover them well — they're our last line of defense against bad docs reaching customers.

### Idempotency

Anything that consumes a webhook or processes a `(repo_id, to_sha)` pair must be idempotent. Webhooks retry. Jobs retry. Don't assume single-delivery.

### Logging

Per-job structured logs: tools called, tokens consumed, validation outcomes, final PR. Per-doc lineage: which jobs wrote it, in what order. When something goes wrong with a doc, we should be able to reconstruct what the agent saw.

## Working with the agent runtime

When modifying or extending the agent:

- **New tools go through the tool interface.** Don't give the agent direct filesystem or shell access. Every capability is a named, audited tool.
- **Cheap model for triage, capable model for generation.** Don't burn the expensive model on classification work. Cost discipline is part of the product.
- **Ground claims in source.** Generated docs include `<!-- src: path:line-range -->` anchors. The validator verifies them. Don't disable this — it's the difference between a tool users trust and one they don't.
- **Validation failures feed back into the agent loop.** If `tsc` rejects a code example, the agent gets the error and tries again. Persistent failures flag for human review rather than auto-publishing.

## Working with storage

- Schema is open-ended; the design doc has a sketch, not a contract. Migrate when needed.
- Two firm rules:
  1. Postgres is the truth about *system state*. The repo is the truth about *what's been published to the customer*. Don't mix these.
  2. The working-copy cache on disk is ephemeral. Anything that can't be reconstructed by re-cloning is a bug.

## Things that are explicitly out of scope (right now)

These are good ideas. They are not v1.

- Local-agent connector (architecture supports it; defer until a customer asks).
- Executing customer code to enrich docs (tests, builds, runtime behavior).
- Multi-repo Q&A (single-repo first; the embedding model and plumbing extend naturally later).
- Web UI (API + GitHub PRs are the v1 surface).
- Bidirectional sync (human edits in repo → Postgres).
- Non-GitHub hosts (GitLab, Bitbucket, self-hosted Git).

If a feature request points at one of these, push back or scope it down. The point of v1 is depth on a narrow product, not breadth.

## How to make changes well

- **Small, focused PRs.** This is a system with many moving parts; large PRs are hard to review and harder to revert.
- **When you change an interface, change all its implementations in the same PR.** No half-migrations sitting in main.
- **When you change a prompt or template, note it in the PR description.** Prompt changes are product changes; they deserve the same scrutiny as code.
- **When in doubt about a design decision, write the alternatives down.** A short comparison in a PR description or ADR beats a buried assumption every time.

## Pointers

- `Architecture.md` — full system design, including build order
- `docs/adr/` — architectural decision records (create this when you make the first one)
- `docs/prompts/` — versioned prompts and templates (create when needed)

When in doubt, ask before reshaping a layer. The interfaces are the parts of this system meant to be stable.