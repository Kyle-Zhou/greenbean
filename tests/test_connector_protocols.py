"""Conformance tests for the connection-layer abstraction.

These tests guard two invariants:

1. The v1 GitHub stubs structurally satisfy the core protocols. If someone
   adds a new method to ``RepoSource`` and forgets to update the GitHub
   implementation, this catches it before it reaches a runtime call site.
2. The dependency arrow points connectors → core, never the reverse. The
   core package must remain importable without the connectors package even
   existing.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from greenbean.connectors.github import (
    GitHubConnector,
    GitHubWriter,
    GitRepoSource,
    WebhookNotifier,
)
from greenbean.core.connectors import (
    ChangeNotifier,
    Connector,
    RepoSource,
    RepoWriter,
    SupportsPullRequests,
)


def _assignable(value: object, protocol: type) -> bool:
    """Structurally check ``value`` against ``protocol``.

    ``Protocol`` classes without ``@runtime_checkable`` cannot be passed to
    ``isinstance``; we instead verify every method on the protocol exists on
    the value.
    """
    for attr in protocol.__dict__:
        if attr.startswith("_"):
            continue
        if not hasattr(value, attr):
            return False
    return True


def test_git_repo_source_satisfies_repo_source() -> None:
    instance = GitRepoSource(cache_root=Path("/tmp/greenbean-test"))
    assert _assignable(instance, RepoSource)


def test_webhook_notifier_satisfies_change_notifier() -> None:
    instance = WebhookNotifier(webhook_secret="secret")
    assert _assignable(instance, ChangeNotifier)


def test_github_writer_satisfies_repo_writer() -> None:
    instance = GitHubWriter(app_id="1", private_key=b"")
    assert _assignable(instance, RepoWriter)


def test_github_writer_advertises_pull_request_capability() -> None:
    """Optional capabilities use ``@runtime_checkable`` and ``isinstance``."""
    instance = GitHubWriter(app_id="1", private_key=b"")
    assert isinstance(instance, SupportsPullRequests)


def test_github_connector_satisfies_connector_protocol() -> None:
    connector = GitHubConnector(
        cache_root=Path("/tmp/greenbean-test"),
        app_id="1",
        private_key=b"",
        webhook_secret="secret",
    )
    assert _assignable(connector, Connector)
    assert connector.name == "github"
    assert _assignable(connector.source, RepoSource)
    assert _assignable(connector.notifier, ChangeNotifier)
    assert _assignable(connector.writer, RepoWriter)


def test_core_does_not_import_connectors() -> None:
    """Architectural rule: dependency arrow points connectors → core only."""
    import greenbean.core as core_pkg

    forbidden_prefix = "greenbean.connectors"
    offenders: list[str] = []

    for module_info in pkgutil.walk_packages(core_pkg.__path__, prefix="greenbean.core."):
        module = importlib.import_module(module_info.name)
        for referenced in vars(module).values():
            mod_name = getattr(referenced, "__module__", "") or ""
            if mod_name.startswith(forbidden_prefix):
                offenders.append(f"{module_info.name} references {mod_name}")

    assert not offenders, "core must not depend on connectors:\n" + "\n".join(offenders)
