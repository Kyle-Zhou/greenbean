"""Mechanical, deterministic doc-plan generator. No LLM in v1.

Async because filesystem walks can be heavy on large repos.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from greenbean.core.planning import DocSpec

_SOURCE_EXTENSIONS = frozenset(
    {
        ".py", ".ts", ".tsx", ".js", ".jsx",
        ".go", ".rs", ".java", ".kt", ".rb",
        ".cpp", ".cc", ".c", ".h", ".hpp",
    }
)

_EXCLUDED_DIRS = frozenset(
    {
        ".git", ".venv", "venv", "node_modules", "__pycache__",
        "dist", "build", "target", "vendor",
    }
)

_SRC_ROOTS = frozenset({"src", "lib", "pkg", "internal", "cmd", "packages"})

_SOURCE_CAP = 500


def _is_excluded(rel_parts: tuple[str, ...]) -> bool:
    return any(part.startswith(".") or part in _EXCLUDED_DIRS for part in rel_parts)


def _collect_source_files(root: Path, under: Path) -> list[str]:
    results: list[str] = []
    for p in under.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        parts = rel.parts
        if _is_excluded(parts[:-1]):
            continue
        if p.suffix in _SOURCE_EXTENSIONS:
            results.append(rel.as_posix())
    results.sort()
    return results


def _significant_subdirs(repo_path: Path) -> list[Path]:
    """Return directories that warrant their own README.

    Rules:
    - immediate children of known src roots (src/, lib/, etc.) always qualify
    - any directory with ≥3 source files qualifies
    - recurse into src roots but not through them (top-level children only)
    - skip excluded dirs and hidden dirs
    """
    candidates: list[Path] = []

    def _walk(directory: Path, depth: int, inside_src_root: bool) -> None:
        try:
            children = sorted(directory.iterdir())
        except PermissionError:
            return

        for child in children:
            if not child.is_dir():
                continue
            name = child.name
            if name.startswith(".") or name in _EXCLUDED_DIRS:
                continue

            rel = child.relative_to(repo_path)
            rel_parts = rel.parts

            if depth == 0 and name in _SRC_ROOTS:
                _walk(child, depth + 1, inside_src_root=True)
                continue

            if inside_src_root:
                candidates.append(child)
                continue

            src_count = sum(
                1
                for p in child.rglob("*")
                if p.is_file()
                and p.suffix in _SOURCE_EXTENSIONS
                and not _is_excluded(p.relative_to(repo_path).parts[:-1])
            )
            if src_count >= 3:
                candidates.append(child)

            if not inside_src_root and rel_parts[0] not in _SRC_ROOTS:
                _walk(child, depth + 1, inside_src_root=False)

    _walk(repo_path, 0, inside_src_root=False)
    return candidates


class DefaultPlanner:
    async def initial_plan(self, repo_path: Path) -> Sequence[DocSpec]:
        return await asyncio.to_thread(self._initial_plan_sync, repo_path)

    def _initial_plan_sync(self, repo_path: Path) -> Sequence[DocSpec]:
        specs: list[DocSpec] = [
            DocSpec(path_in_repo="README.md", doc_type="readme", scope="", spec={}),
            DocSpec(
                path_in_repo="docs/architecture.md",
                doc_type="architecture",
                scope="",
                spec={},
            ),
        ]

        seen_paths = {"README.md", "docs/architecture.md"}

        for subdir in _significant_subdirs(repo_path):
            rel = subdir.relative_to(repo_path)
            scope = rel.as_posix() + "/"
            readme_path = rel.as_posix() + "/README.md"
            if readme_path not in seen_paths:
                seen_paths.add(readme_path)
                specs.append(
                    DocSpec(
                        path_in_repo=readme_path,
                        doc_type="readme",
                        scope=scope,
                        spec={},
                    )
                )

        return specs

    async def source_files_for(self, repo_path: Path, doc: DocSpec) -> Sequence[str]:
        return await asyncio.to_thread(self._source_files_for_sync, repo_path, doc)

    def _source_files_for_sync(self, repo_path: Path, doc: DocSpec) -> Sequence[str]:
        search_root = repo_path / doc.scope if doc.scope else repo_path
        files = _collect_source_files(repo_path, search_root)
        return files[:_SOURCE_CAP]
