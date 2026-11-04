"""Build backend clients from the configuration."""

from __future__ import annotations

from homelab_agent.config.models import (
    AnthropicBackend,
    AppConfig,
    GeminiBackend,
    OpenAICompatBackend,
)
from homelab_agent.llm.anthropic import AnthropicClient
from homelab_agent.llm.base import LLMBackend
from homelab_agent.llm.gemini import GeminiClient
from homelab_agent.llm.openai_compat import OpenAICompatClient
from homelab_agent.observability.logging import MASKER


def build_backends(cfg: AppConfig) -> dict[str, LLMBackend]:
    out: dict[str, LLMBackend] = {}
    for name, b in cfg.backends.items():
        key = AppConfig.secret(b.api_key_env)
        MASKER.add(key)
        if isinstance(b, OpenAICompatBackend):
            out[name] = OpenAICompatClient(name, b, key)
        elif isinstance(b, AnthropicBackend):
            out[name] = AnthropicClient(name, b, key)
        elif isinstance(b, GeminiBackend):
            out[name] = GeminiClient(name, b, key)
    return out
