# Code Map

This page is a quick orientation guide for where logic lives in code.

## High-level flow

`CLI -> Connector boundary + Git service -> Tool layer -> Planning -> Generator -> Output dir`

The customer's source repo is read-only input. Generated docs land in `~/.greenbean/output/<host>/<owner>/<name>/` — never back in the source repo. See `Architecture.md` §8 for the publishing model.

## Module map

- `src/greenbean/cli.py`
  - v1 entrypoints: `greenbean clone`, `greenbean plan init|list`, `greenbean generate`, `greenbean run`, `greenbean watch`.
  - `run` calls `pipeline.run_once` for a single pass; `watch` polls on an interval (`_parse_interval`), fast-forwards the working copy to its upstream when clean (`_advance_to_upstream` — never resets a dirty tree), and re-runs the pipeline when HEAD moves.
  - Publishing helpers (`_output_root_for`, `_safe_output_path`, `_atomic_write_text`) are re-exported from `greenbean.publish`.
- `src/greenbean/pipeline.py`
  - `run_once(...)` — the shared **pipeline body** (sync plan → resolve diff to affected docs → generate → publish → record `last_synced_sha`). Idempotent: HEAD unchanged since the last run is a no-op; first run generates the whole plan, later runs diff `last_synced_sha → HEAD` and regenerate only `affected_docs`. The recorded SHA advances only after a successful pass. `DocGenerator` is a `Protocol` so the loop is testable with a stub instead of a live model.
- `src/greenbean/publish.py`
  - Output-path resolution (`output_root_for`, `safe_output_path`, `local_namespace`) and `atomic_write_text` (`tempfile.mkstemp` + `os.replace`). Lives here, not in `cli`, so `pipeline` can share it without a circular import.
- `src/greenbean/core/connectors.py`
  - Platform abstraction boundary.
  - Defines `RepoCredentials` — the **only** connector-layer protocol. No `ChangeNotifier` (both deployment modes poll, see Architecture.md §10.1), no `RepoWriter` (greenbean does not publish back to the source repo).
- `src/greenbean/connectors/github/`
  - `token_credentials.py` — `TokenCredentials` (single-PAT auth). Used by the v1 CLI.
  - `url.py` — GitHub URL parsing. Accepts plain HTTPS, `git@` SSH, and credentialed `https://x-access-token:TOKEN@github.com/...` forms (the last is what `git remote get-url` returns after a PAT clone).
  - `credentials.py` — `GitHubCredentials` (App + JWT + installation-token mint). **Deprecated for v1**, kept as the future-SaaS auth path (the server-side scheduler will mint installation tokens for cloning and HEAD lookups).
- `src/greenbean/core/git.py`
  - Host-agnostic async wrapper over the `git` CLI.
  - Includes `remote_url(repo_path)` (used by `_output_root_for`) and `ls_remote(url, ref)` (used for SHA lookups against a remote).
  - Shared by sync, planning, and tool implementations.
- `src/greenbean/core/tools.py`
  - Agent-facing read/search protocol (`Tools`).
  - Defines types (`DirEntry`, `GrepHit`) and safety exceptions (`PathOutsideWorkingCopy`).
- `src/greenbean/tools/working_copy.py`
  - Local working-copy implementation of `Tools`.
  - Enforces path safety via `_resolve_safe` and shells out to `rg`/`git` on demand.
- `src/greenbean/planning/`
  - `planner.py` — `DefaultPlanner` (mechanical doc-plan generator).
  - `sqlite_store.py` — `SqliteDocStore` (per-repo SQLite state file).
- `src/greenbean/agent/`
  - `generator.py` — agentic doc-generation loop.
  - `prompts/<doc_type>.md` — system prompts per doc type.

## Placement rules

- **Connector modules are only for platform-specific behavior** — auth, and nothing else. Not publishing (filesystem write to the output dir); not change notification (CLI polls in-process, SaaS polls via a scheduler — no platform abstraction needed).
- **Git operations over a working copy belong in the git service / tool layer**, not connectors.
- **Agent capabilities should be added as named tools under the tool interface** rather than direct shell/fs access.
- **Generated content goes to the output dir on disk**, never to the customer's working tree and never into the SQLite state store (which holds metadata only).
