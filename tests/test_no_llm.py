"""Tests for the no-LLM dev mode: FakeClient + the GREENBEAN_NO_LLM switch."""

from __future__ import annotations

import asyncio

from greenbean.core.llm import TextBlock, UserMessage
from greenbean.llm.fake import FakeClient
from greenbean.settings import Settings


def test_fake_client_returns_end_turn_text_for_target_doc() -> None:
    client = FakeClient()
    resp = asyncio.run(
        client.send_messages(
            model="claude-sonnet-4-6",
            system="sys",
            messages=[UserMessage(content="Generate the documentation for `README.md`.")],
            tools=[],
            max_tokens=1024,
        )
    )
    assert resp.stop_reason == "end_turn"  # terminates the generator loop in one turn
    assert len(resp.content) == 1
    block = resp.content[0]
    assert isinstance(block, TextBlock)
    assert "README.md" in block.text
    assert resp.usage.output_tokens > 0  # synthesised, non-zero usage for logging


def test_no_llm_env_forces_fake_providers(monkeypatch) -> None:
    monkeypatch.setenv("GREENBEAN_NO_LLM", "1")
    monkeypatch.setenv("GREENBEAN_GENERATOR_PROVIDER", "anthropic")  # overridden by the switch
    settings = Settings.from_env()
    assert settings.generator_provider == "fake"
    assert settings.triage_provider == "fake"
    assert isinstance(settings.make_generator_client(), FakeClient)
    assert isinstance(settings.make_triage_client(), FakeClient)


def test_no_llm_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("GREENBEAN_NO_LLM", raising=False)
    settings = Settings.from_env()
    assert settings.generator_provider == "anthropic"
