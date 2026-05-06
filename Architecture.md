# Self-Documenting Repository Platform — System Architecture

## 1. Overview

A platform that keeps a repository's documentation continuously in sync with its code. When code changes, the system detects which docs are affected, regenerates them with an LLM-driven agent, validates the output, and publishes the result back to the repo as a pull request. The same underlying engine doubles as a Q&A agent over the codebase and its documentation.

### Design principles

- **Connection layer is modular.** v1 ships GitHub-only (server-hosted), but the abstraction supports future local-agent and other-host (GitLab, Bitbucket) implementations without rewriting the core.
- **Code stays where it belongs.** Docs are published to the customer's repo as Markdown via PR. The customer owns their docs and can leave at any time with no export step.
- **Postgres is the operational substrate.** The doc plan, source-to-doc map, generation metadata, embeddings, and analytics live server-side. The repo is the published surface; Postgres is the truth about system state.
- **Grep beats RAG for code.** Agents discover the codebase through structured search (ripgrep, tree-sitter, git) on a server-cached working copy. Embeddings are a supplementary navigation aid, not the primary retrieval mechanism.
- **On-demand discovery before precomputed indexes.** The agent's tool layer runs tree-sitter and ripgrep at query time. Precomputed symbol/dep caches are an optimization we add only if profiling demands it.
- **Generation is a function of change, not of code.** The system maintains a doc corpus over time. Each run is "main moved from SHA A to SHA B; what changes?"
- **Show your work.** Every PR explains what changed, why, and what source drove the update.

---

## 2. High-Level Architecture

```
                           ┌──────────────────────┐
                           │   GitHub (customer)  │
                           └──────────┬───────────┘
                                      │ webhook / git / API
                                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Connection Layer                              │
│   ┌──────────────────────┐    ┌────────────────────────────────┐    │
│   │  GitHubConnector     │    │  (future) LocalAgentConnector  │    │
│   │  - WebhookNotifier   │    │  - FilesystemWatcher           │    │
│   │  - RepoCredentials   │    │  - RepoCredentials (no-op)     │    │
│   │  - RepoWriter (PR)   │    │  - LocalWriter                 │    │
│   └──────────┬───────────┘    └────────────────────────────────┘    │
│         (Git service is shared, not part of the connector)           │
└──────────────┼──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                            Sync Layer                                │
│  - Receives change events (webhook + reconciliation cron)            │
│  - Manages working-copy cache (per-tenant disk isolation)            │
│  - Computes diffs, tracks last_synced_sha                            │
│  - Hands off to Planning                                             │
└──────────────┬──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                             Planning                                 │
│  - Maintains the doc plan (which docs should exist for this repo)    │
│  - Maintains the source-to-doc dependency map                        │
│  - Resolves the diff to a candidate set of affected docs             │
│  - Lightweight: mostly mechanical, occasional small-LLM call         │
└──────────────┬──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         Triage Classifier                            │
│  - Cheap model: regenerate | targeted_edit | no_op per candidate     │
│  - Gates the expensive generator                                     │
└──────────────┬──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│              Agent Runtime (one job per surviving doc)               │
│  - Tool layer (read/grep/find_symbol/git/semantic_search)            │
│       on-demand discovery via tree-sitter and ripgrep                │
│  - Generator (capable model, agentic loop)                           │
│  - Validator (tsc/ast parse, symbol resolution, link check)          │
└──────────────┬──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     Publishing & Storage Layer                       │
│  - Postgres (doc plan, source-to-doc map, metadata, pgvector)        │
│  - Working-copy cache (cloned repos on disk, ephemeral)              │
│  - PR builder + writer (batched per generation cycle)                │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                       ┌─────────────┐
                       │  Q&A API    │  (shares Agent Runtime + Tools)
                       └─────────────┘
```

