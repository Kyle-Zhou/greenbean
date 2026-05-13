"""GitHub connector — v1.

GitHub-specific implementations for the connection interfaces.

Two ``RepoCredentials`` implementations live side by side:

- **``TokenCredentials``** — used by the v1 CLI. The user brings a PAT;
  the token is the credential.
- **``GitHubCredentials``** — GitHub App auth, **deprecated for v1** but
  kept in the tree as the SaaS auth path. The SaaS scheduler will use it to
  mint short-lived installation tokens for cloning and HEAD lookups when
  that work resumes.

There is no webhook ingress and no ``ChangeNotifier``. Both deployment modes
poll: CLI via ``greenbean watch``'s in-process timer; SaaS via a server-side
scheduler that queries Postgres.

File reads, diffs, and tree listings are *not* here — they are Git
operations on a working copy and live in the Git service.

Generated docs are **not** published back via this connector. The v1
publish target is a local output directory outside the source repo (see
``greenbean.cli``).
"""

from greenbean.connectors.github.credentials import (
    GitHubCredentials,
    GitHubCredentialsError,
    GitHubRepoInfo,
    GitHubRepoResolver,
)
from greenbean.connectors.github.token_credentials import TokenCredentials

__all__ = [
    "GitHubCredentials",
    "GitHubCredentialsError",
    "GitHubRepoInfo",
    "GitHubRepoResolver",
    "TokenCredentials",
]
