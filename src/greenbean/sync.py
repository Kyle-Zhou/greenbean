"""The pipeline body shared by ``greenbean run`` and ``greenbean watch``.

A single repo, single working copy, single SQLite state file. The function
``run_pipeline`` does what one tick of the automation loop must do:

    re-plan -> diff-resolve -> generate affected docs -> record sync sha

Idempotency lives in two places. First, ``store.get_last_synced_sha()`` is
the gate: when HEAD matches it and the plan has no docs missing a prior
generation, the function no-ops. Second, when HEAD has moved, regeneration
is restricted to docs whose source files appear in
``git diff from_sha..to_sha`` — everything else keeps its previous output.
Together these turn the "always-on watch loop" into "fire-and-forget; it
only burns LLM tokens when something actually changed."

This module owns no resources of its own. The CLI builds the planner,
generator, git service, and store; passes them in. That keeps the pipeline
testable with stubs and unaware of how it's triggered (in-process timer in
the CLI today; a server-side queue worker in future SaaS).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from greenbean.agent import Generator
from greenbean.core.git import GitService
from greenbean.core.planning import Document, PlannedDoc
from greenbean.io import _atomic_write_text, _safe_output_path
from greenbean.planning import DefaultPlanner, SqliteDocStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunSummary:
    """What one ``run_pipeline`` invocation actually did.

    ``no_op`` means HEAD was unchanged and every doc already had a prior
    generation — the function returned without touching the LLM or the
    filesystem. ``regenerated`` is the ordered list of doc paths whose
    output was rewritten this tick.
    """

    from_sha: str | None
    to_sha: str
    no_op: bool
    regenerated: tuple[str, ...] = field(default_factory=tuple)
    added: int = 0
    updated: int = 0
    pruned: int = 0


async def _refresh_plan(
    repo_path: Path,
    planner: DefaultPlanner,
    store: SqliteDocStore,
    planned_sha: str,
) -> tuple[int, int, int]:
    """Re-run the mechanical planner; replace the stored plan; return deltas.

    The planner is deterministic over the working tree, so this is cheap
    even on every tick — when nothing moves the result is ``(0, N, 0)``
    (every existing doc gets a no-op refresh) and no doc is regenerated
    downstream because nothing's flagged.
    """
    specs = await planner.initial_plan(repo_path)
    planned: list[PlannedDoc] = []
    for spec in specs:
        sources = await planner.source_files_for(repo_path, spec)
        planned.append(PlannedDoc(spec=spec, source_paths=list(sources)))
    result = store.replace_plan(planned, planned_sha=planned_sha)
    return result.added, result.updated, result.pruned


def _select_regen_targets(
    store: SqliteDocStore,
    changed_paths: Sequence[str],
) -> list[Document]:
    """Docs to regenerate this tick: union of (a) never-generated and (b) diff-affected.

    ``ungenerated_documents`` covers both the initial run (everything is
    ungenerated) and recovery from partial prior failures. ``affected_docs``
    covers the steady-state case where HEAD moved and we need to redo only
    the docs whose sources or scope intersect the diff. Deduplicated by
    document id so a doc that's both new *and* in the diff is generated
    once, not twice.
    """
    seen: set[str] = set()
    targets: list[Document] = []
    for doc in store.ungenerated_documents():
        if doc.id not in seen:
            seen.add(doc.id)
            targets.append(doc)
    if changed_paths:
        for doc in store.affected_docs(changed_paths):
            if doc.id not in seen:
                seen.add(doc.id)
                targets.append(doc)
    return targets


async def run_pipeline(
    repo_path: Path,
    *,
    store: SqliteDocStore,
    planner: DefaultPlanner,
    generator: Generator,
    git: GitService,
    output_root: Path,
    model: str,
) -> RunSummary:
    """One full pipeline tick. Returns what changed.

    Steps: read HEAD, re-plan, decide which docs to regenerate (any that
    never were, plus any whose sources sit in the ``from_sha..to_sha``
    diff), regenerate them, write atomically to ``output_root``, record
    each generation's metadata, then record the new sync sha. Skips
    everything past the plan refresh when HEAD is unchanged and no doc
    is missing a prior generation.
    """
    to_sha = await git.current_sha(repo_path)
    from_sha = store.get_last_synced_sha()
    logger.info(
        "pipeline: %s -> %s",
        from_sha[:8] if from_sha else "(initial)",
        to_sha[:8],
    )

    added, updated, pruned = await _refresh_plan(repo_path, planner, store, to_sha)
    logger.info(
        "plan refresh: %d added, %d updated, %d pruned", added, updated, pruned
    )

    if from_sha is not None and from_sha != to_sha:
        changed_paths = await git.diff(repo_path, from_sha, to_sha)
        logger.info("diff %s..%s: %d path(s) changed", from_sha[:8], to_sha[:8], len(changed_paths))
    else:
        changed_paths = ()

    targets = _select_regen_targets(store, changed_paths)

    if not targets:
        # HEAD unchanged (or matched) and nothing left ungenerated — true no-op.
        # We still record the sync sha so the first-ever ``run`` against a
        # fully-generated plan stops scanning the diff window on subsequent
        # ticks.
        logger.info("no docs to regenerate")
        store.record_sync(to_sha)
        return RunSummary(
            from_sha=from_sha,
            to_sha=to_sha,
            no_op=True,
            added=added,
            updated=updated,
            pruned=pruned,
        )

    total = len(targets)
    logger.info("regenerating %d doc(s)", total)
    regenerated: list[str] = []
    for i, doc in enumerate(targets, start=1):
        source_paths = store.source_paths_for(doc.id)
        logger.info("[%d/%d] generating %s", i, total, doc.path_in_repo)
        started_at = time.monotonic()
        result = await generator.generate(doc, source_paths)
        elapsed = time.monotonic() - started_at
        logger.info(
            "[%d/%d] done %s in %.1fs (%d tool calls, %d+%d tokens)",
            i,
            total,
            doc.path_in_repo,
            elapsed,
            result.tool_calls,
            result.input_tokens,
            result.output_tokens,
        )
        out_path = _safe_output_path(output_root, doc.path_in_repo)
        _atomic_write_text(out_path, result.content)
        content_hash = hashlib.sha256(result.content.encode()).hexdigest()
        store.record_generation(
            doc.path_in_repo,
            content_hash=content_hash,
            metadata={
                "model": model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "tool_calls": result.tool_calls,
            },
        )
        regenerated.append(doc.path_in_repo)

    store.record_sync(to_sha)

    return RunSummary(
        from_sha=from_sha,
        to_sha=to_sha,
        no_op=False,
        regenerated=tuple(regenerated),
        added=added,
        updated=updated,
        pruned=pruned,
    )