**Note on what's *not* a separate layer anymore.** Earlier drafts had a "Comprehension Engine" between sync and agent, responsible for symbol extraction, dep graph construction, and the doc plan. Two of those three responsibilities turned out to be optimizations rather than essential infrastructure. Symbol maps and import graphs are useful, but tree-sitter and ripgrep are fast enough that the agent can build what it needs on-demand via its tool layer. What *is* essential — the doc plan and the source-to-doc map — is small enough to live as a thin Planning service rather than an "engine." If profiling later shows on-demand discovery is too slow or too expensive, the tool layer is the right place to add caching without changing anything upstream.

---

## 3. Connection Layer

The connector exposes only what's *genuinely platform-specific* — the things a hosting platform does that `git` alone can't. Everything else (reading files, diffing, listing trees, inspecting commits) is a `git` operation on the working copy and lives in a separate `Git` service, not the connector.

### 3.1 What's in the connector

Three small interfaces, each doing one platform-specific thing:

**`ChangeNotifier`** — emits change events when a repo's tracked branch moves.

```
subscribe(handler)
  ─ emits: {repo_id, before_sha, after_sha, ref}
```

GitHub implementation: webhook receiver with signature verification.
Future local-agent implementation: filesystem watcher on `.git/refs/heads/{branch}`.

**`RepoCredentials`** — provides the platform-specific things needed to interact with a repo.

```
get_clone_url(repo_id) -> authenticated URL   # short-lived, for sync-time clone/fetch
get_branch_head(repo_id, branch) -> sha       # for reconciliation drift checks
```

GitHub implementation: mints short-lived installation tokens, calls the GitHub API for branch HEAD. Future local-agent implementation: `get_clone_url` is a no-op (repo already on disk); `get_branch_head` is a local `git rev-parse`.

**`RepoWriter`** — publishes docs.

```
open_pull_request(repo_id, branch, files, title, body) -> pr_url
```

GitHub implementation: pushes the branch via git, opens a PR via the GitHub API. Future local-agent implementation: writes a branch locally and either lets the user push or shells out to `gh pr create`.

**Optional capability interfaces** (connectors implement what they support):
- `SupportsCheckRuns`
- `SupportsCommitComments`
- `SupportsPullRequestReviews`

The core feature-detects rather than assuming.

### 3.2 What's *not* in the connector

The original draft of this layer had a much larger `RepoSource` interface with `get_file`, `list_tree`, `diff`, `get_commit`, etc. Those are gone — they were a category mistake. They're operations on a Git repository, not on a hosting platform. Once we have a working copy on disk (which the sync layer maintains), `git` itself answers all of them, and going through a connector abstraction would route filesystem access through a network-shaped interface for no reason.

Concretely: when the agent needs to read a file, it goes to the tool layer (`read_file`) which goes to the filesystem. It does not go through the connector. When the planner needs a diff, it asks the Git service. It does not call the connector.

### 3.3 The `Git` service

A thin wrapper around `git` (or a library — go-git, libgit2, isomorphic-git) that operates on working copies. Used by sync, planning, and the tool layer alike. Not part of the connection abstraction; it's the same on every platform.

```
clone(url, dest, depth)
fetch(repo_path)
reset_hard(repo_path, sha)
diff(repo_path, from_sha, to_sha) -> [{path, status}]
show(repo_path, sha) -> {author, message, timestamp, parents}
current_sha(repo_path) -> sha
log(repo_path, path?, limit?) -> [commits]
blame(repo_path, path, line) -> commit
```

Implementation: shell out to `git` for v1. It's well-debugged, performant, and handles every edge case. Reach for libgit2 bindings only if you have a concrete reason — needing to run without `git` on PATH, or genuine process-spawn overhead at high QPS.

### 3.4 v1 implementations

- **`GitHubConnector.WebhookNotifier`** — receives `push` webhooks at an HTTP endpoint, validates HMAC signature, emits change events.
- **`GitHubConnector.RepoCredentials`** — mints short-lived installation tokens via the GitHub App API; calls `GET /repos/{owner}/{repo}/branches/{branch}` for reconciliation.
- **`GitHubConnector.RepoWriter`** — pushes a branch via authenticated git, opens a PR via Octokit.
- **`GitService`** — shells out to `git`. Same implementation regardless of connector.

