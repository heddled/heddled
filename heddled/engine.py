"""The turn engine.

Deliberately boring (concept §5): receive message → build context → call model →
execute tool calls → repeat until done → emit reply. All the sophistication is
in what flows through it and who watches.

The one non-obvious property is that a turn is **resumable**. Because approvals
route out of Heddled and come back minutes or days later, the engine persists its
working state after every step and can be re-entered from a job. `run_turn` and
`resume_turn` are the same loop with a different entry point.
"""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Optional

from . import config, policies
from .events import (
    APPROVAL_REQUESTED,
    CONTEXT_BUILT,
    ERROR_RAISED,
    MESSAGE_RECEIVED,
    MESSAGE_SENT,
    MODEL_INVOKED,
    MODEL_RESPONDED,
    TOOL_CALLED,
    TOOL_RESULT,
    TURN_COMPLETED,
    Event,
    new_id,
)
from .providers import ProviderError, estimate_cost_eur, get_provider
from .registry import get_registry, validate_args

PAUSED = "paused"
COMPLETED = "completed"
FAILED = "failed"


class ToolContext:
    """What a tool handler receives as its second argument."""

    def __init__(self, engine: "TurnEngine", tool_name: str):
        self.engine = engine
        self.tool = tool_name
        self.session_id = engine.session_id
        self.turn_id = engine.turn_id
        self.agent = engine.agent.name
        self.agent_version = engine.agent.version
        self.channel = engine.channel
        self.store = engine.store
        self.settings = engine.settings

    def log(self, message: str, **extra) -> None:
        """Anything a handler wants on the spine goes through here."""
        self.engine.emit(
            "tool.result",
            {"tool": self.tool, "log": message, "partial": True, **extra},
        )

    def memory(self) -> dict:
        return self.engine.state.setdefault("memory", {})


@dataclass
class TurnResult:
    status: str
    reply: str = ""
    session_id: str = ""
    turn_id: str = ""
    approval_id: Optional[str] = None
    error: Optional[str] = None


