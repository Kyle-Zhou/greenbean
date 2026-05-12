"""greenbean CLI — single-tenant entry point.

The local-first flow: clone (or point at an existing working tree), plan,
generate. Generated docs land in ``~/.greenbean/output/<host>/<owner>/<name>/``
and never in the source repo — greenbean does not publish docs back to the
customer's repo. ``run`` and ``watch`` (the full automation loop and its
polling wrapper) plug in here next.

    greenbean clone <url> [--token TOKEN] [--branch BRANCH]
                          [--dest DIR] [--depth N]
    greenbean plan init <repo-path> [--state PATH]
    greenbean plan list <repo-path> [--state PATH]
    greenbean generate <repo-path> [--state PATH] [--doc DOC_PATH]
                                   [--dry-run] [--model MODEL]

The token comes from ``--token`` or ``$GITHUB_TOKEN``. If no token is
supplied, the bare URL is used — fine for public repos, ``file://`` URLs
(handy for local testing), or SSH when the user's ssh-agent is set up.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import tempfile
from pathlib import Path

from greenbean.agent import Generator
from greenbean.connectors.github import url as github_url
from greenbean.connectors.github.token_credentials import TokenCredentials
from greenbean.core.connectors import RepoRef
from greenbean.core.git import GitError, GitService
from greenbean.core.planning import PlannedDoc
from greenbean.planning import DefaultPlanner, SqliteDocStore
from greenbean.settings import Settings
from greenbean.tools.working_copy import WorkingCopyTools

DEFAULT_CACHE_ROOT = Path.home() / ".greenbean" / "cache"
DEFAULT_OUTPUT_ROOT = Path.home() / ".greenbean" / "output"
_DEFAULT_STATE_NAME = ".greenbean/state.sqlite"


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    Writes to a uniquely-named sibling temp file then ``os.replace``s into
    position. The temp lives on the same filesystem as the target so the
    rename is atomic on POSIX. Prevents partial files on disk-full, SIGTERM
    mid-write, or crash. The unique temp name also keeps two concurrent
    callers (two processes, future parallel generation) from clobbering
    each other's in-flight write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _safe_output_path(output_root: Path, path_in_repo: str) -> Path:
    """Resolve ``path_in_repo`` under ``output_root``; reject any escape.

    Symmetric with ``WorkingCopyTools._resolve_safe`` on the read side. The
    plan's ``path_in_repo`` is trusted today (the mechanical planner only
    emits clean relative paths), but the planner gains a small-LLM
    refinement step in the build order — at which point this is the right
    place to catch a hallucinated ``../../etc/passwd``.
    """
    if Path(path_in_repo).is_absolute():
        raise ValueError(f"path_in_repo must be relative, got {path_in_repo!r}")
    output_root = output_root.resolve()
    candidate = (output_root / path_in_repo).resolve()
    if candidate != output_root and output_root not in candidate.parents:
        raise ValueError(
            f"path_in_repo escapes the output root: {path_in_repo!r}"
        )
    return candidate


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
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
