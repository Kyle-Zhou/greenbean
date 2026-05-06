"""GitHub ``RepoSource`` backed by an on-disk working-copy cache.

This implementation operates on a local clone managed by the sync
orchestrator. Because all reads go through the local checkout, the same class
will be reused by a future local-agent connector — only the clone management
differs.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from greenbean.core.connectors import Commit, FileChange, RepoRef


class GitRepoSource:
    """Read-only view over a working-copy clone."""

    def __init__(self, cache_root: Path) -> None:
        self._cache_root = cache_root

    def _checkout_for(self, repo: RepoRef) -> Path:
        return self._cache_root / repo.id

    async def get_file(self, repo: RepoRef, path: str, ref: str) -> bytes:
        raise NotImplementedError

    async def list_tree(self, repo: RepoRef, ref: str) -> Sequence[str]:
        raise NotImplementedError

    async def diff(
        self,
        repo: RepoRef,
        from_sha: str,
        to_sha: str,
    ) -> Sequence[FileChange]:
        raise NotImplementedError

    async def get_commit(self, repo: RepoRef, sha: str) -> Commit:
        raise NotImplementedError

    async def get_default_branch(self, repo: RepoRef) -> str:
        raise NotImplementedError