**Auth model.** GitHub App. Customer installs the app on selected repos; we receive an installation ID and mint short-lived installation tokens per operation. Tokens are held in memory only, never written to disk. No long-lived PATs.

### 3.5 Two credential modes (App vs token)

The connection layer ships two `RepoCredentials` implementations. Both satisfy the same protocol; the orchestrator picks one at construction time.

- **`TokenCredentials` — CLI / single-tenant (what v1 ships first).** Operator (or developer) brings a personal access token. No JWT, no installation-token mint — the PAT *is* the credential, embedded as `https://x-access-token:{token}@github.com/{owner}/{name}.git`. Powers the `greenbean clone` CLI in §12 step 1; the entire purpose is to let us dogfood the agent on real repos without first standing up the GitHub App apparatus. Token is read from `--token` or `$GITHUB_TOKEN` and is redacted from any `GitError` log line by `_redact()` in `core/git.py`.
- **`GitHubCredentials` — App mode, multi-tenant SaaS.** The model described in §3.4 above. Scaffolded today (JWT signing + installation-token exchange + `repo_resolver` plumbing) but not the immediate focus. Comes online once agent quality is proven on real repos via CLI mode — see §12 step 6.

This is the payoff for keeping the connector slim: both modes drop in without touching core, and the writer surface can correspondingly evolve from "PAT in push URL" → "App-minted installation token" → "shell out to `gh pr create`" depending on deployment.

### 3.6 What this means for future local-agent support

The slimmer interface is *better* for the local-agent case, not worse. A local-agent connector implements:

