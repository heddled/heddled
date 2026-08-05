"""MCP client — mount a third-party MCP server's tools onto an agent (§12).

Speaks Streamable HTTP / JSON-RPC over `requests`, which covers the servers a
self-hosted Heddled is realistically pointed at. Tools discovered here look
exactly like file-backed tools to the engine.

Agent file:

    adapters:
      tools:
        - {mcp: {url: "https://example/mcp", name: "billing"}}
"""

from __future__ import annotations

import json
from typing import Any

import requests

from .registry import Tool, normalize_schema

_RPC_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def _rpc(url: str, method: str, params: dict = None, headers: dict = None,
         timeout: float = 30) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    resp = requests.post(url, json=body, timeout=timeout,
                         headers={**_RPC_HEADERS, **(headers or {})})
    resp.raise_for_status()
    text = resp.text.strip()
    if text.startswith("event:") or "\ndata:" in text:  # SSE-framed single reply
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    data = json.loads(text)
    if data.get("error"):
        raise RuntimeError(f"mcp error: {data['error']}")
    return data.get("result") or {}


def _spec(ref: dict) -> dict:
    return ref.get("mcp") if isinstance(ref.get("mcp"), dict) else ref


def discover_tools(ref: dict) -> list[Tool]:
    spec = _spec(ref)
    url = spec["url"]
    prefix = spec.get("name") or "mcp"
    headers = spec.get("headers") or {}
    if spec.get("token"):
        headers = {**headers, "Authorization": f"Bearer {spec['token']}"}

    result = _rpc(url, "tools/list", {}, headers)
    tools = []
    for t in result.get("tools", []):
        name = f"{prefix}_{t['name']}" if spec.get("prefix", True) else t["name"]
        tools.append(
            Tool(
                name=name,
                description=t.get("description", ""),
                input_schema=t.get("inputSchema") or normalize_schema(None),
                output_schema=normalize_schema(None),
                handler_path=None,
                dir=None,
                raw={"url": url, "remote_name": t["name"], "headers": headers},
                source="mcp",
            )
        )
    return tools


def make_mcp_tool_handler(raw: dict):
    def handle(args: dict, ctx):
        result = _rpc(
            raw["url"],
            "tools/call",
            {"name": raw["remote_name"], "arguments": args},
            raw.get("headers"),
        )
        content = result.get("content") or []
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        if texts:
            return "\n".join(texts)
        return result

    return handle
