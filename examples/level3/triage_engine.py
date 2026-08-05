"""Level 3: a custom turn engine (concept §6).

When the default loop isn't enough you implement the agent in Python against
the same interfaces. This one adds a deterministic **triage pass** before the
model is ever called: obvious invoice questions are answered straight from the
tool, skipping a model round trip entirely; everything else falls through to
the ordinary loop.

The point of the example is what does *not* change. This engine:

  * emits the same canonical events, so the console renders it identically;
  * mounts the same adapters and obeys the same policies (the refund gate below
    still pauses the turn and routes out of Heddled);
  * is reached through the same `submit_message` door as a YAML agent.

The platform cannot tell the difference, and neither can an external caller.

Mount it with:

    handler: ./triage_engine.py:TriageEngine
"""

from __future__ import annotations

import re

from heddled.engine import COMPLETED, TurnEngine
from heddled.events import MESSAGE_RECEIVED

# "F-2231", "INV-99" — an invoice number and nothing else worth reasoning about.
_JUST_AN_INVOICE = re.compile(r"^\s*(?:status of\s+)?([A-Z]{1,3}-?\d{3,6})\s*\??\s*$", re.I)


class TriageEngine(TurnEngine):
    """The default loop, plus a fast path in front of it."""

    def run(self, text: str, origin: dict = None, sender: str = None):
        match = _JUST_AN_INVOICE.match(text or "")
        if not match:
            # Nothing special about this one — hand it to the normal engine.
            return super().run(text, origin=origin, sender=sender)

        # Same bookkeeping the base engine does, because the console, replay and
        # evals all read the session from these events.
        self.state = self.store.get_state(self.session_id)
        self.state.setdefault("messages", [])
        self.state["channel"] = self.channel
        self.state["origin"] = origin or {}
        self.state["turn_id"] = self.turn_id
        self.state["pending_calls"] = []
        self.state["approvals"] = {}
        self.state["iteration"] = 0
        self.store.create_turn(self.turn_id, self.session_id)

        self.emit(
            MESSAGE_RECEIVED,
            {"text": text, "channel": self.channel, "sender": sender or "user",
             "origin": origin or {}},
        )
        self.state["messages"].append({"role": "user", "content": text})
        self._save()

        # A tool call, straight from the triage decision. `_execute_tool` is the
        # base engine's — so policies, argument validation, redaction, timing and
        # the tool.called / tool.result pair all behave exactly as usual.
        call = {
            "id": "triage-1",
            "name": "lookup_invoice",
            "arguments": {"invoice_number": match.group(1).upper()},
        }
        self.state["pending_calls"] = [call]
        if self._drain_pending_calls() == "paused":
            from heddled.engine import TurnResult

            return TurnResult(status="paused", session_id=self.session_id,
                              turn_id=self.turn_id,
                              approval_id=self.state.get("awaiting_approval_id"))

        last = self.state["messages"][-1]
        return self._finish(f"Triage fast path — {last.get('content')}")
