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
                                   [--dry-run] [--model MODEL] [--view PORT]
    greenbean run <repo-path> [--state PATH] [--full] [--dry-run] [--model M]
    greenbean watch <repo-path> [--interval 5m] [--state PATH] [--model M]
                                [--view PORT]

The token comes from ``--token`` or ``$GITHUB_TOKEN``. If no token is
supplied, the bare URL is used — fine for public repos, ``file://`` URLs
(handy for local testing), or SSH when the user's ssh-agent is set up.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import logging
import os
import sqlite3
import sys
import urllib.parse
from collections.abc import Sequence
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

from greenbean.agent import Generator, GeneratorError
from greenbean.connectors.github import url as github_url
from greenbean.connectors.github.token_credentials import TokenCredentials
from greenbean.core.connectors import RepoRef
from greenbean.core.git import Commit, GitError, GitService
from greenbean.core.llm import LLMError
from greenbean.observability import configure_logging
from greenbean.pipeline import DocGenerator, GeneratorFactory, RunResult, run_once

if TYPE_CHECKING:
    from greenbean.viewer import DocStatus, StatusHolder, ViewerStatus
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


def _add_verbose_flag(p: argparse.ArgumentParser) -> None:
    """Attach the shared ``-v/--verbose`` logging switch to a subcommand parser."""
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging: per-turn detail and full tool inputs/outputs.",
    )


def _apply_no_llm(args: argparse.Namespace) -> None:
    """Honour ``--no-llm`` by flipping the env var Settings.from_env reads."""
    if getattr(args, "no_llm", False):
        os.environ["GREENBEAN_NO_LLM"] = "1"


def _llm_preflight(settings: Settings) -> str | None:
    """Return an actionable error if the generator client can't authenticate.

    The Anthropic SDK defers a missing-key failure all the way to request time,
    surfacing a cryptic ``TypeError`` mid-generation. Catch the common case
    (``ANTHROPIC_API_KEY`` unset) up front so the user gets a clear message and
    a pointer to ``--no-llm`` instead of a traceback.
    """
    if settings.generator_provider == "anthropic" and not os.environ.get(
        "ANTHROPIC_API_KEY"
    ):
        return (
            "ANTHROPIC_API_KEY is not set. Export it "
            "(export ANTHROPIC_API_KEY=sk-ant-...), or pass --no-llm to run "
            "without an API key."
        )
    return None


_LLM_ERROR_HINT = (
    "hint: verify ANTHROPIC_API_KEY and the model name, or pass --no-llm to run "
    "without the API."
)


def _add_view_flag(p: argparse.ArgumentParser) -> None:
    """Attach the shared ``--view PORT`` viewer switch to a subcommand parser."""
    p.add_argument(
        "--view",
        default=None,
        metavar="PORT",
        help="Serve a localhost markdown viewer for the output dir (e.g. 8080 or :8080).",
    )


