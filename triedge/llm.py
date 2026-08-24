"""Model selection helpers.

Triedge works with no API key at all (deterministic fallbacks). When a key is
present, we lazily construct a chat model for the router and the sandbox agents.
"""

from __future__ import annotations

import os
from typing import Optional


def available_provider() -> Optional[str]:
    """Return 'openai' or 'anthropic' if a usable API key is configured."""
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def default_model_name() -> Optional[str]:
    """Provider-qualified model string for `init_chat_model` / deepagents."""
    provider = available_provider()
    if provider == "openai":
        return os.environ.get("TRIEDGE_MODEL", "openai:gpt-4o-mini")
    if provider == "anthropic":
        return os.environ.get("TRIEDGE_MODEL", "anthropic:claude-3-5-sonnet-latest")
    return None


def get_chat_model():
    """Construct a LangChain chat model, or return None if no key is set."""
    model_name = default_model_name()
    if not model_name:
        return None
    try:
        from langchain.chat_models import init_chat_model
    except ImportError:
        try:
            from langchain_core.language_models import init_chat_model  # type: ignore
        except ImportError:
            return None
    try:
        return init_chat_model(model_name)
    except Exception:
        return None
