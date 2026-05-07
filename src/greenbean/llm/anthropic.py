"""Anthropic SDK implementation of the LLMClient protocol.

Preserves prompt caching (cache_control on the system block) which is an
Anthropic-native feature lost when going through provider-agnostic shims like
LiteLLM or OpenRouter.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import anthropic
from anthropic.types import TextBlockParam

from greenbean.core.llm import (
    AssistantMessage,
    ContentBlock,
    LLMResponse,
    Message,
    TextBlock,
    ToolDefinition,
    ToolResult,
    ToolUseBlock,
    Usage,
    UserMessage,
)


def _to_anthropic_tool(t: ToolDefinition) -> dict[str, Any]:
    return {"name": t.name, "description": t.description, "input_schema": t.input_schema}


def _to_anthropic_message(msg: Message) -> dict[str, Any]:
    if isinstance(msg, UserMessage):
        if isinstance(msg.content, str):
            return {"role": "user", "content": msg.content}
        return {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": r.tool_use_id, "content": r.content}
                for r in msg.content
            ],
        }
    parts: list[dict[str, Any]] = []
    for block in msg.content:
        if isinstance(block, TextBlock):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            parts.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
    return {"role": "assistant", "content": parts}


def _from_anthropic_content(content: list[Any]) -> list[ContentBlock]:
    blocks: list[ContentBlock] = []
    for block in content:
        if hasattr(block, "type"):
            if block.type == "text":
                blocks.append(TextBlock(text=block.text))
            elif block.type == "tool_use":
                blocks.append(ToolUseBlock(id=block.id, name=block.name, input=dict(block.input)))
    return blocks


class AnthropicClient:
    """LLMClient backed by the Anthropic SDK with prompt caching enabled."""

    def __init__(self, client: anthropic.AsyncAnthropic | None = None) -> None:
        self._client = client if client is not None else anthropic.AsyncAnthropic()

    async def send_messages(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        max_tokens: int,
    ) -> LLMResponse:
        system_blocks: list[TextBlockParam] = [
            TextBlockParam(type="text", text=system, cache_control={"type": "ephemeral"})
        ]
        response = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            tools=[_to_anthropic_tool(t) for t in tools],  # type: ignore[arg-type]
            messages=[_to_anthropic_message(m) for m in messages],  # type: ignore[arg-type]
        )
        return LLMResponse(
            content=_from_anthropic_content(list(response.content)),
            stop_reason=response.stop_reason or "end_turn",
            usage=Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
        )
