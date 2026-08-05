"""Provider resolution: `provider/model` strings from agent files → an object."""

from __future__ import annotations

from typing import Optional

from .base import ModelResponse, Provider, ProviderError, ToolCall
from .mock import MockProvider

# Services that speak the OpenAI chat-completions API. Each gets its own key and
# base-URL setting, so several can be configured at once — previously they all
# shared `openai_api_key` / `openai_base_url`, which meant choosing between
# OpenAI and anything else rather than using both.
#
# Adding another is one entry here; the wire format is already handled.
OPENAI_COMPATIBLE = {
    "openai": {
        "label": "OpenAI",
        "base": "https://api.openai.com/v1",
        "example": "gpt-4o",
    },
    "deepseek": {
        "label": "DeepSeek",
        "base": "https://api.deepseek.com/v1",
        "example": "deepseek-chat",
    },
    "groq": {
        "label": "Groq",
        "base": "https://api.groq.com/openai/v1",
        "example": "llama-3.3-70b-versatile",
    },
    "mistral": {
        "label": "Mistral",
        "base": "https://api.mistral.ai/v1",
        "example": "mistral-large-latest",
    },
    "together": {
        "label": "Together",
        "base": "https://api.together.xyz/v1",
        "example": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base": "https://openrouter.ai/api/v1",
        "example": "anthropic/claude-sonnet-4",
    },
    "ollama": {
        "label": "Ollama (on this machine)",
        "short": "Ollama",          # used where the parenthetical would read badly
        "base": "http://localhost:11434/v1",
        "example": "llama3.2",
        "local": True,     # no key needed
    },
    "vllm": {
        "label": "vLLM (your own server)",
        "short": "vLLM",
        "base": "http://localhost:8000/v1",
        "example": "your-model",
        "local": True,
    },
}

# Rough €/1M tokens, used only for the budget ledger and the trace's cost hint.
# These are approximations that drift as providers change their prices —
# override per model under Settings (`pricing`) when the number needs to be right.
DEFAULT_PRICING = {
    "anthropic": {"input": 3.00, "output": 15.00},
    "openai": {"input": 2.50, "output": 10.00},
    "deepseek": {"input": 0.25, "output": 1.00},
    "groq": {"input": 0.60, "output": 0.80},
    "mistral": {"input": 2.00, "output": 6.00},
    "together": {"input": 0.80, "output": 0.80},
    "openrouter": {"input": 2.50, "output": 10.00},
    "ollama": {"input": 0.0, "output": 0.0},
    "vllm": {"input": 0.0, "output": 0.0},
    "mock": {"input": 0.0, "output": 0.0},
}


def split_model(model: str) -> tuple[str, str]:
    if "/" in model:
        provider, name = model.split("/", 1)
        return provider.lower(), name
    return "anthropic", model


def known_providers() -> list[dict]:
    """What the model picker and Settings offer."""
    out = [{"key": "anthropic", "label": "Anthropic", "example": "claude-sonnet-4-6",
            "local": False},
           {"key": "mock", "label": "Built-in stand-in (no account needed)",
            "example": "echo", "local": True}]
    for key, spec in OPENAI_COMPATIBLE.items():
        out.append({"key": key, "label": spec["label"], "example": spec["example"],
                    "local": bool(spec.get("local"))})
    return out


def get_provider(model: str, settings: dict = None) -> Provider:
    provider, name = split_model(model or "mock/echo")
    settings = settings or {}

    if provider in ("mock", "echo", "test"):
        return MockProvider(name, settings)
    if provider == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(name, settings)

    # `openai-compatible` and `local` kept as aliases so older agent files that
    # used them keep working.
    if provider in ("openai-compatible", "local"):
        provider = "openai"

    spec = OPENAI_COMPATIBLE.get(provider)
    if spec is not None:
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(name, settings, provider=provider, spec=spec)

    raise ProviderError(
        f"'{provider}' is not a model provider Heddled knows, in '{model}'. "
        f"Try one of: anthropic, " + ", ".join(sorted(OPENAI_COMPATIBLE)) + "."
    )


def estimate_cost_eur(model: str, usage: dict, settings: dict = None) -> float:
    provider, name = split_model(model or "")
    pricing = ((settings or {}).get("pricing") or {}).get(model) or DEFAULT_PRICING.get(
        provider, {"input": 0.0, "output": 0.0}
    )
    return round(
        (usage.get("input_tokens", 0) / 1_000_000) * pricing.get("input", 0)
        + (usage.get("output_tokens", 0) / 1_000_000) * pricing.get("output", 0),
        6,
    )


__all__ = [
    "Provider",
    "ProviderError",
    "ModelResponse",
    "ToolCall",
    "get_provider",
    "split_model",
    "estimate_cost_eur",
    "known_providers",
    "DEFAULT_PRICING",
    "OPENAI_COMPATIBLE",
]
