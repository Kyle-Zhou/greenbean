"""GitHub ``ChangeNotifier`` backed by ``push`` webhooks.

The HTTP receiver is wired in elsewhere (FastAPI app); it validates the HMAC
signature, parses the payload, and feeds verified ``ChangeEvent`` instances
into ``dispatch``. From there each registered subscriber callback runs in
turn — deduplication on ``(repo.id, after_sha)`` is the orchestrator's job,
not the notifier's.

Implementation status: scaffold only in v1. Methods currently raise
``NotImplementedError`` until webhook plumbing is wired in.
"""

from __future__ import annotations

from greenbean.core.connectors import ChangeCallback, ChangeEvent


class WebhookNotifier:
    """Receives validated webhook events and dispatches to subscribers."""

    def __init__(self, webhook_secret: str) -> None:
        self._webhook_secret = webhook_secret
        self._subscribers: list[ChangeCallback] = []

    async def subscribe(self, callback: ChangeCallback) -> None:
        raise NotImplementedError

    async def unsubscribe(self, callback: ChangeCallback) -> None:
        raise NotImplementedError

    async def dispatch(self, event: ChangeEvent) -> None:
        """Internal entrypoint called by the HTTP webhook handler."""
        raise NotImplementedError
