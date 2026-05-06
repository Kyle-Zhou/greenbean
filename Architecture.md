# Self-Documenting Repository Platform — System Architecture

## 1. Overview

A platform that keeps a repository's documentation continuously in sync with its code. When code changes, the system detects which docs are affected, regenerates them with an LLM-driven agent, validates the output, and publishes the result back to the repo as a pull request. The same underlying engine doubles as a Q&A agent over the codebase and its documentation.

### Design principles

- **Connection layer is modular.** v1 ships GitHub-only (server-hosted), but the abstraction supports future local-agent and other-host (GitLab, Bitbucket) implementations without rewriting the core.
- **Code stays where it belongs.** Docs are published to the customer's repo as Markdown via PR. The customer owns their docs and can leave at any time with no export step.
- **Postgres is the operational substrate.** Generation metadata, dependency graphs, embeddings, and analytics live server-side. The repo is the published surface; Postgres is the truth about system state.
- **Grep beats RAG for code.** Agents discover the codebase through structured search (ripgrep, tree-sitter, git) on a server-cached working copy. Embeddings are a supplementary navigation aid, not the primary retrieval mechanism.
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
│   │  - GitRepoSource     │    │  - GitRepoSource               │    │
│   │  - GitHubWriter (PR) │    │  - LocalWriter                 │    │
│   └──────────┬───────────┘    └────────────────────────────────┘    │
└──────────────┼──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          Sync Orchestrator                           │
│  - Receives change events                                            │
│  - Manages working-copy cache                                        │
│  - Tracks last_synced_sha per repo                                   │
│  - Enqueues generation jobs                                          │
└──────────────┬──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Comprehension Engine                           │
│  - Symbol extraction (tree-sitter)                                   │
│  - Dependency graph builder                                          │
│  - Doc plan maintainer                                               │
│  - Source-to-doc dependency index                                    │
└──────────────┬──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          Agent Runtime                               │
│  - Tool layer (read/grep/find_symbol/git/semantic_search)            │
│  - Planner / classifier (cheap model)                                │
│  - Generator (capable model, agentic loop)                           │
│  - Validator (tsc/ast parse, symbol resolution, link check)          │
└──────────────┬──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     Publishing & Storage Layer                       │
│  - Postgres (metadata, plan, dep graph, pgvector index)              │
│  - Working-copy cache (cloned repos on disk)                         │
│  - PR builder + writer                                               │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                       ┌─────────────┐
                       │  Q&A API    │  (shares Agent Runtime + Tools)
                       └─────────────┘
