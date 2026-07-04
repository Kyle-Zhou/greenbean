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
    LLMError,
    LLMResponse,
    Message,
    TextBlock,
    ToolDefinition,
    ToolResult,
    ToolUseBlock,
    Usage,
    UserMessage,
)


def _describe(e: anthropic.APIError) -> str:
    """A concise, user-facing message for an Anthropic SDK error.

    Prefers the human-readable string the API returns in the error body
    (``{"error": {"message": ...}}``) over the SDK's ``.message``, which embeds
    the whole raw response.
    """
    detail: str | None = None
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            detail = err.get("message")
    detail = detail or getattr(e, "message", None) or str(e)
    status = getattr(e, "status_code", None)
    if status:
        return f"Anthropic API error ({status}): {detail}"
    return f"Anthropic API request failed: {detail}"


def _to_anthropic_tool(t: ToolDefinition) -> dict[str, Any]:
    return {"name": t.name, "description": t.description, "input_schema": t.input_schema}


def _to_anthropic_message(msg: Message) -> dict[str, Any]:
    if isinstance(msg, UserMessage):
        if isinstance(msg.content, str):
            return {"role": "user", "content": msg.content}
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": r.tool_use_id,
                    # The API rejects an empty tool_result content string, so a
                    # tool that legitimately returns nothing (empty file, empty
                    # directory) gets a placeholder instead of failing the turn.
                    "content": r.content or "(empty result)",
                    "is_error": r.is_error,
                }
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
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_blocks,
                tools=[_to_anthropic_tool(t) for t in tools],  # type: ignore[arg-type]
                messages=[_to_anthropic_message(m) for m in messages],  # type: ignore[arg-type]
            )
        except anthropic.APIError as e:
            raise LLMError(_describe(e)) from e
        return LLMResponse(
            content=_from_anthropic_content(list(response.content)),
            stop_reason=response.stop_reason or "end_turn",
            usage=Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
        )
