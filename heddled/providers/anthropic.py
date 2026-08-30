"""Anthropic Messages API provider — `model: anthropic/claude-sonnet-4-6`.

Plain `requests` against the HTTP API rather than the SDK: one less dependency
for a self-hosted single binary-ish install, and the surface we need (system,
messages, tools, tool_result) is small and stable.
"""

from __future__ import annotations

import json
import os

import requests

from .base import ModelResponse, Provider, ProviderError, ToolCall

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    name = "anthropic"

    def _key(self) -> str:
        key = self.settings.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError(
                "No Anthropic API key. Set ANTHROPIC_API_KEY or add one under Settings."
            )
        return key

    def _body(self, system, messages, tools, max_tokens, temperature) -> dict:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": _to_anthropic(messages),
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("input_schema") or {"type": "object", "properties": {}},
                }
                for t in tools
            ]
        if temperature is not None:
            body["temperature"] = temperature
        return body

    def _endpoint(self) -> str:
        base = self.settings.get("anthropic_base_url") or os.environ.get(
            "ANTHROPIC_BASE_URL", "https://api.anthropic.com"
        )
        return base.rstrip("/") + "/v1/messages"

    def _headers(self) -> dict:
        return {
            "x-api-key": self._key(),
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    def complete(self, system, messages, tools=None, max_tokens=4096, temperature=None):
        resp = requests.post(
            self._endpoint(),
            headers=self._headers(),
            json=self._body(system, messages, tools, max_tokens, temperature),
            timeout=float(self.settings.get("timeout_s", 120)),
        )
        if resp.status_code >= 400:
            raise ProviderError(f"anthropic {resp.status_code}: {resp.text[:600]}")
        data = resp.json()

        text_parts, calls = [], []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(id=block["id"], name=block["name"], arguments=block.get("input") or {})
                )
        usage = data.get("usage") or {}
        return ModelResponse(
            text="".join(text_parts),
            tool_calls=calls,
            usage={
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            },
            stop_reason=data.get("stop_reason", "end_turn"),
            raw=data,
            model=data.get("model", self.model),
        )

    supports_streaming = True

    def stream(self, system, messages, on_delta, tools=None, max_tokens=4096,
               temperature=None):
        """Same request with `stream: true`, reassembled into one ModelResponse.

        Tool calls arrive as a series of JSON fragments that only parse once the
        block is complete, so they are buffered and decoded at `content_block_stop`
        rather than streamed — a half-built argument object is not something any
        caller could use.
        """
        body = dict(self._body(system, messages, tools, max_tokens, temperature),
                    stream=True)
        resp = requests.post(
            self._endpoint(), headers=self._headers(), json=body, stream=True,
            timeout=float(self.settings.get("timeout_s", 120)),
        )
        if resp.status_code >= 400:
            raise ProviderError(f"anthropic {resp.status_code}: {resp.text[:600]}")

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        stop_reason = "end_turn"
        model = self.model
        block: dict = {}
        partial = ""

        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except ValueError:
                continue
            kind = ev.get("type")

            if kind == "message_start":
                msg = ev.get("message") or {}
                model = msg.get("model", model)
                usage["input_tokens"] = (msg.get("usage") or {}).get("input_tokens", 0)
            elif kind == "content_block_start":
                block = ev.get("content_block") or {}
                partial = ""
            elif kind == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "text_delta":
                    piece = delta.get("text") or ""
                    if piece:
                        text_parts.append(piece)
                        on_delta(piece)
                elif delta.get("type") == "input_json_delta":
                    partial += delta.get("partial_json") or ""
            elif kind == "content_block_stop":
                if block.get("type") == "tool_use":
                    try:
                        args = json.loads(partial) if partial.strip() else {}
                    except ValueError:
                        args = {}
                    calls.append(ToolCall(id=block.get("id", ""),
                                          name=block.get("name", ""), arguments=args))
                block, partial = {}, ""
            elif kind == "message_delta":
                stop_reason = (ev.get("delta") or {}).get("stop_reason") or stop_reason
                usage["output_tokens"] = (ev.get("usage") or {}).get(
                    "output_tokens", usage["output_tokens"])
            elif kind == "error":
                raise ProviderError(
                    f"anthropic stream: {(ev.get('error') or {}).get('message', 'failed')}")

        return ModelResponse(
            text="".join(text_parts), tool_calls=calls, usage=usage,
            stop_reason=stop_reason, model=model,
        )


def _to_anthropic(messages: list[dict]) -> list[dict]:
    """Internal format → Anthropic content blocks. Consecutive tool results are
    merged into one user message, which is what the API expects."""
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m.get("content") or ""})
        elif role == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for c in m.get("tool_calls") or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": c["id"],
                        "name": c["name"],
                        "input": c.get("arguments") or {},
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id"),
                "content": m.get("content") or "",
            }
            if m.get("is_error"):
                block["is_error"] = True
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out
