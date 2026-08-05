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

    def complete(self, system, messages, tools=None, max_tokens=4096, temperature=None):
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

        resp = requests.post(
            self._base() + "/chat/completions",
            headers={
                "Authorization": f"Bearer {self._key()}",
                "content-type": "application/json",
            },
            json=body,
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
