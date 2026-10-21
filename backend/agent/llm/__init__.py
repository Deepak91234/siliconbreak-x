"""LLM provider selection.

`get_provider()` returns None when no provider is configured (no API key), which
switches the agent to its deterministic offline paths: rule-based intent parsing
and the preverified template generators. Callers must treat None as "no LLM",
never as an error.
"""

from __future__ import annotations

import logging

from backend.core.config import Settings, get_settings

from .base import LLMError, LLMProvider, LLMResult

log = logging.getLogger(__name__)

__all__ = ["LLMError", "LLMProvider", "LLMResult", "get_provider"]


def get_provider(settings: Settings | None = None) -> LLMProvider | None:
    s = settings or get_settings()
    if s.llm_provider != "anthropic":
        log.warning("unknown LLM provider %r configured; running without an LLM", s.llm_provider)
        return None
    if not s.anthropic_api_key:
        return None
    from .anthropic_provider import AnthropicProvider

    return AnthropicProvider(s.anthropic_api_key, s.llm_model)
