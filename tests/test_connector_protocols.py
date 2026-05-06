"""Conformance tests for the connection-layer abstraction.

These tests guard two invariants:

1. The v1 GitHub stubs structurally satisfy the core protocols. If someone
   adds a new method to ``RepoCredentials`` and forgets to update the GitHub
   implementation, this catches it before it reaches a runtime call site.
2. The dependency arrow points connectors → core, never the reverse. The
   core package must remain importable without the connectors package even
   existing.
"""

from __future__ import annotations

import importlib
import pkgutil

from greenbean.connectors.github import (
    GitHubConnector,
    GitHubCredentials,
    GitHubRepoInfo,
    GitHubWriter,
    WebhookNotifier,
)
from greenbean.core.connectors import (
    ChangeNotifier,
    Connector,
    RepoCredentials,
    RepoRef,
    RepoWriter,
    SupportsPullRequests,
)


async def _stub_resolver(_: RepoRef) -> GitHubRepoInfo:
    return GitHubRepoInfo(installation_id=1, owner="acme", name="widgets")


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


def test_github_credentials_satisfies_repo_credentials() -> None:
    instance = GitHubCredentials(
        app_id="1",
        private_key=b"",
        repo_resolver=_stub_resolver,
    )
    assert _assignable(instance, RepoCredentials)


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
        app_id="1",
        private_key=b"",
        webhook_secret="secret",
        repo_resolver=_stub_resolver,
    )
    assert _assignable(connector, Connector)
    assert connector.name == "github"
    assert _assignable(connector.notifier, ChangeNotifier)
    assert _assignable(connector.credentials, RepoCredentials)
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
