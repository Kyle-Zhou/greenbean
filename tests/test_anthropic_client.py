"""Tests for the Anthropic client's error mapping.

The happy path is covered indirectly elsewhere; here we pin the one piece of
real logic — turning SDK exceptions into a provider-agnostic ``LLMError`` with a
readable message — without making a network call.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import anthropic
import pytest

from greenbean.core.llm import LLMError, ToolResult, UserMessage
from greenbean.llm.anthropic import AnthropicClient, _describe, _to_anthropic_message


def test_tool_result_empty_content_gets_placeholder() -> None:
    """The API rejects an empty tool_result string; a tool that returns nothing
    (empty file/dir) must be sent a non-empty placeholder."""
    msg = UserMessage(content=[ToolResult(tool_use_id="tu", content="", is_error=True)])
    block = _to_anthropic_message(msg)["content"][0]
    assert block["type"] == "tool_result"
    assert block["content"] == "(empty result)"
    assert block["is_error"] is True


def test_tool_result_normal_content_preserved() -> None:
    msg = UserMessage(content=[ToolResult(tool_use_id="tu", content="hello")])
    block = _to_anthropic_message(msg)["content"][0]
    assert block["content"] == "hello"
    assert block["is_error"] is False


class _StubAPIError(anthropic.APIError):
    """An APIError instance without the SDK's network-bound constructor."""

    def __init__(self, status: int | None, body: object, message: str) -> None:
        self.status_code = status
        self.body = body
        self.message = message


def test_describe_prefers_body_error_message() -> None:
    err = _StubAPIError(401, {"error": {"message": "invalid x-api-key"}}, "raw blob")
    assert _describe(err) == "Anthropic API error (401): invalid x-api-key"


def test_describe_falls_back_to_message_without_status() -> None:
    err = _StubAPIError(None, None, "connection reset")
    assert _describe(err) == "Anthropic API request failed: connection reset"


def test_send_messages_wraps_api_error_in_llm_error() -> None:
    sdk = AsyncMock()
    sdk.messages.create = AsyncMock(
        side_effect=_StubAPIError(401, {"error": {"message": "invalid x-api-key"}}, "raw")
    )
    client = AnthropicClient(client=sdk)

    with pytest.raises(LLMError) as exc:
        asyncio.run(
            client.send_messages(
                model="claude-haiku-4-5-20251001",
                system="sys",
                messages=[],
                tools=[],
                max_tokens=64,
            )
        )
    assert "invalid x-api-key" in str(exc.value)
