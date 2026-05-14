"""Tests for ``greenbean.sync.run_pipeline``.

Uses a real git working copy (for ``GitService`` calls to work) and a fake
Generator that returns deterministic content. No LLM calls, no network.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from greenbean.agent import GenerationResult
from greenbean.core.git import GitService
from greenbean.core.planning import Document
from greenbean.planning import DefaultPlanner, SqliteDocStore
from greenbean.sync import RunSummary, run_pipeline

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _commit_all(repo: Path, message: str) -> str:
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


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
    _commit_all(root, "init")
    return root


class _FakeGenerator:
    """Stand-in for the real Generator: returns canned content per doc.

    Records each call so tests can assert which docs the pipeline asked to
    regenerate this tick.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate(
        self, doc: Document, source_paths: Sequence[str]
    ) -> GenerationResult:
        self.calls.append(doc.path_in_repo)
        return GenerationResult(
            content=f"# {doc.path_in_repo}\nstub content\n",
            input_tokens=1,
            output_tokens=2,
            tool_calls=0,
        )


def _seed_plan(repo: Path, state: Path, sha: str) -> None:
    """Seed a plan into ``state`` so ``run_pipeline`` has docs to work with."""
    planner = DefaultPlanner()

    async def _go() -> None:
        from greenbean.core.planning import PlannedDoc
        specs = await planner.initial_plan(repo)
        planned = []
        for spec in specs:
            sources = await planner.source_files_for(repo, spec)
            planned.append(PlannedDoc(spec=spec, source_paths=list(sources)))
        with SqliteDocStore(state) as store:
            store.replace_plan(planned, planned_sha=sha)

    asyncio.run(_go())


# ----- initial run: from_sha None -------------------------------------------


def test_run_pipeline_initial_run_regenerates_all_planned_docs(
    git_repo: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state.sqlite"
    output_root = tmp_path / "out"
    initial_sha = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    _seed_plan(git_repo, state, initial_sha)

    fake = _FakeGenerator()
    git = GitService()
    planner = DefaultPlanner()

    async def _go() -> RunSummary:
        with SqliteDocStore(state) as store:
            return await run_pipeline(
                git_repo,
                store=store,
                planner=planner,
                generator=fake,  # type: ignore[arg-type]
                git=git,
                output_root=output_root,
                model="test",
            )

    summary = asyncio.run(_go())

    # Every planned doc was regenerated (initial run = nothing prior).
    assert not summary.no_op
    assert summary.from_sha is None
    assert summary.to_sha == initial_sha
    assert set(summary.regenerated) == {"README.md", "docs/architecture.md"}
    # Output landed on disk.
    assert (output_root / "README.md").read_text().startswith("# README.md")
    assert (output_root / "docs" / "architecture.md").exists()
    # Sync state is now recorded.
    with SqliteDocStore(state) as store:
        assert store.get_last_synced_sha() == initial_sha


# ----- no-op steady state: HEAD unchanged + all docs generated ---------------


def test_run_pipeline_noops_when_head_unchanged(
    git_repo: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state.sqlite"
    output_root = tmp_path / "out"
    sha = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    _seed_plan(git_repo, state, sha)

    fake = _FakeGenerator()
    git = GitService()
    planner = DefaultPlanner()

    async def _tick() -> RunSummary:
        with SqliteDocStore(state) as store:
            return await run_pipeline(
                git_repo,
                store=store,
                planner=planner,
                generator=fake,  # type: ignore[arg-type]
                git=git,
                output_root=output_root,
                model="test",
            )

    # First tick generates everything.
    asyncio.run(_tick())
    first_calls = len(fake.calls)
    assert first_calls > 0

    # Second tick: no movement, no new docs → must be a true no-op.
    second = asyncio.run(_tick())
    assert second.no_op
    assert second.regenerated == ()
    assert len(fake.calls) == first_calls  # generator wasn't called again


# ----- HEAD moved: only diff-affected docs regenerate -----------------------


def test_run_pipeline_diff_driven_regen_after_head_moves(
    git_repo: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state.sqlite"
    output_root = tmp_path / "out"
    first_sha = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    _seed_plan(git_repo, state, first_sha)

    fake = _FakeGenerator()
    git = GitService()
    planner = DefaultPlanner()

    async def _tick() -> RunSummary:
        with SqliteDocStore(state) as store:
            return await run_pipeline(
                git_repo,
                store=store,
                planner=planner,
                generator=fake,  # type: ignore[arg-type]
                git=git,
                output_root=output_root,
                model="test",
            )

    # First tick: generate everything.
    asyncio.run(_tick())
    fake.calls.clear()

    # Advance HEAD by touching src/app.py (scope-matches every root-scope doc).
    (git_repo / "src" / "app.py").write_text("def main(): return 42\n")
    second_sha = _commit_all(git_repo, "tweak app.py")
    assert second_sha != first_sha

    summary = asyncio.run(_tick())

    assert not summary.no_op
    assert summary.from_sha == first_sha
    assert summary.to_sha == second_sha
    # README and architecture both have root scope, so they're both affected.
    assert set(summary.regenerated) == {"README.md", "docs/architecture.md"}
    assert set(fake.calls) == {"README.md", "docs/architecture.md"}


# ----- recovery: a previously ungenerated doc gets picked up next tick ------


def test_run_pipeline_picks_up_ungenerated_docs_even_without_diff(
    git_repo: Path, tmp_path: Path
) -> None:
    """If a prior tick recorded a sync but left one doc ungenerated, the
    next tick must regenerate it even when HEAD hasn't moved."""
    state = tmp_path / "state.sqlite"
    output_root = tmp_path / "out"
    sha = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    _seed_plan(git_repo, state, sha)

    # Simulate a previous successful sync where only one doc got generated.
    with SqliteDocStore(state) as store:
        store.record_generation("README.md", content_hash="h", metadata={})
        store.record_sync(sha)

    fake = _FakeGenerator()
    git = GitService()
    planner = DefaultPlanner()

    async def _tick() -> RunSummary:
        with SqliteDocStore(state) as store:
            return await run_pipeline(
                git_repo,
                store=store,
                planner=planner,
                generator=fake,  # type: ignore[arg-type]
                git=git,
                output_root=output_root,
                model="test",
            )

    summary = asyncio.run(_tick())

    # HEAD didn't move but architecture.md was still ungenerated → it gets done.
    assert not summary.no_op
    assert summary.regenerated == ("docs/architecture.md",)
    assert fake.calls == ["docs/architecture.md"]
