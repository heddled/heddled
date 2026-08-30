"""OpenAI-compatible chat completions.

One wire format, many services: `openai/gpt-4o`, `deepseek/deepseek-chat`,
`groq/llama-3.3-70b-versatile`, `ollama/llama3.2` and so on. Each carries its own
key and base URL (`deepseek_api_key`, `deepseek_base_url`, …) so more than one
can be configured at a time — sharing a single `openai_api_key` between them
forced a choice between OpenAI and everything else.
"""

from __future__ import annotations

import json
import os
import uuid

import requests

from .base import ModelResponse, Provider, ProviderError, ToolCall


class OpenAICompatProvider(Provider):
    name = "openai"

    def __init__(self, model, settings=None, provider="openai", spec=None):
        super().__init__(model, settings)
        self.provider = provider
        self.spec = spec or {}
        self.name = provider

    def _key(self) -> str:
        key = (self.settings.get(f"{self.provider}_api_key")
               or os.environ.get(f"{self.provider.upper()}_API_KEY"))
        if not key and self.spec.get("local"):
            return "not-needed"
        if not key:
            label = self.spec.get("label", self.provider)
            raise ProviderError(
                f"No {label} API key. Add {self.provider}_api_key under Settings, "
                f"or set {self.provider.upper()}_API_KEY."
            )
        return key

    def _base(self) -> str:
        return (
            self.settings.get(f"{self.provider}_base_url")
            or os.environ.get(f"{self.provider.upper()}_BASE_URL")
            or self.spec.get("base")
            or "https://api.openai.com/v1"
        ).rstrip("/")

    def _body(self, system, messages, tools, max_tokens, temperature) -> dict:
        body = {
            "model": self.model,
            "messages": _to_openai(system, messages),
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema")
                        or {"type": "object", "properties": {}},
                    },
                }
                for t in tools
            ]
        if temperature is not None:
            body["temperature"] = temperature
        return body

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._key()}",
                "content-type": "application/json"}

    def complete(self, system, messages, tools=None, max_tokens=4096, temperature=None):
        resp = requests.post(
            self._base() + "/chat/completions",
            headers=self._headers(),
            json=self._body(system, messages, tools, max_tokens, temperature),
            timeout=float(self.settings.get("timeout_s", 120)),
        )
        if resp.status_code >= 400:
            raise ProviderError(
                f"{self.spec.get('label', self.provider)} {resp.status_code}: "
                f"{resp.text[:600]}")
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}

        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            calls.append(ToolCall(id=c.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                                  name=fn.get("name", ""), arguments=args))

        usage = data.get("usage") or {}
        return ModelResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
            stop_reason=choice.get("finish_reason", "stop"),
            raw=data,
            model=data.get("model", self.model),
        )


    supports_streaming = True

    def stream(self, system, messages, on_delta, tools=None, max_tokens=4096,
               temperature=None):
        """Same request with `stream: true`.

        Tool calls arrive as fragments indexed by position, so they are collected
        into a dict keyed by that index and decoded at the end — the arguments
        are a JSON string built one piece at a time and cannot be parsed until it
        is whole. `stream_options` asks for a usage report, which most
        OpenAI-compatible services honour and the rest simply ignore; when it is
        missing the turn still completes, it just contributes nothing to the
        token budget.
        """
        body = dict(self._body(system, messages, tools, max_tokens, temperature),
                    stream=True)
        body["stream_options"] = {"include_usage": True}
        resp = requests.post(
            self._base() + "/chat/completions", headers=self._headers(),
            json=body, stream=True,
            timeout=float(self.settings.get("timeout_s", 120)),
        )
        if resp.status_code >= 400:
            raise ProviderError(
                f"{self.spec.get('label', self.provider)} {resp.status_code}: "
                f"{resp.text[:600]}")

        text_parts: list[str] = []
        pending: dict = {}
        usage = {"input_tokens": 0, "output_tokens": 0}
        stop_reason = "end_turn"
        model = self.model

        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                ev = json.loads(chunk)
            except ValueError:
                continue

            model = ev.get("model", model)
            if ev.get("usage"):
                usage["input_tokens"] = ev["usage"].get("prompt_tokens", 0)
                usage["output_tokens"] = ev["usage"].get("completion_tokens", 0)

            for choice in ev.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    text_parts.append(piece)
                    on_delta(piece)
                for call in delta.get("tool_calls") or []:
                    slot = pending.setdefault(
                        call.get("index", 0), {"id": "", "name": "", "args": ""})
                    if call.get("id"):
                        slot["id"] = call["id"]
                    fn = call.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
                if choice.get("finish_reason"):
                    stop_reason = choice["finish_reason"]

        calls = []
        for _, slot in sorted(pending.items()):
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
            except ValueError:
                args = {}
            calls.append(ToolCall(id=slot["id"] or f"call_{uuid.uuid4().hex[:12]}",
                                  name=slot["name"], arguments=args))

        return ModelResponse(
            text="".join(text_parts), tool_calls=calls, usage=usage,
            stop_reason="tool_calls" if calls else stop_reason, model=model,
        )


def _to_openai(system: str, messages: list[dict]) -> list[dict]:
    out = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m.get("content") or ""})
        elif role == "assistant":
            entry = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                entry["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c.get("arguments") or {}),
                        },
                    }
                    for c in m["tool_calls"]
                ]
            out.append(entry)
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id"),
                    "content": m.get("content") or "",
                }
            )
    return out
