"""Tests for the run pipeline (``pipeline.run_once``).

Exercises the full local loop against a real git repo with a stub generator —
no LLM. Covers the idempotency cursor (``last_synced_sha``), first-run full
generation vs. incremental diff-driven generation, dry-run isolation, and the
``--full`` override.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from greenbean import publish
from greenbean.agent.generator import GenerationResult
from greenbean.core.git import GitService
from greenbean.core.planning import Document
from greenbean.pipeline import RunResult, run_once
from greenbean.planning import DefaultPlanner

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _commit(repo: Path, message: str) -> None:
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)


class StubGenerator:
    """Records which docs it was asked to generate; returns canned content."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate(
        self, doc: Document, source_paths: Sequence[str]
    ) -> GenerationResult:
        self.calls.append(doc.path_in_repo)
        return GenerationResult(
            content=f"# {doc.path_in_repo}\n",
            input_tokens=1,
            output_tokens=2,
            tool_calls=0,
        )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git repo with two modules, output dir redirected into tmp."""
    monkeypatch.setattr(publish, "DEFAULT_OUTPUT_ROOT", tmp_path / "output")

    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", str(root), cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "test", cwd=root)

    (root / "README.md").write_text("# repo\n")
    for pkg in ("pkg", "util"):
        d = root / "src" / pkg
        d.mkdir(parents=True)
        for name in ("a", "b", "c"):
            (d / f"{name}.py").write_text(f"# {pkg}.{name}\n")
    _commit(root, "init")
    return root


def _run(
    repo: Path,
    state: Path,
    generator: StubGenerator,
    *,
    dry_run: bool = False,
    force_full: bool = False,
) -> RunResult:
    return asyncio.run(
        run_once(
            repo,
            state,
            git=GitService(),
            planner=DefaultPlanner(),
            make_generator=lambda _rp: generator,
            dry_run=dry_run,
            force_full=force_full,
            log=lambda _m: None,
        )
    )


def test_first_run_generates_full_plan(repo: Path, tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite"
    gen = StubGenerator()

    result = _run(repo, state, gen)

    assert result.full is True
    assert result.skipped is False
    # README, architecture, and a README per module.
    assert set(gen.calls) == {
        "README.md",
        "docs/architecture.md",
        "src/pkg/README.md",
        "src/util/README.md",
    }
    # Files actually landed in the output dir.
    for path in gen.calls:
        assert (result.output_root / path).read_text() == f"# {path}\n"


def test_second_run_unchanged_is_skipped(repo: Path, tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite"
    _run(repo, state, StubGenerator())

    gen2 = StubGenerator()
    result = _run(repo, state, gen2)

    assert result.skipped is True
    assert gen2.calls == []  # generator never invoked when HEAD hasn't moved


def test_incremental_run_regenerates_only_affected_docs(
    repo: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state.sqlite"
    _run(repo, state, StubGenerator())  # establish baseline

    (repo / "src" / "pkg" / "a.py").write_text("# changed\n")
    _commit(repo, "touch pkg")

    gen = StubGenerator()
    result = _run(repo, state, gen)

    assert result.skipped is False
    assert result.full is False
    # pkg's README + the repo-wide docs are affected; util's README is not.
    assert "src/pkg/README.md" in gen.calls
    assert "src/util/README.md" not in gen.calls


def test_run_advances_cursor_even_with_no_affected_docs(
    repo: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state.sqlite"
    _run(repo, state, StubGenerator())

    # A commit that changes nothing the plan maps to (a non-source file with
    # no doc scope covering it beyond the always-on root docs would still
    # match; use an empty commit to isolate the cursor-advance behavior).
    _git("commit", "-q", "--allow-empty", "-m", "empty", cwd=repo)

    gen = StubGenerator()
    result = _run(repo, state, gen)
    assert result.skipped is False
    assert gen.calls == []  # nothing changed -> nothing affected

    # Cursor advanced: a follow-up run is now a no-op.
    follow_up = _run(repo, state, StubGenerator())
    assert follow_up.skipped is True


def test_dry_run_writes_nothing_and_leaves_cursor(repo: Path, tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite"
    gen = StubGenerator()

    result = _run(repo, state, gen, dry_run=True)

    assert gen.calls  # generation happened
    assert not result.output_root.exists()  # but nothing was written

    # Cursor untouched, so a real run still does the full pass.
    gen2 = StubGenerator()
    follow_up = _run(repo, state, gen2)
    assert follow_up.skipped is False
    assert follow_up.full is True


def test_force_full_regenerates_despite_unchanged_head(
    repo: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state.sqlite"
    _run(repo, state, StubGenerator())

    gen = StubGenerator()
    result = _run(repo, state, gen, force_full=True)

    assert result.skipped is False
    assert result.full is True
    assert "src/util/README.md" in gen.calls  # everything, not just a diff
