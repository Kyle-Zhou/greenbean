"""Tests for the Generator agentic loop.

All tests use a fake LLMClient; no real Claude API calls are made.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any
from unittest.mock import AsyncMock

import pytest

from greenbean.agent.generator import (
    Generator,
    GeneratorError,
    _build_initial_message,
    _extract_text,
    _load_prompt,
)
from greenbean.core.llm import (
    AssistantMessage,
    LLMResponse,
    TextBlock,
    ToolDefinition,
    ToolResult,
    ToolUseBlock,
    Usage,
    UserMessage,
)
from greenbean.core.planning import Document
from greenbean.core.tools import DirEntry, GrepHit

# ----- helpers ---------------------------------------------------------------


def _make_doc(
    path: str = "README.md",
    doc_type: str = "readme",
    scope: str = "",
) -> Document:
    return Document(
        id="doc-1",
        path_in_repo=path,
        doc_type=doc_type,
        scope=scope,
        spec={},
        last_planned_sha="a" * 40,
        current_content_hash=None,
        last_generated_at=None,
        generation_metadata={},
    )


def _text_block(text: str) -> TextBlock:
    return TextBlock(text=text)


def _tool_use_block(
    name: str, tool_input: dict[str, Any], tool_id: str = "tu_1"
) -> ToolUseBlock:
    return ToolUseBlock(id=tool_id, name=name, input=tool_input)


def _make_response(
    content: list[Any],
    stop_reason: str,
    input_tokens: int = 10,
    output_tokens: int = 20,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        stop_reason=stop_reason,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _make_client(*responses: LLMResponse) -> Any:
    mock_complete = AsyncMock(side_effect=list(responses))

    class _FakeClient:
        send_messages = mock_complete

    return _FakeClient()


class _StubTools:
    async def read_file(
        self, path: str, *, start: int | None = None, end: int | None = None
    ) -> str:
        return f"# {path}\nstub content"

    async def list_directory(self, path: str = ".") -> Sequence[DirEntry]:
        return [DirEntry(name="README.md", is_dir=False)]

    async def grep(
        self,
        pattern: str,
        *,
        path: str | None = None,
        ignore_case: bool = False,
        max_results: int | None = None,
    ) -> Sequence[GrepHit]:
        return []

    async def git_log(
        self, *, path: str | None = None, limit: int | None = None
    ) -> Sequence[Any]:
        return []

    async def git_blame(self, path: str, line: int) -> Any:
        raise NotImplementedError


# ----- _extract_text ---------------------------------------------------------


def test_extract_text_returns_last_text_block() -> None:
    blocks = [_text_block("first"), _tool_use_block("read_file", {}), _text_block("final")]
    assert _extract_text(blocks) == "final"


def test_extract_text_no_text_block_returns_empty() -> None:
    assert _extract_text([_tool_use_block("read_file", {})]) == ""


def test_extract_text_empty_list_returns_empty() -> None:
    assert _extract_text([]) == ""


# ----- _load_prompt ----------------------------------------------------------


def test_load_prompt_readme_exists() -> None:
    text = _load_prompt("readme")
    assert len(text) > 50


def test_load_prompt_architecture_exists() -> None:
    text = _load_prompt("architecture")
    assert len(text) > 50


def test_load_prompt_unknown_type_raises() -> None:
    with pytest.raises(GeneratorError, match="no prompt template"):
        _load_prompt("nonexistent_xyz")


# ----- _build_initial_message ------------------------------------------------


def test_initial_message_contains_path() -> None:
    doc = _make_doc(path="docs/architecture.md")
    msg = _build_initial_message(doc, [])
    assert "docs/architecture.md" in msg


def test_initial_message_lists_source_paths() -> None:
    doc = _make_doc()
    msg = _build_initial_message(doc, ["src/a.py", "src/b.py"])
    assert "src/a.py" in msg
    assert "src/b.py" in msg


def test_initial_message_includes_scope() -> None:
    doc = _make_doc(scope="src/greenbean/")
    msg = _build_initial_message(doc, [])
    assert "src/greenbean/" in msg


def test_initial_message_no_sources_placeholder() -> None:
    doc = _make_doc()
    msg = _build_initial_message(doc, [])
    assert "No specific source files" in msg


# ----- Generator.generate: end_turn with no tools ----------------------------


def test_generate_end_turn_no_tools() -> None:
    client = _make_client(
        _make_response([_text_block("# README\nHello world")], stop_reason="end_turn")
    )
    gen = Generator(_StubTools(), model="test", client=client)
    result = asyncio.run(gen.generate(_make_doc(), source_paths=[]))
    assert result.content == "# README\nHello world"
    assert result.tool_calls == 0
    assert result.input_tokens == 10
    assert result.output_tokens == 20


# ----- Generator.generate: one tool call then end_turn -----------------------


def test_generate_one_tool_call_then_end_turn() -> None:
    client = _make_client(
        _make_response(
            [_tool_use_block("read_file", {"path": "README.md"})],
            stop_reason="tool_use",
            input_tokens=5,
            output_tokens=8,
        ),
        _make_response(
            [_text_block("# Generated README")],
            stop_reason="end_turn",
            input_tokens=12,
            output_tokens=50,
        ),
    )
    gen = Generator(_StubTools(), model="test", client=client)
    result = asyncio.run(gen.generate(_make_doc(), source_paths=["README.md"]))
    assert result.content == "# Generated README"
    assert result.tool_calls == 1
    assert result.input_tokens == 17
    assert result.output_tokens == 58


# ----- Generator.generate: multiple tools in one turn ------------------------


def test_generate_multiple_tools_in_one_turn() -> None:
    client = _make_client(
        _make_response(
            [
                _tool_use_block("read_file", {"path": "a.py"}, "tu_1"),
                _tool_use_block("grep", {"pattern": "def "}, "tu_2"),
            ],
            stop_reason="tool_use",
        ),
        _make_response([_text_block("done")], stop_reason="end_turn"),
    )
    gen = Generator(_StubTools(), model="test", client=client)
    result = asyncio.run(gen.generate(_make_doc(), source_paths=[]))
    assert result.tool_calls == 2
    assert result.content == "done"


# ----- Generator.generate: empty final response ------------------------------


def test_generate_empty_content_returns_empty_string() -> None:
    client = _make_client(_make_response([], stop_reason="end_turn"))
    gen = Generator(_StubTools(), model="test", client=client)
    result = asyncio.run(gen.generate(_make_doc(), source_paths=[]))
    assert result.content == ""


# ----- Generator.generate: tool_use loop terminates without tool blocks ------


def test_generate_stop_reason_tool_use_but_no_tool_blocks_terminates() -> None:
    """If the model signals tool_use but sends no tool_use blocks, we stop."""
    client = _make_client(
        _make_response([_text_block("partial")], stop_reason="tool_use")
    )
    gen = Generator(_StubTools(), model="test", client=client)
    result = asyncio.run(gen.generate(_make_doc(), source_paths=[]))
    assert result.content == "partial"
    assert result.tool_calls == 0


# ----- Generator.generate: source paths forwarded to initial message ---------


def test_generate_source_paths_appear_in_first_message() -> None:
    captured: list[str] = []

    async def _fake_send_messages(self: Any, **kwargs: Any) -> LLMResponse:
        msgs = kwargs["messages"]
        first = msgs[0]
        assert isinstance(first, UserMessage)
        assert isinstance(first.content, str)
        captured.append(first.content)
        return _make_response([_text_block("ok")], stop_reason="end_turn")

    class _FakeClient:
        send_messages = _fake_send_messages

    gen = Generator(_StubTools(), model="test", client=_FakeClient())
    asyncio.run(gen.generate(_make_doc(), source_paths=["src/foo.py", "src/bar.py"]))
    assert "src/foo.py" in captured[0]
    assert "src/bar.py" in captured[0]
