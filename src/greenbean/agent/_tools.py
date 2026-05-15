"""Tool schemas and dispatcher for the generator's agentic loop.

Defines the JSON schema for each tool exposed to Claude and routes tool-use
blocks to the appropriate Tools method. All tool results are plain strings —
the model reads them as context in the next turn.
"""

from __future__ import annotations

from typing import Any, cast

from greenbean.core.llm import ToolDefinition
from greenbean.core.tools import Tools

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


async def dispatch_tool(name: str, tool_input: object, tools: Tools) -> str:
    """Route a tool-use block to the appropriate Tools method; return a string result.

    Any exception raised by a tool — bad path, missing argument, ripgrep not
    installed, anything — is caught and formatted as a string for the model.
    The agent loop never sees the raw exception, so one bad tool call can't
    crash a whole generation. The model receives the formatted error as a
    ``tool_result`` and can self-correct on the next turn, which matches
    Architecture.md §6.4's "validation failures feed back into the agent
    loop" — the same principle applies to tool failures.
    """
    try:
        return await _dispatch(name, tool_input, tools)
    except KeyError as e:
        # Missing required input key — the model called the tool without a
        # required argument. Make this readable rather than a bare ``'path'``.
        return f"error calling {name}: missing required argument {e}"
    except Exception as e:
        message = str(e) or type(e).__name__
        return f"error calling {name}: {message}"


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
