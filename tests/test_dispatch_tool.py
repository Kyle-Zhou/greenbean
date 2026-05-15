"""Tests for ``greenbean.agent._tools.dispatch_tool``.

The dispatcher's job is to keep agent tool calls insulated from exceptions —
any failure must surface as a ``tool_result`` string the model can read on
its next turn, not a propagating exception that kills the generation loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from greenbean.agent._tools import dispatch_tool
from greenbean.core.tools import DirEntry, GrepHit, PathOutsideWorkingCopy


class _Tools:
    """A tools stub whose methods can be told to raise or return canned data."""

    def __init__(self) -> None:
        self.read_file_raises: BaseException | None = None
        self.read_file_returns: str = "ok"
        self.list_directory_raises: BaseException | None = None
        self.grep_raises: BaseException | None = None
        self.git_log_raises: BaseException | None = None

    async def read_file(
        self, path: str, *, start: int | None = None, end: int | None = None
    ) -> str:
        if self.read_file_raises is not None:
            raise self.read_file_raises
        return self.read_file_returns

    async def list_directory(self, path: str = ".") -> Sequence[DirEntry]:
        if self.list_directory_raises is not None:
            raise self.list_directory_raises
        return (DirEntry(name="x.py", is_dir=False),)

    async def grep(
        self,
        pattern: str,
        *,
        path: str | None = None,
        ignore_case: bool = False,
        max_results: int | None = None,
    ) -> Sequence[GrepHit]:
        if self.grep_raises is not None:
            raise self.grep_raises
        return ()

    async def git_log(
        self, *, path: str | None = None, limit: int | None = None
    ) -> Sequence[Any]:
        if self.git_log_raises is not None:
            raise self.git_log_raises
        return ()

    async def git_blame(self, path: str, line: int) -> Any:
        raise NotImplementedError


# ----- happy path: errors don't change normal-call behaviour ------------------


def test_dispatch_read_file_normal_returns_content() -> None:
    tools = _Tools()
    tools.read_file_returns = "file content"
    result = asyncio.run(dispatch_tool("read_file", {"path": "x.py"}, tools))  # type: ignore[arg-type]
    assert result == "file content"


# ----- tool raises: error returned as string, not propagated ------------------


def test_dispatch_read_file_directory_returns_error_string() -> None:
    tools = _Tools()
    tools.read_file_raises = IsADirectoryError(
        "'packages/types' is a directory, not a file. "
        "Use list_directory to inspect its contents."
    )
    result = asyncio.run(dispatch_tool("read_file", {"path": "packages/types"}, tools))  # type: ignore[arg-type]
    assert "error calling read_file" in result
    assert "is a directory" in result
    assert "list_directory" in result


def test_dispatch_read_file_missing_returns_error_string() -> None:
    tools = _Tools()
    tools.read_file_raises = FileNotFoundError("path does not exist: 'gone.py'")
    result = asyncio.run(dispatch_tool("read_file", {"path": "gone.py"}, tools))  # type: ignore[arg-type]
    assert "error calling read_file" in result
    assert "does not exist" in result


def test_dispatch_read_file_path_escape_returns_error_string() -> None:
    tools = _Tools()
    tools.read_file_raises = PathOutsideWorkingCopy("path escapes the working copy: '../x'")
    result = asyncio.run(dispatch_tool("read_file", {"path": "../x"}, tools))  # type: ignore[arg-type]
    assert "error calling read_file" in result
    assert "escapes" in result


def test_dispatch_grep_ripgrep_missing_returns_error_string() -> None:
    from greenbean.core.tools import RipgrepNotInstalled

    tools = _Tools()
    tools.grep_raises = RipgrepNotInstalled("requires ripgrep on $PATH")
    result = asyncio.run(dispatch_tool("grep", {"pattern": "foo"}, tools))  # type: ignore[arg-type]
    assert "error calling grep" in result
    assert "ripgrep" in result


def test_dispatch_missing_required_argument_returns_error_string() -> None:
    """Model called ``read_file`` without a ``path`` key — must not crash."""
    tools = _Tools()
    result = asyncio.run(dispatch_tool("read_file", {}, tools))  # type: ignore[arg-type]
    assert "error calling read_file" in result
    assert "missing required argument" in result
    assert "path" in result


def test_dispatch_unknown_tool_returns_message() -> None:
    tools = _Tools()
    result = asyncio.run(dispatch_tool("frobnicate", {}, tools))  # type: ignore[arg-type]
    assert "unknown tool" in result


# ----- generator loop is unaffected by tool errors --------------------------


def test_generator_recovers_from_tool_error_and_completes() -> None:
    """A tool-raising turn must come back as a tool_result, not a crash.

    Wires a 2-turn fake-LLM script: turn 1 calls ``read_file`` on a directory
    (raises ``IsADirectoryError``); turn 2 returns ``end_turn`` with content.
    The generator must reach turn 2 — proving the error from turn 1 became
    a tool_result string rather than killing the loop.
    """
    from unittest.mock import AsyncMock

    from greenbean.agent.generator import Generator
    from greenbean.core.llm import (
        LLMResponse,
        TextBlock,
        ToolResult,
        ToolUseBlock,
        Usage,
        UserMessage,
    )
    from greenbean.core.planning import Document

    tools = _Tools()
    tools.read_file_raises = IsADirectoryError("'src' is a directory, not a file.")

    captured_tool_results: list[ToolResult] = []

    turn_one = LLMResponse(
        content=[ToolUseBlock(id="tu_1", name="read_file", input={"path": "src"})],
        stop_reason="tool_use",
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    turn_two = LLMResponse(
        content=[TextBlock(text="# README\nGenerated despite the tool error.")],
        stop_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
    )

    async def _send(**kwargs: Any) -> LLMResponse:
        msgs = kwargs["messages"]
        # On the second call, the most recent UserMessage is the tool_result.
        if len(msgs) >= 2 and isinstance(msgs[-1], UserMessage):
            content = msgs[-1].content
            if isinstance(content, list):
                captured_tool_results.extend(
                    b for b in content if isinstance(b, ToolResult)
                )
        return turn_two if len(msgs) > 1 else turn_one

    client = AsyncMock()
    client.send_messages = _send

    doc = Document(
        id="d", path_in_repo="README.md", doc_type="readme", scope="", spec={},
        last_planned_sha="a" * 40, current_content_hash=None,
        last_generated_at=None, generation_metadata={},
    )

    gen = Generator(tools, model="test", client=client)  # type: ignore[arg-type]
    result = asyncio.run(gen.generate(doc, source_paths=[]))

    assert result.content == "# README\nGenerated despite the tool error."
    assert result.tool_calls == 1
    # The error was passed back to the model as the tool_result.
    assert len(captured_tool_results) == 1
    assert "error calling read_file" in captured_tool_results[0].content
    assert "is a directory" in captured_tool_results[0].content


# ----- catch-all on unexpected exceptions -----------------------------------


def test_dispatch_catches_unexpected_exception_type() -> None:
    """Any ``Exception`` subclass must be caught — not just the known ones."""
    tools = _Tools()
    tools.read_file_raises = RuntimeError("totally novel failure mode")
    result = asyncio.run(dispatch_tool("read_file", {"path": "x"}, tools))  # type: ignore[arg-type]
    assert "error calling read_file" in result
    assert "totally novel failure mode" in result


def test_dispatch_preserves_keyboard_interrupt() -> None:
    """``KeyboardInterrupt`` must propagate so Ctrl-C still works."""
    tools = _Tools()
    tools.read_file_raises = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(dispatch_tool("read_file", {"path": "x"}, tools))  # type: ignore[arg-type]
