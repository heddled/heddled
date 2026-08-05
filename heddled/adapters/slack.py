"""Slack channel + approval adapter — the Phase 2 "one real channel".

Outbound uses either an incoming-webhook URL (simplest self-host story) or a
bot token via chat.postMessage. Approvals go out as a message with approve/deny
links pointing back at Heddled's signed `/approve/<id>` endpoint, so the approver
resolves it from Slack and never opens the console.
"""

from __future__ import annotations

import json
import os

import requests

from .base import Adapter, DeliveryError
from .webhook import _base_url


def _post(cfg: dict, settings: dict, blocks: list, text: str) -> dict:
    hook = cfg.get("webhook_url") or settings.get("slack_webhook_url") or os.environ.get(
        "HEDDLED_SLACK_WEBHOOK_URL"
    )
    token = cfg.get("bot_token") or settings.get("slack_bot_token") or os.environ.get(
        "HEDDLED_SLACK_BOT_TOKEN"
    )
    channel = cfg.get("channel") or settings.get("slack_channel")

    if hook:
        resp = requests.post(hook, json={"text": text, "blocks": blocks}, timeout=15)
        if resp.status_code >= 400:
            raise DeliveryError(f"slack webhook {resp.status_code}: {resp.text[:300]}")
        return {"delivered": "slack:webhook"}
    if token and channel:
        resp = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}", "content-type": "application/json"},
            json={"channel": channel, "text": text, "blocks": blocks},
            timeout=15,
        )
        data = resp.json()
        if not data.get("ok"):
            raise DeliveryError(f"slack api: {data.get('error')}")
        return {"delivered": "slack:api", "ts": data.get("ts")}
    return {"delivered": "none", "reason": "slack not configured (webhook_url or bot_token+channel)"}


class SlackAdapter(Adapter):
    name = "slack"
    kind = "channel"

    def send(self, session, text: str, engine=None) -> dict:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        return _post(self.config, self.settings, blocks, text)


class SlackApprovalAdapter(Adapter):
    name = "slack"
    kind = "approval"

    def deliver_approval(self, agent, approval, engine=None) -> dict:
        base = _base_url(self.settings)
        aid, token = approval["id"], approval["token"]
        args = json.loads(approval["args"])
        approve = f"{base}/approve/{aid}?token={token}&decision=approved"
        deny = f"{base}/approve/{aid}?token={token}&decision=denied"
        pretty = "\n".join(f"• *{k}*: `{v}`" for k, v in args.items()) or "_no arguments_"
        text = f"Approval needed: {agent.name} wants to call `{approval['tool']}`"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn",
                                         "text": f"*{text}*\n{approval['reason'] or ''}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": pretty}},
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "style": "primary", "url": approve,
                     "text": {"type": "plain_text", "text": "Approve"}},
                    {"type": "button", "style": "danger", "url": deny,
                     "text": {"type": "plain_text", "text": "Deny"}},
                    {"type": "button", "url": f"{base}/sessions/{approval['session_id']}",
                     "text": {"type": "plain_text", "text": "View trace"}},
                ],
            },
        ]
        result = _post(self.config, self.settings, blocks, text)
        result.update({"approve_url": approve, "deny_url": deny})
        return result
