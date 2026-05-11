"""``GitHubConnector`` — composition point for the deferred SaaS deployment.

Bundles the GitHub-App ``RepoCredentials`` and the webhook ``ChangeNotifier``.
**Not** used by the v1 CLI — the CLI uses ``TokenCredentials`` directly and
polls via ``greenbean watch``. This aggregate is kept so the GitHub App seam
is documented and ready when SaaS work resumes (see Architecture.md §10).

Lifecycle note: callers should invoke ``aclose`` when done so any owned
network resources (for example an internally created ``httpx.AsyncClient``)
are closed deterministically.
"""

from __future__ import annotations

import httpx

from greenbean.connectors.github.credentials import (
    GitHubCredentials,
    GitHubRepoResolver,
)
from greenbean.connectors.github.notifier import WebhookNotifier


class GitHubConnector:
    name = "github"

    def __init__(
        self,
        *,
        app_id: str,
        private_key: bytes,
        webhook_secret: str,
        repo_resolver: GitHubRepoResolver,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._notifier = WebhookNotifier(webhook_secret=webhook_secret)
        self._credentials = GitHubCredentials(
            app_id=app_id,
            private_key=private_key,
            repo_resolver=repo_resolver,
            http=http,
        )

    async def aclose(self) -> None:
        await self._credentials.aclose()

    @property
    def notifier(self) -> WebhookNotifier:
        return self._notifier

    @property
    def credentials(self) -> GitHubCredentials:
        return self._credentials
