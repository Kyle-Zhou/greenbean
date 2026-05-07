"""SQLite-backed document store for the planning layer.

Single-file SQLite under ~/.greenbean/ in CLI mode. Sync (sqlite3 is sync;
ms-scale operations don't justify asyncio.to_thread).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from greenbean.core.planning import (
    Document,
    PlannedDoc,
    ReplacePlanResult,
)

_DDL = """
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  path_in_repo TEXT UNIQUE NOT NULL,
  doc_type TEXT NOT NULL,
  scope TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  last_planned_sha TEXT NOT NULL,
  current_content_hash TEXT,
  last_generated_at TEXT,
  generation_metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS document_sources (
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  source_path TEXT NOT NULL,
  PRIMARY KEY (document_id, source_path)
);
CREATE INDEX IF NOT EXISTS idx_doc_sources_path ON document_sources(source_path);
"""


def _row_to_document(row: sqlite3.Row) -> Document:
    last_gen: datetime | None = None
    if row["last_generated_at"] is not None:
        last_gen = datetime.fromisoformat(row["last_generated_at"])
    return Document(
        id=row["id"],
        path_in_repo=row["path_in_repo"],
        doc_type=row["doc_type"],
        scope=row["scope"],
        spec=json.loads(row["spec_json"]),
        last_planned_sha=row["last_planned_sha"],
        current_content_hash=row["current_content_hash"],
        last_generated_at=last_gen,
        generation_metadata=json.loads(row["generation_metadata_json"]),
    )


class SqliteDocStore:
    def __init__(self, path: Path) -> None:
        self._con = sqlite3.connect(str(path))
        self._con.row_factory = sqlite3.Row
        self._con.executescript("PRAGMA foreign_keys = ON; PRAGMA journal_mode = WAL;")
        self._con.executescript(_DDL)
        self._con.commit()

    # ----- plan management ---------------------------------------------------

    def replace_plan(
        self, planned: Sequence[PlannedDoc], planned_sha: str
    ) -> ReplacePlanResult:
        planned_paths = {pd.spec.path_in_repo for pd in planned}

        with self._con:
            existing_rows = self._con.execute(
                "SELECT id, path_in_repo, last_generated_at FROM documents"
            ).fetchall()

            existing_by_path: dict[str, sqlite3.Row] = {
                r["path_in_repo"]: r for r in existing_rows
            }

            added = 0
            updated = 0

            for pd in planned:
                path = pd.spec.path_in_repo
                spec_json = json.dumps(dict(pd.spec.spec))
                sources = list(pd.source_paths)

                if path in existing_by_path:
                    doc_id = existing_by_path[path]["id"]
                    self._con.execute(
                        """
                        UPDATE documents
                           SET doc_type = ?, scope = ?, spec_json = ?,
                               last_planned_sha = ?
                         WHERE id = ?
                        """,
                        (pd.spec.doc_type, pd.spec.scope, spec_json, planned_sha, doc_id),
                    )
                    self._con.execute(
                        "DELETE FROM document_sources WHERE document_id = ?", (doc_id,)
                    )
                    updated += 1
                else:
                    doc_id = uuid.uuid4().hex
                    self._con.execute(
                        """
                        INSERT INTO documents
                          (id, path_in_repo, doc_type, scope, spec_json,
                           last_planned_sha, current_content_hash,
                           last_generated_at, generation_metadata_json)
                        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, '{}')
                        """,
                        (
                            doc_id,
                            path,
                            pd.spec.doc_type,
                            pd.spec.scope,
                            spec_json,
                            planned_sha,
                        ),
                    )
                    added += 1

                self._con.executemany(
                    "INSERT INTO document_sources (document_id, source_path) VALUES (?, ?)",
                    [(doc_id, s) for s in sources],
                )

            pruned = 0
            preserved: list[str] = []
            for row in existing_rows:
                if row["path_in_repo"] in planned_paths:
                    continue
                if row["last_generated_at"] is None:
                    self._con.execute(
                        "DELETE FROM documents WHERE id = ?", (row["id"],)
                    )
                    pruned += 1
                else:
                    preserved.append(row["path_in_repo"])

        return ReplacePlanResult(
            added=added,
            updated=updated,
            pruned=pruned,
            preserved=tuple(sorted(preserved)),
        )

    # ----- queries -----------------------------------------------------------

    def list_documents(self) -> Sequence[Document]:
        rows = self._con.execute(
            "SELECT * FROM documents ORDER BY path_in_repo"
        ).fetchall()
        return tuple(_row_to_document(r) for r in rows)

    def get_document(self, path_in_repo: str) -> Document | None:
        row = self._con.execute(
            "SELECT * FROM documents WHERE path_in_repo = ?", (path_in_repo,)
        ).fetchone()
        return _row_to_document(row) if row else None

    def source_count(self, doc_id: str) -> int:
        row = self._con.execute(
            "SELECT COUNT(*) FROM document_sources WHERE document_id = ?", (doc_id,)
        ).fetchone()
        return int(row[0])

    def affected_docs(self, changed_paths: Sequence[str]) -> Sequence[Document]:
        if not changed_paths:
            return ()

        seen: set[str] = set()
        results: list[Document] = []

        for path in changed_paths:
            rows = self._con.execute(
                """
                SELECT DISTINCT d.*
                  FROM documents d
                  JOIN document_sources ds ON ds.document_id = d.id
                 WHERE ds.source_path = ?
                """,
                (path,),
            ).fetchall()
            for row in rows:
                if row["id"] not in seen:
                    seen.add(row["id"])
                    results.append(_row_to_document(row))

            scope_rows = self._con.execute(
                """
                SELECT * FROM documents
                 WHERE scope = ''
                    OR (scope != '' AND ? LIKE scope || '%')
                """,
                (path,),
            ).fetchall()
            for row in scope_rows:
                if row["id"] not in seen:
                    seen.add(row["id"])
                    results.append(_row_to_document(row))

        return tuple(results)

    # ----- generation writes -------------------------------------------------

    def record_generation(
        self,
        path_in_repo: str,
        *,
        content_hash: str,
        metadata: Mapping[str, Any],
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self._con.execute(
            """
            UPDATE documents
               SET current_content_hash = ?,
                   last_generated_at = ?,
                   generation_metadata_json = ?
             WHERE path_in_repo = ?
            """,
            (content_hash, now, json.dumps(dict(metadata)), path_in_repo),
        )
        self._con.commit()

    # ----- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> SqliteDocStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