- `ChangeNotifier` watching `.git/refs/heads/{branch}` instead of webhooks.
- `RepoCredentials.get_clone_url` as a no-op (sync layer skips the clone step entirely when the working copy is the user's actual repo). `get_branch_head` is a local `git rev-parse`.
- `RepoWriter.open_pull_request` via local branch + `gh pr create`, or just a local commit the user reviews.

The Git service is unchanged — it operates on whatever working copy it's pointed at. The tool layer is unchanged. The agent is unchanged. This is what we wanted: the variable parts (auth, notifications, PR creation) are isolated; everything else is platform-agnostic.

---

## 4. Sync Layer

Bridges "GitHub said something happened" and "the agent has files it can grep." Doesn't know anything about docs, LLMs, or generation — its job ends when the working copy is current and a diff is computed.

### 4.1 Responsibilities

- Receive change events from the connection layer (webhooks, plus reconciliation cron as a safety net).
- Verify webhook signatures and check idempotency on `(repo_id, after_sha)`.
- Ensure the working-copy cache is at `after_sha` (clone if missing, otherwise `git fetch` + reset).
- Compute the diff `before_sha → after_sha`.
- Update `repos.last_synced_sha` and hand off to Planning.

### 4.2 Fast path vs worker

The webhook handler should be fast — GitHub retries on slow responses. The handler does cheap validation (signature, idempotency lookup) and enqueues a `SyncJob`. A worker picks the job up and does the actual git operations: it gets a clone URL from `RepoCredentials`, then uses the `Git` service to clone or fetch+reset. This separation lets sync workers (I/O-bound, small) scale independently from generation workers (LLM-bound, larger). Use two queues, not one.

### 4.3 Working-copy cache

The working copy is a *cache*, not storage — fully reconstructable from GitHub. Don't back it up, don't replicate it, evict it freely under disk pressure.

- Located on attached disk per worker pool, namespaced per tenant: `/var/lib/repo-cache/{tenant_id}/{repo_id}/`.
- Shallow clones by default (depth ~50); `git fetch --deepen=N` on demand if more history is needed.
- LRU eviction tracked via `repos.last_accessed_at` in Postgres; re-clone is cheap.
- Per-tenant disk quotas to prevent one customer from starving others.
- Per-repo locks during sync (Postgres advisory lock or Redis) so concurrent webhooks for the same repo don't race on the same working copy. Lock at the repo level, not the tenant level.

### 4.4 Tenant isolation

Three layers, each doing real work:

- **Filesystem.** Working copies are namespaced under `{tenant_id}/{repo_id}/`. Every downstream tool call takes a `repo_id`, resolves it to its canonical path, and refuses to operate on paths that don't resolve under that prefix. Path traversal is rejected at this layer.
- **Process.** A sync worker handles one job at a time. For higher-isolation deployments (enterprise), run each job in an ephemeral container that's torn down after — guarantees that one tenant's process state can never see another's.
- **Credentials.** GitHub installation tokens are minted per-operation, scoped to the minimum repo set, short-lived, and held in memory only. A token issued for tenant A's repo cannot read tenant B's — GitHub enforces this server-side.

All repo operations are read-only at the OS level (grep, read, AST parse, `git log`). No `npm install`, no running customer scripts in v1. Crossing this line requires real sandboxing (Firecracker / gVisor / ephemeral containers) and is explicitly out of scope.

### 4.5 Reconciliation

Webhooks fail. Networks blip. Workers crash mid-sync. A periodic job (every ~15 minutes per active repo) checks the actual default-branch HEAD via the GitHub API against `repos.last_synced_sha`; on drift, enqueues a SyncJob to catch up. Webhooks are the fast path; reconciliation is the correctness guarantee.

### 4.6 Idempotency

The single highest-leverage property in this layer. Idempotency on `(repo_id, after_sha)` makes most failure modes benign: duplicate webhook → no-op; worker crashed mid-sync → restart with no harm; reconciliation finds the same SHA we already processed → return early. Track every webhook delivery in `webhook_deliveries` for both idempotency and debugging.

---

## 5. Planning

Thin layer between sync and the agent runtime. Two responsibilities, both small enough that this isn't really an "engine" — it's a service that maintains two pieces of Postgres state and answers one query.

### 5.1 Responsibilities

**Maintain the doc plan.** The doc plan is the target tree of docs that should exist for a repo (READMEs per significant module, architecture overview, reference docs for public APIs, etc.). When the diff changes module structure — new directories, removed packages — update the plan. This involves judgment, so it's a small-LLM call, not pure mechanics. Most pushes don't change the plan at all.

**Maintain the source-to-doc map.** For each planned doc, record which source files it depends on. This is mechanical: imports, the file paths the doc was generated from, anything inside the doc's "scope" per the plan. Stored in Postgres keyed by source path so the reverse lookup ("which docs depend on this file?") is a fast index hit.

**Resolve the diff to affected docs.** Given a list of changed source files, look up the docs that depend on them. Produces a candidate set for the triage classifier. This is a single Postgres query.

### 5.2 What's *not* here

Earlier drafts had this layer doing symbol extraction and dep-graph construction over the entire repo on every push. That's been pushed down into the agent's tool layer as on-demand discovery — tree-sitter and ripgrep are fast enough that running them at query time is fine, and skipping the precompute eliminates a category of staleness bugs.

If profiling later shows the agent's on-demand discovery is too slow, the right place to add caching is behind the tool interface — not by reviving a precomputed comprehension layer. The agent shouldn't know whether `find_symbol` hits a cache or runs fresh.

### 5.3 Initial onboarding

The doc plan has to start from somewhere. On first install for a repo, the planner does a one-time pass: profiles the repo (language, framework, structure), proposes an initial plan against a default template, and seeds the source-to-doc map. After that, all updates are incremental.

---

## 6. Agent Runtime

The core engine that does the actual reading, writing, and reasoning. Used by both doc generation and Q&A.

### 6.1 Tool layer

Tools are an interface. v1 implements them against the server-side working copy; future local-agent implementations swap the backend without changing agent logic. Discovery is **on-demand** — `find_symbol` runs tree-sitter against the relevant files at query time rather than hitting a precomputed index. If profiling later shows this is too slow, caching goes behind this interface, invisible to the agent.

| Tool | Purpose | Backed by |
|---|---|---|
| `read_file(path, range?)` | Direct file reads | filesystem |
| `list_directory(path)` | Navigation | filesystem |
| `grep(pattern, path?, opts?)` | Literal/regex search | ripgrep |
| `find_symbol(name)` | Definition lookup | tree-sitter, on-demand |
| `find_references(symbol)` | Usage lookup | tree-sitter + ripgrep, on-demand |
| `git_log(path, limit?)` | History for "why" context | `git log` |
| `git_blame(path, line)` | Line-level provenance | `git blame` |
| `semantic_search(query)` | Fuzzy navigation when symbol unknown | pgvector over file summaries |
| `read_doc(path)` | Read existing generated doc | filesystem |

**Retrieval philosophy.** Grep and AST search are primary. Semantic search is a navigational aid for when the agent doesn't yet know what to look for — it returns candidate paths the agent then reads directly. Embeddings find the haystack; grep finds the needle.

### 6.2 The two agent modes

The runtime serves two consumers, both running the same loop (gather context → produce output → validate → iterate) with different prompts and exit conditions.

#### Generator (capable model)

Input: doc spec, prior version (if any), source dependency files, related docs, repo conventions, doc-type template.
Loop: agent decides what additional context it needs, calls tools, writes a draft, self-checks against requirements, emits final doc with embedded source citations.
Output: Markdown with `<!-- src: path:line-range -->` anchors for traceability.

#### Q&A agent (capable model)

Input: user question, repo ID.
Loop: same tool-use loop as the generator, but optimizing for an answer rather than a doc artifact. First-pass uses semantic_search over generated docs (fast, often sufficient); falls back to code-level tools when docs don't contain the answer.

#### A note on triage

The triage classifier (cheap model, returns `regenerate | targeted_edit | no_op` per candidate doc) is its own upstream stage — see Section 2's diagram. It runs between Planning and the Agent Runtime, gating the expensive generator. It uses a small subset of the tool layer (mostly `read_file` on the prior doc and the diff) but isn't part of the runtime proper.

### 6.3 Generation context, in detail

For each doc to generate, the orchestrator assembles:

- **Source files** the doc depends on (full text when fits, summarized otherwise).
- **Prior doc version** if it exists, with a flag indicating whether it has human edits since last system-write.
- **Diff** since prior version, when this is an update.
- **Related docs** linked via the doc plan (so the agent doesn't contradict or duplicate them).
- **Repo conventions** — extracted from CONTRIBUTING.md, existing READMEs, any `STYLE.md` or similar. Drives voice and terminology.
- **Doc-type template** — the system prompt and structural skeleton appropriate for the doc type (reference, conceptual, getting-started, architecture).

### 6.4 Validation

Runs after generation, before publishing. Failures are returned to the agent for self-correction; persistent failures flag for human review.

- **Code example parsing.** TypeScript snippets pass `tsc --noEmit`; Python through `ast.parse`; etc. per language.
- **Symbol resolution.** Every symbol referenced in the doc resolves against a tree-sitter pass over the working copy. Catches hallucinated APIs, the #1 source of user trust loss in auto-doc tools.
- **Internal link integrity.** All `./other-doc.md` and `#anchor` references resolve.
- **Diff sanity.** If a one-line code change produced a 90% rewrite, flag for review rather than auto-publishing.
- **Citation coverage.** Every non-trivial claim has a source anchor.

---

## 7. Storage Layer

### 7.1 Postgres schema (sketch)

```sql
-- Tenant + connection
CREATE TABLE customers (id, name, billing_state, created_at);
CREATE TABLE github_installations (id, customer_id, installation_id, account_login);

-- Repos and sync state
CREATE TABLE repos (
  id UUID PRIMARY KEY,
  customer_id UUID,
  source_type TEXT,          -- 'github' for now
  external_id TEXT,           -- github repo id
  owner TEXT, name TEXT,
  default_branch TEXT,
  last_synced_sha TEXT,
  last_synced_at TIMESTAMPTZ,
  last_accessed_at TIMESTAMPTZ,  -- LRU eviction signal
  cache_pinned BOOLEAN DEFAULT false,
  status TEXT                 -- onboarding | active | paused | error
);

-- Sync layer state
CREATE TABLE webhook_deliveries (
  id UUID PRIMARY KEY,
  repo_id UUID,
  delivery_id TEXT UNIQUE,    -- GitHub's X-GitHub-Delivery header
  event_type TEXT,
  before_sha TEXT, after_sha TEXT,
  received_at TIMESTAMPTZ,
  processed_at TIMESTAMPTZ
);
CREATE INDEX ON webhook_deliveries (repo_id, after_sha);  -- idempotency check

CREATE TABLE sync_jobs (
  id UUID PRIMARY KEY,
  repo_id UUID, before_sha TEXT, after_sha TEXT,
  status TEXT, error TEXT,
  started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ
);

-- Doc plan and state
CREATE TABLE documents (
  id UUID PRIMARY KEY,
  repo_id UUID,
  path_in_repo TEXT,          -- e.g., 'docs/auth/overview.md'
  doc_type TEXT,              -- reference | conceptual | getting_started | architecture | readme
  spec JSONB,                 -- what this doc should cover
  current_content TEXT,
  current_content_hash TEXT,
  last_published_sha TEXT,
  last_published_at TIMESTAMPTZ,
  human_edited BOOLEAN DEFAULT false,
  generation_metadata JSONB   -- model, prompt version, cost, etc.
);
CREATE INDEX ON documents (repo_id, path_in_repo);

CREATE TABLE document_sources (
  document_id UUID,
  source_path TEXT,
  PRIMARY KEY (document_id, source_path)
);
CREATE INDEX ON document_sources (source_path);  -- "which docs depend on this file"

-- Embedding index (for Q&A and semantic_search tool)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE document_chunks (
  id UUID PRIMARY KEY,
  document_id UUID,
  start INT, "end" INT,
  text TEXT,
  embedding vector(1536)
);
CREATE INDEX ON document_chunks USING ivfflat (embedding vector_cosine_ops);

CREATE TABLE file_summaries (
  repo_id UUID, path TEXT,
  summary TEXT, embedding vector(1536),
  at_sha TEXT,
  PRIMARY KEY (repo_id, path)
);

-- Operational
CREATE TABLE generation_jobs (
  id UUID PRIMARY KEY,
  repo_id UUID, document_id UUID,
  from_sha TEXT, to_sha TEXT,
  status TEXT, error TEXT,
  cost_usd NUMERIC,
  started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ
);
CREATE TABLE pull_requests (
  id UUID PRIMARY KEY,
  repo_id UUID,
  pr_number INT, pr_url TEXT,
  state TEXT, opened_at TIMESTAMPTZ, merged_at TIMESTAMPTZ
);

-- OPTIONAL OPTIMIZATIONS — only add when profiling demands it.
-- v1 does symbol/dep discovery on-demand via tree-sitter and ripgrep.
-- These tables become useful if on-demand discovery proves too slow.
--
-- CREATE TABLE symbols (
--   id UUID PRIMARY KEY, repo_id UUID,
--   path TEXT, name TEXT, kind TEXT,
--   start_line INT, end_line INT, signature TEXT,
--   exported BOOLEAN, at_sha TEXT
-- );
-- CREATE TABLE file_dependencies (
--   repo_id UUID, from_path TEXT, to_path TEXT,
--   PRIMARY KEY (repo_id, from_path, to_path)
-- );
```

### 7.2 What lives where

- **Repo (Markdown files committed via PR)** — the published, user-facing docs. Customer-owned, customer-readable, version-controlled by Git.
- **Postgres** — the doc plan, source-to-doc map, generation metadata, embeddings, prior versions, analytics, feedback, sync state. Customer never sees this directly.
- **Working-copy disk cache** — ephemeral mirror of the repo. Re-clonable at any time; not a system of record.

### 7.3 Sync direction and human edits

- System-of-truth flow: source code (repo) → planning (Postgres) → generation → published docs (repo).
- Sync is **one-way for v1**. Postgres → repo only.
- **Human-edit detection.** Before regenerating, compare the current doc in the repo to the hash we recorded when we last wrote it. If it differs, treat the doc as human-edited and one of:
  - Skip regeneration entirely (default, safest).
  - Open a PR with a *proposed* update for the human to merge or discard.
  - Mark `human_edited = true` permanently if the user opts the doc out of automation (e.g., a `<!-- doc-agent: manual -->` sentinel at the top of the file).

---

## 8. Publishing Layer

### 8.1 PR strategy

- One PR per generation job, batching all doc changes for that push.
- Branch name: `doc-agent/sync-{short_sha}`.
- PR title summarizes the change (e.g., "Update auth and session docs for #1234").
- PR body contains:
  - The triggering commit/PR.
  - List of docs added/updated/skipped, with reasoning per item.
  - For each updated doc, a link to the source files that drove the update.
  - A "Reasoning" section showing what the agent considered.

### 8.2 Why batched PRs

A single PR per push keeps the customer's review burden bounded and predictable. Per-doc PRs would flood notifications. The PR description's per-doc breakdown gives reviewers granularity without spamming them.

### 8.3 Failure modes

- **Validation failed after retries** — open the PR with the failing doc included but flagged in the description; or omit the failing doc and note it in the description. Configurable per customer.
- **Webhook missed** — periodic reconciliation job compares `repos.last_synced_sha` against the actual default-branch HEAD via the GitHub API; resyncs any drift.
- **PR conflicts** — rare since the agent only writes to `docs/` paths; on conflict, rebase and retry once, then surface as an error.

---

## 9. Q&A System

Q&A is a thin wrapper over the existing Agent Runtime. The infrastructure investment for doc generation gives it almost for free.

### 9.1 Flow

1. User submits a question scoped to a repo (or, later, a set of repos).
2. Q&A agent runs the same tool-use loop as the generator, with a different system prompt optimized for answering.
3. First-pass tool: `semantic_search` over `document_chunks` (the generated docs). For most product/architecture questions, the doc corpus answers directly.
4. Fallback: code-level tools (`grep`, `find_symbol`, `read_file`) when the docs don't cover it.
5. Response includes citations — links to specific docs and source ranges.

### 9.2 Surface

- API endpoint per repo (`POST /repos/{id}/ask`).
- Optional Slack bot, web UI later.
- Cross-repo Q&A is straightforward once embeddings exist per repo: search across all repo embeddings the user has access to, route to per-repo agents for follow-up grounding.

---

## 10. Future Extension: Local Agent

The architecture is built so a local-agent connector slots in without core changes. Since most of the original "RepoSource" interface was actually Git operations on a working copy, the slimmer connector design makes this *easier*, not harder.

- **Same `Git` service** — operates on a path on disk, doesn't care if that disk is a server or a developer's laptop. No changes needed.
- **Same tool layer backend** — `read_file`, `grep`, `find_symbol`, etc. all work against the local working copy. No changes needed.
- **Different `ChangeNotifier`** — `FilesystemWatcher` watching `.git/refs/heads/{branch}` instead of webhooks.
- **Different `RepoCredentials`** — `get_clone_url` is a no-op; the sync layer skips clone/fetch entirely when the working copy *is* the user's repo. `get_branch_head` is a local `git rev-parse`.
- **Different `RepoWriter`** — writes a local branch, then either lets the user push or shells out to `gh pr create`.
- **LLM calls still go to your server.** Customer code never crosses the network; only the prompts and assembled context the agent decides to send do.

This is the right architecture for security-conscious customers who can't accept a hosted clone of their repo, and it's additive — you ship it when you have demand for it.

---

## 11. Operational Concerns

### 11.1 Cost controls

- Triage classifier (cheap model) gates the expensive generator. Most pushes touch few docs; most touched docs are no-ops.
- Per-customer monthly LLM spend caps; soft-fail to "queued" state and notify when exceeded.
- Cache file summaries by `(repo_id, path, content_hash)` so unchanged files don't get re-embedded.

### 11.2 Observability

- Per-job structured logs: tools called, tokens consumed, validation outcomes, final PR.
- Per-doc lineage: which jobs wrote it, in what order.
- Reasoning artifacts retained for debugging — when a doc looks wrong, you can see what the agent saw.

### 11.3 Security

- GitHub App installation tokens minted per-operation, scoped to the minimum repo set.
- Working copies isolated per tenant on the filesystem.
- Webhook signatures verified.
- No execution of customer code in v1 — read-only static analysis only.
- Customer prompts and code excerpts sent to LLM providers; document this in the customer-facing privacy policy and offer per-customer model-provider routing for enterprise.

### 11.4 Reliability

- Webhook handlers are idempotent on `(repo_id, to_sha)`.
- Generation jobs are retryable; partial progress tracked at the per-doc level so a retry doesn't redo work.
- Reconciliation cron catches missed webhooks.

---

## 12. Build Order

Roughly the sequence to actually ship in. v1 prioritizes a single-tenant CLI we can dogfood ASAP; multi-tenant SaaS infra is gated on the agent demonstrably producing docs that get merged.

1. **CLI mode + Git service. *(done — see `cli.py`, `TokenCredentials`, `GitService`.)*** `greenbean clone <url>` against a remote with a user-supplied PAT, shell-out wrapper around `git`, persistent on-disk working-copy cache under `~/.greenbean/cache/`. End state: a developer can run greenbean against any repo they hold a token for. No Postgres, no webhooks, no tenant isolation yet — those come in step 6.
2. **Tool layer.** All the read/grep/find_symbol/git tools, well-tested in isolation against a working copy. The foundation for everything downstream — both generation and Q&A use it.
3. **Planning service (single-tenant).** Initial doc plan from a default template, source-to-doc map (mechanical, derived from imports + plan), diff-to-affected-docs query. While we're CLI-only, storage is a local SQLite or JSON file under `~/.greenbean/`; Postgres comes with step 6.
4. **Generator agent — narrow scope first.** Only READMEs and a top-level architecture doc. End state: real docs being committed via PR for one or two design-partner customers running the CLI on their own repo.
5. **Triage classifier and validator.** Now that docs exist, optimize the regeneration loop (so most pushes don't trigger expensive generation) and harden against hallucinated symbols / broken examples.
6. **App mode + multi-tenant sync layer.** GitHub App auth (`GitHubCredentials` is already scaffolded), webhook receiver, Postgres-backed working-copy cache under `/var/lib/...`, idempotency on `(repo_id, after_sha)`, advisory locks, reconciliation cron, per-tenant disk quotas. End state: customers install via GitHub App; pushes trigger jobs; multiple tenants share infrastructure safely. **This is the gate from "CLI dogfood" to "SaaS product"** — only worth crossing once steps 4–5 prove people merge the docs.
7. **Doc plan expansion.** Add reference docs for public APIs, then conceptual docs. Each doc type is its own quality investment.
8. **Q&A endpoint.** Built on the existing tool layer + doc embeddings. This is where the modularity pays back.
9. **Local-agent connector.** For security-conscious customers who can't accept a hosted clone of their repo. Architecturally additive — see §10.
10. **Optional optimizations.** Symbol/dep-graph caching behind the tool interface, only if profiling proves the agent's on-demand discovery is the bottleneck.

The single highest-leverage thing to nail in steps 1–4 is the **source-to-doc map + targeted regeneration**. That's what makes the system feel like it actually understands the codebase rather than blindly rewriting docs whenever code changes — and it's what justifies the whole agentic framing in the first place.