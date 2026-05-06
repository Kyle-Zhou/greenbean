"""Token-based GitHub credentials for the CLI / single-tenant mode.

Used when the operator (or a developer running greenbean against their own
repo) brings a personal access token. No GitHub App, no JWT, no
installation-token mint — the token *is* the credential.

The complementary multi-tenant flow (GitHub App, per-customer installation)
lives in ``credentials.py``. Both implementations satisfy the same
``RepoCredentials`` protocol; the orchestrator picks one at construction.

Convention: ``RepoRef.external_id`` is interpreted as ``"owner/name"`` for
this mode. The CLI sets that when it builds an in-memory ``RepoRef``.
"""

from __future__ import annotations

from greenbean.core.connectors import RepoRef
from greenbean.core.git import GitService


class TokenCredentials:
    """``RepoCredentials`` impl backed by a single PAT."""

    def __init__(self, *, token: str, git: GitService) -> None:
        self._token = token
        self._git = git

    async def get_clone_url(self, repo: RepoRef) -> str:
        owner, name = self._owner_name(repo)
        return f"https://x-access-token:{self._token}@github.com/{owner}/{name}.git"

    async def get_branch_head(self, repo: RepoRef, branch: str) -> str:
        url = await self.get_clone_url(repo)
        return await self._git.ls_remote(url, f"refs/heads/{branch}")

    @staticmethod
    def _owner_name(repo: RepoRef) -> tuple[str, str]:
        owner, sep, name = repo.external_id.partition("/")
        if not sep or not owner or not name:
            raise ValueError(
                "TokenCredentials expects RepoRef.external_id of the form "
                f"'owner/name'; got {repo.external_id!r}"
            )
        return owner, name
