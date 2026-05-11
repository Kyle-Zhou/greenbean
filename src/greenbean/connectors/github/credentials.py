"""GitHub ``RepoCredentials`` — GitHub App installation auth.

**Deprecated for v1.** The v1 CLI uses ``TokenCredentials`` (a single PAT) for
all GitHub interactions. This module is kept in the tree as the seam for a
future SaaS / GitHub App / GitHub Actions deployment — when that work
resumes, this is the auth path it will use. It is not wired into any v1 code
path; tests exist to keep the JWT + installation-token flow honest.

Mints short-lived installation tokens via the GitHub App API and uses them to
build authenticated clone URLs and to look up branch HEADs for the
reconciliation cron. Tokens are held in memory only; nothing is persisted to
disk.

The connector itself doesn't know how to map a core ``RepoRef`` to its
GitHub-specific identifiers (``installation_id``, ``owner``, ``name``) — that
mapping lives in Postgres. The orchestrator passes a ``repo_resolver``
callable at construction time, keeping this module decoupled from storage.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
import jwt

from greenbean.core.connectors import RepoRef


@dataclass(frozen=True, slots=True)
class GitHubRepoInfo:
    """The GitHub-specific identifiers needed to act on a repo."""

    installation_id: int
    owner: str
    name: str


GitHubRepoResolver = Callable[[RepoRef], Awaitable[GitHubRepoInfo]]


class GitHubCredentialsError(RuntimeError):
    """Raised when the GitHub API rejects an auth or lookup request."""


class GitHubCredentials:
    """Issues per-operation credentials backed by a GitHub App installation."""

    _API_ROOT = "https://api.github.com"
    _JWT_LIFETIME_SECONDS = 9 * 60  # GitHub caps app JWTs at 10 minutes
    _ACCEPT_HEADER = "application/vnd.github+json"
    _API_VERSION = "2022-11-28"

    def __init__(
        self,
        *,
        app_id: str,
        private_key: bytes,
        repo_resolver: GitHubRepoResolver,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._resolver = repo_resolver
        self._http = http if http is not None else httpx.AsyncClient(timeout=30.0)
        self._owns_http = http is None

    async def aclose(self) -> None:
        """Close the HTTP client, if we own it."""
        if self._owns_http:
            await self._http.aclose()

    async def get_clone_url(self, repo: RepoRef) -> str:
        info = await self._resolver(repo)
        token = await self._mint_installation_token(info.installation_id)
        return f"https://x-access-token:{token}@github.com/{info.owner}/{info.name}.git"

    async def get_branch_head(self, repo: RepoRef, branch: str) -> str:
        info = await self._resolver(repo)
        token = await self._mint_installation_token(info.installation_id)
        url = f"{self._API_ROOT}/repos/{info.owner}/{info.name}/branches/{branch}"
        response = await self._http.get(url, headers=self._installation_headers(token))
        if response.status_code != 200:
            raise GitHubCredentialsError(
                f"branch HEAD lookup for {info.owner}/{info.name}@{branch} failed: "
                f"{response.status_code} {response.text}"
            )
        sha = response.json().get("commit", {}).get("sha")
        if not isinstance(sha, str):
            raise GitHubCredentialsError(
                f"branch HEAD response missing commit.sha for {info.owner}/{info.name}@{branch}"
            )
        return sha

    async def _mint_installation_token(self, installation_id: int) -> str:
        app_jwt = self._sign_app_jwt()
        url = f"{self._API_ROOT}/app/installations/{installation_id}/access_tokens"
        response = await self._http.post(
            url,
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": self._ACCEPT_HEADER,
                "X-GitHub-Api-Version": self._API_VERSION,
            },
        )
        if response.status_code != 201:
            raise GitHubCredentialsError(
                f"installation token mint failed for installation {installation_id}: "
                f"{response.status_code} {response.text}"
            )
        token = response.json().get("token")
        if not isinstance(token, str):
            raise GitHubCredentialsError("installation token response missing 'token' field")
        return token

    def _sign_app_jwt(self) -> str:
        # ``iat`` is set 60 seconds in the past to tolerate clock skew between
        # us and GitHub; if our clock is slightly ahead, an iat in the future
        # would be rejected.
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + self._JWT_LIFETIME_SECONDS,
            "iss": self._app_id,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def _installation_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"token {token}",
            "Accept": self._ACCEPT_HEADER,
            "X-GitHub-Api-Version": self._API_VERSION,
        }
