"""GitHub connector — v1.

GitHub-specific implementations for the connection interfaces.

The v1 CLI uses ``TokenCredentials`` directly: clone with a user-supplied PAT,
poll the working copy with ``greenbean watch``. The GitHub-App-based pieces
(``GitHubCredentials``, ``WebhookNotifier``, ``GitHubConnector``) are
**deprecated for v1** but kept in the tree as the seam for a future SaaS /
GitHub App / GitHub Actions deployment. They are not wired into any v1 code
path.

File reads, diffs, and tree listings are *not* here — they are Git operations
on a working copy and live in the Git service, which is platform-agnostic.

Generated docs are **not** published back via this connector. There is no
``RepoWriter``: the v1 publish target is a local output directory outside the
source repo (see ``greenbean.cli``).
"""

from greenbean.connectors.github.connector import GitHubConnector
from greenbean.connectors.github.credentials import (
    GitHubCredentials,
    GitHubCredentialsError,
    GitHubRepoInfo,
    GitHubRepoResolver,
)
from greenbean.connectors.github.notifier import WebhookNotifier
from greenbean.connectors.github.token_credentials import TokenCredentials

__all__ = [
    "GitHubConnector",
    "GitHubCredentials",
    "GitHubCredentialsError",
    "GitHubRepoInfo",
    "GitHubRepoResolver",
    "TokenCredentials",
    "WebhookNotifier",
]
