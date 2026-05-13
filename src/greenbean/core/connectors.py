"""Connection-layer interfaces.

Platform abstraction boundary — to be implemented for each platform (GitHub
App, GitHub Actions, future hosts) when those deployments come online.

The connector exposes only what's *genuinely platform-specific* — the things a
hosting platform does that ``git`` alone can't. Everything else (reading
files, diffing, listing trees, inspecting commits) is a ``git`` operation on
the working copy and lives in a separate Git service, not here.

One protocol: ``RepoCredentials`` — mints platform auth and answers
branch-HEAD lookups (the latter is what the SaaS scheduler uses to detect
drift between the actual remote HEAD and our recorded ``last_synced_sha``).

There is no ``ChangeNotifier`` and no webhook ingress. Both deployment modes
poll:

- **CLI** polls via ``greenbean watch``'s in-process timer.
- **SaaS** polls via a server-side scheduler that queries Postgres for repos
  due for a poll, calls ``RepoCredentials.get_branch_head``, and enqueues a
  job when HEAD drifts.

There is also no ``RepoWriter``: generated docs are written to a local
output directory outside the source repo (see ``cli.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RepoRef:
    """Stable identifier for a repository in core.

    ``id`` is the internal UUID used as a foreign key everywhere in Postgres
    (or the local equivalent in CLI mode). ``source_type`` and ``external_id``
    together identify the repo on its host (e.g. ``("github", "1234567")``).
    """

    id: str
    source_type: str
    external_id: str


class RepoCredentials(Protocol):
    """Platform-specific things needed to interact with a repo.

    The clone URL is short-lived and intended for sync-time clone/fetch only;
    callers must not persist it. The branch-head lookup is what the scheduler
    uses to detect drift between the actual remote HEAD and our recorded
    ``last_synced_sha``.
    """

    async def get_clone_url(self, repo: RepoRef) -> str:
        """Return a short-lived authenticated URL for cloning or fetching."""
        ...

    async def get_branch_head(self, repo: RepoRef, branch: str) -> str:
        """Return the commit SHA that ``branch`` currently points at on the host."""
        ...
