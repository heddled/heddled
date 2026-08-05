"""Adapter interface.

Principle 2: everything is an adapter or a consumer. An adapter moves messages
in and/or out. Channels and tools are the same interface — tools happen to be
typed and model-invocable, channels happen to carry free text.

Inbound adapters call `runtime.submit_message(...)`; outbound adapters
implement `send`. An adapter that does both (webchat, Teams) implements both.
"""

from __future__ import annotations

from typing import Any, Optional


class Adapter:
    name = "base"
    kind = "channel"  # channel | approval | poller

    def __init__(self, config: dict = None, settings: dict = None):
        self.config = config or {}
        self.settings = settings or {}

    # outbound
    def send(self, session, text: str, engine=None) -> dict:
        raise NotImplementedError

    # approval routing
    def deliver_approval(self, agent, approval, engine=None) -> dict:
        raise NotImplementedError

    # pull trigger
    def poll(self, cursor, config: dict) -> tuple[list[dict], Any]:
        """Return (new_items, new_cursor). Each item becomes one turn."""
        raise NotImplementedError


class DeliveryError(RuntimeError):
    pass
