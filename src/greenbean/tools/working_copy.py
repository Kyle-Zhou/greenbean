"""Tool-layer implementation for local working-copy clones.

Path-safety: every path argument is resolved against the working-copy root
and rejected if the resulting absolute path escapes that root (including via
``..`` segments or symlinks). Architecture §4.4 calls this out as the
filesystem layer of tenant isolation; we apply the same check in single-tenant
mode because it's also the right defense against agent prompt injection that
asks for ``../../etc/passwd``.
The full contract is documented in
``docs/architecture/tool-path-safety.md``.

Discovery is on-demand: ``grep`` shells out to ``rg`` at call time;
``git_log`` / ``git_blame`` shell out to ``git`` via ``GitService``. No
precomputed indexes — that's a deferred optimization.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Sequence
from pathlib import Path

from greenbean.core.git import BlameLine, Commit, GitService
from greenbean.core.tools import (
    DirEntry,
    GrepHit,
    PathOutsideWorkingCopy,
    RipgrepNotInstalled,
)


class WorkingCopyTools:
    """``Tools`` implementation against a directory on disk."""

    def __init__(self, repo_path: Path, *, git: GitService | None = None) -> None:
        self._repo_path = repo_path.resolve()
        self._git = git if git is not None else GitService()

    # ----- file / directory ------------------------------------------------

    async def read_file(
        self,
        path: str,
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> str:
        target = self._resolve_safe(path)
        text = await asyncio.to_thread(target.read_text, encoding="utf-8", errors="replace")
        if start is None and end is None:
            return text
        lines = text.splitlines(keepends=True)
        s = max(1, start or 1) - 1
        e = end if end is not None else len(lines)
        return "".join(lines[s:e])

    async def list_directory(self, path: str = ".") -> Sequence[DirEntry]:
        target = self._resolve_safe(path)
        entries = await asyncio.to_thread(lambda: sorted(target.iterdir()))
        return tuple(DirEntry(name=p.name, is_dir=p.is_dir()) for p in entries)

    # ----- search ----------------------------------------------------------

    async def grep(
        self,
        pattern: str,
        *,
        path: str | None = None,
        ignore_case: bool = False,
        max_results: int | None = None,
    ) -> Sequence[GrepHit]:
        """Return grep hits under the working-copy root.

        ``max_results`` is enforced as a global cap on returned hits. We also
        pass ``--max-count`` to ripgrep as an early cutoff optimization.
        """
        if shutil.which("rg") is None:
            raise RipgrepNotInstalled(
                "greenbean's grep tool requires ripgrep on $PATH "
                "(`brew install ripgrep` / `apt install ripgrep`)"
            )

        search_root = self._resolve_safe(path) if path is not None else self._repo_path
        args: list[str] = ["rg", "--json", "--no-messages"]
        if ignore_case:
            args.append("--ignore-case")
        if max_results is not None:
            args += ["--max-count", str(max_results)]
        args += ["--", pattern, str(search_root)]

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, _ = await proc.communicate()
        # rg exits 1 when there are zero matches; that's not an error for us.
        if proc.returncode not in (0, 1):
            raise RuntimeError(
                f"rg exited {proc.returncode}: {stdout_b.decode('utf-8', 'replace')}"
            )

        hits: list[GrepHit] = []
        for line in stdout_b.decode("utf-8", "replace").splitlines():
            if not line:
                continue
            event = json.loads(line)
            if event.get("type") != "match":
                continue
            data = event["data"]
            abs_path = Path(data["path"]["text"])
            try:
                rel = abs_path.relative_to(self._repo_path).as_posix()
            except ValueError:
                # ``rg`` should only return paths under search_root, which is
                # under repo_path — but be defensive.
                continue
            hits.append(
                GrepHit(
                    path=rel,
                    line=data["line_number"],
                    text=data["lines"]["text"].rstrip("\n"),
                )
            )
            if max_results is not None and len(hits) >= max_results:
                break
        return tuple(hits)

    # ----- git -------------------------------------------------------------

    async def git_log(
        self,
        *,
        path: str | None = None,
        limit: int | None = None,
    ) -> Sequence[Commit]:
        if path is not None:
            self._resolve_safe(path)  # validation only; git takes the relative path
        return await self._git.log(self._repo_path, path=path, limit=limit)

    async def git_blame(self, path: str, line: int) -> BlameLine:
        self._resolve_safe(path)
        return await self._git.blame(self._repo_path, path, line)

    # ----- internals -------------------------------------------------------

    def _resolve_safe(self, rel: str) -> Path:
        """Resolve ``rel`` against the working-copy root; reject any escape."""
        if Path(rel).is_absolute():
            raise PathOutsideWorkingCopy(f"absolute paths are not allowed: {rel!r}")
        candidate = (self._repo_path / rel).resolve()
        if candidate != self._repo_path and self._repo_path not in candidate.parents:
            raise PathOutsideWorkingCopy(
                f"path escapes the working copy: {rel!r}"
            )
        return candidate
