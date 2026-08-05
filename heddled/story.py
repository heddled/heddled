"""What happened, in plain language.

The event stream is the truth and the trace view shows all of it — but "what
happened" and "every record on the spine" are not the same question. Someone
checking why a customer got the wrong answer wants a story:

    You asked: where is invoice F-2231?
    Looked up F-2231 — found it, unpaid, €249
    Paused and asked you to approve a refund of €249
    Ralph approved it
    Issued the refund
    Replied: I've refunded €249 against F-2231.

…not thirteen records including `context.built` and `model.invoked`. Those stay
one click away for whoever needs them; they are mechanism, not story.

This module is a **consumer**: it reads events and produces sentences. It adds
nothing to the spine and the engine does not know it exists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

# Events that describe how the machine works rather than what it did. Hidden
# from the summary; the details view still shows every one of them.
MECHANISM = {"context.built", "model.invoked", "model.responded"}

# Plain readings of the error kinds the engine raises. A stack trace is the
# right thing to show a developer and the wrong thing to show anyone else.
ERROR_READINGS = {
    "provider_error": (
        "Couldn't reach the AI model",
        "Check the model settings and that the API key is still valid.",
    ),
    "model_call_failed": (
        "The AI model didn't respond properly",
        "Usually temporary. Try again; if it keeps happening, check the model settings.",
    ),
    "tool_failed": (
        "A tool ran into a problem",
        "The agent was told about it and carried on. Open the tool to see what went wrong.",
    ),
    "policy_denied": (
        "Blocked by one of your rules",
        "The agent wanted to do this but a rule you set stopped it.",
    ),
    "iteration_limit": (
        "The agent went round in circles and gave up",
        "It kept using tools without reaching an answer. Usually the instructions "
        "need to be clearer about when to stop.",
    ),
    "approval_routing_failed": (
        "Couldn't send the approval request",
        "The approval is still waiting here in Heddled, under Activity.",
    ),
    "outbound_delivery_failed": (
        "Couldn't deliver the reply",
        "The answer was produced but not sent. Check the channel settings.",
    ),
}


@dataclass
class Step:
    """One line of the story."""

    kind: str          # drives the icon and colour
    headline: str      # the sentence itself
    detail: str = ""   # optional second line
    seq: Optional[int] = None   # jump to this event in the details view
    ts: Optional[float] = None
    tone: str = "normal"        # normal | good | warn | bad
    items: list = field(default_factory=list)


def _short(value, limit: int = 160) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def openers(agent, tools: dict, limit: int = 3) -> list[str]:
    """Suggested first messages, built from the agent's own tools.

    Landing in an empty chat box and being asked to think of something is a
    poor first experience, and the person who just made the agent is the least
    sure of what it can handle. Each tool's description already says what it
    does, so it doubles as a prompt.
    """
    out: list[str] = []
    for name, tool in (tools or {}).items():
        # The description is already a sentence about what the tool does, so it
        # turns into a question far more naturally than the tool's name does.
        # "Look up an invoice by number; returns…" → "Can you look up an
        # invoice by number?"
        sentence = (tool.description or "").split(";")[0].split(".")[0].strip()
        if sentence and len(sentence) < 70:
            out.append("Can you " + sentence[0].lower() + sentence[1:] + "?")
        else:
            out.append(f"Can you {name.replace('_', ' ')}?")
        if len(out) >= limit:
            break

    if not out:
        described = (agent.description or "").strip().rstrip(".")
        out.append(f"What can you help me with?" if not described
                   else f"{described}?".capitalize())
    return out[:limit]


_DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
              "Friday", "Saturday"]


def cron_words(expr: str) -> str:
    """`0 8 * * 1-5` → `Weekdays at 08:00`.

    Covers the shapes people actually write in the form. Anything unusual falls
    back to the expression itself rather than to a confident wrong reading.
    """
    parts = (expr or "").split()
    if len(parts) != 5:
        return expr or ""
    minute, hour, dom, month, dow = parts

    if not (minute.isdigit() and hour.isdigit()):
        if minute.startswith("*/") and hour == "*":
            return f"Every {minute[2:]} minutes"
        if hour.startswith("*/"):
            return f"Every {hour[2:]} hours"
        return expr
    at = f"{int(hour):02d}:{int(minute):02d}"

    if dom == "*" and month == "*":
        if dow == "*":
            return f"Every day at {at}"
        if dow in ("1-5", "MON-FRI", "mon-fri"):
            return f"Weekdays at {at}"
        if dow in ("0,6", "6,0", "SAT,SUN", "0", "6"):
            return f"Weekends at {at}"
        if dow.isdigit():
            return f"Every {_DAY_NAMES[int(dow) % 7]} at {at}"
    if dom.isdigit() and month == "*":
        return f"Day {dom} of each month at {at}"
    return expr


def _who(name: Optional[str]) -> str:
    """A person's name reads oddly lowercased in the middle of a sentence."""
    if not name:
        return "Someone"
    return name[0].upper() + name[1:]


def readable_args(args: dict) -> str:
    """`{"invoice_number": "F-2231"}` → `invoice number: F-2231`."""
    if not isinstance(args, dict) or not args:
        return ""
    return ", ".join(
        f"{key.replace('_', ' ')}: {_short(value, 60)}" for key, value in args.items()
    )


