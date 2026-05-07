"""Connection-layer interfaces.

Platform abstraction boundary -> to be implemented for each platform (GitHub, Local Agent, etc.)

The connector exposes only what's *genuinely platform-specific* — the things a
hosting platform does that ``git`` alone can't. Everything else (reading
files, diffing, listing trees, inspecting commits) is a ``git`` operation on
the working copy and lives in a separate Git service, not here.

Three small interfaces, each doing one platform-specific thing:

- ``ChangeNotifier`` — emits change events when a tracked branch advances.
- ``RepoCredentials`` — mints platform auth and answers branch-HEAD lookups.
- ``RepoWriter`` — publishes generated docs back to the customer's repo.

Optional capabilities (check runs, commit comments, PR reviews) live in
separate ``Supports*`` protocols. Core code feature-detects with
``isinstance``; it must never assume any of them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

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


@dataclass(frozen=True, slots=True)
class FileToWrite:
    path: str
    content: bytes


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


class RepoWriter(Protocol):
    """Publishes generated docs back to the customer's repo.

    The required surface is intentionally minimal: open a pull request with a
    set of file writes. Connectors that do not natively support PRs (e.g. a
    local-agent writer) can implement this by writing to a local branch and
    delegating push/PR creation to the user.
    """

    async def open_pull_request(
        self,
        repo: RepoRef,
        branch_name: str,
        files: Iterable[FileToWrite],
        title: str,
        body: str,
        base_branch: str | None = None,
    ) -> str:
        """Open a PR and return its URL."""
        ...


# ---------------------------------------------------------------------------
# Optional capability protocols
# ---------------------------------------------------------------------------
#
# Connectors implement these only when the host supports them. Core code
# feature-detects with ``isinstance``; it must never assume any of them.


@runtime_checkable
class SupportsPullRequests(Protocol):
    """Marker: the writer publishes via real pull requests, not direct push."""

    async def open_pull_request(
        self,
        repo: RepoRef,
        branch_name: str,
        files: Iterable[FileToWrite],
        title: str,
        body: str,
        base_branch: str | None = None,
    ) -> str: ...


@runtime_checkable
class SupportsCheckRuns(Protocol):
    """Host can attach check runs (status badges) to a commit or PR."""

    async def post_check_run(
        self,
        repo: RepoRef,
        sha: str,
        name: str,
        conclusion: str,  # "success" | "failure" | "neutral" | ...
        summary: str,
        details_url: str | None = None,
    ) -> None: ...


@runtime_checkable
class SupportsCommitComments(Protocol):
    """Host can attach comments to commits or PR diffs."""

    async def post_commit_comment(
        self,
        repo: RepoRef,
        sha: str,
        body: str,
        path: str | None = None,
        line: int | None = None,
    ) -> None: ...


@runtime_checkable
class SupportsPullRequestReviews(Protocol):
    """Host supports posting structured PR reviews (approve / request changes / comment)."""

    async def post_pull_request_review(
        self,
        repo: RepoRef,
        pr_number: int,
        event: str,  # "APPROVE" | "REQUEST_CHANGES" | "COMMENT"
        body: str,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Connector aggregate
# ---------------------------------------------------------------------------


class Connector(Protocol):
    """Bundle of the three required interfaces for one host.

    A connector is the unit a customer enables — "GitHub", "Local Agent" — and
    it composes a ``ChangeNotifier``, a ``RepoCredentials`` and a
    ``RepoWriter``. Optional capabilities are discovered via ``isinstance``
    checks against the individual components.
    """

    name: str  # e.g. "github"

    @property
    def notifier(self) -> ChangeNotifier: ...

    @property
    def credentials(self) -> RepoCredentials: ...

    @property
    def writer(self) -> RepoWriter: ...
