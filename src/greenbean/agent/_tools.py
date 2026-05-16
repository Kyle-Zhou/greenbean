"""Tool schemas and dispatcher for the generator's agentic loop.

Defines the JSON schema for each tool exposed to Claude and routes tool-use
blocks to the appropriate Tools method. All tool results are plain strings —
the model reads them as context in the next turn.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from greenbean.core.llm import ToolDefinition
from greenbean.core.tools import Tools

logger = logging.getLogger(__name__)

TOOL_SCHEMAS: list[ToolDefinition] = [
    ToolDefinition(
        name="read_file",
        description="Read the contents of a file in the repository.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the repository root.",
                },
                "start": {
                    "type": "integer",
                    "description": "First line number to read (1-indexed, inclusive).",
                },
                "end": {
                    "type": "integer",
                    "description": "Last line number to read (1-indexed, inclusive).",
                },
            },
            "required": ["path"],
        },
    ),
    ToolDefinition(
        name="list_directory",
        description="List the entries in a directory.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the repository root (default: root).",
                },
            },
        },
    ),
    ToolDefinition(
        name="grep",
        description="Search for a pattern in the repository using ripgrep.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Restrict the search to this file or directory.",
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "Case-insensitive search.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of matches to return.",
                },
            },
            "required": ["pattern"],
        },
    ),
    ToolDefinition(
        name="git_log",
        description="Show recent git commits, optionally filtered to a path.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Show only commits that touched this path.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of commits to return (default: 20).",
                },
            },
        },
    ),
]


def _format_tool_call(name: str, tool_input: object) -> str:
    """Render a one-line summary of a tool call for the progress log.

    Picks the single most informative input argument per tool — the path
    for read/list operations, the pattern for grep, the path filter for
    ``git_log`` — so the operator can scan logs and tell at a glance what
    the agent is doing without each line ballooning into the full JSON.
    """
    inp = cast(dict[str, Any], tool_input) if isinstance(tool_input, dict) else {}
    if name == "read_file":
        path = inp.get("path", "?")
        if inp.get("start") is not None or inp.get("end") is not None:
            return f"read_file {path}:{inp.get('start', '')}-{inp.get('end', '')}"
        return f"read_file {path}"
    if name == "list_directory":
        return f"list_directory {inp.get('path', '.')}"
    if name == "grep":
        pattern = inp.get("pattern", "?")
        scope = f" in {inp['path']}" if inp.get("path") else ""
        return f"grep {pattern!r}{scope}"
    if name == "git_log":
        scope = f" {inp['path']}" if inp.get("path") else ""
        return f"git_log{scope}"
    return f"{name} {inp}"


async def dispatch_tool(name: str, tool_input: object, tools: Tools) -> str:
    """Route a tool-use block to the appropriate Tools method; return a string result.

    Any exception raised by a tool — bad path, missing argument, ripgrep not
    installed, anything — is caught and formatted as a string for the model.
    The agent loop never sees the raw exception, so one bad tool call can't
    crash a whole generation. The model receives the formatted error as a
    ``tool_result`` and can self-correct on the next turn, which matches
    Architecture.md §6.4's "validation failures feed back into the agent
    loop" — the same principle applies to tool failures.

    Emits one INFO log line per call so an operator running ``greenbean run``
    can see live what the agent is asking for; tool errors come back as a
    WARNING log line (in addition to being returned to the model).
    """
    logger.info("  %s", _format_tool_call(name, tool_input))
    try:
        result = await _dispatch(name, tool_input, tools)
    except KeyError as e:
        # Missing required input key — the model called the tool without a
        # required argument. Make this readable rather than a bare ``'path'``.
        message = f"error calling {name}: missing required argument {e}"
        logger.warning("    %s", message)
        return message
    except Exception as e:
        rendered = str(e) or type(e).__name__
        message = f"error calling {name}: {rendered}"
        logger.warning("    %s", message)
        return message
    if result.startswith("error calling"):
        # ``_dispatch`` itself doesn't currently raise on unknown tools — it
        # returns a string. Surface that too so unknown-tool noise shows up
        # in the same place as raised errors.
        logger.warning("    %s", result.splitlines()[0])
    return result


async def _dispatch(name: str, tool_input: object, tools: Tools) -> str:
    """Inner dispatch — kept separate so ``dispatch_tool`` is a clean wrapper."""
    inp = cast(dict[str, Any], tool_input)

    if name == "read_file":
        return await tools.read_file(
            inp["path"],
            start=inp.get("start"),
            end=inp.get("end"),
        )

    if name == "list_directory":
        entries = await tools.list_directory(inp.get("path", "."))
        return "\n".join(("DIR " if e.is_dir else "    ") + e.name for e in entries)

    if name == "grep":
        hits = await tools.grep(
            inp["pattern"],
            path=inp.get("path"),
            ignore_case=bool(inp.get("ignore_case", False)),
            max_results=inp.get("max_results"),
        )
        if not hits:
            return "(no matches)"
        return "\n".join(f"{h.path}:{h.line}: {h.text}" for h in hits)

    if name == "git_log":
        commits = await tools.git_log(path=inp.get("path"), limit=inp.get("limit"))
        if not commits:
            return "(no commits)"
        return "\n".join(
            f"{c.sha[:8]}  {c.author}  {c.timestamp.strftime('%Y-%m-%d')}  "
            f"{c.message.splitlines()[0]}"
            for c in commits
        )

    return f"unknown tool: {name!r}"
