"""Run a tool in isolation — `heddled tool test` and the console's Tools panel.

A tool is one directory: schema + handler, testable without an agent, without a
model, and without a session. That is most of what makes the dev loop fast.
"""

from __future__ import annotations

import time

from .registry import get_registry, validate_args
from .store import get_store


class _StandaloneContext:
    """A ToolContext-shaped object with no turn behind it."""

    def __init__(self, tool_name: str):
        self.tool = tool_name
        self.session_id = "standalone"
        self.turn_id = "standalone"
        self.agent = "standalone"
        self.agent_version = None
        self.channel = "cli"
        self.store = get_store()
        self.settings = self.store.all_settings()
        self.logs: list[str] = []
        self._memory: dict = {}

    def log(self, message: str, **extra):
        self.logs.append(message)

    def memory(self) -> dict:
        return self._memory


def run_tool_standalone(name: str, args: dict) -> dict:
    tool = get_registry().get_tool(name)
    if not tool:
        raise LookupError(f"no tool named '{name}' in {get_registry().tools_dir}")

    errors = validate_args(tool.input_schema, args)
    if errors:
        return {"tool": name, "ok": False, "validation_errors": errors,
                "input_schema": tool.input_schema}

    ctx = _StandaloneContext(name)
    started = time.time()
    try:
        result = tool.load_handler()(args, ctx)
        return {
            "tool": name,
            "ok": True,
            "args": args,
            "result": result,
            "logs": ctx.logs,
            "duration_ms": int((time.time() - started) * 1000),
        }
    except Exception as exc:
        return {
            "tool": name,
            "ok": False,
            "args": args,
            "error": f"{type(exc).__name__}: {exc}",
            "logs": ctx.logs,
            "duration_ms": int((time.time() - started) * 1000),
        }
