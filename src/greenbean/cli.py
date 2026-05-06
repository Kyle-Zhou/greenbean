"""greenbean CLI — single-tenant entry point.

For now, just the clone smoke path so we can run greenbean against a real
repo end to end. The rest of the pipeline (sync → planning → triage →
agent → publish) will plug in here as it's built.

    greenbean clone <url> [--token TOKEN] [--branch BRANCH]
                          [--dest DIR] [--depth N]

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

DEFAULT_CACHE_ROOT = Path.home() / ".greenbean" / "cache"


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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "clone":
        return asyncio.run(_clone(args))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
