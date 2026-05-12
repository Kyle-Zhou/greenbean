# Self-Documenting Repository Platform — System Architecture

## 1. Overview

A platform that keeps a repository's documentation continuously in sync with its code. When code changes, the system detects which docs are affected, regenerates them with an LLM-driven agent, validates the output, and publishes the result to a local doc store outside the source repo, viewable on its own surface. The same underlying engine doubles as a Q&A agent over the codebase and its documentation.

The customer's repo is **read-only input**. Generated docs are not committed back, branched, or PR'd. v1 ships a single-tenant CLI that runs the full pipeline locally; multi-tenant SaaS, GitHub App auth, and GitHub Actions integrations are deferred but the architectural seams for them are preserved.

### Design principles

- **Connection layer is modular.** v1 ships GitHub-only (clone-via-PAT) and the local CLI is the only deployment. The connector abstraction supports future SaaS deployments (GitHub App, webhooks) and other-host implementations (GitLab, Bitbucket) without rewriting the core.
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
│   │  - polling watcher   │    │  - WebhookNotifier             │    │
│   │    (greenbean watch) │    │  - LocalAgentConnector         │    │
│   └──────────┬───────────┘    └────────────────────────────────┘    │
│         (Git service is shared, not part of the connector)           │
└──────────────┼──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                            Sync Layer                                │
│  - Receives change events (CLI: poll-driven; SaaS: webhook + cron)   │
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

**Note on what's *not* in the connector anymore.** Earlier drafts had a `RepoWriter` interface and a publishing layer that opened pull requests. Neither survives the pivot to local-output. greenbean does not write to the source repo; the source repo is read-only input. The "publish" step is a filesystem write into `~/.greenbean/output/...`. External publishers (static site, separate docs repo, GitHub Pages bot) are out of greenbean's process — they consume the output directory.

---

## 3. Connection Layer

The connector exposes only what's *genuinely platform-specific* — the things a hosting platform does that `git` alone can't. Everything else (reading files, diffing, listing trees, inspecting commits) is a `git` operation on the working copy and lives in a separate `Git` service, not the connector.

### 3.1 What's in the connector

Two small interfaces, each doing one platform-specific thing:

**`ChangeNotifier`** — emits change events when a repo's tracked branch moves.

```
subscribe(handler)
  ─ emits: {repo_id, before_sha, after_sha, ref}
```

CLI v1: not used. The `greenbean watch` polling loop runs `git fetch` on the cached working copy and calls into the orchestrator directly when HEAD advances — no notifier abstraction needed for a single in-process consumer.
Future SaaS implementation: webhook receiver with HMAC signature verification (the scaffolded `WebhookNotifier`).
Future local-agent implementation: filesystem watcher on `.git/refs/heads/{branch}`.

**`RepoCredentials`** — provides the platform-specific things needed to interact with a repo.

```
get_clone_url(repo_id) -> authenticated URL   # short-lived, for sync-time clone/fetch
get_branch_head(repo_id, branch) -> sha       # for reconciliation drift checks
```

CLI v1 implementation: `TokenCredentials` — a single PAT supplied via `--token` or `$GITHUB_TOKEN`, embedded as `https://x-access-token:{token}@github.com/{owner}/{name}.git`.
Future SaaS implementation: `GitHubCredentials` — mints short-lived installation tokens, calls the GitHub API for branch HEAD.
Future local-agent implementation: `get_clone_url` is a no-op (repo already on disk); `get_branch_head` is a local `git rev-parse`.

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
- **Polling watcher** *(`greenbean watch`, planned)* — interval-driven loop that does `git fetch` on the cached working copy, compares HEAD against `last_synced_sha`, and invokes the run loop on change. No `ChangeNotifier` abstraction needed in-process.

### 3.5 Deferred / scaffolded implementations (SaaS path)

These exist in the codebase but are **not wired into any v1 code path**. They survive the pivot because the SaaS deployment is paused, not abandoned — when that work resumes, these are the seams it plugs into.

- **`GitHubCredentials`** — GitHub App auth: signs an app JWT, mints short-lived installation tokens, calls `GET /repos/{owner}/{repo}/branches/{branch}` for reconciliation. Module-level docstring marks it deprecated for v1.
- **`WebhookNotifier`** — receives `push` webhooks at an HTTP endpoint, validates HMAC signature, emits change events. Methods raise `NotImplementedError` until wired in.
- **`GitHubConnector`** — composition point that bundles `WebhookNotifier` + `GitHubCredentials`. Documented as scaffolded for the future SaaS deployment.

