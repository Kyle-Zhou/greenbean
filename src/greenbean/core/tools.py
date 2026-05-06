"""Tool layer — the audited surface the agent uses to read and search a repo.

Per architecture §6.1, every capability the agent has is a named tool with a
stable interface. v1 implements these against a server-side working copy
(``WorkingCopyTools``); a future local-agent implementation can swap in a
different backend without changing agent logic.

Discovery is **on-demand** (architecture §6.1): tools run ``rg`` / ``git`` /
filesystem reads at call time rather than hitting precomputed indexes. If
profiling later shows this is too slow, caching goes behind this interface,
invisible to the agent.

The v1 tool set covers what the README/architecture-doc generator needs:
``read_file``, ``list_directory``, ``grep``, ``git_log``, ``git_blame``.
``find_symbol`` / ``find_references`` (tree-sitter) and ``semantic_search``
(pgvector) are deferred to later sub-steps within architecture §12 step 2.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from greenbean.core.git import BlameLine, Commit


@dataclass(frozen=True, slots=True)
class DirEntry:
    name: str
    is_dir: bool


@dataclass(frozen=True, slots=True)
class GrepHit:
    """A single match. ``path`` is relative to the working-copy root."""

    path: str
    line: int
    text: str


class PathOutsideWorkingCopy(ValueError):
    """Raised when a tool call references a path that escapes the working copy.

    Path traversal is rejected at the tool boundary per architecture §4.4.
    """


class RipgrepNotInstalled(RuntimeError):
    """Raised the first time ``grep`` is called if ``rg`` is not on ``$PATH``.

    Greenbean requires ripgrep (per architecture §6.1 retrieval philosophy);
    install with ``brew install ripgrep`` / ``apt install ripgrep``.
    """


class Tools(Protocol):
    """Read-only surface over a working copy.

    Every path argument is **relative to the working-copy root**. Implementations
    must reject paths that resolve outside that root (including via ``..`` or
    symlinks) by raising ``PathOutsideWorkingCopy``.
    """

    async def read_file(
        self,
        path: str,
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> str:
        """Return file contents as text.

        ``start`` / ``end`` are optional 1-indexed inclusive line numbers.
        """
        ...

    async def list_directory(self, path: str = ".") -> Sequence[DirEntry]: ...

    async def grep(
        self,
        pattern: str,
        *,
        path: str | None = None,
        ignore_case: bool = False,
        max_results: int | None = None,
    ) -> Sequence[GrepHit]:
        """Search for pattern matches.

        ``max_results`` is a global cap on total returned hits (across files).
        Implementations may use backend-specific pre-limits for performance,
        but must not return more than ``max_results`` hits.
        """
        ...

    async def git_log(
        self,
        *,
        path: str | None = None,
        limit: int | None = None,
    ) -> Sequence[Commit]: ...

    async def git_blame(self, path: str, line: int) -> BlameLine: ...
