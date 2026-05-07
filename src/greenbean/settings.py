"""Runtime settings loaded from environment variables.

Override any value by setting the corresponding env var before running:

    GREENBEAN_GENERATOR_MODEL=claude-sonnet-4-6 greenbean ...

Adding a new provider: implement LLMClient in llm/<provider>.py and extend
make_generator_client / make_triage_client to instantiate it based on a
GREENBEAN_PROVIDER env var (or by inferring from the model string prefix).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from greenbean.core.llm import LLMClient

_DEFAULT_GENERATOR_MODEL = "claude-sonnet-4-6"
_DEFAULT_TRIAGE_MODEL = "claude-haiku-4-5-20251001"


@dataclass(frozen=True, slots=True)
class Settings:
    generator_model: str
    triage_model: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            generator_model=os.getenv("GREENBEAN_GENERATOR_MODEL", _DEFAULT_GENERATOR_MODEL),
            triage_model=os.getenv("GREENBEAN_TRIAGE_MODEL", _DEFAULT_TRIAGE_MODEL),
        )

    def make_generator_client(self) -> LLMClient:
        from greenbean.llm.anthropic import AnthropicClient
        return AnthropicClient()

    def make_triage_client(self) -> LLMClient:
        from greenbean.llm.anthropic import AnthropicClient
        return AnthropicClient()