**Auth model (future SaaS).** GitHub App. Customer installs the app on selected repos; the system receives an installation ID and mints short-lived installation tokens per operation. Tokens held in memory only, never written to disk. No long-lived PATs.

### 3.6 What this means for future deployments

The slimmer interface (just two protocols, no writer) is *better* for every future deployment:

- **SaaS / GitHub App / GitHub Actions** — implements `GitHubCredentials` (already scaffolded) and `WebhookNotifier` (already scaffolded). The "publish" step still writes to a server-side output store (filesystem or object storage); customers consume it through whatever surface the SaaS exposes (web app, separate repo, Pages site).
- **Local-agent connector** — implements `RepoCredentials.get_clone_url` as a no-op (the working copy *is* the user's repo) and `get_branch_head` as a local `git rev-parse`. Change notification can be a `FilesystemWatcher` on `.git/refs/heads/{branch}`. The Git service is unchanged. The tool layer is unchanged. The agent is unchanged.
- **Other-host (GitLab, Bitbucket)** — implement the two protocols against that host's API. No publishing-back-to-repo plumbing to port; the output dir is the same on every host.

This is what we wanted: the variable parts (auth, notifications) are isolated; everything else is platform-agnostic.

---

## 4. Sync Layer

Bridges "something happened upstream" and "the agent has files it can grep." Doesn't know anything about docs, LLMs, or generation — its job ends when the working copy is current and a diff is computed.

### 4.1 Responsibilities

- Receive change events. CLI: a `greenbean watch` tick or a manual `greenbean run`. SaaS (future): webhooks plus reconciliation cron as a safety net.
- Verify event integrity (HMAC for webhooks; SHA freshness for polling) and check idempotency on `(repo, after_sha)`.
- Ensure the working-copy cache is at `after_sha` (clone if missing, otherwise `git fetch` + reset).
- Compute the diff `before_sha → after_sha`.
- Update `last_synced_sha` and hand off to Planning.

### 4.2 Fast path vs worker (SaaS path)

In the future SaaS deployment the webhook handler should be fast — GitHub retries on slow responses. The handler does cheap validation (signature, idempotency lookup) and enqueues a `SyncJob`. A worker picks the job up and does the actual git operations: it gets a clone URL from `RepoCredentials`, then uses the `Git` service to clone or fetch+reset. This separation lets sync workers (I/O-bound, small) scale independently from generation workers (LLM-bound, larger).

In CLI mode there is no queue: `greenbean run` does the work in-process. `greenbean watch` calls `run` on each tick.

### 4.3 Working-copy cache

The working copy is a *cache*, not storage — fully reconstructable from the remote. Don't back it up, don't replicate it, evict it freely under disk pressure.

- CLI: located under `~/.greenbean/cache/<host>/<owner>/<name>/`. Single-user, no tenant prefix.
- Future SaaS: located on attached disk per worker pool, namespaced per tenant: `/var/lib/repo-cache/{tenant_id}/{repo_id}/`.
- Shallow clones by default (depth ~50); `git fetch --deepen=N` on demand if more history is needed.
- Future SaaS: LRU eviction tracked via `repos.last_accessed_at`; per-tenant disk quotas; per-repo locks during sync (Postgres advisory lock or Redis) so concurrent webhooks for the same repo don't race on the same working copy.

### 4.4 Tenant isolation (SaaS path)

Three layers, each doing real work, when SaaS resumes:

- **Filesystem.** Working copies are namespaced under `{tenant_id}/{repo_id}/`. Every downstream tool call takes a `repo_id`, resolves it to its canonical path, and refuses to operate on paths that don't resolve under that prefix. Path traversal is rejected at this layer.
- **Process.** A sync worker handles one job at a time. For higher-isolation deployments (enterprise), run each job in an ephemeral container that's torn down after.
- **Credentials.** GitHub installation tokens are minted per-operation, scoped to the minimum repo set, short-lived, and held in memory only.

CLI mode is single-tenant by construction; none of this applies until the SaaS gate.

All repo operations are read-only at the OS level (grep, read, AST parse, `git log`). No `npm install`, no running customer scripts in v1. Crossing this line requires real sandboxing (Firecracker / gVisor / ephemeral containers) and is explicitly out of scope.

### 4.5 Reconciliation

Polling and webhooks both fail. Networks blip. Workers crash mid-sync. CLI's `greenbean watch` is itself the reconciliation mechanism — every tick is a fresh check. Future SaaS adds a periodic cron (every ~15 minutes per active repo) that checks the actual default-branch HEAD via the GitHub API against `last_synced_sha`; on drift, enqueues a SyncJob to catch up. Webhooks are the fast path; reconciliation is the correctness guarantee.

### 4.6 Idempotency

The single highest-leverage property in this layer. Idempotency on `(repo, after_sha)` makes most failure modes benign: duplicate event → no-op; worker crashed mid-sync → restart with no harm; reconciliation finds the same SHA we already processed → return early. Track every notification (CLI: tick; SaaS: webhook delivery) for both idempotency and debugging.

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
  last_accessed_at TIMESTAMPTZ,
  cache_pinned BOOLEAN DEFAULT false,
  status TEXT
);

