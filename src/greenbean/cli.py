"""greenbean CLI — single-tenant entry point.

The local-first flow: clone (or point at an existing working tree), plan,
generate. Generated docs land in ``~/.greenbean/output/<host>/<owner>/<name>/``
and never in the source repo — greenbean does not publish docs back to the
customer's repo. ``run`` and ``watch`` close the automation loop on top of
the same pipeline body (see ``greenbean.sync.run_pipeline``).

    greenbean clone <url> [--token TOKEN] [--branch BRANCH]
                          [--dest DIR] [--depth N]
    greenbean plan init <repo-path> [--state PATH]
    greenbean plan list <repo-path> [--state PATH]
    greenbean generate <repo-path> [--state PATH] [--doc DOC_PATH]
                                   [--dry-run] [--model MODEL]
    greenbean run <repo-path> [--state PATH] [--model MODEL] [--pull]
    greenbean watch <repo-path> [--interval 5m] [--state PATH] [--model MODEL]

The token comes from ``--token`` or ``$GITHUB_TOKEN``. If no token is
supplied, the bare URL is used — fine for public repos, ``file://`` URLs
(handy for local testing), or SSH when the user's ssh-agent is set up.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
import time
from pathlib import Path

from greenbean.agent import Generator
from greenbean.connectors.github import url as github_url
from greenbean.connectors.github.token_credentials import TokenCredentials
from greenbean.core.connectors import RepoRef
from greenbean.core.git import GitError, GitService
from greenbean.core.planning import PlannedDoc
from greenbean.io import _atomic_write_text, _safe_output_path
from greenbean.planning import DefaultPlanner, SqliteDocStore
from greenbean.settings import Settings
from greenbean.sync import RunSummary, run_pipeline
from greenbean.tools.working_copy import WorkingCopyTools

# Re-exported so existing tests (and future callers) can keep importing the
# helpers from ``greenbean.cli``. The implementations live in ``greenbean.io``.
__all__ = ["_atomic_write_text", "_safe_output_path", "main"]

logger = logging.getLogger("greenbean")

DEFAULT_CACHE_ROOT = Path.home() / ".greenbean" / "cache"
DEFAULT_OUTPUT_ROOT = Path.home() / ".greenbean" / "output"
_DEFAULT_STATE_NAME = ".greenbean/state.sqlite"


def _configure_logging() -> None:
    """Install a stderr handler for ``greenbean.*`` loggers at INFO.

    Stdout stays the "primary result" channel — clone summaries, plan
    listings, run summaries. Stderr carries diagnostics — per-tool-call
    traces, per-doc progress, warnings. The split lets a user pipe the
    primary result somewhere (``greenbean plan list ... | jq``) without
    progress chatter polluting it. Idempotent for repeat ``main`` calls in
    the same process (e.g. test invocations).
    """
    root = logging.getLogger("greenbean")
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # ``propagate = True`` is kept on purpose: pytest's ``caplog`` fixture
    # attaches at the root logger, and propagating greenbean's logs there
    # makes them captureable in tests without per-test wiring. Root has no
    # handlers by default, so there's no double-emit in normal use.


def _local_namespace(repo_path: Path) -> str:
    """Name unique per working-copy path, derived from basename + path hash.

    Two repos with the same directory name (a common case — every test
    fixture is "repo", every dev tree is "myproject") would otherwise
    collide under ``_local/`` and silently overwrite each other's docs.
    """
    resolved = repo_path.resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:8]
    return f"{resolved.name}-{digest}"


async def _output_root_for(repo_path: Path, git: GitService) -> Path:
    """Resolve where generated docs for ``repo_path`` should land.

    Reads ``origin``'s URL and parses it as a GitHub repo. Falls back to
    ``_local/<basename>-<path-hash>`` when the working copy has no origin
    or its URL isn't a recognizable GitHub URL — keeps local fixtures and
    file:// clones working, while avoiding collisions between different
    repos that happen to share a directory name.
    """
    try:
        remote = await git.remote_url(repo_path)
    except GitError:
        remote = ""
    if remote:
        parsed = github_url.try_parse(remote)
        if parsed is not None:
            return DEFAULT_OUTPUT_ROOT / "github.com" / parsed.owner / parsed.name
    return DEFAULT_OUTPUT_ROOT / "_local" / _local_namespace(repo_path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="greenbean",
        description="Keep a repository's docs continuously in sync with its code.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    clone = sub.add_parser("clone", help="Clone a repository into the local cache.")
    clone.add_argument("url", help="Repository URL (https / ssh / file://).")
    clone.add_argument(
        "--token",
        default=None,
        help="GitHub PAT; falls back to $GITHUB_TOKEN. Required only for private repos.",
    )
    clone.add_argument(
        "--branch",
        default=None,
        help="Branch to clone (default: remote's default branch).",
    )
    clone.add_argument(
        "--dest",
        default=None,
        help="Destination directory (default: ~/.greenbean/cache/<host>/<owner>/<name>).",
    )
    clone.add_argument(
        "--depth",
        type=int,
        default=50,
        help="Shallow clone depth (default: 50; pass 0 for a full clone).",
    )

    plan = sub.add_parser("plan", help="Manage the doc plan for a repository.")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)

    plan_init = plan_sub.add_parser("init", help="Initialise or refresh the doc plan.")
    plan_init.add_argument("repo_path", help="Path to a git working copy.")
    plan_init.add_argument(
        "--state",
        default=None,
        help="SQLite state file (default: <repo-path>/.greenbean/state.sqlite).",
    )

    plan_list = plan_sub.add_parser("list", help="List planned documents.")
    plan_list.add_argument("repo_path", help="Path to a git working copy.")
    plan_list.add_argument(
        "--state",
        default=None,
        help="SQLite state file (default: <repo-path>/.greenbean/state.sqlite).",
    )

    gen = sub.add_parser("generate", help="Generate documentation for planned docs.")
    gen.add_argument("repo_path", help="Path to a git working copy.")
    gen.add_argument(
        "--state",
        default=None,
        help="SQLite state file (default: <repo-path>/.greenbean/state.sqlite).",
    )
    gen.add_argument(
        "--doc",
        default=None,
        metavar="DOC_PATH",
        help="Generate only this document (e.g. README.md). Default: all docs.",
    )
    gen.add_argument(
        "--dry-run",
        action="store_true",
        help="Print generated content to stdout instead of writing files.",
    )
    gen.add_argument(
        "--model",
        default=None,
        help="Anthropic model override (default: $GREENBEAN_GENERATOR_MODEL or claude-sonnet-4-6).",
    )

    run_cmd = sub.add_parser(
        "run",
        help="Run the full pipeline once (plan refresh + diff-driven regeneration).",
    )
    run_cmd.add_argument("repo_path", help="Path to a git working copy.")
    run_cmd.add_argument(
        "--state",
        default=None,
        help="SQLite state file (default: <repo-path>/.greenbean/state.sqlite).",
    )
    run_cmd.add_argument(
        "--model",
        default=None,
        help="Anthropic model override (default: $GREENBEAN_GENERATOR_MODEL or claude-sonnet-4-6).",
    )
    run_cmd.add_argument(
        "--pull",
        action="store_true",
        help=(
            "Before running, git fetch and fast-forward HEAD to its upstream "
            "(skipped if local has unique commits or the working tree is dirty)."
        ),
    )

    watch_cmd = sub.add_parser(
        "watch",
        help="Poll a working copy on an interval; run the pipeline when HEAD moves.",
    )
    watch_cmd.add_argument("repo_path", help="Path to a git working copy.")
    watch_cmd.add_argument(
        "--interval",
        default="5m",
        help="Poll interval (e.g. 30s, 5m, 1h). Default: 5m.",
    )
    watch_cmd.add_argument(
        "--state",
        default=None,
        help="SQLite state file (default: <repo-path>/.greenbean/state.sqlite).",
    )
    watch_cmd.add_argument(
        "--model",
        default=None,
        help="Anthropic model override (default: $GREENBEAN_GENERATOR_MODEL or claude-sonnet-4-6).",
    )

    return parser


async def _clone(args: argparse.Namespace) -> int:
    git = GitService()
    parsed = github_url.try_parse(args.url)
    token = args.token if args.token is not None else os.environ.get("GITHUB_TOKEN")

    if parsed is not None:
        owner, name = parsed.owner, parsed.name
        default_dest = DEFAULT_CACHE_ROOT / "github.com" / owner / name
        if token:
            ref = RepoRef(
                id="cli", source_type="github", external_id=f"{owner}/{name}"
            )
            creds = TokenCredentials(token=token, git=git)
            clone_url = await creds.get_clone_url(ref)
        else:
            clone_url = parsed.https_url
        display_name = f"{owner}/{name}"
    else:
        # Generic URL — fine for ``file://`` testing and public mirrors.
        clone_url = args.url
        leaf = args.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") or "repo"
        default_dest = DEFAULT_CACHE_ROOT / "_other" / leaf
        display_name = leaf

    dest = Path(args.dest).expanduser() if args.dest else default_dest
    if dest.exists():
        print(f"error: destination already exists: {dest}", file=sys.stderr)
        return 2
    dest.parent.mkdir(parents=True, exist_ok=True)

    depth: int | None = args.depth if args.depth and args.depth > 0 else None

    try:
        await git.clone(clone_url, dest, depth=depth, branch=args.branch)
        head = await git.current_sha(dest)
    except GitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"cloned {display_name} -> {dest}")
    print(f"HEAD: {head}")
    return 0


async def _plan_init(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo_path).expanduser().resolve()
    state_path = Path(args.state).expanduser().resolve() if args.state else repo_path / _DEFAULT_STATE_NAME

    git = GitService()
    try:
        sha = await git.current_sha(repo_path)
    except GitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    planner = DefaultPlanner()
    specs = await planner.initial_plan(repo_path)
    planned: list[PlannedDoc] = []
    for spec in specs:
        sources = await planner.source_files_for(repo_path, spec)
        planned.append(PlannedDoc(spec=spec, source_paths=list(sources)))

    state_path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteDocStore(state_path) as store:
        result = store.replace_plan(planned, planned_sha=sha)

    print(f"added {result.added}, updated {result.updated}, pruned {result.pruned}", end="")
    if result.preserved:
        print(f", preserved {list(result.preserved)}", end="")
    print()
    return 0


def _plan_list(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo_path).expanduser().resolve()
    state_path = Path(args.state).expanduser().resolve() if args.state else repo_path / _DEFAULT_STATE_NAME

    if not state_path.exists():
        print(f"error: state file not found: {state_path}", file=sys.stderr)
        print("hint: run `greenbean plan init` first", file=sys.stderr)
        return 1

    with SqliteDocStore(state_path) as store:
        docs = store.list_documents()
        for doc in docs:
            n_sources = store.source_count(doc.id)
            generated = "yes" if doc.last_generated_at is not None else "no"
            sha_short = doc.last_planned_sha[:8]
            scope_display = doc.scope if doc.scope else '""'
            print(
                f"{doc.path_in_repo}  type={doc.doc_type}  scope={scope_display}"
                f"  sources={n_sources}  generated={generated}  sha={sha_short}"
            )
    return 0


async def _generate(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo_path).expanduser().resolve()
    state_path = Path(args.state).expanduser().resolve() if args.state else repo_path / _DEFAULT_STATE_NAME

    if not state_path.exists():
        print(f"error: state file not found: {state_path}", file=sys.stderr)
        print("hint: run `greenbean plan init` first", file=sys.stderr)
        return 1

    settings = Settings.from_env()
    model = args.model or settings.generator_model
    git = GitService()
    tools = WorkingCopyTools(repo_path)
    generator = Generator(tools, model=model, client=settings.make_generator_client())

    output_root = await _output_root_for(repo_path, git)

    with SqliteDocStore(state_path) as store:
        docs = store.list_documents()
        if args.doc:
            docs = [d for d in docs if d.path_in_repo == args.doc]
            if not docs:
                print(f"error: no planned doc with path {args.doc!r}", file=sys.stderr)
                return 1

        if not docs:
            print("no documents in plan", file=sys.stderr)
            return 1

        total = len(docs)
        for i, doc in enumerate(docs, start=1):
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

            if args.dry_run:
                print(f"\n--- {doc.path_in_repo} ---")
                print(result.content)
            else:
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
                logger.info("  wrote %s", out_path)

    return 0


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600}


def _parse_interval(text: str) -> float:
    """Parse durations like ``30s``, ``5m``, ``1h``, or a bare integer (seconds).

    Kept deliberately simple — the polling cadence is human-set and one
    decimal place isn't a feature. Anything fancier (``1h30m``, fractional
    units) is out of scope until someone actually asks.
    """
    text = text.strip().lower()
    if not text:
        raise ValueError("interval must not be empty")
    if text[-1] in _DURATION_UNITS:
        value, unit = text[:-1], text[-1]
        multiplier = _DURATION_UNITS[unit]
    else:
        value, multiplier = text, 1
    try:
        n = int(value)
    except ValueError as e:
        raise ValueError(f"could not parse interval {text!r}") from e
    if n <= 0:
        raise ValueError(f"interval must be positive, got {text!r}")
    return float(n * multiplier)


def _summary_line(summary: RunSummary) -> str:
    """Render a one-line description of a pipeline tick for stdout."""
    short = summary.to_sha[:8]
    if summary.no_op:
        return f"up to date at {short}"
    prev = summary.from_sha[:8] if summary.from_sha else "(initial)"
    return (
        f"synced {prev} -> {short}; regenerated {len(summary.regenerated)} doc(s): "
        + ", ".join(summary.regenerated)
    )


async def _advance_to_upstream(
    git: GitService, repo_path: Path
) -> str | None:
    """Fast-forward HEAD to its upstream's HEAD if safe; return the new sha.

    Three guards, in order:

    1. **An upstream is configured.** ``git rev-parse @{u}`` fails for repos
       with no tracking branch (local-only fixtures, detached HEAD). We
       treat that as a silent no-op — it's the normal state for non-cloned
       working copies.
    2. **The advance is a true fast-forward.** If local has commits the
       upstream doesn't, ``current`` won't be an ancestor of ``target`` —
       resetting would destroy that work. Skip with a warning.
    3. **The working tree is clean.** If the user has uncommitted edits in
       the working copy, ``reset --hard`` would obliterate them. Skip with
       a warning.

    Callers should have already ``git fetch``'d so the ``@{u}`` ref is fresh.
    """
    try:
        target = await git.rev_parse(repo_path, "@{u}")
    except GitError:
        return None
    current = await git.current_sha(repo_path)
    if target == current:
        return None
    if not await git.is_ancestor(repo_path, current, target):
        logger.warning(
            "local HEAD %s has commits not on upstream %s; skipping fast-forward",
            current[:8],
            target[:8],
        )
        return None
    if not await git.is_clean(repo_path):
        logger.warning(
            "working tree has uncommitted changes; skipping fast-forward to %s",
            target[:8],
        )
        return None
    await git.reset_hard(repo_path, target)
    return target


async def _build_pipeline_inputs(
    args: argparse.Namespace,
) -> tuple[Path, Path, GitService, DefaultPlanner, Generator, Path, str] | None:
    """Shared dependency wiring for ``run`` and ``watch``.

    Returns ``None`` (and prints an error) when the state file is missing —
    both commands require ``plan init`` to have created it. Centralised so
    the two callers stay in lockstep on which env / arg overrides apply.
    """
    repo_path = Path(args.repo_path).expanduser().resolve()
    state_path = (
        Path(args.state).expanduser().resolve()
        if args.state
        else repo_path / _DEFAULT_STATE_NAME
    )
    if not state_path.exists():
        print(f"error: state file not found: {state_path}", file=sys.stderr)
        print("hint: run `greenbean plan init` first", file=sys.stderr)
        return None

    settings = Settings.from_env()
    model = args.model or settings.generator_model
    git = GitService()
    planner = DefaultPlanner()
    tools = WorkingCopyTools(repo_path)
    generator = Generator(tools, model=model, client=settings.make_generator_client())
    output_root = await _output_root_for(repo_path, git)
    return repo_path, state_path, git, planner, generator, output_root, model


async def _run(args: argparse.Namespace) -> int:
    deps = await _build_pipeline_inputs(args)
    if deps is None:
        return 1
    repo_path, state_path, git, planner, generator, output_root, model = deps

    if args.pull:
        has_remote = True
        try:
            await git.remote_url(repo_path)
        except GitError:
            has_remote = False
        if not has_remote:
            logger.warning("--pull skipped: no remote configured on this working copy")
        else:
            try:
                await git.fetch(repo_path)
                advanced = await _advance_to_upstream(git, repo_path)
                if advanced is not None:
                    logger.info("advanced HEAD to %s", advanced[:8])
            except GitError as e:
                logger.warning("--pull failed: %s", e)

    try:
        with SqliteDocStore(state_path) as store:
            summary = await run_pipeline(
                repo_path,
                store=store,
                planner=planner,
                generator=generator,
                git=git,
                output_root=output_root,
                model=model,
            )
    except GitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(_summary_line(summary))
    return 0


async def _watch(args: argparse.Namespace) -> int:
    try:
        interval = _parse_interval(args.interval)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    deps = await _build_pipeline_inputs(args)
    if deps is None:
        return 1
    repo_path, state_path, git, planner, generator, output_root, model = deps

    has_remote = True
    try:
        await git.remote_url(repo_path)
    except GitError:
        # A working copy without an ``origin`` (e.g. a local fixture or a
        # repo where the user hasn't set a remote yet) is a perfectly
        # reasonable thing to watch — we just skip the ``git fetch`` step
        # below. The HEAD-moved check still works against the local ref.
        has_remote = False

    print(f"watching {repo_path} every {args.interval} (Ctrl-C to stop)")
    if not has_remote:
        print("note: no origin configured — skipping git fetch each tick")

    with SqliteDocStore(state_path) as store:
        while True:
            if has_remote:
                try:
                    await git.fetch(repo_path)
                    advanced = await _advance_to_upstream(git, repo_path)
                    if advanced is not None:
                        logger.info("advanced HEAD to %s", advanced[:8])
                except GitError as e:
                    logger.warning("fetch failed: %s", e)
            try:
                summary = await run_pipeline(
                    repo_path,
                    store=store,
                    planner=planner,
                    generator=generator,
                    git=git,
                    output_root=output_root,
                    model=model,
                )
                print(_summary_line(summary))
            except GitError as e:
                logger.warning("pipeline failed: %s", e)
            await asyncio.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = _build_parser().parse_args(argv)
    if args.command == "clone":
        return asyncio.run(_clone(args))
    if args.command == "plan":
        if args.plan_command == "init":
            return asyncio.run(_plan_init(args))
        if args.plan_command == "list":
            return _plan_list(args)
    if args.command == "generate":
        return asyncio.run(_generate(args))
    if args.command == "run":
        return asyncio.run(_run(args))
    if args.command == "watch":
        # ``asyncio.run`` re-raises ``KeyboardInterrupt`` from its task
        # cancellation path, so the exit-on-Ctrl-C handling belongs here
        # rather than inside the watch coroutine (where ``CancelledError``
        # bypasses a naive ``except KeyboardInterrupt``).
        try:
            return asyncio.run(_watch(args))
        except KeyboardInterrupt:
            print("\nstopped")
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
