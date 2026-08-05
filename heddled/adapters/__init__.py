"""Adapter registry and the two functions the engine calls into."""

from __future__ import annotations

from typing import Optional

from .base import Adapter, DeliveryError
from .mailbox import MailboxPoller
from .slack import SlackAdapter, SlackApprovalAdapter
from .webchat import WebchatAdapter
from .webhook import WebhookAdapter, WebhookApprovalAdapter

CHANNELS = {
    "webchat": WebchatAdapter,
    "webhook": WebhookAdapter,
    "slack": SlackAdapter,
    "mcp": WebchatAdapter,  # inbound-only; replies return through the MCP result
}

APPROVAL_ADAPTERS = {
    "webhook": WebhookApprovalAdapter,
    "slack": SlackApprovalAdapter,
    "console": WebchatAdapter,
}

POLLERS = {
    "mailbox": MailboxPoller,
    "folder": MailboxPoller,
}


def channel_names(agent) -> list[str]:
    """Agent files may list channels as plain names or as single-key maps
    carrying config: `- webhook: {outbound_url: ...}`."""
    out = []
    for c in agent.channels:
        out.append(next(iter(c)) if isinstance(c, dict) else c)
    return out


def adapter_config(agent, name: str) -> dict:
    for c in agent.channels:
        if isinstance(c, dict) and name in c:
            return c[name] or {}
    return ((agent.raw.get("adapters") or {}).get("config") or {}).get(name, {}) or {}


def get_channel(agent, name: str, settings: dict) -> Optional[Adapter]:
    cls = CHANNELS.get(name)
    if not cls:
        return None
    return cls(adapter_config(agent, name), settings)


def get_poller(name: str, settings: dict) -> Optional[Adapter]:
    cls = POLLERS.get(name)
    return cls({}, settings) if cls else None


def deliver_outbound(agent, session, text: str, engine=None) -> Optional[dict]:
    """Send the reply out on the channel the session arrived on. Unknown or
    unmounted channels are a no-op, not an error — the reply is on the spine
    either way, and consumers can read it there.

    Returns the delivery receipt, which the caller attaches to `message.sent`.
    """
    if session is None:
        return None
    name = session["channel"] or "webchat"
    adapter = get_channel(agent, name, engine.settings if engine else {})
    if adapter is None:
        return {"delivered": "none", "reason": f"no adapter for channel '{name}'"}
    return adapter.send(session, text, engine)


def route_approval(agent, approval, engine=None) -> dict:
    """Deliver an approval request to wherever the human already works."""
    name = approval["routed_to"] or "webhook"
    cls = APPROVAL_ADAPTERS.get(name, WebhookApprovalAdapter)
    settings = engine.settings if engine else {}
    adapter = cls(adapter_config(agent, name), settings)
    return adapter.deliver_approval(agent, approval, engine)


__all__ = [
    "Adapter",
    "DeliveryError",
    "CHANNELS",
    "APPROVAL_ADAPTERS",
    "POLLERS",
    "channel_names",
    "adapter_config",
    "get_channel",
    "get_poller",
    "deliver_outbound",
    "route_approval",
]
