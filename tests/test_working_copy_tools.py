"""Unit tests for ``WorkingCopyTools``.

These hit the real ``git`` and ``rg`` binaries against a tmpdir working copy
— that's the actual contract we ship, and faking it gives almost no
confidence. Tests skip cleanly if either binary is missing.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from greenbean.core.tools import (
    DirEntry,
    GrepHit,
    PathOutsideWorkingCopy,
    RipgrepNotInstalled,
)
from greenbean.tools.working_copy import WorkingCopyTools

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not on PATH")


def _run(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A working copy with a few files and one commit, for read/grep/list tests."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("hello world\nlast line\n")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
        "def greet(name):\n"
        "    return f'hello {name}'\n"
        "\n"
        "def farewell(name):\n"
        "    return f'bye {name}'\n"
    )
    (root / "src" / "util.py").write_text("HELLO = 'shout'\n")
    return root


@pytest.fixture
def git_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A real git repo with two commits; returns (path, first_sha, second_sha)."""
    root = tmp_path / "git-repo"
    root.mkdir()
    _run("git", "init", "-q", "-b", "main", str(root))
    _run("git", "-C", str(root), "config", "user.email", "alice@example.com")
    _run("git", "-C", str(root), "config", "user.name", "Alice")
    (root / "README.md").write_text("first line\n")
    _run("git", "-C", str(root), "add", "README.md")
    _run("git", "-C", str(root), "commit", "-q", "-m", "first commit")
    first = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    (root / "README.md").write_text("first line\nsecond line\n")
    _run("git", "-C", str(root), "add", "README.md")
    _run("git", "-C", str(root), "commit", "-q", "-m", "second commit\n\nbody")
    second = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root, first, second


# ----- read_file --------------------------------------------------------------


def test_read_file_returns_full_contents(repo: Path) -> None:
    tools = WorkingCopyTools(repo)
    text = asyncio.run(tools.read_file("README.md"))
    assert text == "hello world\nlast line\n"


def test_read_file_supports_line_range(repo: Path) -> None:
    tools = WorkingCopyTools(repo)
    text = asyncio.run(tools.read_file("src/app.py", start=4, end=5))
    assert text == "def farewell(name):\n    return f'bye {name}'\n"


def test_read_file_directory_raises_isadirectory(repo: Path) -> None:
    """A directory path must raise a clearly-typed error, not silently read."""
    tools = WorkingCopyTools(repo)
    with pytest.raises(IsADirectoryError, match="is a directory"):
        asyncio.run(tools.read_file("src"))


def test_read_file_missing_raises_filenotfound(repo: Path) -> None:
    tools = WorkingCopyTools(repo)
    with pytest.raises(FileNotFoundError, match="does not exist"):
        asyncio.run(tools.read_file("does/not/exist.py"))


def test_read_file_directory_error_message_suggests_list_directory(
    repo: Path,
) -> None:
    """The directory-error message points the agent at the right tool."""
    tools = WorkingCopyTools(repo)
    with pytest.raises(IsADirectoryError, match="list_directory"):
        asyncio.run(tools.read_file("src"))


# ----- list_directory --------------------------------------------------------


def test_list_directory_root(repo: Path) -> None:
    tools = WorkingCopyTools(repo)
    entries = asyncio.run(tools.list_directory("."))
    assert DirEntry(name="README.md", is_dir=False) in entries
    assert DirEntry(name="src", is_dir=True) in entries


def test_list_directory_subdir(repo: Path) -> None:
    tools = WorkingCopyTools(repo)
    entries = asyncio.run(tools.list_directory("src"))
    names = [e.name for e in entries]
    assert names == sorted(["app.py", "util.py"])


# ----- grep ------------------------------------------------------------------


@needs_rg
def test_grep_finds_matches(repo: Path) -> None:
    tools = WorkingCopyTools(repo)
    hits = asyncio.run(tools.grep("hello"))
    paths = sorted({h.path for h in hits})
    assert paths == ["README.md", "src/app.py"]
    # Every hit line text contains the substring.
    assert all("hello" in h.text for h in hits)


