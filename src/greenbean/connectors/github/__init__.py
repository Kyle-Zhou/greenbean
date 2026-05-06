"""GitHub connector — v1.

Implements the three core interfaces against a GitHub-hosted repo using a
GitHub App installation for auth. File reads, diffs, and tree listings are
*not* here — they are Git operations on a working copy and live in the Git
service, which is platform-agnostic.
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
from greenbean.connectors.github.writer import GitHubWriter

__all__ = [
    "GitHubConnector",
    "GitHubCredentials",
    "GitHubCredentialsError",
    "GitHubRepoInfo",
    "GitHubRepoResolver",
    "GitHubWriter",
    "TokenCredentials",
    "WebhookNotifier",
]
