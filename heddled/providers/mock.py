"""Deterministic offline provider — `model: mock/echo`.

Not a stub: it drives real tool calls so the whole spine (context → model →
tool → approval → reply) is exercisable with no API key and no network. It is
also what eval runs use in mock mode, and what the test suite asserts against.

Behaviour: it looks at the last user message, picks at most one mounted tool
whose name or keywords match, calls it, then answers from the tool result.
"""

from __future__ import annotations

import json
import re
import uuid

from .base import ModelResponse, Provider, ToolCall

# Words that hint at a tool, beyond the tool's own name.
_HINTS = {
    "lookup_invoice": ["invoice", "factuur", "bill", "f-"],
    "create_ticket": ["ticket", "issue", "escalate", "bug"],
    "refund": ["refund", "money back", "reimburse", "terugbetaling"],
    "get_weather": ["weather", "forecast", "temperature"],
}

_INVOICE_RE = re.compile(r"\b([A-Z]{1,3}-?\d{3,6})\b", re.I)
_AMOUNT_RE = re.compile(r"(?:eur|€|\$)\s*([0-9]+(?:[.,][0-9]{1,2})?)|\b([0-9]+(?:[.,][0-9]{1,2})?)\s*(?:eur|euro|€)", re.I)


def _guess_args(tool_name: str, schema: dict, text: str) -> dict:
    args = {}
    props = (schema or {}).get("properties") or {}
    inv = _INVOICE_RE.search(text)
    amt = _AMOUNT_RE.search(text)
    for field, spec in props.items():
        t = spec.get("type", "string")
        low = field.lower()
        if "invoice" in low and inv:
            args[field] = inv.group(1).upper()
        elif t in ("number", "integer") and amt:
            raw = amt.group(1) or amt.group(2)
            val = float(raw.replace(",", "."))
            args[field] = int(val) if t == "integer" else val
        elif t == "boolean":
            args[field] = False
        elif t in ("number", "integer"):
            args[field] = 1
        elif t == "array":
            args[field] = []
        elif t == "object":
            args[field] = {}
        else:
            args[field] = text.strip()[:200]
    return args


class MockProvider(Provider):
    name = "mock"

    supports_streaming = True

    def stream(self, system, messages, on_delta, tools=None, max_tokens=4096,
               temperature=None):
        """Chops the canned reply into word-sized pieces.

        The stand-in provider exists so the whole product can be tried with no
        account anywhere, and that has to include the way replies arrive — if
        streaming only worked once somebody added an API key, the first thing
        they saw would be the one behaviour we do not ship. No artificial delay:
        it is a test double, not a simulation.
        """
        response = self.complete(system, messages, tools=tools,
                                 max_tokens=max_tokens, temperature=temperature)
        text = response.text
        if text:
            pieces = re.findall(r"\S+\s*", text)
            for piece in pieces:
                on_delta(piece)
        return response

    def complete(self, system, messages, tools=None, max_tokens=4096, temperature=None):
        tools = tools or []
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content") or ""
                break

        tool_results = [m for m in messages if m.get("role") == "tool"]
        already_called = {m.get("name") for m in tool_results}

        lowered = last_user.lower()
        for t in tools:
            name = t["name"]
            if name in already_called:
                continue
            # Match on the words of the tool's own name, not just the whole
            # name. People now create tools from a form and call them whatever
            # they like (`office_location`, `order_status`), and an offline demo
            # where the agent ignores the tool you just made is a terrible first
            # run. Deliberately *not* matching on the description: shared words
            # there ("invoice" in both `lookup_invoice` and `refund`) make the
            # wrong tool fire.
            hints = (
                _HINTS.get(name, [])
                + [name.replace("_", " "), name]
                + [w for w in re.split(r"[_\s]+", name) if len(w) > 3]
            )
            if any(h.lower() in lowered for h in hints if h):
                args = _guess_args(name, t.get("input_schema"), last_user)
                return ModelResponse(
                    text=f"I'll use {name} for that.",
                    tool_calls=[ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=args)],
                    usage={"input_tokens": _tok(system, messages), "output_tokens": 24},
                    stop_reason="tool_use",
                    model=self.model,
                )

        if tool_results:
            parts = []
            for r in tool_results[-3:]:
                content = r.get("content") or ""
                parts.append(f"{r.get('name')}: {content}")
            text = "Here is what I found — " + "; ".join(parts)
        elif last_user:
            text = (
                f"[mock/echo] I received: \"{last_user.strip()[:400]}\". "
                f"No mounted tool matched, so this is a direct answer."
            )
        else:
            text = "[mock/echo] Nothing to do."

        return ModelResponse(
            text=text,
            usage={"input_tokens": _tok(system, messages), "output_tokens": max(1, len(text) // 4)},
            stop_reason="end_turn",
            model=self.model,
        )


def _tok(system: str, messages: list) -> int:
    n = len(system or "") // 4
    for m in messages:
        n += len(str(m.get("content") or "")) // 4
    return n
