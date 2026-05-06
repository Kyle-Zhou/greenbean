"""``GitHubConnector`` — bundles the three GitHub interface implementations."""

from __future__ import annotations

from pathlib import Path

from greenbean.connectors.github.notifier import WebhookNotifier
from greenbean.connectors.github.source import GitRepoSource
from greenbean.connectors.github.writer import GitHubWriter


class GitHubConnector:
    name = "github"

    def __init__(
        self,
        *,
        cache_root: Path,
        app_id: str,
        private_key: bytes,
        webhook_secret: str,
    ) -> None:
        self._source = GitRepoSource(cache_root=cache_root)
        self._notifier = WebhookNotifier(webhook_secret=webhook_secret)
        self._writer = GitHubWriter(app_id=app_id, private_key=private_key)

    @property
    def source(self) -> GitRepoSource:
        return self._source

    @property
    def notifier(self) -> WebhookNotifier:
        return self._notifier

    @property
    def writer(self) -> GitHubWriter:
        return self._writer
