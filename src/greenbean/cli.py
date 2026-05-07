"""greenbean CLI — single-tenant entry point.

For now, just the clone smoke path so we can run greenbean against a real
repo end to end. The rest of the pipeline (sync → planning → triage →
agent → publish) will plug in here as it's built.

    greenbean clone <url> [--token TOKEN] [--branch BRANCH]
                          [--dest DIR] [--depth N]
    greenbean plan init <repo-path> [--state PATH]
    greenbean plan list <repo-path> [--state PATH]

The token comes from ``--token`` or ``$GITHUB_TOKEN``. If no token is
supplied, the bare URL is used — fine for public repos, ``file://`` URLs
(handy for local testing), or SSH when the user's ssh-agent is set up.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from greenbean.connectors.github import url as github_url
from greenbean.connectors.github.token_credentials import TokenCredentials
from greenbean.core.connectors import RepoRef
from greenbean.core.git import GitError, GitService
from greenbean.core.planning import PlannedDoc
from greenbean.planning import DefaultPlanner, SqliteDocStore

DEFAULT_CACHE_ROOT = Path.home() / ".greenbean" / "cache"
_DEFAULT_STATE_NAME = ".greenbean/state.sqlite"


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
    state_path = Path(args.state) if args.state else repo_path / _DEFAULT_STATE_NAME

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
    state_path = Path(args.state) if args.state else repo_path / _DEFAULT_STATE_NAME

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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "clone":
        return asyncio.run(_clone(args))
    if args.command == "plan":
        if args.plan_command == "init":
            return asyncio.run(_plan_init(args))
        if args.plan_command == "list":
            return _plan_list(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