-- Sync layer state (webhook deliveries + sync jobs)
CREATE TABLE webhook_deliveries (
  id UUID PRIMARY KEY,
  repo_id UUID,
  delivery_id TEXT UNIQUE,
  event_type TEXT,
  before_sha TEXT, after_sha TEXT,
  received_at TIMESTAMPTZ, processed_at TIMESTAMPTZ
);
CREATE INDEX ON webhook_deliveries (repo_id, after_sha);
CREATE TABLE sync_jobs (
  id UUID PRIMARY KEY,
  repo_id UUID, before_sha TEXT, after_sha TEXT,
  status TEXT, error TEXT,
  started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ
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

### 8.2 Why a directory, not a database

- Files on disk are viewable in any IDE, `cat`, or markdown previewer. No bespoke viewer needed for the user to actually *read* their docs.
- External publishers — static site builders, separate docs repos, GitHub Pages bots, Notion importers — can be written as plain "watch this directory and sync somewhere else" tools, without coupling to greenbean's internals.
- The state store (SQLite/Postgres) remains an *index* over content rather than a content store. That separation is what's already there for working-copy clones (cache on disk; metadata in DB) and it carries over cleanly.

### 8.3 External publishers (deferred)

Out of greenbean's process. Examples that could plug into the output dir without changes here:

- A static-site generator (mkdocs, Docusaurus) pointed at the output dir.
- A separate "docs repo" committer that turns each generation into a commit on a docs-only repo, optionally hosted on GitHub Pages.
- A web app that serves the output dir over HTTP with auth.

When SaaS resumes, the output store moves from `~/.greenbean/output/` to a server-side filesystem or object store; the surface seen by external publishers is the same.

### 8.4 Failure modes

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

The deferred deployment path. The seams are already in the codebase:

- **`GitHubCredentials`** (App-based JWT + installation-token mint) — implements `RepoCredentials` for per-installation auth.
- **`WebhookNotifier`** — implements `ChangeNotifier` for push-driven sync.
- **`GitHubConnector`** — composes the two. (No writer slot — the publish target is server-side storage, not the customer's repo.)

A GitHub Actions integration is a thin variant: the action runs the equivalent of `greenbean run` inside the customer's CI, using `GITHUB_TOKEN` with `TokenCredentials`, and uploads the output dir to a downstream surface (Pages, an artifact store, a separate docs repo). It re-uses the entire CLI core; nothing about the agent or the tool layer changes.

When SaaS work resumes, the moves are: turn on the FastAPI webhook receiver, swap the SQLite store for Postgres, add per-tenant disk isolation under `/var/lib/greenbean/cache/{tenant_id}/...`, mint installation tokens via the existing `GitHubCredentials`, and route generated docs into a tenant-namespaced output store.

### 10.2 Local-agent connector

For security-conscious customers who can't accept a hosted clone of their repo. Architecturally additive, no core changes:

- **Same `Git` service** — operates on a path on disk, doesn't care if that disk is a server or a developer's laptop.
- **Same tool layer backend** — `read_file`, `grep`, `find_symbol`, etc. all work against the local working copy.
- **Different `ChangeNotifier`** — `FilesystemWatcher` watching `.git/refs/heads/{branch}` instead of webhooks.
- **Different `RepoCredentials`** — `get_clone_url` is a no-op; the sync layer skips clone/fetch entirely when the working copy *is* the user's repo. `get_branch_head` is a local `git rev-parse`.
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
- SaaS (future): GitHub App installation tokens minted per-operation, scoped to the minimum repo set, held in memory only. Working copies isolated per tenant on the filesystem. Webhook signatures verified.
- No execution of customer code in v1 — read-only static analysis only.
- Customer prompts and code excerpts sent to LLM providers; document this in the customer-facing privacy policy and offer per-customer model-provider routing for enterprise.

### 11.4 Reliability

- Sync is idempotent on `(repo, to_sha)`.
- Generation jobs are retryable; partial progress tracked at the per-doc level so a retry doesn't redo work.
- CLI: `greenbean watch` ticks are themselves the reconciliation mechanism. SaaS: dedicated reconciliation cron.

---

## 12. Build Order

Roughly the sequence to actually ship in. v1 prioritizes the local end-to-end automation loop a single developer can run on their own machine. SaaS is gated on the agent demonstrably producing useful docs that get viewed and trusted.

1. **CLI mode + Git service. *(done — see `cli.py`, `TokenCredentials`, `GitService`.)*** `greenbean clone <url>` against a remote with a user-supplied PAT, shell-out wrapper around `git`, persistent on-disk working-copy cache under `~/.greenbean/cache/`. End state: a developer can clone any repo they hold a token for. No state store, no automation loop yet.
2. **Tool layer. *(done — see `tools/working_copy.py`.)*** All the read/grep/find_symbol/git tools, well-tested in isolation against a working copy. The foundation for everything downstream — both generation and Q&A use it.
3. **Planning service. *(done — `planning/planner.py`, `planning/sqlite_store.py`.)*** Initial doc plan from a default template, source-to-doc map (mechanical, derived from imports + plan), diff-to-affected-docs query, SQLite-backed.
4. **Generator agent. *(done — `agent/generator.py`.)*** Narrow scope first: READMEs and a top-level architecture doc. Output now lands in `~/.greenbean/output/...` rather than the working copy.
5. **`greenbean run` + `greenbean watch`. *(next.)*** One-shot full loop (`run`) plus an interval-polling wrapper (`watch --interval 5m`) that does `git fetch` and re-invokes `run` when HEAD moves. Idempotency on `(repo, sha)` keeps overlap safe. End state: a developer runs `greenbean watch` on their dev tree (or a CI'd cache clone) and gets continuously refreshed docs in the output dir without further intervention.
6. **Triage classifier and validator.** Now that the loop is end-to-end, optimize the regeneration step (most pushes shouldn't trigger expensive generation) and harden against hallucinated symbols / broken examples.
7. **External publishers.** Plug the output dir into a real viewable surface — a static site, a separate docs repo, GitHub Pages, a Notion importer. Out of greenbean's process; ship one publisher first to prove the seam, then add more.
8. **Doc plan expansion.** Add reference docs for public APIs, then conceptual docs. Each doc type is its own quality investment.
9. **Q&A endpoint.** Built on the existing tool layer + doc embeddings. CLI command first, then a wireable API.
10. **SaaS: GitHub App + multi-tenant sync layer.** Turn on the scaffolded `GitHubCredentials` + `WebhookNotifier`, add the FastAPI webhook receiver, move state from SQLite to Postgres, per-tenant disk quotas, advisory locks, reconciliation cron. **This is the gate from "CLI dogfood" to "SaaS product"** — only worth crossing once steps 5–7 prove the docs are worth subscribing to.
11. **GitHub Actions integration.** A thin packaging of `greenbean run` that runs inside customer CI, uploads the output dir to a downstream surface. Re-uses the entire CLI core.
12. **Local-agent connector.** For security-conscious customers who can't accept a hosted clone of their repo. Architecturally additive — see §10.2.
13. **Optional optimizations.** Symbol/dep-graph caching behind the tool interface, only if profiling proves the agent's on-demand discovery is the bottleneck.

The single highest-leverage thing to nail in steps 1–5 is the **source-to-doc map + targeted regeneration**. That's what makes the system feel like it actually understands the codebase rather than blindly rewriting docs whenever code changes — and it's what justifies the whole agentic framing in the first place.
