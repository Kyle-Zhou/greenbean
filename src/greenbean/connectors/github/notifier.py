"""GitHub ``ChangeNotifier`` backed by ``push`` webhooks.

**Deprecated for v1.** The v1 CLI gets change notifications from the polling
loop in ``greenbean watch`` (``git fetch`` + HEAD comparison on an interval).
This module is kept as the seam for a future SaaS / GitHub App deployment
where webhooks are the right notifier. It is not wired into any v1 code
path.

The HTTP receiver is wired in elsewhere (FastAPI app); it validates the HMAC
signature, parses the payload, and feeds verified ``ChangeEvent`` instances
into ``dispatch``. From there each registered subscriber callback runs in
turn — deduplication on ``(repo.id, after_sha)`` is the orchestrator's job,
not the notifier's.

Implementation status: scaffold only. Methods currently raise
``NotImplementedError`` until webhook plumbing is wired in (post-SaaS gate).
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
