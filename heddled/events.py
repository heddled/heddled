"""The event contract — Heddled's most important API.

Every turn flows through the spine as a sequence of these records. Consumers
(trace store, console, eval runner) only ever see events; adapters only ever
produce or react to them.
"""

from dataclasses import dataclass, field, asdict
from typing import Any, Optional
import time
import uuid

CONTRACT_VERSION = "1"

# Canonical event set (v1). Ordered roughly by where they appear in a turn.
TRIGGER_FIRED = "trigger.fired"
MESSAGE_RECEIVED = "message.received"
CONTEXT_BUILT = "context.built"
MODEL_INVOKED = "model.invoked"
MODEL_RESPONDED = "model.responded"
TOOL_CALLED = "tool.called"
TOOL_RESULT = "tool.result"
APPROVAL_REQUESTED = "approval.requested"
APPROVAL_RESOLVED = "approval.resolved"
OPERATOR_INJECTED = "operator.injected"
MESSAGE_SENT = "message.sent"
TURN_COMPLETED = "turn.completed"
ERROR_RAISED = "error.raised"

EVENT_TYPES = [
    TRIGGER_FIRED,
    MESSAGE_RECEIVED,
    CONTEXT_BUILT,
    MODEL_INVOKED,
    MODEL_RESPONDED,
    TOOL_CALLED,
    TOOL_RESULT,
    APPROVAL_REQUESTED,
    APPROVAL_RESOLVED,
    OPERATOR_INJECTED,
    MESSAGE_SENT,
    TURN_COMPLETED,
    ERROR_RAISED,
]

# Console colour classes, kept next to the contract so a new event type can't
# be added without deciding how it renders.
EVENT_CLASS = {
    TRIGGER_FIRED: "ev-trigger",
    MESSAGE_RECEIVED: "ev-in",
    CONTEXT_BUILT: "ev-context",
    MODEL_INVOKED: "ev-model",
    MODEL_RESPONDED: "ev-model",
    TOOL_CALLED: "ev-tool",
    TOOL_RESULT: "ev-tool",
    APPROVAL_REQUESTED: "ev-approval",
    APPROVAL_RESOLVED: "ev-approval",
    OPERATOR_INJECTED: "ev-operator",
    MESSAGE_SENT: "ev-out",
    TURN_COMPLETED: "ev-done",
    ERROR_RAISED: "ev-error",
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass
class Event:
    """One structured record on the spine."""

    type: str
    session_id: str
    turn_id: Optional[str] = None
    agent: Optional[str] = None
    agent_version: Optional[str] = None
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    seq: Optional[int] = None  # assigned by the store on append
    id: Optional[int] = None  # rowid, assigned by the store

    def to_dict(self) -> dict:
        d = asdict(self)
        d["contract"] = CONTRACT_VERSION
        return d

    @property
    def summary(self) -> str:
        """One-line description used by the timeline column."""
        p = self.payload or {}
        if self.type == TRIGGER_FIRED:
            return f"{p.get('kind', '?')} · {p.get('reason', '')}".strip(" ·")
        if self.type in (MESSAGE_RECEIVED, MESSAGE_SENT):
            text = (p.get("text") or "").replace("\n", " ")
            return (text[:90] + "…") if len(text) > 90 else text
        if self.type == CONTEXT_BUILT:
            return f"{p.get('message_count', 0)} messages · {p.get('tool_count', 0)} tools"
        if self.type == MODEL_INVOKED:
            return p.get("model", "")
        if self.type == MODEL_RESPONDED:
            u = p.get("usage") or {}
            bits = []
            if p.get("tool_calls"):
                bits.append(f"{len(p['tool_calls'])} tool call(s)")
            if u:
                bits.append(f"{u.get('input_tokens', 0)}→{u.get('output_tokens', 0)} tok")
            if p.get("duration_ms") is not None:
                bits.append(f"{p['duration_ms']} ms")
            return " · ".join(bits)
        if self.type in (TOOL_CALLED, TOOL_RESULT):
            s = p.get("tool", "")
            if p.get("partial"):
                return f"{s} · log: {p.get('log', '')}"
            if self.type == TOOL_RESULT:
                s += " · error" if p.get("error") else " · ok"
                if p.get("duration_ms") is not None:
                    s += f" · {p['duration_ms']} ms"
            return s
        if self.type == APPROVAL_REQUESTED:
            return f"{p.get('tool', '')} · awaiting {p.get('routed_to', 'approver')}"
        if self.type == APPROVAL_RESOLVED:
            return f"{p.get('tool', '')} · {p.get('decision', '')} by {p.get('resolver', '?')}"
        if self.type == OPERATOR_INJECTED:
            return (p.get("text") or "")[:90]
        if self.type == TURN_COMPLETED:
            return f"{p.get('status', '')} · {p.get('duration_ms', 0)} ms"
        if self.type == ERROR_RAISED:
            return f"{p.get('kind', 'error')}: {p.get('message', '')}"[:110]
        return ""
