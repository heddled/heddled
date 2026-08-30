"""The chat channel — someone talking to an agent who does not operate it.

Deliberately a separate channel from `webchat`. They behave identically on the
wire (both read replies off the spine over SSE), but `allow_channels` and
`deny_channels` are security controls, and "an admin trying something on the
Test tab" is not the same context as "a colleague typing into a chat box". Two
names lets a policy say `refund` is fine from the console and never from chat;
one name would make that impossible to express.
"""

from .base import Adapter


class ChatAdapter(Adapter):
    name = "chat"
    kind = "channel"

    def send(self, session, text: str, engine=None) -> dict:
        # The chat page is a spine consumer, exactly as the console is.
        return {"delivered": "spine", "channel": "chat"}

    def deliver_approval(self, agent, approval, engine=None) -> dict:
        # Whoever is chatting cannot approve — that is the whole point of the
        # gate. The operator's console is the fallback, as ever.
        return {"delivered": "console-fallback", "approval_id": approval["id"]}