def _tool_outcome(payload: dict) -> tuple[str, str]:
    """Say what a tool actually came back with, rather than dumping JSON."""
    result = payload.get("result")
    if payload.get("error"):
        return "bad", _short(result, 200)

    if isinstance(result, dict):
        # The shapes Heddled's own no-code tools return, read out loud.
        if result.get("found") is True:
            return "good", _short(result.get("value"))
        if result.get("found") is False:
            return "warn", _short(result.get("message") or "not found")
        if result.get("ok") is False:
            return "bad", _short(result.get("error") or "the call failed")
        if "text" in result and len(result) == 1:
            return "good", _short(result["text"])
        # A flat result reads far better as "status: unpaid, amount: 249" than
        # as the JSON it happens to be stored as.
        if all(not isinstance(v, (dict, list)) for v in result.values()):
            return "good", readable_args(result)
        return "good", _short(result)
    return "good", _short(result)


def build(events: list, viewer: str = "You") -> list[Step]:
    """Turn one session's events into a story.

    `viewer` is what to call whoever started the conversation — "You" in the
    console, a name or channel elsewhere.
    """
    steps: list[Step] = []
    # Remember arguments from tool.called so the result line can name them.
    pending: dict[str, dict] = {}

    for event in events:
        payload = event.payload or {}
        kind = event.type

        if kind in MECHANISM:
            continue

        if kind == "trigger.fired":
            why = payload.get("reason") or payload.get("kind", "a schedule")
            steps.append(Step(
                kind="start", tone="normal", seq=event.seq, ts=event.ts,
                headline="Started automatically",
                detail=f"Triggered by {why}.",
            ))

        elif kind == "message.received":
            sender = payload.get("sender") or "someone"
            who = viewer if sender in ("user", "you", None) else sender
            steps.append(Step(
                kind="ask", tone="normal", seq=event.seq, ts=event.ts,
                headline=f"{who} asked",
                detail=_short(payload.get("text"), 400),
            ))

        elif kind == "tool.called":
            pending[payload.get("call_id") or payload.get("tool")] = payload
            # The result line carries the story; a bare "called X" adds nothing.
            continue

        elif kind == "tool.result":
            if payload.get("partial"):
                continue
            tool = payload.get("tool", "a tool")
            call = pending.pop(payload.get("call_id") or tool, {})
            asked = readable_args(call.get("arguments") or {})
            tone, outcome = _tool_outcome(payload)
            steps.append(Step(
                kind="did", tone=tone, seq=event.seq, ts=event.ts,
                headline=f"Used {tool.replace('_', ' ')}"
                         + (f" — {asked}" if asked else ""),
                detail=outcome,
            ))

        elif kind == "approval.requested":
            args = readable_args(payload.get("arguments") or {})
            steps.append(Step(
                kind="wait", tone="warn", seq=event.seq, ts=event.ts,
                headline=f"Paused — needs approval to use "
                         f"{str(payload.get('tool', '')).replace('_', ' ')}",
                detail=(f"It wants to do this with {args}. " if args else "")
                       + f"Sent to {payload.get('routed_to', 'an approver')}.",
            ))

        elif kind == "approval.resolved":
            decision = payload.get("decision", "answered")
            steps.append(Step(
                kind="approved" if decision == "approved" else "denied",
                tone="good" if decision == "approved" else "bad",
                seq=event.seq, ts=event.ts,
                headline=f"{_who(payload.get('resolver'))} "
                         f"{'approved' if decision == 'approved' else 'refused'} it",
                detail=_short(payload.get("note")),
            ))

        elif kind == "operator.injected":
            steps.append(Step(
                kind="note", tone="normal", seq=event.seq, ts=event.ts,
                headline=f"{payload.get('operator', 'Someone')} stepped in",
                detail=_short(payload.get("text"), 300),
            ))

        elif kind == "message.sent":
            steps.append(Step(
                kind="replied", tone="normal", seq=event.seq, ts=event.ts,
                headline="Replied",
                detail=_short(payload.get("text"), 600),
            ))

        elif kind == "error.raised":
            reading = ERROR_READINGS.get(payload.get("kind"))
            headline, hint = reading if reading else (
                "Something went wrong", _short(payload.get("message"), 200))
            detail = hint
            if reading and payload.get("tool"):
                detail = f"Tool: {payload['tool']}. " + hint
            steps.append(Step(
                kind="problem", tone="bad", seq=event.seq, ts=event.ts,
                headline=headline, detail=detail,
            ))

        elif kind == "turn.completed":
            if payload.get("status") == "ok":
                ms = payload.get("duration_ms") or 0
                took = "under a second" if ms < 1000 else f"{ms / 1000:.1f} seconds"
                steps.append(Step(
                    kind="done", tone="good", seq=event.seq, ts=event.ts,
                    headline="Finished", detail=f"Took {took}.",
                ))
            else:
                steps.append(Step(
                    kind="done", tone="bad", seq=event.seq, ts=event.ts,
                    headline="Stopped without finishing",
                    detail=_short(payload.get("error"), 200),
                ))

    return steps


def headline(session, steps: list[Step]) -> str:
    """One sentence for the top of the page and for the Activity list."""
    status = session["status"] if session is not None else "ended"
    if status == "waiting-approval":
        waiting = next((s for s in steps if s.kind == "wait"), None)
        return waiting.headline if waiting else "Waiting for someone to approve something"
    if status == "error":
        problem = next((s for s in reversed(steps) if s.kind == "problem"), None)
        return problem.headline if problem else "Stopped without finishing"
    if status == "running":
        return "Working on it right now"
    used = [s for s in steps if s.kind == "did"]
    if used:
        names = ", ".join(dict.fromkeys(
            s.headline.replace("Used ", "").split(" — ")[0] for s in used))
        return f"Answered, using {names}"
    return "Answered without needing any tools"
