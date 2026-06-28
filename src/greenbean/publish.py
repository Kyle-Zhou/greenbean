"""Publishing helpers — resolve where generated docs land and write them safely.

The v1 publishing surface is a plain filesystem write into the output dir
(``~/.greenbean/output/<host>/<owner>/<name>/<doc-path>``); generated content
never goes back to the source repo. These helpers are shared by the ``run``
pipeline and the ``generate`` command — keep them here, not in ``cli``, so the
pipeline can import them without a cycle.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from greenbean.connectors.github import url as github_url
from greenbean.core.git import GitError, GitService

DEFAULT_OUTPUT_ROOT = Path.home() / ".greenbean" / "output"


def atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    Writes to a uniquely-named sibling temp file then ``os.replace``s into
    position. The temp lives on the same filesystem as the target so the
    rename is atomic on POSIX. Prevents partial files on disk-full, SIGTERM
    mid-write, or crash. The unique temp name also keeps two concurrent
    callers (two processes, future parallel generation) from clobbering
    each other's in-flight write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def safe_output_path(output_root: Path, path_in_repo: str) -> Path:
    """Resolve ``path_in_repo`` under ``output_root``; reject any escape.

    Symmetric with ``WorkingCopyTools._resolve_safe`` on the read side. The
    plan's ``path_in_repo`` is trusted today (the mechanical planner only
    emits clean relative paths), but the planner gains a small-LLM
    refinement step in the build order — at which point this is the right
    place to catch a hallucinated ``../../etc/passwd``.
    """
    if Path(path_in_repo).is_absolute():
        raise ValueError(f"path_in_repo must be relative, got {path_in_repo!r}")
    output_root = output_root.resolve()
    candidate = (output_root / path_in_repo).resolve()
    if candidate != output_root and output_root not in candidate.parents:
        raise ValueError(f"path_in_repo escapes the output root: {path_in_repo!r}")
    return candidate


def local_namespace(repo_path: Path) -> str:
    """Name unique per working-copy path, derived from basename + path hash.

    Two repos with the same directory name (a common case — every test
    fixture is "repo", every dev tree is "myproject") would otherwise
    collide under ``_local/`` and silently overwrite each other's docs.
    """
    resolved = repo_path.resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:8]
    return f"{resolved.name}-{digest}"


async def output_root_for(repo_path: Path, git: GitService) -> Path:
    """Resolve where generated docs for ``repo_path`` should land.

    Reads ``origin``'s URL and parses it as a GitHub repo. Falls back to
    ``_local/<basename>-<path-hash>`` when the working copy has no origin
    or its URL isn't a recognizable GitHub URL — keeps local fixtures and
    file:// clones working, while avoiding collisions between different
    repos that happen to share a directory name.
    """
    try:
        remote = await git.remote_url(repo_path)
    except GitError:
        remote = ""
    if remote:
        parsed = github_url.try_parse(remote)
        if parsed is not None:
            return DEFAULT_OUTPUT_ROOT / "github.com" / parsed.owner / parsed.name
    return DEFAULT_OUTPUT_ROOT / "_local" / local_namespace(repo_path)
