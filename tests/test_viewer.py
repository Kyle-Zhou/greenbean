"""Tests for the localhost markdown viewer (``viewer.py``).

Pure helpers are tested directly; the serving path is exercised by starting the
server on an ephemeral port (0) and fetching over the loopback.
"""

from __future__ import annotations

import http.client
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from greenbean.viewer import _resolve_under, start_viewer


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
