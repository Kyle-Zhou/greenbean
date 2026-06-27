"""greenbean CLI — single-tenant entry point.

The local-first flow: clone (or point at an existing working tree), plan,
generate. Generated docs land in ``~/.greenbean/output/<host>/<owner>/<name>/``
and never in the source repo — greenbean does not publish docs back to the
customer's repo. ``run`` is the one-shot full loop (refresh plan, generate the
docs whose sources changed, publish); ``watch`` is its polling wrapper.

    greenbean clone <url> [--token TOKEN] [--branch BRANCH]
                          [--dest DIR] [--depth N]
    greenbean plan init <repo-path> [--state PATH]
    greenbean plan list <repo-path> [--state PATH]
    greenbean generate <repo-path> [--state PATH] [--doc DOC_PATH]
                                   [--dry-run] [--model MODEL]
    greenbean run <repo-path> [--state PATH] [--full] [--dry-run] [--model M]
    greenbean watch <repo-path> [--interval 5m] [--state PATH] [--model M]

The token comes from ``--token`` or ``$GITHUB_TOKEN``. If no token is
supplied, the bare URL is used — fine for public repos, ``file://`` URLs
(handy for local testing), or SSH when the user's ssh-agent is set up.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import os
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

from greenbean.agent import Generator
from greenbean.connectors.github import url as github_url
from greenbean.connectors.github.token_credentials import TokenCredentials
from greenbean.core.connectors import RepoRef
from greenbean.core.git import GitError, GitService
from greenbean.pipeline import DocGenerator, GeneratorFactory, RunResult, run_once
from greenbean.planning import DefaultPlanner, SqliteDocStore
from greenbean.publish import (
    DEFAULT_OUTPUT_ROOT,
    atomic_write_text as _atomic_write_text,
    output_root_for as _output_root_for,
    safe_output_path as _safe_output_path,
)
from greenbean.settings import Settings
from greenbean.tools.working_copy import WorkingCopyTools

DEFAULT_CACHE_ROOT = Path.home() / ".greenbean" / "cache"
_DEFAULT_STATE_NAME = ".greenbean/state.sqlite"
_DEFAULT_WATCH_INTERVAL = "5m"

# Publishing helpers moved to ``greenbean.publish`` so the run pipeline can
# share them without importing ``cli``. Re-exported here under their original
# names for the commands (and tests) that already reference them; ``__all__``
# marks them as explicit re-exports so mypy's no-implicit-reexport is satisfied.
__all__ = [
    "DEFAULT_OUTPUT_ROOT",
    "_atomic_write_text",
    "_output_root_for",
    "_safe_output_path",
    "main",
]


def _make_generator_factory(model: str, settings: Settings) -> GeneratorFactory:
    """Bind a generator-builder to a model + client for the run pipeline."""

    def factory(repo_path: Path) -> DocGenerator:
        return Generator(
            WorkingCopyTools(repo_path),
            model=model,
            client=settings.make_generator_client(),
        )

    return factory


def _resolve_repo_and_state(args: argparse.Namespace) -> tuple[Path, Path]:
    """Resolve the working-copy path and its state file from common args.

    The ``--state`` override is absolute; otherwise the state file lives at
    ``<repo-path>/.greenbean/state.sqlite``. Shared by every repo-scoped
    command so the defaulting can't drift between them.
    """
    repo_path = Path(args.repo_path).expanduser().resolve()
    if args.state:
        state_path = Path(args.state).expanduser().resolve()
    else:
        state_path = repo_path / _DEFAULT_STATE_NAME
    return repo_path, state_path


def _add_no_llm_flag(p: argparse.ArgumentParser) -> None:
    """Attach the shared ``--no-llm`` dev switch to a subcommand parser."""
    p.add_argument(
        "--no-llm",
        action="store_true",
        help="Use canned fake responses instead of any LLM API (no network, no cost).",
    )


def _apply_no_llm(args: argparse.Namespace) -> None:
    """Honour ``--no-llm`` by flipping the env var Settings.from_env reads."""
    if getattr(args, "no_llm", False):
        os.environ["GREENBEAN_NO_LLM"] = "1"


def _parse_interval(value: str) -> float:
    """Parse a poll interval like ``30s``, ``5m``, ``1h``, or bare seconds.

    Returns seconds as a float. Raises ``ValueError`` on anything unparseable
    so the CLI can report a clean error instead of a stack trace.
    """
    text = value.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    unit = units.get(text[-1:])
    number = text[:-1] if unit is not None else text
    try:
        seconds = float(number) * (unit if unit is not None else 1)
    except ValueError:
        raise ValueError(
            f"invalid interval {value!r}: expected e.g. 30s, 5m, 1h, or seconds"
        ) from None
    if seconds <= 0:
        raise ValueError(f"interval must be positive: {value!r}")
    return seconds


def _parse_port(value: str) -> int:
    """Parse a viewer port, accepting both ``8080`` and ``:8080`` forms."""
    text = value.strip().lstrip(":")
    try:
        port = int(text)
    except ValueError:
        raise ValueError(f"invalid port {value!r}") from None
    if not (1 <= port <= 65535):
        raise ValueError(f"port out of range: {port}")
    return port


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
    _add_no_llm_flag(gen)

    run = sub.add_parser(
        "run",
        help="Run one pass of the loop: refresh the plan, generate changed docs, publish.",
    )
    run.add_argument("repo_path", help="Path to a git working copy.")
    run.add_argument(
        "--state",
        default=None,
        help="SQLite state file (default: <repo-path>/.greenbean/state.sqlite).",
    )
    run.add_argument(
        "--full",
        action="store_true",
        help="Regenerate every planned doc, ignoring the diff (force a full pass).",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate but write nothing; leaves sync state untouched.",
    )
    run.add_argument("--model", default=None, help="Anthropic model override.")
    _add_no_llm_flag(run)

    watch = sub.add_parser(
        "watch",
        help="Poll a working copy on an interval and run the loop when HEAD moves.",
    )
    watch.add_argument("repo_path", help="Path to a git working copy.")
    watch.add_argument(
        "--interval",
        default=_DEFAULT_WATCH_INTERVAL,
        help=f"Poll interval, e.g. 30s / 5m / 1h (default: {_DEFAULT_WATCH_INTERVAL}).",
    )
    watch.add_argument(
        "--state",
        default=None,
        help="SQLite state file (default: <repo-path>/.greenbean/state.sqlite).",
    )
    watch.add_argument("--model", default=None, help="Anthropic model override.")
    watch.add_argument(
        "--view",
        default=None,
        metavar="PORT",
        help="Serve a localhost markdown viewer for the output dir (e.g. 8080 or :8080).",
    )
    _add_no_llm_flag(watch)

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
    repo_path, state_path = _resolve_repo_and_state(args)

    git = GitService()
    try:
        sha = await git.current_sha(repo_path)
    except GitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    planner = DefaultPlanner()
    planned = await planner.plan_with_sources(repo_path)

    state_path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteDocStore(state_path) as store:
        result = store.replace_plan(planned, planned_sha=sha)

    print(f"added {result.added}, updated {result.updated}, pruned {result.pruned}", end="")
    if result.preserved:
        print(f", preserved {list(result.preserved)}", end="")
    print()
    return 0


def _plan_list(args: argparse.Namespace) -> int:
    _repo_path, state_path = _resolve_repo_and_state(args)

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
    repo_path, state_path = _resolve_repo_and_state(args)

    if not state_path.exists():
        print(f"error: state file not found: {state_path}", file=sys.stderr)
        print("hint: run `greenbean plan init` first", file=sys.stderr)
        return 1

    _apply_no_llm(args)
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

        for doc in docs:
            source_paths = store.source_paths_for(doc.id)
            print(f"generating {doc.path_in_repo} ...", end=" ", flush=True)
            result = await generator.generate(doc, source_paths)
            print(
                f"done ({result.tool_calls} tool calls, "
                f"{result.input_tokens}+{result.output_tokens} tokens)"
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
                print(f"  wrote {out_path}")

    return 0


def _print_run_summary(result: RunResult) -> None:
    """One-line outcome for a pipeline pass (skips already logged inline)."""
    if result.skipped:
        return
    mode = "full" if result.full else "incremental"
    print(
        f"{mode} pass at {result.after_sha[:8]}: "
        f"generated {len(result.generated)} doc(s)"
    )
    if result.generated:
        print(f"  -> {result.output_root}")


async def _advance_to_upstream(git: GitService, repo_path: Path) -> None:
    """Fetch and fast-forward the working copy to its upstream, safely.

    Best-effort: a missing remote (offline, no origin) is ignored. Never
    resets a dirty tree — a developer's uncommitted work is sacred, so if the
    upstream moved while there are local changes, leave HEAD alone and let the
    pipeline react to whatever is committed.
    """
    try:
        await git.fetch(repo_path)
    except GitError:
        return  # no remote / offline — fall through to current HEAD
    target = await git.upstream_sha(repo_path)
    if target is None or target == await git.current_sha(repo_path):
        return
    if await git.is_dirty(repo_path):
        print("upstream moved but working tree is dirty — not resetting")
        return
    await git.reset_hard(repo_path, target)
    print(f"advanced working copy to {target[:8]}")


async def _run(args: argparse.Namespace) -> int:
    repo_path, state_path = _resolve_repo_and_state(args)

    _apply_no_llm(args)
    settings = Settings.from_env()
    model = args.model or settings.generator_model
    try:
        result = await run_once(
            repo_path,
            state_path,
            git=GitService(),
            planner=DefaultPlanner(),
            make_generator=_make_generator_factory(model, settings),
            dry_run=args.dry_run,
            force_full=args.full,
        )
    except GitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _print_run_summary(result)
    return 0


async def _watch(args: argparse.Namespace) -> int:
    repo_path, state_path = _resolve_repo_and_state(args)

    try:
        interval = _parse_interval(args.interval)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # watch is a long-lived streaming command; line-buffer so progress shows
    # promptly when stdout is redirected to a file/pipe (e.g. in CI), not just
    # on a TTY. Affects every print below, including the pipeline's log lines.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)

    _apply_no_llm(args)
    settings = Settings.from_env()
    model = args.model or settings.generator_model
    git = GitService()
    planner = DefaultPlanner()
    factory = _make_generator_factory(model, settings)

    viewer: ThreadingHTTPServer | None = None
    if args.view is not None:
        try:
            port = _parse_port(args.view)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        from greenbean.viewer import start_viewer

        try:
            viewer = start_viewer(DEFAULT_OUTPUT_ROOT, port)
        except OSError as e:
            print(f"error: could not start viewer on port {port}: {e}", file=sys.stderr)
            return 2
        print(f"viewer: http://127.0.0.1:{port}/")

    print(f"watching {repo_path} every {args.interval} (Ctrl-C to stop)")
    try:
        while True:
            try:
                await _advance_to_upstream(git, repo_path)
                result = await run_once(
                    repo_path,
                    state_path,
                    git=git,
                    planner=planner,
                    make_generator=factory,
                )
                _print_run_summary(result)
            except GitError as e:
                print(f"error: {e}", file=sys.stderr)
            await asyncio.sleep(interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nstopped")
        return 0
    finally:
        if viewer is not None:
            viewer.shutdown()


def main(argv: list[str] | None = None) -> int:
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
        return asyncio.run(_watch(args))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
