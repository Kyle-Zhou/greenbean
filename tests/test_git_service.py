"""Tests for the ``GitService`` shell-out wrapper.

These hit a real ``git`` binary. We create a bare repo in a tmpdir as the
"remote", seed it with a commit, then point ``GitService.clone`` at it.
That covers the realistic clone path without the network.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from greenbean.core.git import GitError, GitService

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _run(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def remote_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a bare "remote" repo with one commit; return (path, head_sha)."""
    work = tmp_path / "work"
    work.mkdir()
    _run("git", "init", "-q", "-b", "main", str(work))
    _run("git", "-C", str(work), "config", "user.email", "test@example.com")
    _run("git", "-C", str(work), "config", "user.name", "test")
    (work / "README.md").write_text("hello\n")
    _run("git", "-C", str(work), "add", "README.md")
    _run("git", "-C", str(work), "commit", "-q", "-m", "init")

    bare = tmp_path / "remote.git"
    _run("git", "clone", "-q", "--bare", str(work), str(bare))
    sha = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return bare, sha


def test_clone_pulls_repo_and_current_sha_matches(
    remote_repo: tuple[Path, str], tmp_path: Path
) -> None:
    bare, expected_sha = remote_repo
    dest = tmp_path / "checkout"

    git = GitService()

    async def run() -> str:
        await git.clone(str(bare), dest, depth=1, branch="main")
        return await git.current_sha(dest)

    head = asyncio.run(run())

    assert (dest / "README.md").read_text() == "hello\n"
    assert head == expected_sha


def test_clone_failure_raises_git_error(tmp_path: Path) -> None:
    git = GitService()

    async def run() -> None:
        await git.clone(
            str(tmp_path / "does-not-exist"),
            tmp_path / "dest",
            depth=1,
        )

    with pytest.raises(GitError):
        asyncio.run(run())


def test_fetch_and_reset_against_advancing_remote(
    remote_repo: tuple[Path, str], tmp_path: Path
) -> None:
    """After we clone, advance the bare repo, fetch, and reset to the new SHA."""
    bare, first_sha = remote_repo
    dest = tmp_path / "checkout"
    git = GitService()

    asyncio.run(git.clone(str(bare), dest, depth=None, branch="main"))
    assert asyncio.run(git.current_sha(dest)) == first_sha

    # Advance the remote by pushing a new commit from a fresh working clone.
    pusher = tmp_path / "pusher"
    _run("git", "clone", "-q", str(bare), str(pusher))
    _run("git", "-C", str(pusher), "config", "user.email", "test@example.com")
    _run("git", "-C", str(pusher), "config", "user.name", "test")
    (pusher / "second.txt").write_text("two\n")
    _run("git", "-C", str(pusher), "add", "second.txt")
    _run("git", "-C", str(pusher), "commit", "-q", "-m", "second")
    _run("git", "-C", str(pusher), "push", "-q", "origin", "main")
    new_sha = subprocess.run(
        ["git", "-C", str(pusher), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    async def fetch_and_reset() -> str:
        await git.fetch(dest)
        await git.reset_hard(dest, new_sha)
        return await git.current_sha(dest)

    assert asyncio.run(fetch_and_reset()) == new_sha
    assert (dest / "second.txt").read_text() == "two\n"
