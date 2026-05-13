# CLAUDE.md

Guidance for Claude (and other agentic coding tools) working in this repository.

## What this project is

An agentic platform that keeps a repository's documentation continuously in sync with its code. When code changes, the system detects which docs are affected, regenerates them with an LLM-driven agent, validates the output, and **publishes the result to a local doc store outside the source repo** (under `~/.greenbean/output/...`), where it's viewable on its own surface. The customer's repo is read-only input — greenbean does not commit, branch, or PR docs back to it. The same engine doubles as a Q&A agent over the codebase and its docs.

See `Architecture.md` for the full design. This file is the operating manual for working in the codebase.

## Current build frontier

v1 ships a **local end-to-end automation loop.** The shape: `greenbean clone` → `plan init` → `generate` → (next) `run` / `watch`. `clone` uses `TokenCredentials` (`connectors/github/token_credentials.py`) and lands the working copy under `~/.greenbean/cache/<host>/<owner>/<name>/`. `plan init` populates a SQLite state file (`.greenbean/state.sqlite` next to the working copy by default). `generate` runs the agent and writes generated docs to `~/.greenbean/output/<host>/<owner>/<name>/<doc-path>`. The next step is wiring `greenbean run` (one-shot full loop) and `greenbean watch <repo-path> --interval N` (polling wrapper that does `git fetch` and re-invokes `run` when HEAD moves) — see `Architecture.md` §12 step 5.

**Generated docs do not go back to the source repo. Ever.** No PR writer, no `git push`, no commit on the customer's behalf. The output dir is the v1 publishing surface; future external publishers (static site, separate docs repo, GitHub Pages bot) consume that directory.

**SaaS / GitHub App are deferred but scaffolded.** `GitHubCredentials` (App + JWT + installation-token mint) lives in `connectors/github/credentials.py` with a deprecated-for-v1 banner. It's not wired into any v1 code path; it exists so the SaaS deployment can plug it into the server-side scheduler when that work resumes. **No webhook plumbing exists** — both CLI and SaaS use polling. The CLI polls in-process via `greenbean watch`'s timer; SaaS will poll via a server-side scheduler that queries Postgres for repos due for a check. See `Architecture.md` §10.1.

**Don't add tenant-aware plumbing, webhook plumbing, or `ChangeNotifier`-style abstractions in core paths.** If a change feels like it's "for when we go multi-tenant," "for when we ship the App," or "in case we want webhooks later," push back on the timing and flag it on the PR.

## Design principles (non-negotiable)

These are the spine of the system. Don't violate them without an explicit conversation:

1. **The connector is *only* what's platform-specific.** v1 ships GitHub-hosted only, but the abstraction must support future SaaS / GitHub App / GitHub Actions / local-agent / other-host connectors. What lives behind the connector: **auth** (one protocol: `RepoCredentials`). What does *not* live behind the connector: file reads, diffs, tree listings, commit metadata (those are `git` operations on a working copy); **publishing** (the output dir is a plain filesystem write); and **change notification** (both modes poll — there's no `ChangeNotifier` and no webhook ingress). If you find yourself adding a connector method to do something `git` already does, to publish docs, or to receive a push notification, that's the wrong layer.
2. **The source repo is read-only input; the output dir is the published surface.** Customer repos are cloned, read, grepped, and AST-parsed — never written to. Generated docs land in `~/.greenbean/output/<host>/<owner>/<name>/` for v1; external publishers can read from there. Operational state (doc plan, source-to-doc map, generation metadata, embeddings, sync state) lives in SQLite in CLI mode, Postgres later. Don't blur these lines: don't write to the customer's repo, and don't put doc *content* in the state store.
3. **Grep beats RAG for code.** Structured search (ripgrep, tree-sitter, git) is the primary retrieval mechanism. Embeddings are a supplementary navigation aid for when the agent doesn't yet know what to search for. Don't reach for vector search as a default.
4. **Generation is a function of change, not of code.** Steady-state runs are "main moved from SHA A to SHA B; what changes?" Avoid full-repo regenerations except on initial onboarding.
5. **No execution of customer code in v1.** Read-only static analysis only — `read`, `grep`, `git log`, tree-sitter. No `npm install`, no running scripts, no test execution. Crossing this line requires real sandboxing infra and an explicit decision.
6. **Show your work.** Every PR explains what changed, why, and what source drove the update. Reasoning artifacts are retained for debugging.
7. **Preserve human edits aggressively.** A developer's edit to a generated doc is sacred. Detect it, respect it, never silently overwrite.

## Architectural shape

```
Connection → Sync → Planning → Triage → Agent Runtime → Output dir
                                              │              │
                                              ▼              ▼
                                          Q&A API   (external publishers)

(Git service is shared across Sync, Planning, and Agent Runtime)
```

