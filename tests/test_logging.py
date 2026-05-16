"""Smoke tests for greenbean's progress logging.

We don't pin the exact format — just verify the major events show up so a
future format tweak doesn't quietly remove a line that operators rely on.
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
from greenbean.agent._tools import _format_tool_call, dispatch_tool
from greenbean.core.tools import DirEntry, GrepHit

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


# ----- _format_tool_call: pure unit -----------------------------------------


def test_format_tool_call_read_file_path_only() -> None:
    assert _format_tool_call("read_file", {"path": "src/x.py"}) == "read_file src/x.py"


def test_format_tool_call_read_file_with_range() -> None:
    out = _format_tool_call("read_file", {"path": "src/x.py", "start": 10, "end": 20})
    assert "src/x.py" in out
    assert "10" in out and "20" in out


def test_format_tool_call_grep_scoped() -> None:
    out = _format_tool_call("grep", {"pattern": "def foo", "path": "src/"})
    assert "grep" in out and "def foo" in out and "src/" in out


def test_format_tool_call_unknown_tool_falls_back() -> None:
    out = _format_tool_call("frobnicate", {"x": 1})
    assert "frobnicate" in out


# ----- dispatch_tool logs each call -----------------------------------------


class _Tools:
    """Minimal stub matching the parts of Tools the dispatcher uses."""

    def __init__(self) -> None:
        self.read_raises: BaseException | None = None

    async def read_file(
        self, path: str, *, start: int | None = None, end: int | None = None
    ) -> str:
        if self.read_raises is not None:
            raise self.read_raises
        return "stub"

    async def list_directory(self, path: str = ".") -> Sequence[DirEntry]:
        return ()

    async def grep(
        self,
        pattern: str,
        *,
        path: str | None = None,
        ignore_case: bool = False,
        max_results: int | None = None,
    ) -> Sequence[GrepHit]:
        return ()

    async def git_log(
        self, *, path: str | None = None, limit: int | None = None
    ) -> Sequence[Any]:
        return ()

    async def git_blame(self, path: str, line: int) -> Any:
        raise NotImplementedError


def test_dispatch_tool_logs_call_at_info(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="greenbean.agent._tools")
    asyncio.run(dispatch_tool("read_file", {"path": "x.py"}, _Tools()))  # type: ignore[arg-type]
    info_lines = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert any("read_file x.py" in line for line in info_lines)


def test_dispatch_tool_logs_error_at_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="greenbean.agent._tools")
    tools = _Tools()
    tools.read_raises = IsADirectoryError("'src' is a directory")
    asyncio.run(dispatch_tool("read_file", {"path": "src"}, tools))  # type: ignore[arg-type]
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("error calling read_file" in line for line in warnings)


# ----- run_pipeline emits pipeline / plan / per-doc lines -------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", str(root), cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "README.md").write_text("# hi\n")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def main(): pass\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)
    return root


@pytest.fixture
def fake_generator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class _Fake:
        async def generate(
            self, doc: Any, source_paths: Sequence[str]
        ) -> GenerationResult:
            return GenerationResult(content=f"# {doc.path_in_repo}\n", input_tokens=1, output_tokens=2, tool_calls=0)

    monkeypatch.setattr(cli, "Generator", lambda *a, **kw: _Fake())
    monkeypatch.setattr(cli, "DEFAULT_OUTPUT_ROOT", tmp_path / "out")


def test_run_logs_pipeline_plan_and_per_doc(
    git_repo: Path,
    tmp_path: Path,
    fake_generator: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = tmp_path / "state.sqlite"
    cli.main(["plan", "init", str(git_repo), "--state", str(state)])

    caplog.set_level(logging.INFO, logger="greenbean.sync")
    cli.main(["run", str(git_repo), "--state", str(state)])

    messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert any(line.startswith("pipeline: (initial) ->") for line in messages)
    assert any("plan refresh:" in line for line in messages)
    assert any(line.startswith("regenerating ") for line in messages)
    # First doc generation: [1/N] generating <path>
    assert any("[1/" in line and "generating" in line for line in messages)
    # And the matching done line with elapsed + tokens.
    assert any("done" in line and "tokens" in line for line in messages)


def test_run_logs_no_op_branch(
    git_repo: Path,
    tmp_path: Path,
    fake_generator: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = tmp_path / "state.sqlite"
    cli.main(["plan", "init", str(git_repo), "--state", str(state)])
    cli.main(["run", str(git_repo), "--state", str(state)])

    caplog.clear()
    caplog.set_level(logging.INFO, logger="greenbean.sync")
    cli.main(["run", str(git_repo), "--state", str(state)])

    messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert any("no docs to regenerate" in line for line in messages)


# ----- Generator logs warning on unusual stop_reason ------------------------


def test_generator_warns_on_unusual_stop_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from unittest.mock import AsyncMock

    from greenbean.agent.generator import Generator
    from greenbean.core.llm import LLMResponse, TextBlock, Usage
    from greenbean.core.planning import Document

    response = LLMResponse(
        content=[TextBlock(text="truncated...")],
        stop_reason="max_tokens",
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    client = AsyncMock()
    client.send_messages = AsyncMock(return_value=response)

    doc = Document(
        id="d", path_in_repo="README.md", doc_type="readme", scope="", spec={},
        last_planned_sha="a" * 40, current_content_hash=None,
        last_generated_at=None, generation_metadata={},
    )

    caplog.set_level(logging.WARNING, logger="greenbean.agent.generator")
    gen = Generator(_Tools(), model="test", client=client)  # type: ignore[arg-type]
    asyncio.run(gen.generate(doc, source_paths=[]))

    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("max_tokens" in line for line in warnings)
    assert any("truncated" in line.lower() for line in warnings)
