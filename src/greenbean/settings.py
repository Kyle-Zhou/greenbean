"""Runtime settings loaded from environment variables.

Override any value by setting the corresponding env var before running:

    GREENBEAN_GENERATOR_PROVIDER=anthropic
    GREENBEAN_GENERATOR_MODEL=claude-sonnet-4-6
    GREENBEAN_TRIAGE_PROVIDER=anthropic
    GREENBEAN_TRIAGE_MODEL=claude-haiku-4-5-20251001

Set GREENBEAN_NO_LLM=1 (or pass --no-llm on the CLI) to swap every model call
for the no-network FakeClient — for exercising the pipeline without API cost.

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


def _env_no_llm() -> bool:
    return os.getenv("GREENBEAN_NO_LLM", "").strip().lower() in {"1", "true", "yes", "on"}


def _make_client(provider: str) -> LLMClient:
    if provider == "fake":
        from greenbean.llm.fake import FakeClient
        return FakeClient()
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
        # GREENBEAN_NO_LLM forces both paths onto the FakeClient, regardless of
        # any provider overrides — the dev/no-cost switch wins.
        if _env_no_llm():
            return cls(
                generator_provider="fake",
                generator_model=os.getenv("GREENBEAN_GENERATOR_MODEL", _DEFAULT_GENERATOR_MODEL),
                triage_provider="fake",
                triage_model=os.getenv("GREENBEAN_TRIAGE_MODEL", _DEFAULT_TRIAGE_MODEL),
            )
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
