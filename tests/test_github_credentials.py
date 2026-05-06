"""Tests for ``GitHubCredentials``.

We exercise the real JWT signing and HTTP shape using ``httpx.MockTransport``
so the tests don't hit GitHub. The transport asserts on the request the
client sends (auth headers, JSON body, URL) and returns canned responses.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from greenbean.connectors.github.credentials import (
    GitHubCredentials,
    GitHubCredentialsError,
    GitHubRepoInfo,
)
from greenbean.core.connectors import RepoRef


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


@pytest.fixture
def repo() -> RepoRef:
    return RepoRef(id="repo-uuid", source_type="github", external_id="9999")


async def _resolver(_: RepoRef) -> GitHubRepoInfo:
    return GitHubRepoInfo(installation_id=42, owner="acme", name="widgets")


def test_get_clone_url_mints_token_and_embeds_it(
    rsa_keypair: tuple[bytes, bytes],
    repo: RepoRef,
) -> None:
    private_pem, public_pem = rsa_keypair
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/app/installations/42/access_tokens":
            assert request.method == "POST"
            bearer = request.headers["Authorization"]
            assert bearer.startswith("Bearer ")
            claims = jwt.decode(
                bearer.removeprefix("Bearer "),
                public_pem,
                algorithms=["RS256"],
            )
            assert claims["iss"] == "12345"
            return httpx.Response(201, json={"token": "ghs_secret_token"})
        raise AssertionError(f"unexpected request: {request.url}")

    async def run() -> str:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            creds = GitHubCredentials(
                app_id="12345",
                private_key=private_pem,
                repo_resolver=_resolver,
                http=http,
            )
            return await creds.get_clone_url(repo)

    url = asyncio.run(run())
    assert url == "https://x-access-token:ghs_secret_token@github.com/acme/widgets.git"
    assert len(seen) == 1


def test_get_branch_head_returns_sha(
    rsa_keypair: tuple[bytes, bytes],
    repo: RepoRef,
) -> None:
    private_pem, _ = rsa_keypair

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "ghs_branch_token"})
        if request.url.path == "/repos/acme/widgets/branches/main":
            assert request.headers["Authorization"] == "token ghs_branch_token"
            return httpx.Response(
                200,
                json={"name": "main", "commit": {"sha": "deadbeef" * 5}},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async def run() -> str:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            creds = GitHubCredentials(
                app_id="12345",
                private_key=private_pem,
                repo_resolver=_resolver,
                http=http,
            )
            return await creds.get_branch_head(repo, "main")

    sha = asyncio.run(run())
    assert sha == "deadbeef" * 5


def test_token_mint_failure_surfaces_error(
    rsa_keypair: tuple[bytes, bytes],
    repo: RepoRef,
) -> None:
    private_pem, _ = rsa_keypair

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=json.dumps({"message": "Bad credentials"}))

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            creds = GitHubCredentials(
                app_id="12345",
                private_key=private_pem,
                repo_resolver=_resolver,
                http=http,
            )
            await creds.get_clone_url(repo)

    with pytest.raises(GitHubCredentialsError, match="installation token mint failed"):
        asyncio.run(run())
