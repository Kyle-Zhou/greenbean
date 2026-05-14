"""Output-directory helpers shared by ``cli`` and ``sync``.

``_atomic_write_text`` and ``_safe_output_path`` live here so the pipeline
body (``sync.run_pipeline``) and the one-shot ``generate`` CLI command can
share them without circular imports. They both write into the
``~/.greenbean/output/...`` tree and so want the same atomicity and
path-safety guarantees.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _atomic_write_text(path: Path, content: str) -> None:
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


def _safe_output_path(output_root: Path, path_in_repo: str) -> Path:
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
        raise ValueError(
            f"path_in_repo escapes the output root: {path_in_repo!r}"
        )
    return candidate
