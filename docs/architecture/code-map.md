# Code Map

This page is a quick orientation guide for where logic lives in code.

## High-level flow

`CLI -> Connector boundary + Git service -> Tool layer -> Planning -> Generator -> Output dir`

The customer's source repo is read-only input. Generated docs land in `~/.greenbean/output/<host>/<owner>/<name>/` — never back in the source repo. See `Architecture.md` §8 for the publishing model.

## Module map

- `src/greenbean/cli.py`
  - v1 entrypoints: `greenbean clone`, `greenbean plan init|list`, `greenbean generate`.
  - `_output_root_for(repo_path, git)` resolves the per-repo output root from the working copy's `origin` URL; falls back to `_local/<basename>` for unrecognized remotes.
  - `greenbean run` and `greenbean watch` (the full automation loop) are next — see Architecture.md §12 step 5.
- `src/greenbean/core/connectors.py`
  - Platform abstraction boundary.
  - Defines `ChangeNotifier` and `RepoCredentials` — the only two protocols. There is **no** `RepoWriter` (greenbean does not publish back to the source repo) and no `Connector` aggregate.
- `src/greenbean/connectors/github/`
  - `token_credentials.py` — `TokenCredentials` (single-PAT auth). Used by the v1 CLI.
  - `url.py` — GitHub URL parsing.
  - `credentials.py` — `GitHubCredentials` (App + JWT + installation-token mint). **Deprecated for v1**, kept as the future-SaaS seam.
  - `notifier.py` — `WebhookNotifier`. **Deprecated for v1**, kept as the future-SaaS seam.
  - `connector.py` — `GitHubConnector` aggregate (notifier + credentials). **Deprecated for v1**, kept for the future-SaaS deployment.
- `src/greenbean/core/git.py`
  - Host-agnostic async wrapper over the `git` CLI.
  - Includes `remote_url(repo_path)` for resolving the output root from a working copy.
  - Shared by sync, planning, and tool implementations.
- `src/greenbean/core/tools.py`
  - Agent-facing read/search protocol (`Tools`).
  - Defines types (`DirEntry`, `GrepHit`) and safety exceptions.
- `src/greenbean/tools/working_copy.py`
  - Local working-copy implementation of `Tools`.
  - Enforces path safety and shells out to `rg`/`git` on demand.
- `src/greenbean/planning/`
  - `planner.py` — `DefaultPlanner` (mechanical doc-plan generator).
  - `sqlite_store.py` — `SqliteDocStore` (per-repo SQLite state file).
- `src/greenbean/agent/`
  - `generator.py` — agentic doc-generation loop.
  - `prompts/<doc_type>.md` — system prompts per doc type.

## Placement rules

- **Connector modules are only for platform-specific behavior** — auth and change notification. Nothing else. No publishing (that's a filesystem write to the output dir, not platform-specific).
- **Git operations over a working copy belong in the git service / tool layer**, not connectors.
- **Agent capabilities should be added as named tools under the tool interface** rather than direct shell/fs access.
- **Generated content goes to the output dir on disk**, never to the customer's working tree and never into the SQLite state store.
