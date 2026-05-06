"""GitHub ``RepoWriter`` — opens pull requests via the GitHub App.

Uses short-lived installation tokens minted per operation; no long-lived PATs.
"""

from __future__ import annotations

from collections.abc import Iterable

from greenbean.core.connectors import FileToWrite, RepoRef


class GitHubWriter:
    """Creates a branch, commits files, and opens a PR via the GitHub App."""

    def __init__(self, app_id: str, private_key: bytes) -> None:
        self._app_id = app_id
        self._private_key = private_key

    async def open_pull_request(
        self,
        repo: RepoRef,
        branch_name: str,
        files: Iterable[FileToWrite],
        title: str,
        body: str,
        base_branch: str | None = None,
    ) -> str:
        raise NotImplementedError