```

---

## 3. Connection Layer

The connection layer is an abstraction with two sub-interfaces. v1 implements both for GitHub-hosted repos.

### 3.1 Interfaces

**`RepoSource`** — read access to a repo's contents and history.

```
get_file(path, ref) -> bytes
list_tree(ref) -> [paths]
diff(from_sha, to_sha) -> [{path, status, old_blob, new_blob}]
get_commit(sha) -> {author, message, timestamp, parents}
get_default_branch() -> ref
```

**`ChangeNotifier`** — tells the orchestrator when a repo has changed.

```
subscribe(repo_id, callback)
```

Implementations push events of shape `{repo_id, from_sha, to_sha, ref, triggered_by}`.

**`RepoWriter`** — write access for publishing docs.

```
open_pull_request(repo_id, branch_name, files: [{path, content}], title, body) -> pr_url
```

**Optional capability interfaces** (connectors implement what they support):
- `SupportsPullRequests`
- `SupportsCheckRuns`
- `SupportsCommitComments`

The core never assumes these exist; it feature-detects.

### 3.2 v1 implementations

- **`GitHubConnector.WebhookNotifier`** — receives `push` webhooks, validates signature, emits change events.
- **`GitRepoSource`** — operates on a working-copy cache directory. Same implementation reused later for local-agent connectors.
- **`GitHubWriter`** — uses GitHub App installation token to create branch, commit, and open PR.

**Auth model.** GitHub App per Anthropic-style installation. Customer installs the app on selected repos; we receive an installation ID and mint short-lived installation tokens per operation. No long-lived PATs.

---

## 4. Sync Orchestrator

Stateless service that consumes change events from the connection layer and drives the rest of the pipeline.

### 4.1 Responsibilities

- Look up `repos.last_synced_sha` for the repo.
- Ensure the working-copy cache is at `to_sha` (clone if missing, otherwise `git fetch` + reset).
- Compute the diff `last_synced_sha → to_sha`.
- Enqueue a `GenerationJob` with the diff and repo ID.
- On job completion, advance `last_synced_sha`.

### 4.2 Working-copy cache

- Located on attached disk per worker pool (e.g., `/var/lib/repos/{repo_id}`).
- Shallow clones by default; deepened on demand when `git log` is needed for context.
- LRU eviction when disk pressure exceeds threshold; re-clone is cheap.
- One repo, one working copy per worker — no concurrent writes to the same checkout. Use a per-repo lock during sync.

### 4.3 Tenant isolation

- Working copies for different customers live in separate directories with separate filesystem permissions.
- All repo operations are read-only at the OS level (grep, read, AST parse, `git log`). No `npm install`, no running customer scripts in v1. This keeps the security model simple — we're processing files, not executing code.
- Future "execute tests to enrich docs" features require sandboxed runners (Firecracker / gVisor / ephemeral containers) and are explicitly out of scope for v1.

---

## 5. Comprehension Engine

Builds and maintains structural understanding of each repo. **No LLMs in this stage** — it's deterministic, fast, and cached.

### 5.1 Pipeline

1. **Project profile.** Detect language(s), framework(s), package manager(s) from manifest files. Cached and refreshed on manifest changes.
2. **Symbol extraction.** Tree-sitter parses every source file and emits a structured inventory: functions, classes, exports, types, public API surface.
3. **Dependency graph.** Static analysis of imports/requires builds a directed graph: `file → files it depends on`.
4. **Entry-point detection.** `main.py`, `index.ts`, route definitions, CLI commands, top-level exports.
5. **Doc plan.** Given the above, propose the target doc tree (READMEs per module, architecture overview, reference docs for public APIs, etc.). The plan is stored in Postgres and evolves with the repo.
6. **Source-to-doc index.** For each planned doc, record which source files it depends on. This is what makes targeted regeneration possible.

### 5.2 Incremental updates

On every push, the engine updates only what changed:
- Re-parse changed files (tree-sitter).
- Update edges in the dep graph touching changed files.
- Update the doc plan if module structure changed (new directories, removed packages).

The full pipeline only runs once, on initial onboarding. Steady-state runs are fast.

---

## 6. Agent Runtime

The core engine that does the actual reading, writing, and reasoning. Used by both doc generation and Q&A.

### 6.1 Tool layer

Tools are an interface. v1 implements them against the server-side working copy; future local-agent implementations swap the backend without changing agent logic.

| Tool | Purpose | Backed by |
|---|---|---|
| `read_file(path, range?)` | Direct file reads | filesystem |
| `list_directory(path)` | Navigation | filesystem |
| `grep(pattern, path?, opts?)` | Literal/regex search | ripgrep |
| `find_symbol(name)` | Definition lookup | tree-sitter index |
| `find_references(symbol)` | Usage lookup | tree-sitter index |
| `git_log(path, limit?)` | History for "why" context | `git log` |
| `git_blame(path, line)` | Line-level provenance | `git blame` |
| `semantic_search(query)` | Fuzzy navigation when symbol unknown | pgvector over file summaries |
| `read_doc(path)` | Read existing generated doc | filesystem |

**Retrieval philosophy.** Grep and AST search are primary. Semantic search is a navigational aid for when the agent doesn't yet know what to look for — it returns candidate paths the agent then reads directly. Embeddings find the haystack; grep finds the needle.

### 6.2 The three agent modes

All three are the same loop (gather context → produce output → validate → iterate) with different prompts and exit conditions.

#### Triage classifier (cheap model, e.g., Haiku)

Input: a diff and a candidate doc.
Output: `regenerate | targeted_edit | no_op` plus rationale.
Purpose: avoid expensive regeneration when a code change doesn't actually affect what a doc says (internal refactors, formatting, etc.).

#### Generator (capable model, e.g., Opus)

Input: doc spec, prior version (if any), source dependency files, related docs, repo conventions, doc-type template.
Loop: agent decides what additional context it needs, calls tools, writes a draft, self-checks against requirements, emits final doc with embedded source citations.
Output: Markdown with `<!-- src: path:line-range -->` anchors for traceability.

#### Q&A agent (capable model)

Input: user question, repo ID.
Loop: same tool-use loop as the generator, but optimizing for an answer rather than a doc artifact. First-pass uses semantic_search over generated docs (fast, often sufficient); falls back to code-level tools when docs don't contain the answer.

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
- **Symbol resolution.** Every symbol referenced in the doc resolves in the symbol index. Catches hallucinated APIs, the #1 source of user trust loss in auto-doc tools.
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
  status TEXT                 -- onboarding | active | paused | error
);

-- Comprehension cache
CREATE TABLE symbols (
  id UUID PRIMARY KEY,
  repo_id UUID,
  path TEXT, name TEXT, kind TEXT,
  start_line INT, end_line INT,
  signature TEXT,
  exported BOOLEAN,
  at_sha TEXT
);
CREATE INDEX ON symbols (repo_id, name);
CREATE INDEX ON symbols (repo_id, path);

CREATE TABLE file_dependencies (
  repo_id UUID, from_path TEXT, to_path TEXT,
  PRIMARY KEY (repo_id, from_path, to_path)
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
  repo_id UUID, from_sha TEXT, to_sha TEXT,
  status TEXT, error TEXT,
  cost_usd NUMERIC,
  started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ
);
CREATE TABLE pull_requests (
  id UUID PRIMARY KEY,
  job_id UUID, repo_id UUID,
  pr_number INT, pr_url TEXT,
  state TEXT, opened_at TIMESTAMPTZ, merged_at TIMESTAMPTZ
);
```

