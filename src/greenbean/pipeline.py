"""The run pipeline — one idempotent pass of the full local loop.

``run_once`` is the shared **pipeline body** the architecture calls for: sync
the plan to current HEAD, resolve what changed to a candidate set of docs,
generate them, publish to the output dir, and record the SHA we processed.
``greenbean run`` calls it once; ``greenbean watch`` calls it on a timer; a
future SaaS worker would call it after picking up a job. Same code, different
triggers.

Idempotency lives here: if HEAD still equals the last processed SHA, the run
is a no-op. On the first run for a repo (no recorded SHA) we generate the whole
plan; afterwards we diff ``last_synced_sha → HEAD`` and regenerate only the
docs whose sources changed (``affected_docs``). The recorded SHA only advances
after a successful pass, so a crash mid-generation is retried, not skipped.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from greenbean.agent.generator import GenerationResult
from greenbean.core.git import GitService
from greenbean.core.planning import Document
from greenbean.planning import DefaultPlanner, SqliteDocStore
from greenbean.publish import atomic_write_text, output_root_for, safe_output_path


class DocGenerator(Protocol):
    """The slice of ``Generator`` the pipeline needs.

    A protocol so tests can inject a stub that returns canned content without
    standing up an LLM client — agent loops are tested with fixtures, not live
    model calls.
    """

    async def generate(
        self, doc: Document, source_paths: Sequence[str]
    ) -> GenerationResult: ...


# Builds a generator bound to a specific working copy. Injected so the CLI can
# wire in a real ``Generator`` while tests pass a stub.
GeneratorFactory = Callable[[Path], DocGenerator]


@dataclass(frozen=True, slots=True)
class RunResult:
    before_sha: str | None
    after_sha: str
    skipped: bool  # HEAD unchanged since last run — nothing done
    full: bool  # whole-plan generation (first run) vs. incremental diff
    generated: tuple[str, ...]  # doc paths generated this pass
    output_root: Path


async def run_once(
    repo_path: Path,
    state_path: Path,
    *,
    git: GitService,
    planner: DefaultPlanner,
    make_generator: GeneratorFactory,
    dry_run: bool = False,
    force_full: bool = False,
    log: Callable[[str], None] = print,
) -> RunResult:
    """Run one pass of the loop over the working copy at ``repo_path``.

    Operates on the working copy's current HEAD as-is — it does not fetch or
    move the tree (that's ``watch``'s job). ``force_full`` regenerates the
    whole plan regardless of the diff; ``dry_run`` generates but writes
    nothing and leaves ``last_synced_sha`` untouched.
    """
    head = await git.current_sha(repo_path)
    output_root = await output_root_for(repo_path, git)

    state_path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteDocStore(state_path) as store:
        last = store.get_last_synced_sha()
        if last == head and not force_full:
            log(f"up to date at {head[:8]} — nothing to do")
            return RunResult(
                before_sha=last,
                after_sha=head,
                skipped=True,
                full=False,
                generated=(),
                output_root=output_root,
            )

        # Refresh the plan against current HEAD before resolving the diff, so
        # newly added files have source rows and removed docs are pruned.
        planned = await planner.plan_with_sources(repo_path)
        store.replace_plan(planned, planned_sha=head)

        if last is None or force_full:
            docs: list[Document] = list(store.list_documents())
            full = True
        else:
            changed = await git.changed_files(repo_path, last, head)
            docs = list(store.affected_docs(changed))
            full = False
            log(f"{len(changed)} file(s) changed -> {len(docs)} doc(s) affected")

        generator = make_generator(repo_path)
        generated: list[str] = []
        for doc in docs:
            source_paths = store.source_paths_for(doc.id)
            log(f"generating {doc.path_in_repo} ...")
            result = await generator.generate(doc, source_paths)
            if dry_run:
                log(f"--- {doc.path_in_repo} ---\n{result.content}")
            else:
                out_path = safe_output_path(output_root, doc.path_in_repo)
                atomic_write_text(out_path, result.content)
                content_hash = hashlib.sha256(result.content.encode()).hexdigest()
                store.record_generation(
                    doc.path_in_repo,
                    content_hash=content_hash,
                    metadata={
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "tool_calls": result.tool_calls,
                    },
                )
            generated.append(doc.path_in_repo)

        # Advance the cursor even when nothing was affected: HEAD moved, we've
        # accounted for it, and we don't want to re-diff this range next tick.
        # Skip on dry runs so they never mutate sync state.
        if not dry_run:
            store.set_last_synced_sha(head)

        return RunResult(
            before_sha=last,
            after_sha=head,
            skipped=False,
            full=full,
            generated=tuple(generated),
            output_root=output_root,
        )
