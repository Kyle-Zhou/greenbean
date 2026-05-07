# Code Map

This page is a quick orientation guide for where logic lives in code.

## High-level flow

`CLI -> Connector boundary + Git service -> Tool layer -> (future) sync/planning/agent/publish`

## Module map

- `src/greenbean/cli.py`
  - Current v1 entrypoint (`greenbean clone`).
  - Parses user input, resolves clone URL strategy, invokes `GitService`.
- `src/greenbean/core/connectors.py`
  - Platform abstraction boundary.
  - Defines `ChangeNotifier`, `RepoCredentials`, `RepoWriter`, and optional capabilities.
- `src/greenbean/connectors/github/`
  - GitHub-specific connector implementations and stubs.
  - `connector.py` composes notifier + credentials + writer into one connector object.
- `src/greenbean/core/git.py`
  - Host-agnostic async wrapper over the `git` CLI.
  - Shared by sync, planning, and tool implementations.
- `src/greenbean/core/tools.py`
  - Agent-facing read/search protocol (`Tools`).
  - Defines types (`DirEntry`, `GrepHit`) and safety exceptions.
- `src/greenbean/tools/working_copy.py`
  - Local working-copy implementation of `Tools`.
  - Enforces path safety and shells out to `rg`/`git` on demand.

## Placement rules

- Connector modules are only for platform-specific behavior (auth, webhook ingress, PR publishing).
- Git operations over a working copy belong in the git service/tool layer, not connectors.
- Agent capabilities should be added as named tools under the tool interface rather than direct shell/fs access.
