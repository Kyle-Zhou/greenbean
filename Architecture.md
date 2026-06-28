# Self-Documenting Repository Platform — System Architecture

## 1. Overview

A platform that keeps a repository's documentation continuously in sync with its code. When code changes, the system detects which docs are affected, regenerates them with an LLM-driven agent, validates the output, and publishes the result to a local doc store outside the source repo, viewable on its own surface. The same underlying engine doubles as a Q&A agent over the codebase and its documentation.

The customer's repo is **read-only input**. Generated docs are not committed back, branched, or PR'd. v1 ships a single-tenant CLI that runs the full pipeline locally; multi-tenant SaaS, GitHub App auth, and GitHub Actions integrations are deferred but the architectural seams for them are preserved.

**Notification model: polling, both modes.** The CLI polls in-process (`greenbean watch`'s timer); the future SaaS polls via a server-side scheduler that queries Postgres. There is no webhook ingress in either deployment — push-based notification is explicitly out of scope (see §10.1).

### Design principles

- **Connection layer is modular.** v1 ships GitHub-only (clone-via-PAT) and the local CLI is the only deployment. The connector abstraction supports future SaaS deployments (GitHub App auth) and other-host implementations (GitLab, Bitbucket) without rewriting the core.
- **The source repo is read-only input; the output dir is the published surface.** Customer repos are cloned, read, grepped, and AST-parsed — never written to. Generated docs land in `~/.greenbean/output/<host>/<owner>/<name>/` for v1; future external publishers (static site, separate docs repo, GitHub Pages) consume that directory.
- **Postgres is the operational substrate (eventually).** v1 is single-tenant CLI mode on SQLite. The doc plan, source-to-doc map, and generation metadata live in a per-repo SQLite file under `~/.greenbean/`. When SaaS work resumes, the same schema model moves to Postgres; the operational substrate idea is unchanged.
- **Grep beats RAG for code.** Agents discover the codebase through structured search (ripgrep, tree-sitter, git) on a working copy. Embeddings are a supplementary navigation aid, not the primary retrieval mechanism.
- **On-demand discovery before precomputed indexes.** The agent's tool layer runs tree-sitter and ripgrep at query time. Precomputed symbol/dep caches are an optimization we add only if profiling demands it.
- **Generation is a function of change, not of code.** The system maintains a doc corpus over time. Each run is "main moved from SHA A to SHA B; what changes?"
- **Show your work.** Every generated doc carries source citations explaining what changed, why, and what source drove the update.

---

## 2. High-Level Architecture

```
                           ┌──────────────────────┐
                           │   GitHub (customer)  │
                           └──────────┬───────────┘
                                      │ git clone / fetch (read-only)
                                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Connection Layer                              │
│   ┌──────────────────────┐    ┌────────────────────────────────┐    │
│   │  GitHub (CLI v1)     │    │  (deferred) SaaS / Local Agent │    │
│   │  - TokenCredentials  │    │  - GitHubCredentials (App)     │    │
│   │  - in-process timer  │    │  - LocalAgentConnector         │    │
│   │    (greenbean watch) │    │                                │    │
│   └──────────┬───────────┘    └────────────────────────────────┘    │
│         (Git service is shared, not part of the connector)           │
└──────────────┼──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                            Sync Layer                                │
│  - Two triggers, one pipeline:                                       │
│      CLI: greenbean watch's in-process timer                         │
│      SaaS: server-side scheduler queries Postgres + enqueues jobs    │
│  - Manages working-copy cache under ~/.greenbean/cache/...           │
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
│                    Output Dir + Storage Layer                        │
│  - SQLite (CLI) / Postgres (future SaaS): plan, source-to-doc map,   │
│    metadata, embeddings                                              │
│  - Working-copy cache (cloned repos on disk, ephemeral)              │
│  - Generated docs on disk under ~/.greenbean/output/...              │
│      (consumed by future external publishers — static site, Pages)   │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                       ┌─────────────┐
                       │  Q&A API    │  (shares Agent Runtime + Tools)
                       └─────────────┘
```

**Note on what's *not* a separate layer anymore.** Earlier drafts had a "Comprehension Engine" between sync and agent, responsible for symbol extraction, dep graph construction, and the doc plan. Two of those three responsibilities turned out to be optimizations rather than essential infrastructure. Symbol maps and import graphs are useful, but tree-sitter and ripgrep are fast enough that the agent can build what it needs on-demand via its tool layer. What *is* essential — the doc plan and the source-to-doc map — is small enough to live as a thin Planning service rather than an "engine." If profiling later shows on-demand discovery is too slow or too expensive, the tool layer is the right place to add caching without changing anything upstream.

**Note on what's *not* in the connector anymore.** Earlier drafts had a `RepoWriter` interface (publishing back via PRs) and a `ChangeNotifier` interface (push-based webhook ingress). Neither survives. The "publish" step is a plain filesystem write into `~/.greenbean/output/...` — not platform-specific, not abstracted. Notification is **pull-based, polling-only** in both deployment modes (CLI's in-process timer; SaaS's server-side scheduler) — no webhook receiver, no `ChangeNotifier` protocol. The only thing left in the connector is `RepoCredentials` (auth + branch-HEAD lookup).

---

## 3. Connection Layer

The connector exposes only what's *genuinely platform-specific* — the things a hosting platform does that `git` alone can't. Everything else (reading files, diffing, listing trees, inspecting commits) is a `git` operation on the working copy and lives in a separate `Git` service, not the connector.

### 3.1 What's in the connector

One protocol:

**`RepoCredentials`** — provides the platform-specific things needed to interact with a repo.

```
get_clone_url(repo_id) -> authenticated URL   # short-lived, for sync-time clone/fetch
get_branch_head(repo_id, branch) -> sha       # for poll-driven drift detection
```

CLI v1 implementation: `TokenCredentials` — a single PAT supplied via `--token` or `$GITHUB_TOKEN`, embedded as `https://x-access-token:{token}@github.com/{owner}/{name}.git`.
Future SaaS implementation: `GitHubCredentials` — mints short-lived installation tokens via a GitHub App, calls the GitHub API for branch HEAD. Scaffolded but deprecated for v1.
Future local-agent implementation: `get_clone_url` is a no-op (repo already on disk); `get_branch_head` is a local `git rev-parse`.

**Notification is not a connector concern.** Both deployment modes are pull-based:

- CLI: `greenbean watch`'s in-process timer calls `git fetch` (no API call) on the cached working copy.
- SaaS: a server-side scheduler queries Postgres for repos due for a poll, then calls `RepoCredentials.get_branch_head`.

Neither path needs a connector-level abstraction. Earlier drafts had a `ChangeNotifier` protocol with a `WebhookNotifier` implementation — both have been removed (see §10.1 on why webhook ingress is out of scope).

### 3.2 What's *not* in the connector

The original draft of this layer had a much larger `RepoSource` interface with `get_file`, `list_tree`, `diff`, `get_commit`, etc. Those are gone — they were a category mistake. They're operations on a Git repository, not on a hosting platform. Once we have a working copy on disk (which the sync layer maintains), `git` itself answers all of them, and going through a connector abstraction would route filesystem access through a network-shaped interface for no reason.

There is also no `RepoWriter`. greenbean publishes generated docs to a local output directory outside the source repo; it never opens pull requests or pushes to the customer's repo. The "writer" responsibility is a plain filesystem write — not platform-specific, not abstracted.

Concretely: when the agent needs to read a file, it goes to the tool layer (`read_file`) which goes to the filesystem. It does not go through the connector. When the planner needs a diff, it asks the Git service. When the orchestrator publishes a doc, it writes to `~/.greenbean/output/...`. The connector is only auth + change notification.

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
remote_url(repo_path, name) -> str
ls_remote(url, ref) -> sha
```

Implementation: shell out to `git` for v1. It's well-debugged, performant, and handles every edge case. Reach for libgit2 bindings only if you have a concrete reason — needing to run without `git` on PATH, or genuine process-spawn overhead at high QPS.

### 3.4 v1 implementations (CLI mode)

- **`TokenCredentials`** — single-PAT auth for `greenbean clone`. Token comes from `--token` or `$GITHUB_TOKEN`; URLs are redacted from any error log line by `_redact()` in `core/git.py`. This is the only `RepoCredentials` impl wired into the CLI.
- **`GitService`** — shells out to `git`. Same implementation regardless of connector.
- **`greenbean watch`** *(planned)* — interval-driven in-process loop that does `git fetch` on the cached working copy, compares HEAD against `last_synced_sha`, and invokes the run pipeline on change. No abstraction layer between trigger and pipeline; both run in one process. An optional `--view :8080` flag bundles a small HTTP doc viewer into the same process (see §8.2).

### 3.5 Deferred / scaffolded implementations (SaaS path)

`GitHubCredentials` exists in the codebase but is **not wired into any v1 code path**. It survives the pivot because the SaaS deployment is paused, not abandoned — when that work resumes, this is how the server-side scheduler will mint short-lived installation tokens.

- **`GitHubCredentials`** — GitHub App auth: signs an app JWT, mints short-lived installation tokens, calls `GET /repos/{owner}/{repo}/branches/{branch}` for poll-driven drift detection. Module-level docstring marks it deprecated for v1.

**Auth model (future SaaS).** GitHub App. Customer installs the app on selected repos; the system receives an installation ID and mints short-lived installation tokens per operation. Tokens held in memory only, never written to disk. No long-lived PATs.

### 3.6 What this means for future deployments

The slim interface (one protocol, no writer, no notifier) is *better* for every future deployment:

- **SaaS / GitHub App** — implements `GitHubCredentials` (already scaffolded). A server-side scheduler queries Postgres for repos due for a poll, calls `get_branch_head`, enqueues a job on drift; stateless workers consume. The "publish" step writes to a server-side output store (filesystem or object storage); customers consume it through whatever surface the SaaS exposes.
- **GitHub Actions** — packages `greenbean run` for CI use; the action authenticates with `GITHUB_TOKEN` via `TokenCredentials` and uploads the output dir to a downstream surface (Pages, an artifact store, a separate docs repo). No new code paths.
- **Local-agent connector** — implements `RepoCredentials.get_clone_url` as a no-op (the working copy *is* the user's repo) and `get_branch_head` as a local `git rev-parse`. The Git service is unchanged. The tool layer is unchanged. The agent is unchanged.
- **Other-host (GitLab, Bitbucket)** — implement the one protocol against that host's API. No publishing-back-to-repo plumbing to port; the output dir is the same on every host.

This is what we wanted: the variable parts (auth) are isolated; everything else is platform-agnostic.

---

## 4. Sync Layer

Bridges "something happened upstream" and "the agent has files it can grep." Doesn't know anything about docs, LLMs, or generation — its job ends when the working copy is current and a diff is computed.

### 4.1 Responsibilities

- Run the pipeline trigger. CLI: a `greenbean watch` tick or a manual `greenbean run`. SaaS (future): a server-side scheduler that picks repos due for a poll and enqueues sync jobs.
- Check idempotency on `(repo, after_sha)` — both modes use the same key so a duplicate trigger is a no-op.
- Ensure the working-copy cache is at `after_sha` (clone if missing, otherwise `git fetch` + reset).
- Compute the diff `before_sha → after_sha`.
- Update `last_synced_sha` and hand off to Planning.

### 4.2 Two triggers, one pipeline

The CLI and the SaaS deployment share the **pipeline body** (sync → planning → triage → generation → publish) but use different triggers to invoke it.

**CLI trigger (v1).** `greenbean watch <repo-path> --interval 5m` is a single process that, on each tick:
1. Runs `git fetch` (if a remote exists).
2. Reads the current HEAD via `GitService.current_sha`.
3. If HEAD != `last_synced_sha`, invokes the pipeline body in-process.

One repo, one process, one in-memory timer. No queue, no workers. `greenbean run` is the same thing without the loop.

**SaaS trigger (deferred).** A server-side scheduler ticks every ~1 min and runs a query like:

```sql
SELECT id, default_branch FROM repos
 WHERE status = 'active'
   AND (last_polled_at IS NULL
        OR last_polled_at + (poll_interval_seconds || ' seconds')::interval < now())
 LIMIT 500;
```

For each due repo, the scheduler calls `RepoCredentials.get_branch_head` (a cheap API call), compares to `last_synced_sha`, and on drift inserts a row into `sync_jobs` (`ON CONFLICT (repo_id, after_sha) DO NOTHING` for idempotency). A pool of stateless workers consumes `sync_jobs` and runs the pipeline body.

**Why a queue/workers in SaaS instead of "watch per repo".** A per-repo watcher process (or asyncio task) doesn't scale: holding N watchers in memory means N timers competing for the event loop, no central backpressure, and a worker restart resets every timer to "now" → thundering-herd polling. A scheduler that's just a SQL query plus a stateless worker pool scales to ~100k repos with a handful of processes, survives crashes without losing state, and gives you operational visibility (queue depth, oldest pending job) for free.

**What's shared.** The pipeline body. The CLI calls it directly from `watch`'s timer; the SaaS worker calls it after picking up a `sync_jobs` row. Same code, two triggers.

### 4.3 Working-copy cache

The working copy is a *cache*, not storage — fully reconstructable from the remote. Don't back it up, don't replicate it, evict it freely under disk pressure.

- CLI: located under `~/.greenbean/cache/<host>/<owner>/<name>/`. Single-user, no tenant prefix.
- Future SaaS: located on attached disk per worker pool, namespaced per tenant: `/var/lib/repo-cache/{tenant_id}/{repo_id}/`.
- Shallow clones by default (depth ~50); `git fetch --deepen=N` on demand if more history is needed.
- Future SaaS: LRU eviction tracked via `repos.last_accessed_at`; per-tenant disk quotas; per-repo locks during sync (Postgres advisory lock or Redis) so concurrent workers for the same repo don't race on the same working copy.

### 4.4 Tenant isolation (SaaS path)

Three layers, each doing real work, when SaaS resumes:

- **Filesystem.** Working copies are namespaced under `{tenant_id}/{repo_id}/`. Every downstream tool call takes a `repo_id`, resolves it to its canonical path, and refuses to operate on paths that don't resolve under that prefix. Path traversal is rejected at this layer.
- **Process.** A sync worker handles one job at a time. For higher-isolation deployments (enterprise), run each job in an ephemeral container that's torn down after.
- **Credentials.** GitHub installation tokens are minted per-operation, scoped to the minimum repo set, short-lived, and held in memory only.

CLI mode is single-tenant by construction; none of this applies until the SaaS gate.

All repo operations are read-only at the OS level (grep, read, AST parse, `git log`). No `npm install`, no running customer scripts in v1. Crossing this line requires real sandboxing (Firecracker / gVisor / ephemeral containers) and is explicitly out of scope.

### 4.5 Reconciliation

The polling cadence **is** the reconciliation guarantee. There's no separate reconciliation cron because there's no faster path that could miss events.

- CLI: every `greenbean watch` tick is a full check of HEAD vs `last_synced_sha`. A missed tick (process restart, machine asleep) is caught by the next one.
- SaaS: the scheduler's poll loop is the reconciliation. A worker crash mid-pipeline leaves the `sync_jobs` row pending; another worker picks it up. A scheduler crash gets restarted; due-row backlog drains.

The worst-case latency is `poll_interval`: if you promise customers "your docs update within 15 min of a push," set the interval to 15 min. There is no scenario where greenbean misses a change permanently — every poll is a fresh check against the actual remote HEAD.

### 4.6 Idempotency

The single highest-leverage property in this layer. Idempotency on `(repo, after_sha)` makes most failure modes benign: duplicate trigger → no-op; worker crashed mid-sync → restart with no harm; scheduler finds the same SHA we already processed → return early. In CLI mode the dedup is in-process (compare HEAD against `last_synced_sha`); in SaaS it's the `(repo_id, after_sha)` primary key on `sync_jobs` with `ON CONFLICT DO NOTHING` on insert.

---

## 5. Planning

Thin layer between sync and the agent runtime. Two responsibilities, both small enough that this isn't really an "engine" — it's a service that maintains two pieces of state and answers one query.

### 5.1 Responsibilities

**Maintain the doc plan.** The doc plan is the target tree of docs that should exist for a repo (READMEs per significant module, architecture overview, reference docs for public APIs, etc.). When the diff changes module structure — new directories, removed packages — update the plan. This involves judgment, so it's a small-LLM call, not pure mechanics. Most pushes don't change the plan at all.

**Maintain the source-to-doc map.** For each planned doc, record which source files it depends on. This is mechanical: imports, the file paths the doc was generated from, anything inside the doc's "scope" per the plan. Stored keyed by source path so the reverse lookup ("which docs depend on this file?") is a fast index hit.

**Resolve the diff to affected docs.** Given a list of changed source files, look up the docs that depend on them. Produces a candidate set for the triage classifier. This is a single SQLite query in CLI mode (Postgres in future SaaS).

### 5.2 What's *not* here

Earlier drafts had this layer doing symbol extraction and dep-graph construction over the entire repo on every push. That's been pushed down into the agent's tool layer as on-demand discovery — tree-sitter and ripgrep are fast enough that running them at query time is fine, and skipping the precompute eliminates a category of staleness bugs.

If profiling later shows the agent's on-demand discovery is too slow, the right place to add caching is behind the tool interface — not by reviving a precomputed comprehension layer. The agent shouldn't know whether `find_symbol` hits a cache or runs fresh.

### 5.3 Initial onboarding

The doc plan has to start from somewhere. On first install for a repo, the planner does a one-time pass: profiles the repo (language, framework, structure), proposes an initial plan against a default template, and seeds the source-to-doc map. After that, all updates are incremental.

---

## 6. Agent Runtime

The core engine that does the actual reading, writing, and reasoning. Used by both doc generation and Q&A.

### 6.1 Tool layer

Tools are an interface. v1 implements them against the working copy (whether that's a CLI cache clone or a future server-side cache); future local-agent implementations swap the backend without changing agent logic. Discovery is **on-demand** — `find_symbol` runs tree-sitter against the relevant files at query time rather than hitting a precomputed index. If profiling later shows this is too slow, caching goes behind this interface, invisible to the agent.

| Tool | Purpose | Backed by |
|---|---|---|
| `read_file(path, range?)` | Direct file reads | filesystem |
| `list_directory(path)` | Navigation | filesystem |
| `grep(pattern, path?, opts?)` | Literal/regex search | ripgrep |
| `find_symbol(name)` | Definition lookup | tree-sitter, on-demand |
| `find_references(symbol)` | Usage lookup | tree-sitter + ripgrep, on-demand |
| `git_log(path, limit?)` | History for "why" context | `git log` |
| `git_blame(path, line)` | Line-level provenance | `git blame` |
| `semantic_search(query)` | Fuzzy navigation when symbol unknown | pgvector / sqlite-vss over file summaries |
| `read_doc(path)` | Read existing generated doc | filesystem (output dir) |

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
- **Prior doc version** if it exists, read from the output dir.
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

### 7.1 What lives where

- **Output directory (`~/.greenbean/output/<host>/<owner>/<name>/`)** — the published, viewable docs. Plain Markdown files on disk, mirroring the planned doc paths. Browsable in any IDE / `cat` / markdown previewer. v1's "publishing surface."
- **State store** — the doc plan, source-to-doc map, generation metadata, prior-version hashes. CLI: a single SQLite file under the working-copy at `<repo>/.greenbean/state.sqlite` (or wherever `--state` points). Future SaaS: Postgres with `customers`, `repos`, `documents`, `document_sources`, `generation_jobs`, etc.
- **Working-copy disk cache** — ephemeral mirror of the repo under `~/.greenbean/cache/...`. Re-clonable at any time; not a system of record.
- **Embeddings** (later) — `document_chunks` and `file_summaries` tables for Q&A and `semantic_search`. SQLite (sqlite-vss) in CLI; pgvector in SaaS.

### 7.2 SQLite schema (CLI v1)

```sql
CREATE TABLE documents (
  id TEXT PRIMARY KEY,
  path_in_repo TEXT UNIQUE NOT NULL,
  doc_type TEXT NOT NULL,
  scope TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  last_planned_sha TEXT NOT NULL,
  current_content_hash TEXT,
  last_generated_at TEXT,
  generation_metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE document_sources (
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  source_path TEXT NOT NULL,
  PRIMARY KEY (document_id, source_path)
);
CREATE INDEX idx_doc_sources_path ON document_sources(source_path);
```

`current_content_hash` is the hash of the bytes written to the output dir. The "human-edit detection" question — *did anyone modify this generated doc since we last wrote it?* — collapses for v1 because the docs aren't in a place humans typically edit. If a developer does edit a file under `~/.greenbean/output/...`, hash mismatch triggers the same skip-or-PR logic the SaaS variant would use (see §7.4).

### 7.3 Postgres schema (deferred — future SaaS)

```sql
-- Tenant + connection
CREATE TABLE customers (id, name, billing_state, created_at);
CREATE TABLE github_installations (id, customer_id, installation_id, account_login);

-- Repos and sync state (note last_polled_at / poll_interval_seconds for scheduler)
CREATE TABLE repos (
  id UUID PRIMARY KEY,
  customer_id UUID,
  source_type TEXT,          -- 'github' for now
  external_id TEXT,           -- github repo id
  owner TEXT, name TEXT,
  default_branch TEXT,
  last_synced_sha TEXT,
  last_synced_at TIMESTAMPTZ,
  last_polled_at TIMESTAMPTZ,
  poll_interval_seconds INT DEFAULT 900,  -- 15 min
  last_accessed_at TIMESTAMPTZ,
  cache_pinned BOOLEAN DEFAULT false,
  status TEXT
);

-- Sync layer state — one job per (repo, after_sha). Inserted by the
-- scheduler when it detects drift; consumed by the worker pool.
CREATE TABLE sync_jobs (
  repo_id UUID NOT NULL,
  after_sha TEXT NOT NULL,
  triggered_by TEXT NOT NULL,     -- 'scheduler' | 'manual'
  status TEXT NOT NULL,            -- 'pending' | 'running' | 'completed' | 'failed'
  error TEXT,
  enqueued_at TIMESTAMPTZ DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  PRIMARY KEY (repo_id, after_sha) -- the idempotency key
);

-- Doc plan, generation, embeddings — same shape as SQLite, scaled out
CREATE TABLE documents ( ... );
CREATE TABLE document_sources ( ... );
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE document_chunks (
  id UUID PRIMARY KEY, document_id UUID,
  start INT, "end" INT, text TEXT, embedding vector(1536)
);
CREATE INDEX ON document_chunks USING ivfflat (embedding vector_cosine_ops);

CREATE TABLE generation_jobs (
  id UUID PRIMARY KEY,
  repo_id UUID, document_id UUID,
  from_sha TEXT, to_sha TEXT,
  status TEXT, error TEXT,
  cost_usd NUMERIC,
  started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ
);
```

There is no `pull_requests` table. There never will be — greenbean does not open PRs.

### 7.4 Sync direction and human edits

- System-of-truth flow: source code (repo, read-only) → planning (state store) → generation → published docs (output dir).
- The output dir is **write-only from greenbean's perspective**. Generated content gets written; nothing reads back into Postgres/SQLite as truth.
- **Human-edit detection.** Before regenerating, compare the current file in the output dir to the hash recorded at last write. If it differs, treat the doc as human-edited and one of:
  - Skip regeneration entirely (default, safest).
  - Stage the proposed update next to it (e.g. `<doc>.proposed.md`) for the user to merge or discard.
  - Mark the doc as opted-out permanently if a `<!-- doc-agent: manual -->` sentinel is present at the top of the file.

---

## 8. Publishing

### 8.1 v1 — local output directory

The publish step is a filesystem write:

```
~/.greenbean/output/<host>/<owner>/<name>/<doc-path>
```

For a repo at `~/.greenbean/cache/github.com/foo/bar/`, the README lands at `~/.greenbean/output/github.com/foo/bar/README.md`. Subdirectory READMEs mirror the planned `path_in_repo`. The output root is resolved by reading the working copy's `origin` URL via `GitService.remote_url`; when the URL doesn't parse as GitHub, the fallback is `~/.greenbean/output/_local/<basename>-<path-hash>/` (the hash suffix is an 8-char sha256 of the resolved working-copy path — keeps two repos that share a directory name from clobbering each other).

Generated content writes are atomic per file (write temp, rename) and accompanied by a `record_generation` call into the SQLite store: content hash, model, tokens, tool-call count.

### 8.2 Local doc viewer (`--view`)

`greenbean watch run --view :8080` bundles a small HTTP doc viewer into the same process. It is **not a separate server** — there is no new daemon, no extra command to keep running, and no inbound traffic beyond localhost. It's a flag on the polling loop that's already running.

What it does:

- Serves the output dir over `http://localhost:8080/`. Each tracked repo lands under its own path (`github.com/<owner>/<name>/...` or `_local/<basename>-<hash>/...`).
- Renders `.md` files through a markdown library on the fly. No build step, no static-site generator.
- Provides cross-repo navigation: an index page at `/` lists every repo greenbean is watching, with their planned doc trees beneath them.
- Auto-refreshes on the client when the underlying file changes (the page polls a small `?since=<mtime>` endpoint; no websockets, no SSE, no extra deps).
- Shows a **watch status panel** on the index: the repo being watched (with its GitHub URL), the current commit (short SHA, subject, author, time, linked to the commit on GitHub), branch, last sync time, a live countdown to the next sync, the poll interval, and the last run's outcome. The watch loop pushes a snapshot each tick; the page polls a `/__status` JSON endpoint to stay live. Remote URLs are shown credential-free (no embedded token).

What it does **not** do:

- It is not a publishing surface. It's a local dogfood / preview surface — the URL is `localhost`, never exposed beyond the developer's machine. Real publishing happens through external publishers (§8.3).
- It is not authenticated. It binds to localhost only.
- It does not modify the output dir. Read-only.

When to use the IDE vs the viewer:

- **IDE markdown preview** (`Cmd+Shift+V` in VS Code/Cursor, etc.) is the default — zero new processes, zero new code, live-reloads on save, works for any single file you're focused on.
- **`--view`** is for cross-repo browsing, a stable URL to bookmark or screen-share, or dogfooding the published experience before wiring up a real publisher.

Implementation: a few dozen lines of Starlette (or equivalent) inside `greenbean watch`. The viewer code lives next to the polling loop, not in a separate package — it's part of the same process and shares no state beyond reading the output dir from disk.

### 8.3 Why a directory, not a database

- Files on disk are viewable in any IDE, `cat`, or markdown previewer. No bespoke viewer needed for the user to actually *read* their docs.
- External publishers — static site builders, separate docs repos, GitHub Pages bots, Notion importers — can be written as plain "watch this directory and sync somewhere else" tools, without coupling to greenbean's internals.
- The state store (SQLite/Postgres) remains an *index* over content rather than a content store. That separation is what's already there for working-copy clones (cache on disk; metadata in DB) and it carries over cleanly.

### 8.4 External publishers (deferred)

Out of greenbean's process. Distinct from §8.2's `--view`, which is a localhost dogfood surface — external publishers are how real readers see the docs:

- A static-site generator (mkdocs, Docusaurus) pointed at the output dir.
- A separate "docs repo" committer that turns each generation into a commit on a docs-only repo, optionally hosted on GitHub Pages.
- A web app that serves the output dir over HTTP with auth.

When SaaS resumes, the output store moves from `~/.greenbean/output/` to a server-side filesystem or object store; the surface seen by external publishers is the same.

### 8.5 Failure modes

- **Validation failed after retries** — write the failing doc to `<doc>.failed.md` next to where it would have gone, and record the failure in `generation_jobs` for follow-up. Don't overwrite the prior good version with a failing draft.
- **Notification missed** — CLI: `greenbean watch` runs the loop on an interval; idempotency on `(repo, sha)` makes a missed tick benign on the next one. SaaS: reconciliation cron compares actual default-branch HEAD against `last_synced_sha`.
- **Output dir conflicts** — a developer has edited a generated doc by hand. Hash mismatch is detected pre-write; fall back to the `<doc>.proposed.md` strategy described in §7.4.

---

## 9. Q&A System

Q&A is a thin wrapper over the existing Agent Runtime. The infrastructure investment for doc generation gives it almost for free.

### 9.1 Flow

1. User submits a question scoped to a repo (or, later, a set of repos).
2. Q&A agent runs the same tool-use loop as the generator, with a different system prompt optimized for answering.
3. First-pass tool: `semantic_search` over `document_chunks` (the generated docs in the output dir, indexed for retrieval). For most product/architecture questions, the doc corpus answers directly.
4. Fallback: code-level tools (`grep`, `find_symbol`, `read_file`) when the docs don't cover it.
5. Response includes citations — links to specific docs in the output dir and source ranges in the working copy.

### 9.2 Surface

- CLI v1: `greenbean ask <repo-path> "<question>"` printing to stdout.
- Future SaaS: API endpoint per repo, optional Slack bot, web UI.
- Cross-repo Q&A is straightforward once embeddings exist per repo: search across all repo embeddings the user has access to, route to per-repo agents for follow-up grounding.

---

## 10. Future Extensions

The architecture is built so additional deployments slot in without core changes.

### 10.1 SaaS / GitHub App / GitHub Actions

The deferred deployment path. **Polling everywhere, no webhook ingress.** The architecture is:

```
[ Scheduler tick — every ~1 min ]
   │
   │ SELECT repos due for poll
   ▼
   for each: get_branch_head() → if drift, INSERT INTO sync_jobs
   │
   ▼
[ Stateless worker pool ]
   pick from sync_jobs → fetch + reset → run pipeline → mark done
```

The seam that's already in the codebase:

- **`GitHubCredentials`** (App-based JWT + installation-token mint) — implements `RepoCredentials` for per-installation auth. The scheduler uses `get_branch_head` to detect drift; workers use `get_clone_url` to clone/fetch.

When SaaS work resumes, the moves are:
1. Stand up the scheduler service (a process that runs the "due repos" query on a timer).
2. Stand up the worker pool (stateless workers consuming `sync_jobs` via `SELECT ... FOR UPDATE SKIP LOCKED`).
3. Migrate state from SQLite to Postgres.
4. Add per-tenant disk isolation under `/var/lib/greenbean/cache/{tenant_id}/...`.
5. Mint installation tokens via the existing `GitHubCredentials`.
6. Route generated docs into a tenant-namespaced output store.

A GitHub Actions integration is a thin variant on top of `greenbean run`, not on top of SaaS: the action runs the CLI inside the customer's CI using `GITHUB_TOKEN` with `TokenCredentials`, and uploads the output dir to a downstream surface (Pages, an artifact store, a separate docs repo). It can ship before SaaS — it doesn't depend on any SaaS infrastructure.

**Why no webhooks.** Four reasons, in order of importance:

1. **Simplicity.** Polling-only eliminates a whole subsystem: no public HTTPS endpoint, no HMAC verification, no signature retries, no separate reconciliation cron (polling *is* the reconciliation), no "GitHub disabled our webhook because our endpoint was slow" failure mode to detect and recover from. The SaaS deployment becomes a scheduler + worker pool + Postgres, with each moving part having a single clear purpose.
2. **Natural coalescing for busy repos.** A repo with 10 pushes inside one poll interval generates 10 webhook deliveries (each with a distinct `after` SHA, each landing as its own `sync_jobs` row). Greenbean's regen model only cares about the diff from `last_synced_sha` to the current HEAD — intermediate SHAs are uninteresting. A webhook-driven system either over-processes (one pipeline run per push) or needs explicit "supersede older pending SHAs" debounce logic. Polling fast-forwards to current HEAD by definition, so coalescing is free.
3. **Bounded work per repo per interval.** Polling guarantees ≤1 pipeline run per repo per `poll_interval`, regardless of upstream push rate. With webhooks the work rate tracks the push rate and needs separate rate-limiting.
4. **One mental model.** CLI and SaaS both poll; only the trigger source differs (in-process timer vs server-side scheduler). The pipeline body doesn't know or care which kicked it off.

What we give up is latency. Webhooks deliver in seconds; polling at a 5-min cadence has 2.5-min average and 5-min worst-case latency. Doc generation isn't latency-sensitive — nobody is watching the doc site waiting for a refresh — so this is the right trade. If sub-minute latency ever becomes a real product need, webhooks can be bolted onto the existing `sync_jobs` queue as a fast path without changing anything else.

### 10.2 Local-agent connector

For security-conscious customers who can't accept a hosted clone of their repo. Architecturally additive, no core changes:

- **Same `Git` service** — operates on a path on disk, doesn't care if that disk is a server or a developer's laptop.
- **Same tool layer backend** — `read_file`, `grep`, `find_symbol`, etc. all work against the local working copy.
- **Different trigger** — a filesystem watcher on `.git/refs/heads/{branch}` for instant local feedback, *or* the same `greenbean watch` polling loop. Either way, no notifier abstraction; the trigger calls the pipeline body directly.
- **Different `RepoCredentials`** — `get_clone_url` is a no-op (the working copy *is* the user's repo); `get_branch_head` is a local `git rev-parse`.
- **Output dir** — same filesystem write, just on the user's machine. Optionally configurable per-repo to point at a docs-site repo they push themselves.
- **LLM calls still go to a server.** Customer code never crosses the network; only the prompts and assembled context the agent decides to send do.

This is the right architecture for security-conscious customers who can't accept a hosted clone of their repo, and it's additive — ship it when there's demand.

---

## 11. Operational Concerns

### 11.1 Cost controls

- Triage classifier (cheap model) gates the expensive generator. Most pushes touch few docs; most touched docs are no-ops.
- Per-customer monthly LLM spend caps (SaaS); soft-fail to "queued" state and notify when exceeded.
- Cache file summaries by `(repo_id, path, content_hash)` so unchanged files don't get re-embedded.

### 11.2 Observability

- Per-job structured logs: tools called, tokens consumed, validation outcomes, output path written.
- Per-doc lineage: which jobs wrote it, in what order.
- Reasoning artifacts retained for debugging — when a doc looks wrong, you can see what the agent saw.

### 11.3 Security

- CLI v1: PAT supplied by the operator; URLs with embedded tokens are redacted from any log line by `_redact()` in `core/git.py`.
- SaaS (future): GitHub App installation tokens minted per-operation, scoped to the minimum repo set, held in memory only. Working copies isolated per tenant on the filesystem. No public HTTP ingress (polling-only model — see §10.1).
- No execution of customer code in v1 — read-only static analysis only.
- Customer prompts and code excerpts sent to LLM providers; document this in the customer-facing privacy policy and offer per-customer model-provider routing for enterprise.

### 11.4 Reliability

- Sync is idempotent on `(repo, to_sha)`.
- Generation jobs are retryable; partial progress tracked at the per-doc level so a retry doesn't redo work.
- CLI: `greenbean watch` ticks are themselves the reconciliation mechanism. SaaS: the scheduler's poll loop is the reconciliation — no separate cron.

---

## 12. Build Order

Roughly the sequence to actually ship in. v1 prioritizes the local end-to-end automation loop a single developer can run on their own machine. SaaS is gated on the agent demonstrably producing useful docs that get viewed and trusted.

1. **CLI mode + Git service. *(done — see `cli.py`, `TokenCredentials`, `GitService`.)*** `greenbean clone <url>` against a remote with a user-supplied PAT, shell-out wrapper around `git`, persistent on-disk working-copy cache under `~/.greenbean/cache/`. End state: a developer can clone any repo they hold a token for. No state store, no automation loop yet.
2. **Tool layer. *(done — see `tools/working_copy.py`.)*** All the read/grep/find_symbol/git tools, well-tested in isolation against a working copy. The foundation for everything downstream — both generation and Q&A use it.
3. **Planning service. *(done — `planning/planner.py`, `planning/sqlite_store.py`.)*** Initial doc plan from a default template, source-to-doc map (mechanical, derived from imports + plan), diff-to-affected-docs query, SQLite-backed.
4. **Generator agent. *(done — `agent/generator.py`.)*** Narrow scope first: READMEs and a top-level architecture doc. Output now lands in `~/.greenbean/output/...` rather than the working copy.
5. **`greenbean run` + `greenbean watch`. *(done — `pipeline.py`, `cli.py`, `viewer.py`.)*** One-shot full loop (`run`) plus an interval-polling wrapper (`watch --interval 5m`) that does `git fetch`, fast-forwards a clean working copy to its upstream, and re-invokes the pipeline when HEAD moves. Idempotency on `(repo, sha)` via the stored `last_synced_sha` keeps overlap safe and makes the no-change case a no-op. A developer runs `greenbean watch` on their dev tree (or a CI'd cache clone) and gets continuously refreshed docs in the output dir. The optional `--view :8080` flag (§8.2) bundles a localhost markdown viewer into the same process, including a live status panel (watched repo, current commit, last/next sync).
6. **External publishers.** Plug the output dir into a real viewable surface — a static site, a separate docs repo, GitHub Pages, a Notion importer. Out of greenbean's process; ship one publisher first to prove the seam, then add more.
7. **GitHub Actions integration.** A thin packaging of `greenbean run` that runs inside customer CI, authenticates with `GITHUB_TOKEN` via `TokenCredentials`, and uploads the output dir to a downstream surface (Pages, an artifact, a separate docs repo). **Re-uses the entire CLI core; does not depend on SaaS infrastructure.** This is the first "no-install" distribution path: a customer drops a `.github/workflows/greenbean.yml` and gets docs without standing up any infra.
8. **Triage classifier and validator.** Now that the loop is end-to-end, optimize the regeneration step (most pushes shouldn't trigger expensive generation) and harden against hallucinated symbols / broken examples.
9. **Doc plan expansion.** Add reference docs for public APIs, then conceptual docs. Each doc type is its own quality investment.
10. **Q&A endpoint.** Built on the existing tool layer + doc embeddings. CLI command first, then a wireable API.
11. **SaaS: GitHub App + multi-tenant scheduler/workers.** Turn on the scaffolded `GitHubCredentials`, stand up the scheduler service (runs the "due repos" query every ~1 min) and a stateless worker pool consuming `sync_jobs`, move state from SQLite to Postgres, per-tenant disk quotas, advisory locks per repo. **No HTTP ingress, no webhooks** (polling everywhere — see §10.1). **This is the gate from "CLI dogfood" to "SaaS product"** — only worth crossing once steps 5–8 prove the docs are worth hosting for someone else.
12. **Local-agent connector.** For security-conscious customers who can't accept a hosted clone of their repo. Architecturally additive — see §10.2.
13. **Optional optimizations.** Symbol/dep-graph caching behind the tool interface, only if profiling proves the agent's on-demand discovery is the bottleneck.

The single highest-leverage thing to nail across steps 1–8 is the **source-to-doc map (step 3) + targeted regeneration (step 8)**. Together they're what makes the system feel like it actually understands the codebase rather than blindly rewriting docs whenever code changes — and that's what justifies the whole agentic framing in the first place.
