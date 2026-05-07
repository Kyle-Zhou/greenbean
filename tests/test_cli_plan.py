"""End-to-end CLI tests for `greenbean plan init` and `greenbean plan list`.

Uses a real git repo (git init) so current_sha works.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from greenbean.cli import main


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", str(root), cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)

    (root / "README.md").write_text("# hello\n")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def main(): pass\n")

    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)
    return root


# ----- plan init -------------------------------------------------------------


def test_plan_init_succeeds(git_repo: Path, tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite"
    rc = main(["plan", "init", str(git_repo), "--state", str(state)])
    assert rc == 0
    assert state.exists()


def test_plan_init_prints_summary(
    git_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state.sqlite"
    main(["plan", "init", str(git_repo), "--state", str(state)])
    out = capsys.readouterr().out
    assert "added" in out
    assert "updated" in out
    assert "pruned" in out


def test_plan_init_idempotent(
    git_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state.sqlite"
    main(["plan", "init", str(git_repo), "--state", str(state)])
    capsys.readouterr()

    main(["plan", "init", str(git_repo), "--state", str(state)])
    out = capsys.readouterr().out
    assert "added 0" in out


def test_plan_init_creates_state_dir(git_repo: Path, tmp_path: Path) -> None:
    state = tmp_path / "nested" / "dir" / "state.sqlite"
    rc = main(["plan", "init", str(git_repo), "--state", str(state)])
    assert rc == 0
    assert state.exists()


def test_plan_init_fails_on_non_git_dir(tmp_path: Path) -> None:
    not_git = tmp_path / "notgit"
    not_git.mkdir()
    state = tmp_path / "state.sqlite"
    rc = main(["plan", "init", str(not_git), "--state", str(state)])
    assert rc != 0


# ----- plan list -------------------------------------------------------------


def test_plan_list_shows_docs(
    git_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state.sqlite"
    main(["plan", "init", str(git_repo), "--state", str(state)])
    capsys.readouterr()

    rc = main(["plan", "list", str(git_repo), "--state", str(state)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "README.md" in out
    assert "docs/architecture.md" in out


def test_plan_list_shows_expected_fields(
    git_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state.sqlite"
    main(["plan", "init", str(git_repo), "--state", str(state)])
    capsys.readouterr()

    main(["plan", "list", str(git_repo), "--state", str(state)])
    out = capsys.readouterr().out
    assert "type=" in out
    assert "scope=" in out
    assert "sources=" in out
    assert "generated=no" in out
    assert "sha=" in out


def test_plan_list_fails_without_state(git_repo: Path, tmp_path: Path) -> None:
    state = tmp_path / "nonexistent.sqlite"
    rc = main(["plan", "list", str(git_repo), "--state", str(state)])
    assert rc != 0


# ----- init then list roundtrip ----------------------------------------------


def test_plan_init_then_list_always_includes_readme_and_arch(
    git_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state.sqlite"
    main(["plan", "init", str(git_repo), "--state", str(state)])
    capsys.readouterr()

    main(["plan", "list", str(git_repo), "--state", str(state)])
    out = capsys.readouterr().out
    lines = out.splitlines()
    paths = [line.split("  ")[0] for line in lines if line.strip()]
    assert "README.md" in paths
    assert "docs/architecture.md" in paths