def _start_viewer(port_arg: str) -> tuple[ThreadingHTTPServer, StatusHolder]:
    """Start the localhost viewer for ``--view``; return the server + status holder.

    Raises ``ValueError`` on a bad port and ``OSError`` if the bind fails, so
    the caller can map both to a clean exit code. Prints the URL on success.
    """
    from greenbean.viewer import StatusHolder, start_viewer

    port = _parse_port(port_arg)
    holder = StatusHolder()
    server = start_viewer(DEFAULT_OUTPUT_ROOT, port, holder)
    print(f"viewer: http://127.0.0.1:{port}/")
    return server, holder


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
    _add_view_flag(gen)
    _add_no_llm_flag(gen)
    _add_verbose_flag(gen)

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
    _add_verbose_flag(run)

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
    _add_view_flag(watch)
    _add_no_llm_flag(watch)
    _add_verbose_flag(watch)

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

    # Validate --view up front so a bad port fails before any generation work.
    # With --view the command becomes long-lived (serves until Ctrl-C), so
    # line-buffer stdout — same as watch — to keep the viewer link and progress
    # visible promptly when output is piped, not stuck in a block buffer.
    if args.view is not None:
        try:
            _parse_port(args.view)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if isinstance(sys.stdout, io.TextIOWrapper):
            sys.stdout.reconfigure(line_buffering=True)

    _apply_no_llm(args)
    settings = Settings.from_env()
    if (msg := _llm_preflight(settings)) is not None:
        print(f"error: {msg}", file=sys.stderr)
        return 1
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
            try:
                result = await generator.generate(doc, source_paths)
            except (LLMError, GeneratorError) as e:
                print(file=sys.stderr)  # close the dangling "generating ..." line
                print(f"error: {e}", file=sys.stderr)
                print(_LLM_ERROR_HINT, file=sys.stderr)
                return 1
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

    if args.view is None:
        return 0

    # Keep the process alive to serve the viewer (generate is otherwise
    # one-shot, which would kill the daemon thread immediately).
    outcome = (
        f"dry run — {len(docs)} doc(s), nothing written"
        if args.dry_run
        else f"generated {len(docs)} doc(s)"
    )
    try:
        viewer, status_holder = _start_viewer(args.view)
    except (ValueError, OSError) as e:
        print(f"error: could not start viewer: {e}", file=sys.stderr)
        return 2
    status_holder.set(
        await _build_status(git, repo_path, state_path, mode="generate", outcome=outcome)
    )
    print("serving generated docs — Ctrl-C to stop")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nstopped")
    finally:
        viewer.shutdown()
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


def _display_remote(raw: str) -> str:
    """Strip any embedded credentials from a remote URL before showing it."""
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme in ("http", "https") and "@" in parsed.netloc:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urllib.parse.urlunparse((parsed.scheme, host, parsed.path, "", "", ""))
    return raw


def _run_outcome(result: RunResult) -> str:
    """One-line result string for the status panel from a pipeline pass."""
    if result.skipped:
        return "up to date (no change)"
    if result.full:
        return f"full generation — {len(result.generated)} doc(s)"
    return f"incremental — {len(result.generated)} doc(s)"


def _collect_doc_statuses(state_path: Path, repo_rel: str) -> tuple[DocStatus, ...]:
    """Per-doc generation status from the store, for the index table.

    ``repo_rel`` is the repo's output subdir relative to the viewer root, so a
    generated doc can be linked to its served page. Best-effort: a missing or
    unreadable state file yields an empty tuple rather than breaking the panel.
    """
    from greenbean.viewer import DocStatus

    if not state_path.exists():
        return ()
    out: list[DocStatus] = []
    try:
        with SqliteDocStore(state_path) as store:
            for doc in store.list_documents():
                md = doc.generation_metadata or {}
                generated = doc.last_generated_at is not None
                last = (
                    doc.last_generated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
                    if doc.last_generated_at is not None
                    else None
                )
                it, ot = md.get("input_tokens"), md.get("output_tokens")
                tokens = f"{it}+{ot}" if it is not None and ot is not None else None
                href = f"{repo_rel}/{doc.path_in_repo}" if (generated and repo_rel) else None
                out.append(
                    DocStatus(
                        path=doc.path_in_repo,
                        href=href,
                        last_generated=last,
                        model=md.get("model"),
                        tokens=tokens,
                        generated=generated,
                    )
                )
    except sqlite3.Error:
        return tuple(out)
    return tuple(out)


