"""Planning layer value types and errors.

Shared by DefaultPlanner and SqliteDocStore. No protocols here — a second
backend doesn't exist yet; extract the interface when it does.

holds the shared value types (DocSpec, Document, etc.) used by both sqlite_store.py and default_planner.py — that separation is load-bearing   
since both modules import from it and neither should import from the other
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class DocSpec:
    path_in_repo: str
    doc_type: str
    scope: str
    spec: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.scope and not self.scope.endswith("/"):
            raise ValueError(f"non-root scope must end with '/': got {self.scope!r}")


@dataclass(frozen=True, slots=True)
class Document:
    id: str
    path_in_repo: str
    doc_type: str
    scope: str
    spec: Mapping[str, Any]
    last_planned_sha: str
    current_content_hash: str | None
    last_generated_at: datetime | None
    generation_metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PlannedDoc:
    spec: DocSpec
    source_paths: Sequence[str]


@dataclass(frozen=True, slots=True)
class ReplacePlanResult:
    added: int
    updated: int
    pruned: int
    preserved: tuple[str, ...]


class PlanningError(RuntimeError): ...