- **Connection Layer** — one protocol: `RepoCredentials`. No `RepoWriter` (no publishing back to the source repo). No `ChangeNotifier` (no push notifications — both deployments poll). Two `RepoCredentials` impls live side by side: `TokenCredentials` (BYO PAT, what the CLI uses today) and `GitHubCredentials` (GitHub App + JWT, scaffolded but **deprecated for v1** — the future-SaaS auth path). Only platform-specific things (auth) live here — *not* file reads/diffs/commit lookups (those are Git operations on a working copy), *not* publishing (filesystem write into the output dir), and *not* change notification (the trigger lives in `watch`'s in-process timer for CLI or in the server-side scheduler for SaaS).
- **Git service** — thin shell-out wrapper around `git` (or libgit2). Used by sync, planning, and the tool layer. Not part of the connector — same on every platform.
- **Sync Layer** — two triggers, one pipeline body. **CLI:** `greenbean watch`'s in-process timer ticks, calls `git fetch` on the cache, runs the pipeline if HEAD moved. **SaaS (future):** a server-side scheduler queries Postgres for repos due for a poll, calls `RepoCredentials.get_branch_head`, enqueues `sync_jobs` on drift; a stateless worker pool consumes the queue. Both modes idempotent on `(repo_id, after_sha)`.
- **Planning** — maintains the doc plan and the source-to-doc map; resolves a diff to a candidate set of affected docs. Lightweight, mostly mechanical. Backed by SQLite in CLI mode, Postgres later.
- **Triage Classifier** — cheap model that gates the expensive generator: `regenerate | targeted_edit | no_op` per candidate.
- **Agent Runtime** — tool layer (read/grep/find_symbol/git, on-demand discovery), generator (capable model, agentic loop), validator. Same runtime serves doc generation and Q&A with different prompts.
- **Output dir** — `~/.greenbean/output/<host>/<owner>/<name>/<doc-path>`. Plain Markdown files mirroring planned doc paths. SQLite tracks metadata only (content hash, last generated, model, tokens). External publishers (static site, separate docs repo, Pages) consume this dir; they're out of greenbean's process.

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

Anything that processes a `(repo_id, after_sha)` pair must be idempotent. Polling can see the same SHA on consecutive ticks; jobs retry. Don't assume single-delivery.

### Logging

Per-job structured logs: tools called, tokens consumed, validation outcomes, output path written. Per-doc lineage: which jobs wrote it, in what order. When something goes wrong with a doc, we should be able to reconstruct what the agent saw.

## Working with the agent runtime

When modifying or extending the agent:

- **New tools go through the tool interface.** Don't give the agent direct filesystem or shell access. Every capability is a named, audited tool.
- **Cheap model for triage, capable model for generation.** Don't burn the expensive model on classification work. Cost discipline is part of the product.
- **Ground claims in source.** Generated docs include `<!-- src: path:line-range -->` anchors. The validator verifies them. Don't disable this — it's the difference between a tool users trust and one they don't.
- **Validation failures feed back into the agent loop.** If `tsc` rejects a code example, the agent gets the error and tries again. Persistent failures flag for human review rather than auto-publishing.

## Working with storage

- Schema is open-ended; the design doc has a sketch, not a contract. Migrate when needed.
- Three firm rules:
  1. The state store (SQLite in CLI, Postgres later) is the truth about *system state* — plan, source-to-doc map, generation metadata, content hashes. Doc *content* lives in the output dir on disk, not in the state store.
  2. The output dir (`~/.greenbean/output/...`) is the truth about *what's been published*. The customer's source repo is read-only input — never write to it.
  3. The working-copy cache on disk is ephemeral. Anything that can't be reconstructed by re-cloning is a bug.

## Things that are explicitly out of scope

**Permanent (not "deferred"):**

- **Publishing docs back to the source repo.** No PRs, no `git push`, no commits on the customer's behalf. The output dir is the publishing surface.
- **Webhook ingress.** Both deployment modes poll: CLI in-process via `greenbean watch`; SaaS server-side via a scheduler that queries Postgres for repos due for a check. Polling wins on simplicity (no HTTP ingress, no HMAC, no signature retries, no reconciliation cron — polling *is* the reconciliation) and on natural coalescing — a repo with 10 pushes inside one poll interval generates one pipeline run (we fast-forward to current HEAD), whereas webhooks land as 10 separate jobs unless you write debounce logic. The trade is latency: seconds (webhooks) vs minutes (polling). Doc generation isn't latency-sensitive — nobody is watching the doc site waiting for a refresh. If sub-minute latency ever becomes a real product need, webhooks could be bolted onto the existing `sync_jobs` queue as a fast path. Until then, don't build it. See Architecture.md §10.1 for the full reasoning.

**Deferred — good ideas, not v1:**

- SaaS deployment, GitHub App auth, multi-tenant disk isolation, scheduler/worker pool. `GitHubCredentials` is scaffolded in `connectors/github/credentials.py` (deprecated-for-v1 banner) for when this work resumes.
- GitHub Actions integration. Same core, different packaging — re-uses `greenbean run` + `TokenCredentials`; does not depend on SaaS infrastructure (see Architecture.md §12 step 7).
- External publishers (static site, separate docs repo, GitHub Pages bot). Plug into the output dir; out of greenbean's process.
- Local-agent connector (architecture supports it; defer until a customer asks).
- Executing customer code to enrich docs (tests, builds, runtime behavior).
- Multi-repo Q&A (single-repo first; the embedding model and plumbing extend naturally later).
- Web UI for browsing docs (the output dir is files-on-disk; any markdown viewer works).
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