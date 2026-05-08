"""Provider-agnostic LLM client protocol and value types.

The Generator depends on this interface, not on any specific SDK. Swap the
backend (Anthropic, OpenAI, ...) by passing a different LLMClient
implementation — no changes to agent logic required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


ContentBlock = TextBlock | ToolUseBlock


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_use_id: str
    content: str


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: list[ContentBlock]
    stop_reason: str
    usage: Usage


@dataclass(frozen=True, slots=True)
class UserMessage:
    content: str | list[ToolResult]


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    content: list[ContentBlock]


Message = UserMessage | AssistantMessage


class LLMClient(Protocol):
    async def send_messages(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        max_tokens: int,
    ) -> LLMResponse: ...
