"""Tests for the GitHub URL parser."""

from __future__ import annotations

import pytest

from greenbean.connectors.github import url as github_url


@pytest.mark.parametrize(
    "raw,owner,name",
    [
        ("https://github.com/acme/widgets", "acme", "widgets"),
        ("https://github.com/acme/widgets.git", "acme", "widgets"),
        ("https://github.com/acme/widgets/", "acme", "widgets"),
        ("https://www.github.com/acme/widgets", "acme", "widgets"),
        ("http://github.com/acme/widgets", "acme", "widgets"),
        ("git@github.com:acme/widgets.git", "acme", "widgets"),
        ("git@github.com:acme/widgets", "acme", "widgets"),
        # Credentialed HTTPS: what `git remote get-url` returns after a PAT clone.
        # Must parse so generated docs land under github.com/<owner>/<name>/...
        # rather than the _local/... fallback.
        ("https://x-access-token:TOKEN@github.com/acme/widgets.git", "acme", "widgets"),
        ("https://user:pass@github.com/acme/widgets", "acme", "widgets"),
    ],
)
def test_parse_accepts_common_forms(raw: str, owner: str, name: str) -> None:
    parsed = github_url.parse(raw)
    assert parsed.owner == owner
    assert parsed.name == name
    assert parsed.https_url == f"https://github.com/{owner}/{name}.git"


@pytest.mark.parametrize(
    "raw",
    [
        "https://gitlab.com/acme/widgets",
        "https://github.com/acme",  # missing repo
        "file:///tmp/repo",
        "not a url",
        "",
    ],
)
def test_parse_rejects_non_github_urls(raw: str) -> None:
    assert github_url.try_parse(raw) is None
    with pytest.raises(ValueError):
        github_url.parse(raw)