async def _build_status(
    git: GitService,
    repo_path: Path,
    state_path: Path,
    *,
    mode: str,
    outcome: str,
    interval_label: str = "",
    interval_seconds: float | None = None,
) -> ViewerStatus:
    """Assemble the viewer's status snapshot from git + the store.

    Shared by ``watch`` and ``generate`` via ``mode``; ``interval_*`` only apply
    to ``watch`` (they drive the next-sync countdown). Best-effort: each git
    lookup is guarded so a transient failure degrades a single field to ``None``
    rather than breaking the panel. GitHub remotes get browsable web URLs (repo +
    commit); other remotes are shown credential-free.
    """
    from greenbean.viewer import ViewerStatus

    branch: str | None = None
    with contextlib.suppress(GitError):
        branch = await git.current_branch(repo_path)

    head_sha: str | None = None
    head_short: str | None = None
    subject: str | None = None
    author: str | None = None
    head_time: str | None = None
    commits: Sequence[Commit] = ()
    try:
        commits = await git.log(repo_path, limit=1)
    except GitError:
        commits = ()
    if commits:
        c = commits[0]
        head_sha = c.sha
        head_short = c.sha[:8]
        subject = c.message.splitlines()[0] if c.message.strip() else None
        author = c.author
        head_time = c.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    repo_url: str | None = None
    repo_web: str | None = None
    commit_web: str | None = None
    try:
        raw_remote = await git.remote_url(repo_path)
    except GitError:
        raw_remote = ""
    if raw_remote:
        parsed = github_url.try_parse(raw_remote)
        if parsed is not None:
            repo_web = f"https://github.com/{parsed.owner}/{parsed.name}"
            repo_url = repo_web
            if head_sha:
                commit_web = f"{repo_web}/commit/{head_sha}"
        else:
            repo_url = _display_remote(raw_remote)

    repo_out = await _output_root_for(repo_path, git)
    try:
        repo_rel = repo_out.relative_to(DEFAULT_OUTPUT_ROOT).as_posix()
    except ValueError:
        repo_rel = ""
    docs = _collect_doc_statuses(state_path, repo_rel)

    now = datetime.now().astimezone()
    next_epoch = now.timestamp() + interval_seconds if interval_seconds is not None else None
    return ViewerStatus(
        repo_path=str(repo_path),
        repo_url=repo_url,
        repo_web_url=repo_web,
        branch=branch,
        head_short=head_short,
        head_subject=subject,
        head_author=author,
        head_time=head_time,
        commit_web_url=commit_web,
        last_sync=now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        next_sync_epoch=next_epoch,
        interval=interval_label,
        last_outcome=outcome,
        output_root=str(DEFAULT_OUTPUT_ROOT),
        mode=mode,
        docs=docs,
    )


async def _run(args: argparse.Namespace) -> int:
    repo_path, state_path = _resolve_repo_and_state(args)

    _apply_no_llm(args)
    settings = Settings.from_env()
    if (msg := _llm_preflight(settings)) is not None:
        print(f"error: {msg}", file=sys.stderr)
        return 1
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
    except (LLMError, GeneratorError) as e:
        print(f"error: {e}", file=sys.stderr)
        print(_LLM_ERROR_HINT, file=sys.stderr)
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
    if (msg := _llm_preflight(settings)) is not None:
        print(f"error: {msg}", file=sys.stderr)
        return 1
    model = args.model or settings.generator_model
    git = GitService()
    planner = DefaultPlanner()
    factory = _make_generator_factory(model, settings)

    viewer: ThreadingHTTPServer | None = None
    status_holder: StatusHolder | None = None
    if args.view is not None:
        try:
            viewer, status_holder = _start_viewer(args.view)
        except (ValueError, OSError) as e:
            print(f"error: could not start viewer: {e}", file=sys.stderr)
            return 2

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
                if status_holder is not None:
                    status_holder.set(
                        await _build_status(
                            git,
                            repo_path,
                            state_path,
                            mode="watch",
                            outcome=_run_outcome(result),
                            interval_label=args.interval,
                            interval_seconds=interval,
                        )
                    )
            except GitError as e:
                print(f"error: {e}", file=sys.stderr)
            except (LLMError, GeneratorError) as e:
                # Keep watching: a transient API failure shouldn't kill the
                # long-lived poll loop — log it and retry on the next tick.
                print(f"error: {e}", file=sys.stderr)
                print(_LLM_ERROR_HINT, file=sys.stderr)
            await asyncio.sleep(interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nstopped")
        return 0
    finally:
        if viewer is not None:
            viewer.shutdown()


def _dispatch(args: argparse.Namespace) -> int:
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    configure_logging(getattr(args, "verbose", False))
    try:
        return _dispatch(args)
    except Exception:
        # Anything not already handled with a clean message reaches here; log it
        # with a traceback (via the logger, not a raw dump) so the failure and
        # its cause are legible instead of a wall of stack frames.
        logging.getLogger("greenbean.cli").exception("unexpected error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
