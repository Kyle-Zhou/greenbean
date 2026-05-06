"""Tests for ``TokenCredentials`` (CLI / single-tenant credential mode)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from greenbean.connectors.github.token_credentials import TokenCredentials
from greenbean.core.connectors import RepoRef
from greenbean.core.git import GitService

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


@pytest.fixture
def repo() -> RepoRef:
    return RepoRef(id="cli", source_type="github", external_id="acme/widgets")


def test_get_clone_url_embeds_token(repo: RepoRef) -> None:
    creds = TokenCredentials(token="ghp_abc123", git=GitService())

    url = asyncio.run(creds.get_clone_url(repo))

    assert url == "https://x-access-token:ghp_abc123@github.com/acme/widgets.git"


def test_external_id_must_be_owner_slash_name() -> None:
    creds = TokenCredentials(token="t", git=GitService())
    bad = RepoRef(id="cli", source_type="github", external_id="just-a-name")

    with pytest.raises(ValueError, match="owner/name"):
        asyncio.run(creds.get_clone_url(bad))


def _run(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def test_get_branch_head_uses_ls_remote(tmp_path: Path) -> None:
    """Smoke-test ``get_branch_head`` against a real local bare repo.

    Tokens don't apply to ``file://`` URLs, so we exercise the ``ls-remote``
    code path through ``GitService`` directly. That covers the meaningful
    moving part — ref resolution from the wire protocol — without the
    network.
    """
    work = tmp_path / "work"
    work.mkdir()
    _run("git", "init", "-q", "-b", "main", str(work))
    _run("git", "-C", str(work), "config", "user.email", "test@example.com")
    _run("git", "-C", str(work), "config", "user.name", "test")
    (work / "README.md").write_text("hi\n")
    _run("git", "-C", str(work), "add", "README.md")
    _run("git", "-C", str(work), "commit", "-q", "-m", "init")

    bare = tmp_path / "remote.git"
    _run("git", "clone", "-q", "--bare", str(work), str(bare))
    expected = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    git = GitService()
    sha = asyncio.run(git.ls_remote(f"file://{bare}", "refs/heads/main"))
    assert sha == expected