### 7.2 What lives where

- **Repo (Markdown files committed via PR)** — the published, user-facing docs. Customer-owned, customer-readable, version-controlled by Git.
- **Postgres** — everything *about* docs: plan, dep graph, generation metadata, embeddings, prior versions, analytics, feedback. Customer never sees this directly.
- **Working-copy disk cache** — ephemeral mirror of the repo. Re-clonable at any time; not a system of record.

### 7.3 Sync direction and human edits

- System-of-truth flow: source code (repo) → comprehension (Postgres) → generation → published docs (repo).
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

The architecture is built so a local-agent connector slots in without core changes.

- **Same `RepoSource` implementation** (`GitRepoSource`) — operates on a path on disk, doesn't care if that disk is a server or a developer's laptop.
- **Different `ChangeNotifier`** — `FilesystemWatcher` watching `.git/refs/heads/{branch}`.
- **Different `RepoWriter`** — writes to a local branch and lets the user push, or opens a PR via the user's gh CLI.
- **Different `Tools` backend** — same interface, executes shell tools on the local machine instead of the server.
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

Roughly the sequence I'd actually ship in:

1. **Connection layer + sync orchestrator.** GitHub App, webhook receiver, working-copy cache, basic sync state. End state: we know when a repo changes and we have its files on disk.
2. **Comprehension engine.** Tree-sitter symbol extraction, dep graph, file summaries with embeddings. End state: we have a structured map of every connected repo.
3. **Tool layer.** All the read/grep/find_symbol/git tools, well-tested in isolation. This is the foundation for everything downstream.
4. **Generator agent — narrow scope first.** Only READMEs and a top-level architecture doc. End state: real docs being committed via PR for one or two design-partner customers.
5. **Triage classifier and validator.** Now that docs exist, optimize the regeneration loop and harden against hallucinated symbols / broken examples.
6. **Doc plan expansion.** Add reference docs for public APIs, then conceptual docs. Each doc type is its own quality investment.
7. **Q&A endpoint.** Built on the existing tool layer + doc embeddings. This is where the modularity pays back.
8. **Local-agent connector.** Once enough security-conscious customers ask for it.

The single highest-leverage thing to nail in the first three steps is the **dependency graph + targeted regeneration**. That's what makes the system feel like it actually understands the codebase rather than blindly rewriting docs whenever code changes — and it's what justifies the whole agentic framing in the first place.
