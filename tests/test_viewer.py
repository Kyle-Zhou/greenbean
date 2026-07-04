"""Tests for the localhost markdown viewer (``viewer.py``).

Pure helpers are tested directly; the serving path is exercised by starting the
server on an ephemeral port (0) and fetching over the loopback.
"""

from __future__ import annotations

import http.client
import json
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from greenbean.viewer import (
    DocStatus,
    StatusHolder,
    ViewerStatus,
    _resolve_under,
    start_viewer,
)


def _sample_status() -> ViewerStatus:
    return ViewerStatus(
        repo_path="/x/repo",
        repo_url="https://github.com/o/r",
        repo_web_url="https://github.com/o/r",
        branch="main",
        head_short="abc12345",
        head_subject="fix things",
        head_author="Dev",
        head_time="2026-06-28 10:00:00 UTC",
        commit_web_url="https://github.com/o/r/commit/abc12345deadbeef",
        last_sync="2026-06-28 10:00:00 UTC",
        next_sync_epoch=1_700_000_000.0,
        interval="5m",
        last_outcome="incremental — 2 doc(s)",
        output_root="/home/.greenbean/output",
    )


def _generate_status() -> ViewerStatus:
    return ViewerStatus(
        repo_path="/x/repo",
        repo_url=None,
        repo_web_url=None,
        branch="main",
        head_short="abc12345",
        head_subject="fix things",
        head_author="Dev",
        head_time="2026-06-28 10:00:00 UTC",
        commit_web_url=None,
        last_sync="2026-06-28 10:00:00 UTC",
        next_sync_epoch=None,
        interval="",
        last_outcome="generated 2 doc(s)",
        output_root="/home/.greenbean/output",
        mode="generate",
        docs=(
            DocStatus(
                path="README.md",
                href="github.com/o/r/README.md",
                last_generated="2026-06-28 10:00:00 UTC",
                model="claude-sonnet-4-6",
                tokens="406+85",
                generated=True,
            ),
            DocStatus(
                path="CONTRIBUTING.md",
                href=None,
                last_generated=None,
                model=None,
                tokens=None,
                generated=False,
            ),
        ),
    )


def test_resolve_under_allows_nested_path(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    assert _resolve_under(root, "/github.com/o/r/README.md") == (
        root / "github.com/o/r/README.md"
    ).resolve()


def test_resolve_under_rejects_parent_escape(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    assert _resolve_under(root, "/../secret.md") is None


def test_resolve_under_root_is_allowed(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    assert _resolve_under(root, "/") == root.resolve()


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    root = tmp_path / "output"
    (root / "github.com" / "o" / "r").mkdir(parents=True)
    (root / "github.com" / "o" / "r" / "README.md").write_text("# Title\n\nhello\n")

    server = start_viewer(root, 0)
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", root
    finally:
        server.shutdown()


def _get(url: str) -> str:
    with urllib.request.urlopen(url) as resp:
        data: bytes = resp.read()
    return data.decode("utf-8")


def test_index_lists_generated_docs(served: tuple[str, Path]) -> None:
    base, _ = served
    body = _get(base + "/")
    assert "github.com/o/r/README.md" in body


def test_markdown_is_rendered_to_html(served: tuple[str, Path]) -> None:
    base, _ = served
    page = _get(base + "/github.com/o/r/README.md")
    assert "<h1" in page and "Title" in page
    assert "hello" in page


def test_mtime_endpoint_returns_a_timestamp(served: tuple[str, Path]) -> None:
    base, _ = served
    value = _get(base + "/__mtime?path=github.com/o/r/README.md")
    assert float(value) > 0


def test_unknown_path_is_404(served: tuple[str, Path]) -> None:
    base, _ = served
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base + "/does/not/exist.md")
    assert exc.value.code == 404


def test_traversal_request_does_not_escape_root(served: tuple[str, Path]) -> None:
    base, root = served
    (root.parent / "secret.md").write_text("# secret\n")
    host = base.removeprefix("http://")
    conn = http.client.HTTPConnection(host)
    conn.request("GET", "/../secret.md")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    assert "secret" not in body  # never served, whether normalized or rejected


def test_status_panel_present_without_status(served: tuple[str, Path]) -> None:
    base, _ = served
    assert "watch status" in _get(base + "/")


def test_status_json_empty_without_holder(served: tuple[str, Path]) -> None:
    base, _ = served
    assert json.loads(_get(base + "/__status")) == {}


# ----- status panel (with a holder) ------------------------------------------


@pytest.fixture
def served_with_status(tmp_path: Path) -> Iterator[str]:
    root = tmp_path / "output"
    (root / "github.com" / "o" / "r").mkdir(parents=True)
    (root / "github.com" / "o" / "r" / "README.md").write_text("# Title\n")

    holder = StatusHolder()
    holder.set(_sample_status())
    server = start_viewer(root, 0, holder)
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


def test_status_json_reflects_holder(served_with_status: str) -> None:
    data = json.loads(_get(served_with_status + "/__status"))
    assert data["branch"] == "main"
    assert data["commit_web_url"].endswith("/commit/abc12345deadbeef")
    assert data["last_outcome"] == "incremental — 2 doc(s)"
    assert data["next_sync_epoch"] == 1_700_000_000.0


def test_index_renders_status_fields(served_with_status: str) -> None:
    body = _get(served_with_status + "/")
    assert "watch status" in body
    assert "github.com/o/r" in body  # repo link
    assert "abc12345" in body  # commit short sha
    assert "fix things" in body  # commit subject


# ----- per-doc table + generate mode -----------------------------------------


@pytest.fixture
def served_generate(tmp_path: Path) -> Iterator[str]:
    root = tmp_path / "output"
    (root / "github.com" / "o" / "r").mkdir(parents=True)
    (root / "github.com" / "o" / "r" / "README.md").write_text("# Title\n")

    holder = StatusHolder()
    holder.set(_generate_status())
    server = start_viewer(root, 0, holder)
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


def test_generate_mode_panel_and_per_doc_table(served_generate: str) -> None:
    body = _get(served_generate + "/")
    assert "generate status" in body  # mode-aware header
    assert "next sync" not in body  # sync-cadence rows dropped under generate
    # per-doc table: a generated doc (with metadata) and a not-yet-generated one
    assert 'id="gb-docs"' in body
    assert "README.md" in body and "claude-sonnet-4-6" in body and "406+85" in body
    assert "CONTRIBUTING.md" in body and "not generated" in body


def test_status_json_includes_docs(served_generate: str) -> None:
    data = json.loads(_get(served_generate + "/__status"))
    assert data["mode"] == "generate"
    assert data["next_sync_epoch"] is None
    paths = {d["path"]: d for d in data["docs"]}
    assert paths["README.md"]["generated"] is True
    assert paths["README.md"]["href"] == "github.com/o/r/README.md"
    assert paths["CONTRIBUTING.md"]["generated"] is False
