"""End-to-end smoke test for ``greenbean clone``.

Stands up a local bare repo, runs ``cli.main(["clone", file://..., --dest])``
and asserts the working copy is on disk with the expected HEAD. This is the
"basic functionality" check — if it passes, the CLI plumbing works.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from greenbean import cli

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