class TurnEngine:
    """One instance per turn execution (or resumption)."""

    def __init__(self, store, agent, session_id: str, turn_id: str,
                 channel: str = "webchat", tool_mocks: dict = None,
                 emit_hook=None, call_chain: list = None, caller: str = None):
        self.store = store
        self.agent = agent
        self.session_id = session_id
        self.turn_id = turn_id
        self.channel = channel
        # Who is driving this turn from outside — an MCP caller, an API key
        # holder, a parent agent. Policies can key on it (§12).
        self.caller = caller
        self.registry = get_registry()
        self.settings = store.all_settings()
        self.tool_mocks = tool_mocks  # eval replay: {tool: result}
        self.emit_hook = emit_hook
        self.state: dict = {}
        self.redact_rules = policies.agent_redaction_rules(agent)
        self._secret_values = policies.secret_values(self.settings)
        self.call_chain = call_chain or []
        self._t0 = time.time()

    # -------------------------------------------------------------- spine I/O

    def emit(self, type_: str, payload: dict) -> Event:
        # Two different things, in order. The agent's own redaction rules are a
        # choice about customer data; stripping stored credentials is not
        # optional and applies to every event regardless of configuration.
        payload = policies.redact_value(payload, self.redact_rules)
        payload = policies.strip_secrets(payload, self._secret_values)
        ev = self.store.append(
            Event(
                type=type_,
                session_id=self.session_id,
                turn_id=self.turn_id,
                agent=self.agent.name,
                agent_version=self.agent.version,
                payload=payload,
            )
        )
        if self.emit_hook:
            try:
                self.emit_hook(ev)
            except Exception:
                pass
        return ev

    # ------------------------------------------------------------ entry points

    def run(self, text: str, origin: dict = None, sender: str = None) -> TurnResult:
        """Start a fresh turn from an inbound message."""
        self.state = self.store.get_state(self.session_id)
        self.state.setdefault("messages", [])
        self.state["channel"] = self.channel
        self.state["origin"] = origin or {}
        self.state["turn_id"] = self.turn_id
        self.store.create_turn(self.turn_id, self.session_id)
        self.emit(
            MESSAGE_RECEIVED,
            {"text": text, "channel": self.channel, "sender": sender or "user",
             "origin": origin or {}},
        )
        self.state["messages"].append({"role": "user", "content": text})
        self.state["pending_calls"] = []
        self.state["approvals"] = {}
        self.state["iteration"] = 0
        self._save()
        return self._loop()

    def resume(self) -> TurnResult:
        """Re-enter a paused turn (an approval came back, or an operator
        injected a message)."""
        self.state = self.store.get_state(self.session_id)
        self.state.setdefault("messages", [])
        self.channel = self.state.get("channel", self.channel)
        self.store.set_turn_status(self.turn_id, "running")
        self.store.update_session(self.session_id, status="running")
        return self._loop()

    # ------------------------------------------------------------- the loop

    def _loop(self) -> TurnResult:
        try:
            while True:
                if self.state.get("iteration", 0) > config.MAX_TOOL_ITERATIONS:
                    return self._fail(
                        "iteration_limit",
                        f"agent exceeded {config.MAX_TOOL_ITERATIONS} tool iterations",
                    )

                # 1. Any tool calls left over from the last model response?
                if self.state.get("pending_calls"):
                    outcome = self._drain_pending_calls()
                    if outcome == PAUSED:
                        return TurnResult(
                            status=PAUSED,
                            session_id=self.session_id,
                            turn_id=self.turn_id,
                            approval_id=self.state.get("awaiting_approval_id"),
                        )
                    continue

                # 2. Otherwise take a model step.
                block = policies.check_turn_budget(self.agent, self.store, self.session_id)
                if block:
                    return self._fail("policy_denied", block)

                response = self._model_step()
                if response is None:
                    return TurnResult(status=FAILED, session_id=self.session_id,
                                      turn_id=self.turn_id, error=self.state.get("error"))

                if not response.tool_calls:
                    return self._finish(response.text)

                self.state["messages"].append(
                    {
                        "role": "assistant",
                        "content": response.text,
                        "tool_calls": [
                            {"id": c.id, "name": c.name, "arguments": c.arguments}
                            for c in response.tool_calls
                        ],
                    }
                )
                self.state["pending_calls"] = [
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in response.tool_calls
                ]
                self.state["iteration"] = self.state.get("iteration", 0) + 1
                self._save()
        except Exception as exc:  # never let a turn die silently
            return self._fail(type(exc).__name__, str(exc), traceback.format_exc())

    # ---------------------------------------------------------- model calling

    def _build_context(self) -> tuple[str, list[dict], list[dict]]:
        tools = self.registry.agent_tools(self.agent)
        tool_schemas = [t.to_model_schema() for t in tools.values()]
        system = self.agent.instructions or ""
        memory = self.state.get("memory") or {}
        if memory:
            system += "\n\n## Session memory\n" + json.dumps(memory, indent=2)
        summary = self.state.get("summary")
        if summary:
            system += "\n\n## Earlier in this session\n" + summary
        messages = self._windowed_messages()
        return system, messages, tool_schemas

    def _windowed_messages(self) -> list[dict]:
        """`memory: session: auto` — keep a rolling window and summarise what
        falls out of it, so long sessions do not grow the context forever."""
        messages = self.state.get("messages", [])
        mode = (self.agent.memory or {}).get("session", "auto")
        limit = int((self.agent.memory or {}).get("max_messages", 40))
        if mode in ("none", "off"):
            # only the current turn's user message
            for m in reversed(messages):
                if m.get("role") == "user":
                    return [m]
            return messages[-1:]
        if mode == "auto" and len(messages) > limit:
            dropped = messages[:-limit]
            kept = messages[-limit:]
            # do not start the window on a tool result with no matching call
            while kept and kept[0].get("role") == "tool":
                kept = kept[1:]
            self.state["summary"] = _summarize(dropped, self.state.get("summary"))
            return kept
        return messages

    def _model_step(self):
        system, messages, tool_schemas = self._build_context()
        ctx_event = self.emit(
            CONTEXT_BUILT,
            {
                "model": self.agent.model,
                "system": system,
                "messages": messages,
                "tools": tool_schemas,
                "message_count": len(messages),
                "tool_count": len(tool_schemas),
                "iteration": self.state.get("iteration", 0),
            },
        )
        self.emit(
            MODEL_INVOKED,
            {"model": self.agent.model, "context_seq": ctx_event.seq,
             "tool_count": len(tool_schemas)},
        )
        t0 = time.time()
        try:
            provider = get_provider(self.agent.model, self.settings)
            response = provider.complete(system=system, messages=messages, tools=tool_schemas)
        except ProviderError as exc:
            self.state["error"] = str(exc)
            self.emit(ERROR_RAISED, {"kind": "provider_error", "message": str(exc)})
            self._fail("provider_error", str(exc))
            return None
        except Exception as exc:
            self.state["error"] = str(exc)
            self.emit(
                ERROR_RAISED,
                {"kind": "model_call_failed", "message": str(exc),
                 "trace": traceback.format_exc()},
            )
            self._fail("model_call_failed", str(exc))
            return None

        duration_ms = int((time.time() - t0) * 1000)
        cost = estimate_cost_eur(self.agent.model, response.usage, self.settings)
        payload = response.to_payload()
        payload["duration_ms"] = duration_ms
        payload["cost_eur"] = cost
        self.emit(MODEL_RESPONDED, payload)

        tokens = response.usage.get("input_tokens", 0) + response.usage.get("output_tokens", 0)
        if tokens:
            self.store.record_spend("tokens", tokens, agent=self.agent.name,
                                    session_id=self.session_id)
        if cost:
            self.store.record_spend("eur", cost, agent=self.agent.name,
                                    session_id=self.session_id)
        return response

    # ----------------------------------------------------------- tool calling

    def _drain_pending_calls(self) -> Optional[str]:
        """Execute queued tool calls in order. Returns PAUSED if one of them
        needs a human first; the remaining calls stay queued."""
        while self.state.get("pending_calls"):
            call = self.state["pending_calls"][0]
            decision_key = call["id"]
            verdict = (self.state.get("approvals") or {}).get(decision_key)

            if verdict is None:
                decision = policies.check_tool_call(
                    self.agent, call["name"], self.channel, self.store, self.session_id,
                    caller=self.caller,
                )
                if not decision.allowed:
                    self.emit(
                        ERROR_RAISED,
                        {
                            "kind": "policy_denied",
                            "tool": call["name"],
                            "message": decision.reason,
                            "policy": decision.policy,
                        },
                    )
                    self._append_tool_result(call, f"Denied by policy: {decision.reason}",
                                             is_error=True)
                    continue
                if decision.requires_approval:
                    self._request_approval(call, decision)
                    return PAUSED
            elif verdict == "denied":
                note = (self.state.get("approval_notes") or {}).get(decision_key) or ""
                self._append_tool_result(
                    call,
                    f"Denied by approver. {note}".strip(),
                    is_error=True,
                )
                continue

            self._execute_tool(call)
        return None

    def _execute_tool(self, call: dict) -> None:
        name, args = call["name"], call.get("arguments") or {}
        tool = self.registry.agent_tools(self.agent).get(name)
        started = time.time()
        self.emit(
            TOOL_CALLED,
            {"tool": name, "arguments": args, "call_id": call["id"],
             "mocked": bool(self.tool_mocks and name in self.tool_mocks)},
        )

        if tool is None:
            self._append_tool_result(
                call, f"Tool '{name}' is not mounted on agent '{self.agent.name}'.", is_error=True,
                duration_ms=0,
            )
            return

        errors = validate_args(tool.input_schema, args)
        if errors:
            self._append_tool_result(
                call, "Invalid arguments: " + "; ".join(errors), is_error=True, duration_ms=0
            )
            return

        # Eval replay: play back the recorded result instead of touching the world.
        if self.tool_mocks is not None and name in self.tool_mocks:
            result = self.tool_mocks[name]
            self._append_tool_result(call, result, duration_ms=0, mocked=True)
            return

        try:
            handler = tool.load_handler()
            ctx = ToolContext(self, name)
            result = handler(args, ctx)
            duration = int((time.time() - started) * 1000)
            if tool.cost_eur:
                self.store.record_spend("eur", tool.cost_eur, agent=self.agent.name,
                                        session_id=self.session_id, tool=name)
            self._append_tool_result(call, result, duration_ms=duration)
        except Exception as exc:
            duration = int((time.time() - started) * 1000)
            self.emit(
                ERROR_RAISED,
                {"kind": "tool_failed", "tool": name, "message": str(exc),
                 "trace": traceback.format_exc()},
            )
            self._append_tool_result(
                call, f"{type(exc).__name__}: {exc}", is_error=True, duration_ms=duration
            )

    def _append_tool_result(self, call: dict, result, is_error: bool = False,
                            duration_ms: int = None, mocked: bool = False) -> None:
        content = result if isinstance(result, str) else json.dumps(result, default=str)
        self.emit(
            TOOL_RESULT,
            {
                "tool": call["name"],
                "call_id": call["id"],
                "result": result,
                "error": is_error,
                "duration_ms": duration_ms,
                "mocked": mocked,
            },
        )
        self.state["messages"].append(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": content,
                "is_error": is_error,
            }
        )
        self.state["pending_calls"] = [
            c for c in self.state.get("pending_calls", []) if c["id"] != call["id"]
        ]
        self._save()

    # -------------------------------------------------------------- approvals

    def _request_approval(self, call: dict, decision) -> None:
        from .adapters import route_approval

        approval_id = self.store.create_approval(
            session_id=self.session_id,
            turn_id=self.turn_id,
            agent=self.agent.name,
            tool=call["name"],
            args=call.get("arguments") or {},
            reason=decision.reason,
            routed_to=decision.approval_adapter,
            token=new_id("tok"),
        )
        approval = self.store.get_approval(approval_id)

        # Route first, then emit once with the delivery receipt attached: a
        # single `approval.requested` per pause keeps the timeline honest.
        delivery, routing_error = None, None
        try:
            delivery = route_approval(self.agent, approval, self)
        except Exception as exc:
            routing_error = str(exc)

        self.emit(
            APPROVAL_REQUESTED,
            {
                "approval_id": approval_id,
                "tool": call["name"],
                "arguments": call.get("arguments") or {},
                "reason": decision.reason,
                "routed_to": decision.approval_adapter,
                "call_id": call["id"],
                "delivery": delivery,
            },
        )
        if routing_error:
            self.emit(
                ERROR_RAISED,
                {"kind": "approval_routing_failed", "message": routing_error,
                 "adapter": decision.approval_adapter,
                 "fallback": "console waiting-approval filter"},
            )

        self.state["awaiting_approval_id"] = approval_id
        self.state["awaiting_call_id"] = call["id"]
        self._save()
        self.store.set_turn_status(self.turn_id, "waiting-approval")
        self.store.update_session(self.session_id, status="waiting-approval")

    # ---------------------------------------------------------------- finish

    def _finish(self, text: str) -> TurnResult:
        from .adapters import deliver_outbound

        delivery, delivery_error = None, None
        try:
            delivery = deliver_outbound(
                self.agent, self.store.get_session(self.session_id), text, self
            )
        except Exception as exc:
            delivery_error = str(exc)

        self.emit(MESSAGE_SENT, {"text": text, "channel": self.channel,
                                 "delivery": delivery})
        if delivery_error:
            self.emit(ERROR_RAISED, {"kind": "outbound_delivery_failed",
                                     "message": delivery_error})
        self.state["messages"].append({"role": "assistant", "content": text})
        self.state["pending_calls"] = []
        self.state.pop("awaiting_approval_id", None)
        self.state.pop("awaiting_call_id", None)
        self._save()

        duration = int((time.time() - self._t0) * 1000)
        self.emit(TURN_COMPLETED, {"status": "ok", "duration_ms": duration,
                                   "iterations": self.state.get("iteration", 0)})
        self.store.end_turn(self.turn_id, "completed")
        session = self.store.get_session(self.session_id)
        title = (session["title"] if session else None) or _title_from(self.state)
        self.store.update_session(self.session_id, status="ended", title=title,
                                  agent_version=self.agent.version)
        return TurnResult(status=COMPLETED, reply=text, session_id=self.session_id,
                          turn_id=self.turn_id)

    def _fail(self, kind: str, message: str, trace: str = None) -> TurnResult:
        self.emit(ERROR_RAISED, {"kind": kind, "message": message, "trace": trace})
        self.emit(TURN_COMPLETED, {"status": "error", "error": message,
                                   "duration_ms": int((time.time() - self._t0) * 1000)})
        self._save()
        self.store.end_turn(self.turn_id, "error", message)
        self.store.update_session(self.session_id, status="error", error=message)
        return TurnResult(status=FAILED, session_id=self.session_id, turn_id=self.turn_id,
                          error=message)

    def _save(self) -> None:
        self.store.set_state(self.session_id, self.state)


def _title_from(state: dict) -> Optional[str]:
    for m in state.get("messages", []):
        if m.get("role") == "user" and m.get("content"):
            t = " ".join(str(m["content"]).split())
            return (t[:72] + "…") if len(t) > 72 else t
    return None


def _summarize(dropped: list[dict], previous: str = None) -> str:
    """Cheap extractive rolling summary. Deliberately not a model call — that
    would put an invisible cost inside every long session; a smarter summariser
    is a tool adapter's job."""
    lines = []
    if previous:
        lines.append(previous)
    for m in dropped:
        role = m.get("role")
        if role == "user":
            lines.append(f"user asked: {str(m.get('content'))[:160]}")
        elif role == "assistant" and m.get("content"):
            lines.append(f"agent replied: {str(m['content'])[:160]}")
        elif role == "tool":
            lines.append(f"tool {m.get('name')} returned: {str(m.get('content'))[:120]}")
    text = "\n".join(lines)
    return text[-4000:]
