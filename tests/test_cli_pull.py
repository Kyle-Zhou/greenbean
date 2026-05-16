"""Tests for ``_advance_to_upstream`` and the ``run --pull`` flag.

Builds a real bare remote + cloned working copy so the upstream tracking
relationship is genuine — mocking ``@{u}`` resolution would defeat the
whole point of the fast-forward safety guards.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from greenbean import cli
from greenbean.agent import GenerationResult
from greenbean.core.git import GitService

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def paired_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Bare 'origin' + working clone that tracks origin/main.

    Returns (working_copy, bare_remote). Use the helpers below to advance
    the bare via a second working clone — that's what simulates "someone
    else pushed."
    """
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", str(seed))
    _git("-C", str(seed), "config", "user.email", "test@example.com")
    _git("-C", str(seed), "config", "user.name", "test")
    (seed / "README.md").write_text("# hi\n")
    (seed / "src").mkdir()
    (seed / "src" / "app.py").write_text("def main(): pass\n")
    _git("-C", str(seed), "add", "-A")
    _git("-C", str(seed), "commit", "-q", "-m", "init")

    bare = tmp_path / "origin.git"
    _git("clone", "-q", "--bare", str(seed), str(bare))

    work = tmp_path / "work"
    _git("clone", "-q", str(bare), str(work))
    _git("-C", str(work), "config", "user.email", "test@example.com")
    _git("-C", str(work), "config", "user.name", "test")

    return work, bare


def _push_new_commit_to_bare(bare: Path, tmp_path: Path, filename: str = "extra.py") -> str:
    """Stand up a second working clone of the bare, commit, push, return new sha."""
    pusher = tmp_path / f"pusher-{filename}"
    _git("clone", "-q", str(bare), str(pusher))
    _git("-C", str(pusher), "config", "user.email", "pusher@example.com")
    _git("-C", str(pusher), "config", "user.name", "pusher")
    (pusher / filename).write_text("# pushed by upstream\n")
    _git("-C", str(pusher), "add", filename)
    _git("-C", str(pusher), "commit", "-q", "-m", f"add {filename}")
    _git("-C", str(pusher), "push", "-q", "origin", "main")
    return _head(pusher)


# ----- _advance_to_upstream pure unit tests ---------------------------------


