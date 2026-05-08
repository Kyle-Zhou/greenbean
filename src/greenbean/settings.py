"""Runtime settings loaded from environment variables.

Override any value by setting the corresponding env var before running:

    GREENBEAN_GENERATOR_PROVIDER=anthropic
    GREENBEAN_GENERATOR_MODEL=claude-sonnet-4-6
    GREENBEAN_TRIAGE_PROVIDER=anthropic
    GREENBEAN_TRIAGE_MODEL=claude-haiku-4-5-20251001

Adding a new provider: implement LLMClient in llm/<provider>.py and add a
branch in _make_client.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from greenbean.core.llm import LLMClient

_DEFAULT_GENERATOR_PROVIDER = "anthropic"
_DEFAULT_GENERATOR_MODEL = "claude-sonnet-4-6"
_DEFAULT_TRIAGE_PROVIDER = "anthropic"
_DEFAULT_TRIAGE_MODEL = "claude-haiku-4-5-20251001"


def _make_client(provider: str) -> LLMClient:
    if provider == "anthropic":
        from greenbean.llm.anthropic import AnthropicClient
        return AnthropicClient()
    # manually add others if provider == "OpenAI" ...
    raise ValueError(f"unknown provider: {provider!r}")


@dataclass(frozen=True, slots=True)
class Settings:
    generator_provider: str
    generator_model: str
    triage_provider: str
    triage_model: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            generator_provider=os.getenv("GREENBEAN_GENERATOR_PROVIDER", _DEFAULT_GENERATOR_PROVIDER),
            generator_model=os.getenv("GREENBEAN_GENERATOR_MODEL", _DEFAULT_GENERATOR_MODEL),
            triage_provider=os.getenv("GREENBEAN_TRIAGE_PROVIDER", _DEFAULT_TRIAGE_PROVIDER),
            triage_model=os.getenv("GREENBEAN_TRIAGE_MODEL", _DEFAULT_TRIAGE_MODEL),
        )

    def make_generator_client(self) -> LLMClient:
        return _make_client(self.generator_provider)

    def make_triage_client(self) -> LLMClient:
        return _make_client(self.triage_provider)
