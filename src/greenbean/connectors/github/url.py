"""Parse GitHub repository URLs into ``(owner, name)`` pairs.

Accepts the common forms users actually paste:

- ``https://github.com/owner/name``
- ``https://github.com/owner/name.git``
- ``https://github.com/owner/name/`` (trailing slash)
- ``git@github.com:owner/name.git``
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GitHubRepoUrl:
    owner: str
    name: str

    @property
    def https_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}.git"


def parse(url: str) -> GitHubRepoUrl:
    """Parse a GitHub repo URL. Raises ``ValueError`` if it doesn't match."""
    parsed = try_parse(url)
    if parsed is None:
        raise ValueError(f"not a recognized GitHub repository URL: {url!r}")
    return parsed


def try_parse(url: str) -> GitHubRepoUrl | None:
    """Like ``parse`` but returns ``None`` on miss instead of raising."""
    if url.startswith("git@github.com:"):
        rest = url.removeprefix("git@github.com:").rstrip("/")
        rest = rest.removesuffix(".git")
        owner, sep, name = rest.partition("/")
        if not sep or not owner or not name:
            return None
        return GitHubRepoUrl(owner=owner, name=name)

    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        return None
    if p.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    parts = [seg for seg in p.path.split("/") if seg]
    if len(parts) < 2:
        return None
    owner, name = parts[0], parts[1].removesuffix(".git")
    if not owner or not name:
        return None
    return GitHubRepoUrl(owner=owner, name=name)
