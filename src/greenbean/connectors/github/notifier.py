"""GitHub ``ChangeNotifier`` backed by ``push`` webhooks.

The HTTP receiver is wired in elsewhere (FastAPI app) and feeds verified
events into this notifier, which fans them out to subscribed callbacks.
"""

from __future__ import annotations

from greenbean.core.connectors import ChangeCallback, ChangeEvent, RepoRef


class WebhookNotifier:
    """Receives validated webhook events and dispatches to subscribers."""

    def __init__(self, webhook_secret: str) -> None:
        self._webhook_secret = webhook_secret
        self._subscribers: dict[str, ChangeCallback] = {}

    async def subscribe(self, repo: RepoRef, callback: ChangeCallback) -> None:
        raise NotImplementedError

    async def unsubscribe(self, repo: RepoRef) -> None:
        raise NotImplementedError

    async def dispatch(self, event: ChangeEvent) -> None:
        """Internal entrypoint called by the HTTP webhook handler."""
        raise NotImplementedError
