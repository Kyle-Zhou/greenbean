"""Tests for SqliteDocStore against an in-memory SQLite database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from greenbean.core.planning import DocSpec, PlannedDoc
from greenbean.planning.sqlite_store import SqliteDocStore


def _store() -> SqliteDocStore:
    return SqliteDocStore(Path(":memory:"))


def _make_planned(
    path: str,
    doc_type: str = "readme",
    scope: str = "",
    sources: list[str] | None = None,
) -> PlannedDoc:
    return PlannedDoc(
        spec=DocSpec(path_in_repo=path, doc_type=doc_type, scope=scope, spec={}),
        source_paths=sources or [],
    )


SHA_A = "a" * 40
SHA_B = "b" * 40


# ----- replace_plan: insert path ---------------------------------------------


def test_replace_plan_insert_adds_rows() -> None:
    with _store() as store:
        planned = [
            _make_planned("README.md", sources=["src/a.py"]),
            _make_planned("docs/architecture.md"),
        ]
        result = store.replace_plan(planned, SHA_A)
        assert result.added == 2
        assert result.updated == 0
        assert result.pruned == 0
        assert result.preserved == ()
        assert len(store.list_documents()) == 2


def test_replace_plan_insert_stores_sources() -> None:
    with _store() as store:
        planned = [_make_planned("README.md", sources=["src/a.py", "src/b.py"])]
        store.replace_plan(planned, SHA_A)
        doc = store.get_document("README.md")
        assert doc is not None
        assert store.source_count(doc.id) == 2


def test_replace_plan_new_doc_has_null_generation_fields() -> None:
    with _store() as store:
        store.replace_plan([_make_planned("README.md")], SHA_A)
        doc = store.get_document("README.md")
        assert doc is not None
        assert doc.current_content_hash is None
        assert doc.last_generated_at is None
        assert dict(doc.generation_metadata) == {}


# ----- replace_plan: update path ---------------------------------------------


def test_replace_plan_update_same_plan_twice_no_churn() -> None:
    with _store() as store:
        planned = [_make_planned("README.md", sources=["src/a.py"])]
        store.replace_plan(planned, SHA_A)
        result = store.replace_plan(planned, SHA_B)
        assert result.added == 0
        assert result.updated == 1
        assert result.pruned == 0
        assert len(store.list_documents()) == 1


def test_replace_plan_update_refreshes_sha() -> None:
    with _store() as store:
        planned = [_make_planned("README.md")]
        store.replace_plan(planned, SHA_A)
        store.replace_plan(planned, SHA_B)
        doc = store.get_document("README.md")
        assert doc is not None
        assert doc.last_planned_sha == SHA_B


def test_replace_plan_update_replaces_sources() -> None:
    with _store() as store:
        store.replace_plan([_make_planned("README.md", sources=["old.py"])], SHA_A)
        store.replace_plan([_make_planned("README.md", sources=["new.py"])], SHA_B)
        doc = store.get_document("README.md")
        assert doc is not None
        assert store.source_count(doc.id) == 1
        rows = store._con.execute(
            "SELECT source_path FROM document_sources WHERE document_id = ?", (doc.id,)
        ).fetchall()
        assert rows[0]["source_path"] == "new.py"


# ----- replace_plan: orphan pruning ------------------------------------------


def test_replace_plan_prunes_ungenerated_orphan() -> None:
    with _store() as store:
        store.replace_plan(
            [_make_planned("README.md"), _make_planned("docs/architecture.md")], SHA_A
        )
        result = store.replace_plan([_make_planned("README.md")], SHA_B)
        assert result.pruned == 1
        assert store.get_document("docs/architecture.md") is None
        assert store.get_document("README.md") is not None


# ----- replace_plan: preserve generated orphans ------------------------------


def test_replace_plan_preserves_generated_doc() -> None:
    with _store() as store:
        store.replace_plan(
            [_make_planned("README.md"), _make_planned("docs/architecture.md")], SHA_A
        )
        store.record_generation(
            "docs/architecture.md", content_hash="abc123", metadata={}
        )
        result = store.replace_plan([_make_planned("README.md")], SHA_B)
        assert result.pruned == 0
        assert "docs/architecture.md" in result.preserved
        assert store.get_document("docs/architecture.md") is not None


# ----- record_generation -----------------------------------------------------


def test_record_generation_writes_fields() -> None:
    with _store() as store:
        store.replace_plan([_make_planned("README.md")], SHA_A)
        store.record_generation(
            "README.md", content_hash="deadbeef", metadata={"tokens": 100}
        )
        doc = store.get_document("README.md")
        assert doc is not None
        assert doc.current_content_hash == "deadbeef"
        assert doc.last_generated_at is not None
        assert doc.generation_metadata["tokens"] == 100


# ----- affected_docs ---------------------------------------------------------


def test_affected_docs_exact_source_match() -> None:
    with _store() as store:
        store.replace_plan(
            [_make_planned("README.md", sources=["src/a.py", "src/b.py"])], SHA_A
        )
        docs = store.affected_docs(["src/a.py"])
        assert len(docs) == 1
        assert docs[0].path_in_repo == "README.md"


def test_affected_docs_scope_prefix_match() -> None:
    with _store() as store:
        store.replace_plan(
            [_make_planned("src/greenbean/README.md", scope="src/greenbean/", sources=[])],
            SHA_A,
        )
        docs = store.affected_docs(["src/greenbean/cli.py"])
        assert len(docs) == 1
        assert docs[0].path_in_repo == "src/greenbean/README.md"


def test_affected_docs_root_scope_matches_everything() -> None:
    with _store() as store:
        store.replace_plan(
            [_make_planned("README.md", scope="", sources=[])], SHA_A
        )
        docs = store.affected_docs(["anywhere/deep/file.py"])
        assert len(docs) == 1
        assert docs[0].path_in_repo == "README.md"


def test_affected_docs_trailing_slash_prevents_prefix_collision() -> None:
    with _store() as store:
        store.replace_plan(
            [
                _make_planned("src/foo/README.md", scope="src/foo/", sources=[]),
            ],
            SHA_A,
        )
        docs = store.affected_docs(["src/foobar/x.py"])
        assert not any(d.path_in_repo == "src/foo/README.md" for d in docs)


def test_affected_docs_deduplicates_results() -> None:
    with _store() as store:
        store.replace_plan(
            [_make_planned("README.md", scope="", sources=["src/a.py"])], SHA_A
        )
        docs = store.affected_docs(["src/a.py"])
        ids = [d.id for d in docs]
        assert len(ids) == len(set(ids))


def test_affected_docs_empty_paths_returns_empty() -> None:
    with _store() as store:
        store.replace_plan([_make_planned("README.md")], SHA_A)
        assert store.affected_docs([]) == ()


# ----- context manager -------------------------------------------------------


def test_context_manager_closes_connection() -> None:
    with SqliteDocStore(Path(":memory:")) as store:
        store.replace_plan([_make_planned("README.md")], SHA_A)
    with pytest.raises(sqlite3.ProgrammingError):
        store.list_documents()
