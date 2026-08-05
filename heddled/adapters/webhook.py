"""Generic webhook channel + the reference approval adapter.

Inbound lives in the HTTP layer (`POST /api/agents/<name>/webhook`); this module
is the outbound half: POST the reply to a configured URL, and POST an approval
request carrying the proposed action, exact arguments, and signed approve/deny
links so the human never opens Heddled (concept §8).
"""

from __future__ import annotations

import json
import os

import requests

from .base import Adapter, DeliveryError


def _base_url(settings: dict) -> str:
    return (
        settings.get("public_url")
        or os.environ.get("HEDDLED_PUBLIC_URL")
        or f"http://localhost:{os.environ.get('HEDDLED_PORT', '5005')}"
    ).rstrip("/")


class WebhookAdapter(Adapter):
    name = "webhook"
    kind = "channel"

    def send(self, session, text: str, engine=None) -> dict:
        url = self.config.get("outbound_url") or self.settings.get("webhook_outbound_url")
        if not url:
            return {"delivered": "none", "reason": "no outbound_url configured"}
        payload = {
            "session_id": session["id"],
            "agent": session["agent"],
            "text": text,
            "channel": "webhook",
        }
        resp = requests.post(url, json=payload, timeout=15,
                             headers=self.config.get("headers") or {})
        if resp.status_code >= 400:
            raise DeliveryError(f"outbound webhook {resp.status_code}: {resp.text[:300]}")
        return {"delivered": url, "status": resp.status_code}


class WebhookApprovalAdapter(Adapter):
    """The reference implementation for out-of-Heddled approval. Everything nicer
    (Teams card, Slack Block Kit, signed email) is a skin over these two events."""

    name = "webhook"
    kind = "approval"

    def deliver_approval(self, agent, approval, engine=None) -> dict:
        base = _base_url(self.settings)
        token = approval["token"]
        aid = approval["id"]
        body = {
            "type": "approval.requested",
            "approval_id": aid,
            "agent": agent.name,
            "agent_version": agent.version,
            "session_id": approval["session_id"],
            "turn_id": approval["turn_id"],
            "tool": approval["tool"],
            "arguments": json.loads(approval["args"]),
            "reason": approval["reason"],
            "approve_url": f"{base}/approve/{aid}?token={token}&decision=approved",
            "deny_url": f"{base}/approve/{aid}?token={token}&decision=denied",
            "review_url": f"{base}/sessions/{approval['session_id']}",
        }
        url = self.config.get("url") or self.settings.get("approval_webhook_url")
        if not url:
            # No adapter configured: the console's waiting-approval filter is the
            # documented fallback consumer of the same events.
            return {"delivered": "console-fallback", **{k: body[k] for k in
                                                        ("approve_url", "deny_url")}}
        resp = requests.post(url, json=body, timeout=15,
                             headers=self.config.get("headers") or {})
        if resp.status_code >= 400:
            raise DeliveryError(f"approval webhook {resp.status_code}: {resp.text[:300]}")
        return {"delivered": url, "status": resp.status_code,
                "approve_url": body["approve_url"], "deny_url": body["deny_url"]}