def test_advance_returns_new_sha_when_upstream_is_ahead(
    paired_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    work, bare = paired_repo
    git = GitService()
    head_before = _head(work)

    upstream_sha = _push_new_commit_to_bare(bare, tmp_path)
    asyncio.run(git.fetch(work))

    new_sha = asyncio.run(cli._advance_to_upstream(git, work))

    assert new_sha == upstream_sha
    assert _head(work) == upstream_sha
    assert _head(work) != head_before


def test_advance_noop_when_already_at_upstream(
    paired_repo: tuple[Path, Path],
) -> None:
    work, _ = paired_repo
    git = GitService()
    asyncio.run(git.fetch(work))

    assert asyncio.run(cli._advance_to_upstream(git, work)) is None


def test_advance_skipped_when_no_upstream(tmp_path: Path) -> None:
    """A local-only repo with no tracking branch must be a silent no-op."""
    repo = tmp_path / "local-only"
    repo.mkdir()
    _git("init", "-q", "-b", "main", str(repo))
    _git("-C", str(repo), "config", "user.email", "test@example.com")
    _git("-C", str(repo), "config", "user.name", "test")
    (repo / "x.py").write_text("x\n")
    _git("-C", str(repo), "add", "x.py")
    _git("-C", str(repo), "commit", "-q", "-m", "init")

    assert asyncio.run(cli._advance_to_upstream(GitService(), repo)) is None


def test_advance_skipped_when_local_has_unique_commits(
    paired_repo: tuple[Path, Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A divergence between local and upstream must NOT trigger reset --hard."""
    work, bare = paired_repo

    # Local commit that the bare doesn't have.
    (work / "local-only.py").write_text("local work\n")
    _git("-C", str(work), "add", "local-only.py")
    _git("-C", str(work), "commit", "-q", "-m", "local commit")
    local_head = _head(work)

    # Upstream advances independently — diverged history.
    _push_new_commit_to_bare(bare, tmp_path)
    asyncio.run(GitService().fetch(work))

    caplog.set_level(logging.WARNING, logger="greenbean.cli")
    result = asyncio.run(cli._advance_to_upstream(GitService(), work))

    assert result is None
    # Local HEAD was not destroyed.
    assert _head(work) == local_head
    # User got a warning explaining why.
    assert any(
        "not on upstream" in r.message for r in caplog.records
        if r.levelno == logging.WARNING
    )


def test_advance_skipped_when_working_tree_dirty(
    paired_repo: tuple[Path, Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Uncommitted local edits must NOT be obliterated by the advance."""
    work, bare = paired_repo

    # Dirty the tree.
    (work / "README.md").write_text("# I am editing this\n")
    head_before = _head(work)

    # Upstream advances.
    _push_new_commit_to_bare(bare, tmp_path)
    asyncio.run(GitService().fetch(work))

    caplog.set_level(logging.WARNING, logger="greenbean.cli")
    result = asyncio.run(cli._advance_to_upstream(GitService(), work))

    assert result is None
    assert _head(work) == head_before
    # The dirty edit is still on disk.
    assert (work / "README.md").read_text() == "# I am editing this\n"
    assert any(
        "uncommitted changes" in r.message for r in caplog.records
        if r.levelno == logging.WARNING
    )


# ----- run --pull end-to-end ------------------------------------------------


class _FakeGenerator:
    async def generate(
        self, doc: Any, source_paths: Sequence[str]
    ) -> GenerationResult:
        return GenerationResult(
            content=f"# {doc.path_in_repo}\nstub\n",
            input_tokens=1, output_tokens=2, tool_calls=0,
        )


@pytest.fixture
def fake_generator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "Generator", lambda *a, **kw: _FakeGenerator())
    monkeypatch.setattr(cli, "DEFAULT_OUTPUT_ROOT", tmp_path / "out")


def test_run_with_pull_advances_then_runs_pipeline(
    paired_repo: tuple[Path, Path],
    tmp_path: Path,
    fake_generator: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    work, bare = paired_repo
    state = tmp_path / "state.sqlite"

    # Seed the plan and run once so last_synced_sha is set at the initial HEAD.
    cli.main(["plan", "init", str(work), "--state", str(state)])
    cli.main(["run", str(work), "--state", str(state)])

    # Upstream advances; without --pull, run is a no-op because HEAD didn't move.
    upstream_sha = _push_new_commit_to_bare(bare, tmp_path)

    caplog.clear()
    caplog.set_level(logging.INFO, logger="greenbean.cli")
    caplog.set_level(logging.INFO, logger="greenbean.sync")

    rc = cli.main(["run", str(work), "--state", str(state), "--pull"])
    assert rc == 0
    assert _head(work) == upstream_sha

    # Pipeline saw the new sha as ``to_sha``.
    pipeline_lines = [
        r.message for r in caplog.records
        if r.name == "greenbean.sync" and r.levelno == logging.INFO
    ]
    assert any("pipeline:" in line and upstream_sha[:8] in line for line in pipeline_lines)


def test_run_without_pull_does_not_advance(
    paired_repo: tuple[Path, Path],
    tmp_path: Path,
    fake_generator: None,
) -> None:
    """Confirms the default (no --pull) behaviour: HEAD stays put even when
    upstream has moved. This is the v1 contract for ``run`` — explicit opt-in."""
    work, bare = paired_repo
    state = tmp_path / "state.sqlite"

    cli.main(["plan", "init", str(work), "--state", str(state)])
    cli.main(["run", str(work), "--state", str(state)])

    head_before = _head(work)
    _push_new_commit_to_bare(bare, tmp_path)

    cli.main(["run", str(work), "--state", str(state)])

    assert _head(work) == head_before


def test_run_pull_warns_when_no_remote(
    tmp_path: Path,
    fake_generator: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """--pull against a local-only working copy must warn, not fail."""
    repo = tmp_path / "local-only"
    repo.mkdir()
    _git("init", "-q", "-b", "main", str(repo))
    _git("-C", str(repo), "config", "user.email", "test@example.com")
    _git("-C", str(repo), "config", "user.name", "test")
    (repo / "README.md").write_text("# hi\n")
    _git("-C", str(repo), "add", "-A")
    _git("-C", str(repo), "commit", "-q", "-m", "init")

    state = tmp_path / "state.sqlite"
    cli.main(["plan", "init", str(repo), "--state", str(state)])

    caplog.set_level(logging.WARNING, logger="greenbean.cli")
    rc = cli.main(["run", str(repo), "--state", str(state), "--pull"])

    assert rc == 0
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("no remote configured" in line for line in warnings)
