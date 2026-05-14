"""End-to-end CLI tests for ``greenbean run`` and ``greenbean watch``.

The Generator constructor is monkey-patched to return a stub that doesn't
touch any LLM. Everything else is real: real git working copy, real
SQLite state file, real planner, real filesystem writes.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from greenbean import cli
from greenbean.agent import GenerationResult
from greenbean.core.planning import Document

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
    async def generate(
        self, doc: Document, source_paths: Sequence[str]
    ) -> GenerationResult:
        return GenerationResult(
            content=f"# {doc.path_in_repo}\nstub\n",
            input_tokens=1,
            output_tokens=2,
            tool_calls=0,
        )


@pytest.fixture
def fake_generator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Replace ``cli.Generator`` so no LLM client is constructed in tests."""
    monkeypatch.setattr(cli, "Generator", lambda *a, **kw: _FakeGenerator())
    # The pipeline picks an output root under ``$HOME/.greenbean/output`` by
    # default; redirect it to a per-test tmp location so tests don't write
    # into the real $HOME.
    monkeypatch.setattr(cli, "DEFAULT_OUTPUT_ROOT", tmp_path / "out")


# ----- _parse_interval (pure) -----------------------------------------------


def test_parse_interval_accepts_seconds() -> None:
    assert cli._parse_interval("30s") == 30.0


def test_parse_interval_accepts_minutes() -> None:
    assert cli._parse_interval("5m") == 300.0


def test_parse_interval_accepts_hours() -> None:
    assert cli._parse_interval("2h") == 7200.0


def test_parse_interval_accepts_bare_integer() -> None:
    assert cli._parse_interval("45") == 45.0


def test_parse_interval_rejects_zero() -> None:
    with pytest.raises(ValueError, match="positive"):
        cli._parse_interval("0s")


def test_parse_interval_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="could not parse"):
        cli._parse_interval("five minutes")


# ----- _summary_line --------------------------------------------------------


def test_summary_line_no_op() -> None:
    from greenbean.sync import RunSummary
    line = cli._summary_line(
        RunSummary(from_sha="a" * 40, to_sha="b" * 40, no_op=True)
    )
    assert line.startswith("up to date at bbbbbbbb")


def test_summary_line_initial_run() -> None:
    from greenbean.sync import RunSummary
    line = cli._summary_line(
        RunSummary(
            from_sha=None,
            to_sha="c" * 40,
            no_op=False,
            regenerated=("README.md",),
        )
    )
    assert "(initial)" in line
    assert "README.md" in line


def test_summary_line_diff_driven() -> None:
    from greenbean.sync import RunSummary
    line = cli._summary_line(
        RunSummary(
            from_sha="a" * 40,
            to_sha="b" * 40,
            no_op=False,
            regenerated=("README.md", "docs/architecture.md"),
        )
    )
    assert "aaaaaaaa -> bbbbbbbb" in line
    assert "2 doc(s)" in line


# ----- greenbean run (end-to-end) -------------------------------------------


def test_run_requires_state_file(
    git_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "missing.sqlite"
    rc = cli.main(["run", str(git_repo), "--state", str(state)])
    assert rc == 1
    assert "state file not found" in capsys.readouterr().err


def test_run_initial_generates_all_docs(
    git_repo: Path,
    tmp_path: Path,
    fake_generator: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state.sqlite"
    cli.main(["plan", "init", str(git_repo), "--state", str(state)])
    capsys.readouterr()

    rc = cli.main(["run", str(git_repo), "--state", str(state)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "synced (initial)" in out
    assert "README.md" in out
    assert "docs/architecture.md" in out


def test_run_second_call_is_no_op(
    git_repo: Path,
    tmp_path: Path,
    fake_generator: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state.sqlite"
    cli.main(["plan", "init", str(git_repo), "--state", str(state)])
    cli.main(["run", str(git_repo), "--state", str(state)])
    capsys.readouterr()

    rc = cli.main(["run", str(git_repo), "--state", str(state)])
    assert rc == 0
    assert "up to date" in capsys.readouterr().out


def test_run_after_commit_regenerates_diff_affected(
    git_repo: Path,
    tmp_path: Path,
    fake_generator: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state.sqlite"
    cli.main(["plan", "init", str(git_repo), "--state", str(state)])
    cli.main(["run", str(git_repo), "--state", str(state)])
    capsys.readouterr()

    (git_repo / "src" / "app.py").write_text("def main(): return 1\n")
    _commit_all(git_repo, "second")

    rc = cli.main(["run", str(git_repo), "--state", str(state)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "synced" in out
    # Root-scope docs are affected by any source change.
    assert "README.md" in out


# ----- greenbean watch (one tick via patched asyncio.sleep) -----------------


def test_watch_runs_one_tick_then_exits_on_sigint(
    git_repo: Path,
    tmp_path: Path,
    fake_generator: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Patch ``asyncio.sleep`` so the watch loop's first sleep raises and
    Ctrl-C-style cleanup runs. The pipeline body must still have executed
    one tick before the interrupt."""
    state = tmp_path / "state.sqlite"
    cli.main(["plan", "init", str(git_repo), "--state", str(state)])
    capsys.readouterr()

    real_sleep = asyncio.sleep

    async def _stop_after_first(delay: float) -> None:
        # The pipeline's tick has already completed by the time the watch
        # loop reaches its sleep; abort here so the test doesn't hang.
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "sleep", _stop_after_first)

    try:
        rc = cli.main(["watch", str(git_repo), "--interval", "5s", "--state", str(state)])
    finally:
        monkeypatch.setattr(asyncio, "sleep", real_sleep)

    assert rc == 0
    out = capsys.readouterr().out
    assert "watching" in out
    assert "synced (initial)" in out
    assert "stopped" in out


def test_watch_rejects_bad_interval(
    git_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state.sqlite"
    cli.main(["plan", "init", str(git_repo), "--state", str(state)])
    capsys.readouterr()

    rc = cli.main(["watch", str(git_repo), "--interval", "bogus", "--state", str(state)])
    assert rc == 2
    assert "could not parse" in capsys.readouterr().err
