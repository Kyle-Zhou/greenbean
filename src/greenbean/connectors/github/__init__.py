"""GitHub connector — v1.

Implements the three core interfaces against a GitHub-hosted repo using a
GitHub App installation for auth and an on-disk working-copy cache for reads.
"""

from greenbean.connectors.github.connector import GitHubConnector
from greenbean.connectors.github.notifier import WebhookNotifier
from greenbean.connectors.github.source import GitRepoSource
from greenbean.connectors.github.writer import GitHubWriter

__all__ = [
    "GitHubConnector",
    "GitHubWriter",
    "GitRepoSource",
    "WebhookNotifier",
]
