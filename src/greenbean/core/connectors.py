"""Connection-layer interfaces.

Platform abstraction boundary — to be implemented for each platform (GitHub
App, GitHub Actions, future hosts) when those deployments come online.

The connector exposes only what's *genuinely platform-specific* — the things a
hosting platform does that ``git`` alone can't. Everything else (reading
files, diffing, listing trees, inspecting commits) is a ``git`` operation on
the working copy and lives in a separate Git service, not here.

Two protocols, each doing one platform-specific thing:

- ``ChangeNotifier`` — emits change events when a tracked branch advances.
- ``RepoCredentials`` — mints platform auth and answers branch-HEAD lookups.

There is no ``RepoWriter`` here: greenbean does not publish docs back to the
source repo. Generated docs are written to a local output directory outside
the repo (see ``cli.py``); future external publishers (static site, separate
docs repo, etc.) consume that directory.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepoRef:
    """Stable identifier for a repository in core.

    ``id`` is the internal UUID used as a foreign key everywhere in Postgres.
    ``source_type`` and ``external_id`` together identify the repo on its host
    (e.g. ``("github", "1234567")``).
    """

    id: str
    source_type: str
    external_id: str


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """Emitted by a ``ChangeNotifier`` when a repo's tracked ref advances."""

    repo: RepoRef
    ref: str
    before_sha: str | None  # ``None`` for the very first event on a repo
    after_sha: str
    triggered_by: str  # e.g. "webhook", "reconciliation", "manual"


# ---------------------------------------------------------------------------
# Core interfaces
# ---------------------------------------------------------------------------


ChangeCallback = Callable[[ChangeEvent], Awaitable[None]]


class ChangeNotifier(Protocol):
    """Push-based source of repo change events.

    Implementations register a single global handler; the orchestrator
    dispatches downstream. Events must be delivered idempotently keyed on
    ``(repo.id, after_sha)`` — webhooks retry, reconciliation jobs may re-emit
    the same event. Deduplication is the orchestrator's job, but notifiers
    should not invent spurious events.
    """

    async def subscribe(self, callback: ChangeCallback) -> None: ...

    async def unsubscribe(self, callback: ChangeCallback) -> None: ...


class RepoCredentials(Protocol):
    """Platform-specific things needed to interact with a repo.

    The clone URL is short-lived and intended for sync-time clone/fetch only;
    callers must not persist it. The branch-head lookup is what reconciliation
    uses to detect drift between the actual remote HEAD and our recorded
    ``last_synced_sha``.
    """

    async def get_clone_url(self, repo: RepoRef) -> str:
        """Return a short-lived authenticated URL for cloning or fetching."""
        ...

    async def get_branch_head(self, repo: RepoRef, branch: str) -> str:
        """Return the commit SHA that ``branch`` currently points at on the host."""
        ...
