"""``GitHubConnector`` — bundles the three GitHub interface implementations."""

from __future__ import annotations

from greenbean.connectors.github.credentials import GitHubCredentials
from greenbean.connectors.github.notifier import WebhookNotifier
from greenbean.connectors.github.writer import GitHubWriter


class GitHubConnector:
    name = "github"

    def __init__(
        self,
        *,
        app_id: str,
        private_key: bytes,
        webhook_secret: str,
    ) -> None:
        self._notifier = WebhookNotifier(webhook_secret=webhook_secret)
        self._credentials = GitHubCredentials(app_id=app_id, private_key=private_key)
        self._writer = GitHubWriter(app_id=app_id, private_key=private_key)

    @property
    def notifier(self) -> WebhookNotifier:
        return self._notifier

    @property
    def credentials(self) -> GitHubCredentials:
        return self._credentials

    @property
    def writer(self) -> GitHubWriter:
        return self._writer
