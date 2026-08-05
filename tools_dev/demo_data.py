"""Build a convincing demo instance, for screenshots on the site.

The console in a screenshot has to look like somebody's Tuesday, not like a test
fixture. `mock/echo` is honest but it says so in every reply, and its tool
results are raw JSON dumps — fine for the suite, wrong for a picture of the
product.

So this writes a small, plausible estate and a few recorded conversations
straight onto the event spine. Nothing here fakes the console: every screen
renders these exactly as it renders real traffic, because to the console they
are real traffic. Only the content is written rather than earned.

    python tools_dev/demo_data.py /path/to/demo-root

Then point a Heddled at that root and take pictures.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/heddled-demo")

AGENTS = {
    "billing_support": {
        "description": "Answers invoice and payment questions for the finance team.",
        "model": "anthropic/claude-sonnet-4-6",
        "tools": ["lookup_invoice", "issue_refund", "create_ticket"],
        "instructions": """\
You are the billing assistant for a small finance team. You answer questions
about invoices, arrange refunds, and raise a ticket when something needs a
person.

Rules:

- Always look an invoice up before saying anything about its status or amount.
- Quote exact amounts and invoice numbers. Never estimate.
- A refund over €100 needs a reason recorded on the ticket first.
- If you cannot settle something with the tools you have, create a ticket and
  tell the customer its number.
