"""The three ways an agent works in its workspace.

Built in rather than left to a hand-written handler, and the reason is the
confinement: if every operator wrote their own `read_file`, every operator
would write their own path check and one of them would get it wrong. This is
written once, tested once — see tests/test_workspace.py, which spends most of
its length trying to escape — and every agent with a workspace inherits it.

Three tools rather than one with a mode, because policy has to be able to tell
them apart. `read_file` wants to be ungated so the agent gets on with the job;
`write_file` wants `requires_approval` and to be unavailable on the chat
channel. One tool with an `operation` argument makes that inexpressible.
"""

from __future__ import annotations

from typing import Callable

from . import workspace
from .registry import Tool, normalize_schema

#: name → (what it does, what it takes)
OPERATIONS = {
    "list_files": (
        "List the files in the workspace. Use this first to see what is there.",
        {"type": "object", "properties": {}},
    ),
    "read_file": (
        "Read one text file from the workspace and return its contents.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "The file's name, as list_files gives it."}
            },
            "required": ["path"],
        },
    ),
    "write_file": (
        "Write a text file into the workspace, replacing it if it is already "
        "there. Returns the name written.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "What to call it, inside the workspace."},
                "content": {"type": "string", "description": "The whole file."},
            },
            "required": ["path", "content"],
        },
    ),
}

OUTPUTS = {
    "list_files": {"files": "string"},
    "read_file": {"content": "string"},
    "write_file": {"path": "string", "bytes": "number", "replaced": "boolean"},
}


def tools_for(agent) -> dict[str, Tool]:
    """The file tools this agent has, which is none unless it has a workspace."""
    if not getattr(agent, "workspace", None):
        return {}
    out: dict[str, Tool] = {}
    for name, (description, schema) in OPERATIONS.items():
        out[name] = Tool(
            name=name,
            description=description,
            input_schema=schema,
            output_schema=normalize_schema(OUTPUTS[name]),
            handler_path=None,
            dir=agent.path.parent if agent.path else None,
            raw={"operation": name, "agent": agent.name},
            source="workspace",
        )
    return out


def make_workspace_handler(operation: str, agent_name: str) -> Callable:
    """Bind an operation to the agent whose workspace it works in.

    The root is resolved per call rather than captured once: an agent's file can
    change under a running worker, and a stale root is a path check against the
    wrong directory.
    """

    def handle(args, ctx):
        from .registry import get_registry

        agent = get_registry().get_agent(agent_name)
        if not agent:
            raise workspace.WorkspaceError(f"no agent named '{agent_name}'")
        root = workspace.resolve_root(agent)
        if root is None:
            raise workspace.WorkspaceError(
                f"'{agent_name}' has no workspace, so there are no files to reach")

        if operation == "list_files":
            files = workspace.listing(root)
            ctx.log(f"{len(files)} file(s) in the workspace")
            if not files:
                return {"files": "The workspace is empty."}
            return {"files": "\n".join(
                f"{f['path']} ({f['bytes']} bytes)" for f in files)}

        if operation == "read_file":
            path = args.get("path")
            content = workspace.read(root, path)
            ctx.log(f"read {path}")
            return {"content": content}

        if operation == "write_file":
            path = args.get("path")
            result = workspace.write(root, path, args.get("content"))
            ctx.log(f"wrote {result['path']} ({result['bytes']} bytes)")
            return result

        raise workspace.WorkspaceError(f"unknown operation '{operation}'")

    return handle
