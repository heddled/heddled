"""`heddled init` — write a working agent + two tools into a fresh project.

Keeps the "ship an agent in an afternoon" promise honest: the files it writes
are the same ones the repo ships as its example, so `heddled init && heddled dev`
gives you something that already runs.
"""

from __future__ import annotations

from pathlib import Path

AGENT_YAML = """\
name: assistant
description: A starter agent. Edit this file — the console writes it too.
model: mock/echo            # swap for anthropic/claude-sonnet-4-6 once you add a key
instructions: ./assistant.md

adapters:
  channels: [webchat, webhook]
  tools: [echo]

policies:
  - tool: "*"
    rate_limit: { max_calls: 30, per_seconds: 60 }

memory:
  session: auto
"""

AGENT_MD = """\
You are a helpful assistant. Use the tools you have been given when they apply,
and say plainly when you cannot do something.
"""

TOOL_YAML = """\
name: echo
description: Return the text you are given. A placeholder to copy.
input:  { text: string }
output: { text: string }
handler: ./handler.py
"""

TOOL_PY = '''\
"""handle(args, ctx) is the whole tool contract."""


def handle(args, ctx):
    ctx.log("echoing")
    return {"text": args["text"]}
'''


def scaffold(root: Path, force: bool = False) -> list[Path]:
    agents = root / "agents"
    tools = root / "tools" / "echo"
    created: list[Path] = []

    targets = [
        (agents / "assistant.yaml", AGENT_YAML),
        (agents / "assistant.md", AGENT_MD),
        (tools / "tool.yaml", TOOL_YAML),
        (tools / "handler.py", TOOL_PY),
    ]
    for path, content in targets:
        if path.exists() and not force:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(path)
    for d in (root / "data", root / "var" / "mailbox"):
        d.mkdir(parents=True, exist_ok=True)
    return created
