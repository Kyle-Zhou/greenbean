"""End-to-end smoke test for ``greenbean clone``.

Stands up a local bare repo, runs ``cli.main(["clone", file://..., --dest])``
and asserts the working copy is on disk with the expected HEAD. This is the
"basic functionality" check — if it passes, the CLI plumbing works.

Also covers ``_output_root_for`` — the helper that decides where generated
docs land — since it's the seam between "what the working copy is" and
"where docs end up."
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from greenbean import cli
from greenbean.core.git import GitService

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _run(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def bare_remote(tmp_path: Path) -> tuple[Path, str]:
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


def test_cli_clone_against_local_bare_repo(
    bare_remote: tuple[Path, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bare, expected_sha = bare_remote
    dest = tmp_path / "checkout"

    rc = cli.main(["clone", f"file://{bare}", "--dest", str(dest), "--depth", "0"])

    assert rc == 0
    assert (dest / "README.md").read_text() == "hello\n"
    assert (dest / ".git").is_dir()

    captured = capsys.readouterr()
    assert "cloned" in captured.out
    assert f"HEAD: {expected_sha}" in captured.out


def test_cli_clone_refuses_existing_destination(
    bare_remote: tuple[Path, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bare, _ = bare_remote
    dest = tmp_path / "occupied"
    dest.mkdir()

    rc = cli.main(["clone", f"file://{bare}", "--dest", str(dest)])

    assert rc == 2
    assert "already exists" in capsys.readouterr().err


# ----- _output_root_for ------------------------------------------------------


def _init_repo(path: Path, *, remote: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run("git", "init", "-q", "-b", "main", str(path))
    _run("git", "-C", str(path), "config", "user.email", "test@example.com")
    _run("git", "-C", str(path), "config", "user.name", "test")
    (path / "README.md").write_text("hi\n")
    _run("git", "-C", str(path), "add", "README.md")
    _run("git", "-C", str(path), "commit", "-q", "-m", "init")
    if remote is not None:
        _run("git", "-C", str(path), "remote", "add", "origin", remote)


def test_output_root_uses_github_namespace_for_github_remote(tmp_path: Path) -> None:
    repo = tmp_path / "weirdname"
    _init_repo(repo, remote="https://github.com/acme/widgets.git")

    root = asyncio.run(cli._output_root_for(repo, GitService()))

    assert root == cli.DEFAULT_OUTPUT_ROOT / "github.com" / "acme" / "widgets"


def test_output_root_handles_credentialed_github_remote(tmp_path: Path) -> None:
    """`git remote get-url` returns the token-embedded URL after a PAT clone.

    Must still resolve to the github.com namespace, not _local fallback.
    """
    repo = tmp_path / "anyname"
    _init_repo(
        repo,
        remote="https://x-access-token:ghp_FAKE_TOKEN@github.com/acme/widgets.git",
    )

    root = asyncio.run(cli._output_root_for(repo, GitService()))

    assert root == cli.DEFAULT_OUTPUT_ROOT / "github.com" / "acme" / "widgets"


def test_output_root_falls_back_to_local_namespace_without_remote(tmp_path: Path) -> None:
    repo = tmp_path / "myproject"
    _init_repo(repo)  # no remote

    root = asyncio.run(cli._output_root_for(repo, GitService()))

    assert root.parent == cli.DEFAULT_OUTPUT_ROOT / "_local"
    assert root.name.startswith("myproject-")


def test_output_root_local_namespaces_collide_only_when_paths_match(
    tmp_path: Path,
) -> None:
    """Two repos with the same basename at different paths must not collide."""
    a = tmp_path / "a" / "repo"
    b = tmp_path / "b" / "repo"
    _init_repo(a)
    _init_repo(b)

    root_a = asyncio.run(cli._output_root_for(a, GitService()))
    root_b = asyncio.run(cli._output_root_for(b, GitService()))

    assert root_a != root_b
    # Same basename, different hash suffix.
    assert root_a.name.startswith("repo-")
    assert root_b.name.startswith("repo-")


def test_output_root_local_namespace_is_stable(tmp_path: Path) -> None:
    """Calling twice for the same path must return the same output root."""
    repo = tmp_path / "stable"
    _init_repo(repo)

    first = asyncio.run(cli._output_root_for(repo, GitService()))
    second = asyncio.run(cli._output_root_for(repo, GitService()))

    assert first == second


# ----- _safe_output_path -----------------------------------------------------


def test_safe_output_path_accepts_plain_relative_path(tmp_path: Path) -> None:
    out = cli._safe_output_path(tmp_path, "README.md")
    assert out == (tmp_path / "README.md").resolve()


def test_safe_output_path_accepts_nested_relative_path(tmp_path: Path) -> None:
    out = cli._safe_output_path(tmp_path, "docs/architecture.md")
    assert out == (tmp_path / "docs" / "architecture.md").resolve()


def test_safe_output_path_rejects_absolute(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be relative"):
        cli._safe_output_path(tmp_path, "/etc/passwd")


def test_safe_output_path_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes the output root"):
        cli._safe_output_path(tmp_path, "../../etc/passwd")


def test_safe_output_path_rejects_mid_path_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes the output root"):
        cli._safe_output_path(tmp_path, "docs/../../escape.md")


# ----- _atomic_write_text ----------------------------------------------------


def test_atomic_write_creates_file_with_content(tmp_path: Path) -> None:
    target = tmp_path / "doc.md"
    cli._atomic_write_text(target, "# hello\n")
    assert target.read_text() == "# hello\n"


def test_atomic_write_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "nested" / "doc.md"
    cli._atomic_write_text(target, "x")
    assert target.read_text() == "x"


def test_atomic_write_leaves_no_temp_after_success(tmp_path: Path) -> None:
    target = tmp_path / "doc.md"
    cli._atomic_write_text(target, "x")
    siblings = list(target.parent.iterdir())
    assert siblings == [target], f"unexpected leftover files: {siblings}"


def test_atomic_write_replaces_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "doc.md"
    target.write_text("old")
    cli._atomic_write_text(target, "new")
    assert target.read_text() == "new"


def test_atomic_write_cleans_up_temp_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the rename fails, the .tmp file must not leak."""
    target = tmp_path / "doc.md"

    def _boom(src: str, dst: str) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(cli.os, "replace", _boom)

    with pytest.raises(OSError, match="simulated rename failure"):
        cli._atomic_write_text(target, "x")

    siblings = list(tmp_path.iterdir())
    assert siblings == [], f"temp file leaked: {siblings}"
