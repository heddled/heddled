# A starter set

`agents/` and `tools/` are not in this repository — they are yours, and what you
tell an agent to do is not something to publish by accident. This is the set to
copy in when you want to start from something that already works rather than
from nothing.

```bash
cp -r examples/starter/agents examples/starter/tools .
```

Then `docker compose up`, or restart if it is already running — the registry
re-reads from disk, so the agent is there on the next page load.

What you get:

| | |
|---|---|
| `agents/support` | the agent from [`heddled-concept.md`](../../heddled-concept.md), made real — a scheduled run, a mailbox poller, an approval gate on refunds, and redaction on everything |
| `tools/lookup_invoice` | returns a fixed invoice, so it works with no credentials anywhere |
| `tools/create_ticket` | writes a ticket id to the trace |
| `tools/refund` | gated behind `requires_approval`, so the first thing you see is a turn that stops and waits |

It runs on `model: mock/echo` — a deterministic fake provider that needs no API
key. Swap that line for `anthropic/claude-sonnet-4-6` (or any other model in the
README's table) once you have a key in Settings.

`heddled init` writes a smaller version of the same idea into an empty
directory, if you would rather start from one agent and one tool.