@needs_rg
def test_grep_ignore_case(repo: Path) -> None:
    tools = WorkingCopyTools(repo)
    case_sensitive = asyncio.run(tools.grep("HELLO"))
    case_insensitive = asyncio.run(tools.grep("HELLO", ignore_case=True))
    assert {h.path for h in case_sensitive} == {"src/util.py"}
    assert {h.path for h in case_insensitive} >= {"README.md", "src/app.py", "src/util.py"}


@needs_rg
def test_grep_scoped_to_subpath(repo: Path) -> None:
    tools = WorkingCopyTools(repo)
    hits = asyncio.run(tools.grep("hello", path="src"))
    assert {h.path for h in hits} == {"src/app.py"}


@needs_rg
def test_grep_zero_matches_returns_empty(repo: Path) -> None:
    tools = WorkingCopyTools(repo)
    hits = asyncio.run(tools.grep("definitely-not-in-any-file"))
    assert hits == ()


@needs_rg
def test_grep_returns_grep_hit_records(repo: Path) -> None:
    tools = WorkingCopyTools(repo)
    hits = asyncio.run(tools.grep("hello world"))
    assert len(hits) == 1
    hit = hits[0]
    assert isinstance(hit, GrepHit)
    assert hit.path == "README.md"
    assert hit.line == 1
    assert hit.text == "hello world"


def test_grep_raises_when_ripgrep_missing(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("greenbean.tools.working_copy.shutil.which", lambda _name: None)
    tools = WorkingCopyTools(repo)
    with pytest.raises(RipgrepNotInstalled):
        asyncio.run(tools.grep("anything"))


# ----- git_log / git_blame ---------------------------------------------------


@needs_git
def test_git_log_returns_newest_first(git_repo: tuple[Path, str, str]) -> None:
    root, first, second = git_repo
    tools = WorkingCopyTools(root)

    commits = asyncio.run(tools.git_log())

    assert [c.sha for c in commits] == [second, first]
    assert commits[0].author == "Alice"
    assert commits[0].message.startswith("second commit")
    assert "body" in commits[0].message
    assert commits[1].parents == ()
    assert commits[0].parents == (first,)


@needs_git
def test_git_log_respects_limit(git_repo: tuple[Path, str, str]) -> None:
    root, _, second = git_repo
    tools = WorkingCopyTools(root)

    commits = asyncio.run(tools.git_log(limit=1))

    assert [c.sha for c in commits] == [second]


@needs_git
def test_git_blame_returns_owning_commit(git_repo: tuple[Path, str, str]) -> None:
    root, _first, second = git_repo
    tools = WorkingCopyTools(root)

    blame = asyncio.run(tools.git_blame("README.md", 2))

    assert blame.sha == second
    assert blame.author == "Alice"
    assert blame.text == "second line"


# ----- path traversal --------------------------------------------------------


def test_rejects_absolute_paths(repo: Path) -> None:
    tools = WorkingCopyTools(repo)
    with pytest.raises(PathOutsideWorkingCopy):
        asyncio.run(tools.read_file("/etc/passwd"))


def test_rejects_dotdot_traversal(repo: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    tools = WorkingCopyTools(repo)
    with pytest.raises(PathOutsideWorkingCopy):
        asyncio.run(tools.read_file("../secret.txt"))


def test_rejects_symlink_escape(repo: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    (repo / "link").symlink_to(secret)
    tools = WorkingCopyTools(repo)
    with pytest.raises(PathOutsideWorkingCopy):
        asyncio.run(tools.read_file("link"))


@needs_rg
def test_grep_rejects_subpath_escape(repo: Path, tmp_path: Path) -> None:
    tools = WorkingCopyTools(repo)
    with pytest.raises(PathOutsideWorkingCopy):
        asyncio.run(tools.grep("hello", path="../"))
