"""Conformance tests for the connection-layer abstraction.

These tests guard two invariants:

1. The GitHub stubs (``GitHubCredentials``, ``WebhookNotifier``) structurally
   satisfy the core protocols. If someone adds a new method to
   ``RepoCredentials`` and forgets to update the GitHub implementation, this
   catches it before it reaches a runtime call site. The stubs are
   deprecated for v1 but kept for the future SaaS deployment, so the
   conformance check stays.
2. The dependency arrow points connectors → core, never the reverse. The
   core package must remain importable without the connectors package even
   existing.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

from greenbean.connectors.github import (
    GitHubCredentials,
    GitHubRepoInfo,
    WebhookNotifier,
)
from greenbean.connectors.github.token_credentials import TokenCredentials
from greenbean.core.connectors import (
    ChangeNotifier,
    RepoCredentials,
    RepoRef,
)
from greenbean.core.git import GitService


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


def _assert_method_signature_matches_protocol(
    value: object,
    protocol: type,
    method_name: str,
) -> None:
    """Guard against protocol-conforming names with drifting call signatures."""
    protocol_method = getattr(protocol, method_name)
    impl_method = getattr(type(value), method_name)
    expected = inspect.signature(protocol_method)
    actual = inspect.signature(impl_method)
    assert actual == expected, (
        f"{type(value).__name__}.{method_name} signature drifted from "
        f"{protocol.__name__}.{method_name}: expected {expected}, got {actual}"
    )


def test_github_credentials_satisfies_repo_credentials() -> None:
    instance = GitHubCredentials(
        app_id="1",
        private_key=b"",
        repo_resolver=_stub_resolver,
    )
    assert _assignable(instance, RepoCredentials)
    _assert_method_signature_matches_protocol(
        instance, RepoCredentials, "get_clone_url"
    )
    _assert_method_signature_matches_protocol(
        instance, RepoCredentials, "get_branch_head"
    )


def test_token_credentials_satisfies_repo_credentials() -> None:
    instance = TokenCredentials(token="x", git=GitService())
    assert _assignable(instance, RepoCredentials)
    _assert_method_signature_matches_protocol(
        instance, RepoCredentials, "get_clone_url"
    )
    _assert_method_signature_matches_protocol(
        instance, RepoCredentials, "get_branch_head"
    )


def test_webhook_notifier_satisfies_change_notifier() -> None:
    instance = WebhookNotifier(webhook_secret="secret")
    assert _assignable(instance, ChangeNotifier)
    _assert_method_signature_matches_protocol(instance, ChangeNotifier, "subscribe")
    _assert_method_signature_matches_protocol(instance, ChangeNotifier, "unsubscribe")


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
