"""GitHub ``RepoCredentials`` — GitHub App installation auth.

Mints short-lived installation tokens via the GitHub App API and uses them to
build authenticated clone URLs and to look up branch HEADs for the
reconciliation cron. Tokens are held in memory only; nothing is persisted to
disk.
"""

from __future__ import annotations

from greenbean.core.connectors import RepoRef


class GitHubCredentials:
    """Issues per-operation credentials backed by a GitHub App installation."""

    def __init__(self, app_id: str, private_key: bytes) -> None:
        self._app_id = app_id
        self._private_key = private_key

    async def get_clone_url(self, repo: RepoRef) -> str:
        """Return ``https://x-access-token:<token>@github.com/{owner}/{name}.git``.

        The token is an installation token scoped to ``repo`` and short-lived;
        callers must not persist it.
        """
        raise NotImplementedError

    async def get_branch_head(self, repo: RepoRef, branch: str) -> str:
        """Look up ``branch``'s HEAD SHA via ``GET /repos/{owner}/{repo}/branches/{branch}``."""
        raise NotImplementedError