- Be brief. Two or three sentences is usually right.
""",
        "policies": [
            {"tool": "issue_refund", "requires_approval": True,
             "budget": {"max_eur_per_day": 500}},
            {"tool": "*", "redact": ["iban", "creditcard"]},
        ],
        "triggers": [
            {"schedule": "0 8 * * 1-5",
             "message": "Summarise yesterday's unpaid invoices and flag anything overdue."},
        ],
        "channels": ["webchat", "webhook"],
    },
    "invoice_intake": {
        "description": "Reads invoices as they arrive and files them.",
        "model": "anthropic/claude-haiku-4-5",
        "tools": ["lookup_invoice", "create_ticket"],
        "instructions": "You process incoming invoices. Extract the number, the "
                        "amount and the supplier, then file it. Raise a ticket if "
                        "anything is missing or looks wrong.\n",
        "policies": [],
        "triggers": [
            {"poll": "mailbox", "every": "5m",
             "config": {"source": "imap", "folder": "Invoices"},
             "on_new": "File this invoice."},
        ],
        "channels": ["webhook"],
    },
    "hr_helper": {
        "description": "Answers everyday questions about leave, expenses and policy.",
        "model": "anthropic/claude-sonnet-4-6",
        "tools": ["office_location"],
        "instructions": "You answer staff questions about leave, expenses and "
                        "office practicalities. Point people at a person when the "
                        "answer depends on their contract.\n",
        "policies": [],
        "triggers": [],
        "channels": ["webchat"],
    },
}

TOOLS = {
    "lookup_invoice": {
        "description": "Find an invoice by its number.",
        "input": {"invoice_number": "string"},
        "output": {"status": "string", "amount_eur": "number", "customer": "string",
                   "due": "string"},
    },
    "issue_refund": {
        "description": "Refund an invoice, in whole or in part.",
        "input": {"invoice_number": "string", "amount_eur": "number", "reason": "string"},
        "output": {"refund_id": "string", "status": "string"},
    },
    "create_ticket": {
        "description": "Raise a ticket for a person to pick up.",
        "input": {"subject": "string", "detail": "string"},
        "output": {"ticket": "string", "queue": "string"},
    },
    "office_location": {
        "description": "Where a team sits, and which days they are in.",
        "input": {"team": "string"},
        "output": {"floor": "string", "days": "string"},
    },
}


def write_files() -> None:
    (ROOT / "agents").mkdir(parents=True, exist_ok=True)
    (ROOT / "tools").mkdir(parents=True, exist_ok=True)

    for name, spec in AGENTS.items():
        lines = [
            f"name: {name}",
            f"description: {spec['description']}",
            f"model: {spec['model']}",
            f"instructions: ./{name}.md",
            "",
            "adapters:",
            f"  channels: [{', '.join(spec['channels'])}]",
            "  tools:",
        ]
        lines += [f"    - {t}" for t in spec["tools"]]
        if spec["triggers"]:
            # Hand-rolling this produced invalid YAML for the nested poll config;
            # let the library do it and just indent the result.
            import yaml

            dumped = yaml.safe_dump(spec["triggers"], sort_keys=False,
                                    default_flow_style=False).rstrip()
            lines.append("triggers:")
            lines += ["  " + line for line in dumped.splitlines()]
        lines.append("policies:")
        if spec["policies"]:
            for p in spec["policies"]:
                tool = p["tool"]
                # A bare * is a YAML alias marker, not a wildcard.
                lines.append(f'  - tool: {tool if tool != "*" else chr(34) + "*" + chr(34)}')
                if p.get("requires_approval"):
                    lines.append("    requires_approval: true")
                if p.get("budget"):
                    lines.append(f"    budget: {{ max_eur_per_day: {p['budget']['max_eur_per_day']} }}")
                if p.get("redact"):
                    lines.append(f"    redact: [{', '.join(p['redact'])}]")
        else:
            lines[-1] = "policies: []"
        lines += ["", "memory:", "  session: auto", ""]
        (ROOT / "agents" / f"{name}.yaml").write_text("\n".join(lines), encoding="utf-8")
        (ROOT / "agents" / f"{name}.md").write_text(spec["instructions"], encoding="utf-8")

    for name, spec in TOOLS.items():
        d = ROOT / "tools" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "tool.yaml").write_text(
            f"name: {name}\n"
            f"description: {spec['description']}\n"
            f"input:  {{{', '.join(f'{k}: {v}' for k, v in spec['input'].items())}}}\n"
            f"output: {{{', '.join(f'{k}: {v}' for k, v in spec['output'].items())}}}\n"
            "handler: ./handler.py\n", encoding="utf-8")
        (d / "handler.py").write_text(
            "def handle(args, ctx):\n"
            "    raise NotImplementedError('demo fixture — traces are pre-recorded')\n",
            encoding="utf-8")


# --------------------------------------------------------------- conversations

def seed_events(store, registry) -> None:
    from heddled.events import Event

    agent = registry.get_agent("billing_support")
    version = agent.version if agent else "demo"
    now = time.time()

    def conversation(minutes_ago, channel, origin, question, steps, status="ended",
                     title=None, env="prod"):
        start = now - minutes_ago * 60
        sid = store.create_session(agent="billing_support", agent_version=version,
                                   channel=channel, trigger_origin=origin, env=env)
        turn = f"t_{int(start)}"
        at = [start]

        def add(kind, payload, gap=0.4):
            at[0] += gap
            store.append(Event(type=kind, session_id=sid, turn_id=turn,
                               agent="billing_support", agent_version=version,
                               payload=payload, ts=at[0]))

        add("message.received", {"text": question, "sender": origin.get("sender", "customer")}, 0)
        for step in steps:
            add(*step)
        store.update_session(sid, status=status, title=title or question[:60])
        return sid

    # 1 — the everyday one: a question, a lookup, a straight answer.
    conversation(
        6, "webchat", {"kind": "webchat", "sender": "priya@acme.example"},
        "Hi — is invoice F-2231 paid yet? It's for the March retainer.",
        [
            ("context.built", {"message_count": 1, "tool_count": 3,
                               "system": "You are the billing assistant…"}),
            ("model.invoked", {"model": "anthropic/claude-sonnet-4-6",
                               "context_seq": 2, "tool_count": 3}),
            ("model.responded", {"text": "Let me look that invoice up.",
                                 "tool_calls": [{"name": "lookup_invoice",
                                                 "arguments": {"invoice_number": "F-2231"}}],
                                 "usage": {"input_tokens": 1204, "output_tokens": 31},
                                 "duration_ms": 940}),
            ("tool.called", {"tool": "lookup_invoice",
                             "arguments": {"invoice_number": "F-2231"}}),
            ("tool.result", {"tool": "lookup_invoice", "duration_ms": 61,
                             "result": {"status": "unpaid", "amount_eur": 249.00,
                                        "customer": "Acme BV", "due": "2026-08-14"}}),
            ("context.built", {"message_count": 3, "tool_count": 3}),
            ("model.invoked", {"model": "anthropic/claude-sonnet-4-6",
                               "context_seq": 7, "tool_count": 3}),
            ("model.responded", {"text": "Invoice F-2231 for Acme BV is still unpaid…",
                                 "usage": {"input_tokens": 1388, "output_tokens": 48},
                                 "duration_ms": 1120}),
            ("message.sent", {"text": "Invoice F-2231 for Acme BV is still unpaid — "
                                      "€249.00, due 14 August. Nothing has come in "
                                      "against it yet. Would you like me to send a "
                                      "reminder?"}),
            ("turn.completed", {"status": "ok", "duration_ms": 2310}),
        ],
        title="Is invoice F-2231 paid yet?")

    # 2 — the one that stops and waits for a person.
    paused = conversation(
        2, "webchat", {"kind": "webchat", "sender": "priya@acme.example"},
        "Acme were double-charged on F-2231. Can you refund €249?",
        [
            ("context.built", {"message_count": 1, "tool_count": 3}),
            ("model.invoked", {"model": "anthropic/claude-sonnet-4-6",
                               "context_seq": 13, "tool_count": 3}),
            ("model.responded", {"text": "I'll check the invoice first.",
                                 "tool_calls": [{"name": "lookup_invoice",
                                                 "arguments": {"invoice_number": "F-2231"}}],
                                 "usage": {"input_tokens": 1240, "output_tokens": 28},
                                 "duration_ms": 870}),
            ("tool.called", {"tool": "lookup_invoice",
                             "arguments": {"invoice_number": "F-2231"}}),
            ("tool.result", {"tool": "lookup_invoice", "duration_ms": 58,
                             "result": {"status": "paid twice", "amount_eur": 249.00,
                                        "customer": "Acme BV", "due": "2026-08-14"}}),
            ("model.invoked", {"model": "anthropic/claude-sonnet-4-6",
                               "context_seq": 17, "tool_count": 3}),
            ("model.responded", {"text": "That is a genuine double payment. Refunding.",
                                 "tool_calls": [{"name": "issue_refund",
                                                 "arguments": {"invoice_number": "F-2231",
                                                               "amount_eur": 249.0,
                                                               "reason": "duplicate payment"}}],
                                 "usage": {"input_tokens": 1502, "output_tokens": 44},
                                 "duration_ms": 1010}),
            ("approval.requested", {"tool": "issue_refund", "routed_to": "webhook",
                                    "arguments": {"invoice_number": "F-2231",
                                                  "amount_eur": 249.0,
                                                  "reason": "duplicate payment"},
                                    "reason": "issue_refund requires approval"}),
        ],
        status="waiting-approval",
        title="Refund €249 on F-2231 — waiting for approval")

    store.create_approval(session_id=paused, turn_id=f"t_{int(now - 120)}",
                          agent="billing_support", tool="issue_refund",
                          # create_approval serialises this itself — passing a
                          # JSON string here double-encodes it.
                          args={"invoice_number": "F-2231", "amount_eur": 249.0,
                                "reason": "duplicate payment"},
                          routed_to="webhook", status="pending",
                          reason="issue_refund requires approval")

    # 3 — one that nobody asked for: the weekday schedule.
    conversation(
        61, "webchat", {"kind": "schedule", "reason": "cron 0 8 * * 1-5 at 08:00"},
        "Summarise yesterday's unpaid invoices and flag anything overdue.",
        [
            ("trigger.fired", {"kind": "schedule", "reason": "cron 0 8 * * 1-5 at 08:00"}),
            ("context.built", {"message_count": 1, "tool_count": 3}),
            ("model.invoked", {"model": "anthropic/claude-sonnet-4-6", "tool_count": 3}),
            ("model.responded", {"usage": {"input_tokens": 1180, "output_tokens": 96},
                                 "duration_ms": 1640}),
            ("message.sent", {"text": "Four invoices are unpaid this morning, "
                                      "€3,180 in total. Two are past due: F-2188 "
                                      "(Nordwind, €1,450, 9 days) and F-2201 "
                                      "(Halcyon, €780, 3 days)."}),
            ("turn.completed", {"status": "ok", "duration_ms": 1980}),
        ],
        title="Weekday summary — 4 unpaid, 2 overdue")

    # 4 — one that arrived from another system.
    conversation(
        26, "webhook", {"kind": "webhook", "reason": "inbound POST", "caller": "billing-portal"},
        "Customer asked about invoice F-2204 through the portal.",
        [
            ("context.built", {"message_count": 1, "tool_count": 3}),
            ("model.invoked", {"model": "anthropic/claude-sonnet-4-6", "tool_count": 3}),
            ("model.responded", {"tool_calls": [{"name": "lookup_invoice",
                                                 "arguments": {"invoice_number": "F-2204"}}],
                                 "usage": {"input_tokens": 1190, "output_tokens": 26},
                                 "duration_ms": 780}),
            ("tool.called", {"tool": "lookup_invoice",
                             "arguments": {"invoice_number": "F-2204"}}),
            ("tool.result", {"tool": "lookup_invoice", "duration_ms": 55,
                             "result": {"status": "paid", "amount_eur": 1180.00,
                                        "customer": "Halcyon Ltd", "due": "2026-07-30"}}),
            ("model.invoked", {"model": "anthropic/claude-sonnet-4-6", "tool_count": 3}),
            ("model.responded", {"usage": {"input_tokens": 1402, "output_tokens": 38},
                                 "duration_ms": 900}),
            ("message.sent", {"text": "F-2204 was paid in full on 28 July — €1,180.00 "
                                      "from Halcyon Ltd. Nothing outstanding."}),
            ("turn.completed", {"status": "ok", "duration_ms": 1810}),
        ],
        title="Portal enquiry about F-2204")


def main() -> None:
    import os

    os.environ["HEDDLED_ROOT"] = str(ROOT)
    write_files()

    from heddled.registry import get_registry
    from heddled.store import get_store

    store, registry = get_store(), get_registry()
    seed_events(store, registry)

    # A configured instance: without a key the console quite rightly warns on
    # every agent page that it will stop on its first turn, which is true and
    # completely wrong to photograph.
    store.set_setting("anthropic_api_key", "sk-ant-demo-not-a-real-key")
    store.set_setting("default_env", "prod")

    for name in ("billing_support", "invoice_intake", "hr_helper"):
        agent = registry.get_agent(name)
        if agent:
            store.record_agent_version(agent)
            store.promote(name, "prod", agent.version, by="alex")
            store.promote(name, "staging", agent.version, by="alex")

    print(f"demo estate written to {ROOT}")
    print(f"  {len(AGENTS)} agents, {len(TOOLS)} tools, "
          f"{len(store.list_sessions())} conversations")


if __name__ == "__main__":
    main()
