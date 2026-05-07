"""Tests for DefaultPlanner — filesystem-based, no git or LLM needed."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from greenbean.planning.planner import DefaultPlanner


def _make_file(root: Path, rel: str, content: str = "x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


# ----- always-included docs --------------------------------------------------


def test_empty_repo_always_includes_two_docs(tmp_path: Path) -> None:
    planner = DefaultPlanner()
    specs = asyncio.run(planner.initial_plan(tmp_path))
    paths = {s.path_in_repo for s in specs}
    assert "README.md" in paths
    assert "docs/architecture.md" in paths


def test_node_modules_only_repo_still_has_two_docs(tmp_path: Path) -> None:
    _make_file(tmp_path, "node_modules/lodash/index.js")
    _make_file(tmp_path, "node_modules/react/index.js")
    planner = DefaultPlanner()
    specs = asyncio.run(planner.initial_plan(tmp_path))
    paths = {s.path_in_repo for s in specs}
    assert "README.md" in paths
    assert "docs/architecture.md" in paths
    assert not any("node_modules" in p for p in paths)


def test_always_included_docs_have_empty_scope(tmp_path: Path) -> None:
    planner = DefaultPlanner()
    specs = asyncio.run(planner.initial_plan(tmp_path))
    by_path = {s.path_in_repo: s for s in specs}
    assert by_path["README.md"].scope == ""
    assert by_path["docs/architecture.md"].scope == ""


# ----- significant subdirectory heuristics -----------------------------------


def test_src_root_child_gets_readme(tmp_path: Path) -> None:
    _make_file(tmp_path, "src/greenbean/cli.py")
    planner = DefaultPlanner()
    specs = asyncio.run(planner.initial_plan(tmp_path))
    paths = {s.path_in_repo for s in specs}
    assert "src/greenbean/README.md" in paths


def test_non_src_root_dir_with_three_files_gets_readme(tmp_path: Path) -> None:
    for i in range(3):
        _make_file(tmp_path, f"mylib/mod{i}.py")
    planner = DefaultPlanner()
    specs = asyncio.run(planner.initial_plan(tmp_path))
    paths = {s.path_in_repo for s in specs}
    assert "mylib/README.md" in paths


def test_small_dir_below_three_files_omitted(tmp_path: Path) -> None:
    _make_file(tmp_path, "tiny/a.py")
    _make_file(tmp_path, "tiny/b.py")
    planner = DefaultPlanner()
    specs = asyncio.run(planner.initial_plan(tmp_path))
    paths = {s.path_in_repo for s in specs}
    assert "tiny/README.md" not in paths


def test_hidden_dirs_excluded(tmp_path: Path) -> None:
    for i in range(3):
        _make_file(tmp_path, f".hidden/mod{i}.py")
    planner = DefaultPlanner()
    specs = asyncio.run(planner.initial_plan(tmp_path))
    paths = {s.path_in_repo for s in specs}
    assert not any(".hidden" in p for p in paths)


def test_no_duplicate_paths_in_plan(tmp_path: Path) -> None:
    _make_file(tmp_path, "src/greenbean/cli.py")
    _make_file(tmp_path, "src/greenbean/core/git.py")
    planner = DefaultPlanner()
    specs = asyncio.run(planner.initial_plan(tmp_path))
    paths = [s.path_in_repo for s in specs]
    assert len(paths) == len(set(paths))


# ----- scope invariant -------------------------------------------------------


def test_sub_readme_has_trailing_slash_scope(tmp_path: Path) -> None:
    _make_file(tmp_path, "src/greenbean/cli.py")
    planner = DefaultPlanner()
    specs = asyncio.run(planner.initial_plan(tmp_path))
    for spec in specs:
        if spec.path_in_repo != "README.md" and spec.path_in_repo != "docs/architecture.md":
            assert spec.scope.endswith("/"), f"{spec.path_in_repo} scope missing trailing slash"


# ----- source_files_for ------------------------------------------------------


@pytest.mark.parametrize(
    "repo_shape,scope,expected_suffixes",
    [
        pytest.param(
            [("src/a.py",), ("src/b.ts",), ("README.md",)],
            "",
            {".py", ".ts"},
            id="root-scope-returns-all-source-files",
        ),
        pytest.param(
            [("src/mymod/a.py",), ("src/mymod/b.py",), ("other/c.go",)],
            "src/mymod/",
            {".py"},
            id="scoped-to-subdir",
        ),
        pytest.param(
            [("node_modules/x.js",), ("src/app.py",)],
            "",
            {".py"},
            id="node_modules-excluded-from-sources",
        ),
    ],
)
def test_source_files_for(
    tmp_path: Path,
    repo_shape: list[tuple[str]],
    scope: str,
    expected_suffixes: set[str],
) -> None:
    from greenbean.core.planning import DocSpec

    for (rel,) in repo_shape:
        _make_file(tmp_path, rel)

    spec = DocSpec(path_in_repo="README.md", doc_type="readme", scope=scope, spec={})
    planner = DefaultPlanner()
    sources = asyncio.run(planner.source_files_for(tmp_path, spec))

    for suffix in expected_suffixes:
        assert any(s.endswith(suffix) for s in sources), (
            f"expected a {suffix} file in sources, got {list(sources)}"
        )
    for source in sources:
        assert not any(ex in source for ex in ["node_modules", ".venv", "__pycache__"])


def test_source_files_for_returns_sorted(tmp_path: Path) -> None:
    from greenbean.core.planning import DocSpec

    for name in ["c.py", "a.py", "b.py"]:
        _make_file(tmp_path, f"src/{name}")

    spec = DocSpec(path_in_repo="README.md", doc_type="readme", scope="src/", spec={})
    planner = DefaultPlanner()
    sources = list(asyncio.run(planner.source_files_for(tmp_path, spec)))
    assert sources == sorted(sources)


def test_source_files_for_respects_scope_boundary(tmp_path: Path) -> None:
    from greenbean.core.planning import DocSpec

    _make_file(tmp_path, "src/foo/a.py")
    _make_file(tmp_path, "src/foobar/b.py")

    spec = DocSpec(path_in_repo="src/foo/README.md", doc_type="readme", scope="src/foo/", spec={})
    planner = DefaultPlanner()
    sources = asyncio.run(planner.source_files_for(tmp_path, spec))
    assert all(s.startswith("src/foo/") for s in sources)
    assert not any("foobar" in s for s in sources)
