"""Connection-layer interfaces.

These are the contracts every connector implements. The core orchestrator
talks to platforms (GitHub today, GitLab / local agent tomorrow) only through
these protocols.

Capabilities that not every host can support — pull requests, check runs,
commit comments — live in separate ``Supports*`` protocols. Core code
feature-detects with ``isinstance`` rather than assuming.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
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
class Commit:
    sha: str
    author: str
    message: str
    timestamp: datetime
    parents: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FileChange:
    """A single path's diff between two refs."""

    path: str
    status: str  # "added" | "modified" | "removed" | "renamed"
    old_blob: bytes | None
    new_blob: bytes | None
    old_path: str | None = None  # set when ``status == "renamed"``


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """Emitted by a ``ChangeNotifier`` when a repo's tracked ref advances."""

    repo: RepoRef
    ref: str
    from_sha: str | None  # ``None`` for the very first event on a repo
    to_sha: str
    triggered_by: str  # e.g. "webhook", "reconciliation", "manual"


@dataclass(frozen=True, slots=True)
class FileToWrite:
    path: str
    content: bytes


# ---------------------------------------------------------------------------
# Core interfaces
# ---------------------------------------------------------------------------


class RepoSource(Protocol):
    """Read-only access to a repository's contents and history."""

    async def get_file(self, repo: RepoRef, path: str, ref: str) -> bytes: ...

    async def list_tree(self, repo: RepoRef, ref: str) -> Sequence[str]: ...

    async def diff(
        self,
        repo: RepoRef,
        from_sha: str,
        to_sha: str,
    ) -> Sequence[FileChange]: ...

    async def get_commit(self, repo: RepoRef, sha: str) -> Commit: ...

    async def get_default_branch(self, repo: RepoRef) -> str: ...


ChangeCallback = Callable[[ChangeEvent], Awaitable[None]]


class ChangeNotifier(Protocol):
    """Push-based source of repo change events.

    Implementations must deliver events idempotently keyed on
    ``(repo.id, to_sha)`` — webhooks retry, reconciliation jobs may re-emit.
    Deduplication is the orchestrator's job, but notifiers should not invent
    spurious change events.
    """

    async def subscribe(self, repo: RepoRef, callback: ChangeCallback) -> None: ...

    async def unsubscribe(self, repo: RepoRef) -> None: ...


class RepoWriter(Protocol):
    """Write access for publishing generated docs back to the customer's repo.

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


# ---------------------------------------------------------------------------
# Connector aggregate
# ---------------------------------------------------------------------------


class Connector(Protocol):
    """Bundle of the three required interfaces for one host.

    A connector is the unit a customer enables — "GitHub", "Local Agent" — and
    it composes a ``RepoSource``, a ``ChangeNotifier`` and a ``RepoWriter``.
    Optional capabilities are discovered via ``isinstance`` checks against the
    individual components.
    """

    name: str  # e.g. "github"

    @property
    def source(self) -> RepoSource: ...

    @property
    def notifier(self) -> ChangeNotifier: ...

    @property
    def writer(self) -> RepoWriter: ...
