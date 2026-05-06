"""Git service — thin async wrapper around the ``git`` CLI.

Operates on a working copy on disk. Used by sync, planning, and the agent's
tool layer. Not part of the connection abstraction; it is the same on every
host platform.

We shell out to ``git`` rather than reach for a binding library. ``git`` is
well-debugged, performant, and handles every edge case. Reach for libgit2 /
go-git only if there's a concrete reason — needing to run without ``git`` on
``$PATH`` or genuine process-spawn overhead at high QPS.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

# URLs containing embedded credentials (``https://x-access-token:TOKEN@host/...``)
# show up in our argv when we shell out to ``git clone``. Redact before any
# user-visible string so a failed clone doesn't leak the PAT into logs.
_CREDENTIAL_URL_RE = re.compile(r"https://[^@\s/]+:[^@\s/]+@")


def _redact(text: str) -> str:
    return _CREDENTIAL_URL_RE.sub("https://<redacted>@", text)


class GitError(RuntimeError):
    """Raised when a ``git`` invocation exits non-zero."""

    def __init__(self, args: tuple[str, ...], returncode: int, stderr: str) -> None:
        safe_args = tuple(_redact(a) for a in args)
        safe_stderr = _redact(stderr)
        rendered = " ".join(safe_args)
        super().__init__(f"git {rendered} exited {returncode}: {safe_stderr.strip()}")
        self.git_args = safe_args
        self.returncode = returncode
        self.stderr = safe_stderr


class GitService:
    """Async wrapper around the ``git`` CLI."""

    def __init__(self, git_binary: str = "git") -> None:
        self._git = git_binary

    async def clone(
        self,
        url: str,
        dest: Path,
        *,
        depth: int | None = 50,
        branch: str | None = None,
    ) -> None:
        """Clone ``url`` into ``dest``.

        Defaults to a shallow clone (depth=50) per the architecture's
        working-copy-cache strategy. Pass ``depth=None`` for a full clone.
        """
        args: list[str] = ["clone"]
        if depth is not None:
            args += ["--depth", str(depth)]
        if branch is not None:
            args += ["--branch", branch, "--single-branch"]
        args += [url, str(dest)]
        await self._run(*args)

    async def fetch(self, repo_path: Path, *, depth: int | None = None) -> None:
        """``git fetch --prune`` against the configured remote."""
        args: list[str] = ["-C", str(repo_path), "fetch", "--prune"]
        if depth is not None:
            args += ["--depth", str(depth)]
        await self._run(*args)

    async def reset_hard(self, repo_path: Path, sha: str) -> None:
        await self._run("-C", str(repo_path), "reset", "--hard", sha)

    async def current_sha(self, repo_path: Path) -> str:
        out = await self._run("-C", str(repo_path), "rev-parse", "HEAD")
        return out.strip()

    async def ls_remote(self, url: str, ref: str) -> str:
        """Look up the SHA that ``ref`` resolves to on the remote at ``url``.

        ``ref`` should be fully qualified (e.g. ``"refs/heads/main"``). Raises
        ``GitError`` if the ref doesn't exist on the remote.
        """
        out = await self._run("ls-remote", "--exit-code", url, ref)
        line = next((line for line in out.splitlines() if line.strip()), "")
        if not line:
            raise GitError(
                ("ls-remote", url, ref), 0, f"ref {ref!r} not found on remote"
            )
        return line.split("\t", 1)[0]

    async def _run(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            self._git,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        rc = proc.returncode if proc.returncode is not None else -1
        if rc != 0:
            raise GitError(args, rc, stderr_b.decode("utf-8", errors="replace"))
        return stdout_b.decode("utf-8")
