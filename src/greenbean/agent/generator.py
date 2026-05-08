"""Documentation generator — the core agentic loop.

Loads a system prompt from src/greenbean/agent/prompts/{doc_type}.md,
feeds the LLM an initial message describing the document to produce and the
source files to focus on, then runs a tool-use loop until the model emits
end_turn. The last TextBlock in the final response is the generated document.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from greenbean.agent._tools import TOOL_SCHEMAS, dispatch_tool
from greenbean.core.llm import (
    AssistantMessage,
    LLMClient,
    TextBlock,
    ToolResult,
    ToolUseBlock,
    UserMessage,
)
from greenbean.core.planning import Document
from greenbean.core.tools import Tools

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_MAX_TOKENS = 8192
_MAX_TURNS = 30


@dataclass(frozen=True, slots=True)
class GenerationResult:
    content: str
    input_tokens: int
    output_tokens: int
    tool_calls: int


class GeneratorError(RuntimeError): ...


def _load_prompt(doc_type: str) -> str:
    prompt_path = _PROMPTS_DIR / f"{doc_type}.md"
    if not prompt_path.exists():
        raise GeneratorError(
            f"no prompt template for doc_type {doc_type!r}: {prompt_path}"
        )
    return prompt_path.read_text(encoding="utf-8")


def _build_initial_message(doc: Document, source_paths: Sequence[str]) -> str:
    lines = [f"Generate the documentation for `{doc.path_in_repo}`."]
    if doc.scope:
        lines.append(f"Scope: `{doc.scope}` — focus on this subtree.")
    if source_paths:
        lines.append("\nKey source files to examine:")
        for p in source_paths:
            lines.append(f"  - {p}")
    else:
        lines.append("\nNo specific source files pinned — explore the relevant scope.")
    return "\n".join(lines)


def _extract_text(content: Sequence[object]) -> str:
    """Return the text of the last TextBlock in a response content list."""
    for block in reversed(list(content)):
        if isinstance(block, TextBlock):
            return block.text
    return ""


class Generator:
    """Runs the doc-generation agentic loop for a single document."""

    def __init__(
        self,
        tools: Tools,
        *,
        model: str,
        client: LLMClient,
    ) -> None:
        self._tools = tools
        self._model = model
        self._client = client

    async def generate(
        self,
        doc: Document,
        source_paths: Sequence[str],
    ) -> GenerationResult:
        system_text = _load_prompt(doc.doc_type)
        initial_message = _build_initial_message(doc, source_paths)

        messages: list[UserMessage | AssistantMessage] = [
            UserMessage(content=initial_message)
        ]
        total_input = 0
        total_output = 0
        tool_calls = 0
        last_content: list[object] = []

        for _ in range(_MAX_TURNS):
            response = await self._client.send_messages(
                model=self._model,
                system=system_text,
                messages=messages,
                tools=TOOL_SCHEMAS,
                max_tokens=_MAX_TOKENS,
            )
            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens
            last_content = list(response.content)

            messages.append(AssistantMessage(content=response.content))

            if response.stop_reason == "end_turn":
                break

            tool_results: list[ToolResult] = []
            for block in response.content:
                if isinstance(block, ToolUseBlock):
                    tool_calls += 1
                    result = await dispatch_tool(block.name, block.input, self._tools)
                    tool_results.append(ToolResult(tool_use_id=block.id, content=result))

            if not tool_results:
                break

            messages.append(UserMessage(content=tool_results))
        else:
            raise GeneratorError(
                f"generation did not complete within {_MAX_TURNS} turns "
                f"(stop_reason={response.stop_reason!r})"
            )

        return GenerationResult(
            content=_extract_text(last_content),
            input_tokens=total_input,
            output_tokens=total_output,
            tool_calls=tool_calls,
        )
