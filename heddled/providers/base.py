"""Provider-agnostic model interface.

Heddled's internal message format is deliberately small:

    {"role": "user"|"assistant"|"tool", "content": str,
     "tool_calls": [{"id","name","arguments"}],   # assistant only
     "tool_call_id": str, "name": str}            # tool only

Each provider translates to and from its own wire format, so swapping
`model:` in an agent file is the whole migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ModelResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    stop_reason: str = "end_turn"
    raw: Any = None
    model: str = ""

    def to_payload(self) -> dict:
        return {
            "text": self.text,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in self.tool_calls
            ],
            "usage": self.usage,
            "stop_reason": self.stop_reason,
            "model": self.model,
        }


class Provider:
    """Base class. `complete` is synchronous by design — turns are I/O-bound and
    run on worker threads (decision 3)."""

    name = "base"

    def __init__(self, model: str, settings: dict = None):
        self.model = model
        self.settings = settings or {}

    def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict] = None,
        max_tokens: int = 4096,
        temperature: float = None,
    ) -> ModelResponse:
        raise NotImplementedError


class ProviderError(RuntimeError):
    pass
