"""Web-chat dev-harness channel.

Test surface only — never a product frontend (concept, Phase 0). Outbound
delivery is a no-op because the console reads the reply straight off the spine
over SSE; the adapter exists so the channel is a real, named thing in traces.
"""

from .base import Adapter


class WebchatAdapter(Adapter):
    name = "webchat"
    kind = "channel"

    def send(self, session, text: str, engine=None) -> dict:
        # The console is a spine consumer; message.sent is already appended.
        return {"delivered": "spine", "channel": "webchat"}

    def deliver_approval(self, agent, approval, engine=None) -> dict:
        # Fallback surface: the waiting-approval filter on Sessions (§8).
        return {"delivered": "console-fallback", "approval_id": approval["id"]}
