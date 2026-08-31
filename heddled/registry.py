"""Files-first loading of agents and tools.

Principle 3: every agent, tool and policy is a versionable file on disk. The
registry is the only thing that reads them, and it re-reads on every access so
`heddled dev` hot-reloads for free.

An agent's *version* is the sha256 of its definition plus its instructions —
edit either and you get a new version, with no version field to forget to bump.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from . import config

# --------------------------------------------------------------------- schemas

# Shorthand type names accepted in tool.yaml `input:`/`output:` maps.
_TYPE_MAP = {
    "string": "string",
    "str": "string",
    "text": "string",
    "number": "number",
    "float": "number",
    "int": "integer",
    "integer": "integer",
    "bool": "boolean",
    "boolean": "boolean",
    "object": "object",
    "dict": "object",
    "array": "array",
    "list": "array",
}


def normalize_schema(spec: Any) -> dict:
    """Accept either a JSON Schema object or the `{field: type}` shorthand from
    the concept doc and always return a JSON Schema object."""
    if not spec:
        return {"type": "object", "properties": {}}
    if isinstance(spec, dict) and spec.get("type") == "object" and "properties" in spec:
        return spec
    props, required = {}, []
    for key, val in (spec or {}).items():
        if isinstance(val, str):
            optional = val.endswith("?")
            t = val.rstrip("?").strip()
            props[key] = {"type": _TYPE_MAP.get(t, "string")}
            if not optional:
                required.append(key)
        elif isinstance(val, dict):
            props[key] = val
            if not val.get("optional"):
                required.append(key)
        else:
            props[key] = {"type": "string"}
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def validate_args(schema: dict, args: dict) -> list[str]:
    """Minimal structural validation — enough to catch a model hallucinating a
    field name, without pulling in a JSON Schema dependency."""
    errors = []
    props = schema.get("properties") or {}
    for req in schema.get("required") or []:
        if req not in args:
            errors.append(f"missing required field '{req}'")
    checks = {
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for k, v in (args or {}).items():
        if k not in props:
            continue
        want = props[k].get("type")
        py = checks.get(want)
        if not py:
            continue
        if isinstance(v, bool) and want != "boolean":
            # bool subclasses int in Python, so a `True` would otherwise slip
            # through `number` and `integer` unnoticed.
            errors.append(f"field '{k}' should be {want}, got boolean")
        elif not isinstance(v, py):
            # ints arriving where a number is wanted are fine: `checks` maps
            # "number" to (int, float).
            errors.append(f"field '{k}' should be {want}, got {type(v).__name__}")
    return errors


# ----------------------------------------------------------------------- tools


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    output_schema: dict
    handler_path: Optional[Path]
    dir: Path
    raw: dict
    mock: Any = None  # optional canned result used by `heddled tool test --mock`
    cost_eur: float = 0.0
    source: str = "file"  # file | agent | mcp

    def to_model_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    @property
    def is_no_code(self) -> bool:
        from . import tooltypes

        return tooltypes.is_no_code(self.raw)

    def load_handler(self) -> Callable[[dict, Any], Any]:
        if self.source == "agent":
            from .subagent import make_agent_tool_handler

            return make_agent_tool_handler(self.raw["agent"])
        if self.source == "mcp":
            from .mcp_client import make_mcp_tool_handler

            return make_mcp_tool_handler(self.raw)
        if self.source == "workspace":
            from .filetools import make_workspace_handler

            return make_workspace_handler(self.raw["operation"], self.raw["agent"])
        if self.is_no_code:
            # Built from a form rather than written. The engine cannot tell.
            from . import tooltypes

            return tooltypes.build_handler(self.raw)
        if not self.handler_path or not self.handler_path.exists():
            raise FileNotFoundError(f"tool '{self.name}' has no handler at {self.handler_path}")
        mod_name = f"heddled_tool_{self.name}_{abs(hash(str(self.handler_path)))}"
        spec = importlib.util.spec_from_file_location(mod_name, self.handler_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        fn = getattr(module, "handle", None) or getattr(module, "main", None)
        if not callable(fn):
            raise AttributeError(f"tool '{self.name}' handler defines no handle(args, ctx)")
        return fn


# ---------------------------------------------------------------------- agents


@dataclass
class Trigger:
    kind: str  # schedule | poll
    raw: dict
    index: int

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.index}"


@dataclass
class Agent:
    name: str
    path: Path
    raw: dict
    version: str
    instructions: str
    model: str
    channels: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    triggers: list[Trigger] = field(default_factory=list)
    policies: list[dict] = field(default_factory=list)
    memory: dict = field(default_factory=dict)
    expose: dict = field(default_factory=dict)
    workspace: Any = None
    description: str = ""
    handler: Optional[str] = None  # Level 3: dotted path to a custom turn engine
    #: The definition this agent was built from. Set for every agent, so one
    #: rebuilt from a published snapshot behaves like one read off disk even
    #: though there is no longer a file with those bytes in it.
    source: str = ""
    #: Which environment pinned it, when it came from a published version
    #: rather than the working file.
    pinned_for: Optional[str] = None

    @property
    def short_version(self) -> str:
        return self.version[:8]

    def policy_for_tool(self, tool: str) -> dict:
        """Merge every policy block that applies to this tool, most specific last."""
        merged: dict = {}
        for p in self.policies:
            target = p.get("tool")
            if target in (None, "*", tool):
                merged.update({k: v for k, v in p.items() if k != "tool"})
        return merged

    def raw_text(self) -> str:
        # A pinned version's bytes no longer exist on disk, so the definition it
        # was built from is the answer — not whatever the file says today.
        if self.source:
            return self.source
        return self.path.read_text(encoding="utf-8")


def _mtime(path: Optional[Path]) -> Optional[float]:
    """When a file last changed, or None if there isn't one. Used to decide
    whether a cached parse is still good."""
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def build_agent(raw_text: str, instructions: str, path: Path = None,
                pinned_for: str = None) -> Agent:
    """Parse a definition into an Agent.

    Kept separate from reading files so a published version can be rebuilt from
    the bytes that were promoted, which is what makes a deployment a version
    bound to an environment rather than a note about one.
    """
    raw = yaml.safe_load(raw_text) or {}
    name = raw.get("name") or (path.stem if path else "")
    version = hashlib.sha256((raw_text + "\x00" + instructions).encode()).hexdigest()

    adapters = raw.get("adapters") or {}
    triggers = []
    for i, t in enumerate(raw.get("triggers") or []):
        kind = "schedule" if "schedule" in t else ("poll" if "poll" in t else "unknown")
        triggers.append(Trigger(kind=kind, raw=t, index=i))

    return Agent(
        name=name,
        path=path if path is not None else Path(config.AGENTS_DIR) / f"{name}.yaml",
        raw=raw,
        version=version,
        instructions=instructions,
        model=raw.get("model") or "mock/echo",
        channels=list(adapters.get("channels") or []),
        tool_names=list(adapters.get("tools") or []),
        triggers=triggers,
        policies=list(raw.get("policies") or []),
        memory=raw.get("memory") or {},
        expose=raw.get("expose") or {},
        workspace=raw.get("workspace"),
        description=raw.get("description", ""),
        handler=raw.get("handler"),
        source=raw_text,
        pinned_for=pinned_for,
    )


class Registry:
    """Reads agents/ and tools/ from disk. Cached by mtime so a hot loop does
    not stat-and-parse on every event, but an edit is picked up immediately."""

    def __init__(self, agents_dir: Path = None, tools_dir: Path = None):
        self.agents_dir = Path(agents_dir or config.AGENTS_DIR)
        self.tools_dir = Path(tools_dir or config.TOOLS_DIR)
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._extra_tools: dict[str, Tool] = {}

    # ------------------------------------------------------------------ tools

    def tools(self) -> dict[str, Tool]:
        out: dict[str, Tool] = {}
        if self.tools_dir.exists():
            for tdir in sorted(self.tools_dir.iterdir()):
                manifest = tdir / "tool.yaml"
                if not tdir.is_dir() or not manifest.exists():
                    continue
                try:
                    out_tool = self._load_tool(manifest)
                    out[out_tool.name] = out_tool
                except Exception as exc:  # a broken tool must not hide the good ones
                    print(f"[registry] skipping {manifest}: {exc}", file=sys.stderr)
        out.update(self._extra_tools)
        return out

    def _load_tool(self, manifest: Path) -> Tool:
        key = f"tool:{manifest}"
        mtime = manifest.stat().st_mtime
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] == mtime:
                return cached[1]
        raw = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        name = raw.get("name") or manifest.parent.name
        handler = raw.get("handler") or "./handler.py"
        tool = Tool(
            name=name,
            description=raw.get("description", ""),
            input_schema=normalize_schema(raw.get("input")),
            output_schema=normalize_schema(raw.get("output")),
            handler_path=(manifest.parent / handler).resolve(),
            dir=manifest.parent,
            raw=raw,
            mock=raw.get("mock"),
            cost_eur=float(raw.get("cost_eur", 0) or 0),
        )
        with self._lock:
            self._cache[key] = (mtime, tool)
        return tool

    def get_tool(self, name: str) -> Optional[Tool]:
        return self.tools().get(name)

    def register_tool(self, tool: Tool) -> None:
        """Attach a tool that has no directory — agents-as-tools and MCP."""
        self._extra_tools[tool.name] = tool

    # ----------------------------------------------------------------- agents

    def agents(self) -> dict[str, Agent]:
        out: dict[str, Agent] = {}
        if not self.agents_dir.exists():
            return out
        for path in sorted(self.agents_dir.glob("*.y*ml")):
            try:
                a = self._load_agent(path)
                out[a.name] = a
            except Exception as exc:
                print(f"[registry] skipping {path}: {exc}", file=sys.stderr)
        return out

    def _load_agent(self, path: Path) -> Agent:
        """Parse an agent, or hand back the parse from last time.

        The cache was being written and never read, so every call re-read and
        re-parsed every agent file and its instructions, and hashed both. One
        console page asks for the agents several times over; at three hundred
        agents that was most of the time spent rendering it.

        Both files decide the version, so both are watched: editing the
        instructions alone must still produce a new version.
        """
        key = f"agent:{path}"
        with self._lock:
            cached = self._cache.get(key)
        if cached:
            yaml_at, md_path, md_at, agent = cached
            if _mtime(path) == yaml_at and _mtime(md_path) == md_at:
                return agent

        raw_text = path.read_text(encoding="utf-8")
        ipath = None
        instructions = (yaml.safe_load(raw_text) or {}).get("instructions") or ""
        if isinstance(instructions, str) and (
            instructions.strip().startswith("./") or instructions.strip().endswith(".md")
        ):
            ipath = (path.parent / instructions.strip()).resolve()
            instructions = ipath.read_text(encoding="utf-8") if ipath.exists() else ""

        agent = build_agent(raw_text, instructions, path=path)
        with self._lock:
            self._cache[key] = (_mtime(path), ipath, _mtime(ipath), agent)
        return agent

    def get_agent(self, name: str) -> Optional[Agent]:
        return self.agents().get(name)

    def agent_tools(self, agent: Agent) -> dict[str, Tool]:
        """Resolve an agent's mounted tools, including `agent:<name>` delegation
        entries and `mcp:<server>/<tool>` entries."""
        all_tools = self.tools()
        resolved: dict[str, Tool] = {}
        for ref in agent.tool_names:
            if isinstance(ref, dict):  # e.g. {mcp: server_name}
                for t in self._mcp_tools(ref):
                    resolved[t.name] = t
                continue
            if ref.startswith("agent:"):
                sub = ref.split(":", 1)[1]
                subagent = self.get_agent(sub)
                if not subagent:
                    continue
                resolved[f"ask_{sub}"] = Tool(
                    name=f"ask_{sub}",
                    description=subagent.description
                    or f"Delegate to the '{sub}' agent and return its reply.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "message": {"type": "string", "description": "What to ask the agent."}
                        },
                        "required": ["message"],
                    },
                    output_schema=normalize_schema({"reply": "string", "session_id": "string"}),
                    handler_path=None,
                    dir=subagent.path.parent,
                    raw={"agent": sub},
                    source="agent",
                )
                continue
            t = all_tools.get(ref)
            if t:
                resolved[t.name] = t

        # A workspace and the tools that reach it are one choice, not four: an
        # agent given a folder to work in gets the three ways of working in it,
        # and an agent without one has no file tools to mount by mistake.
        from . import filetools

        resolved.update(filetools.tools_for(agent))
        return resolved

    def _mcp_tools(self, ref: dict) -> list[Tool]:
        from .mcp_client import discover_tools

        try:
            return discover_tools(ref)
        except Exception as exc:
            print(f"[registry] mcp discovery failed for {ref}: {exc}", file=sys.stderr)
            return []

    # ------------------------------------------------------------------ write

    def write_agent(self, name: str, text: str) -> Agent:
        """The console's only write path for agent definitions — writes the file,
        so `git diff` stays the truth."""
        yaml.safe_load(text)  # fail before touching disk if it isn't valid YAML
        path = self.agents_dir / f"{name}.yaml"
        if not path.exists():
            alt = self.agents_dir / f"{name}.yml"
            if alt.exists():
                path = alt
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return self._load_agent(path)

    def write_instructions(self, agent: Agent, text: str) -> None:
        ref = agent.raw.get("instructions")
        if isinstance(ref, str) and (ref.strip().startswith("./") or ref.strip().endswith(".md")):
            (agent.path.parent / ref.strip()).write_text(text, encoding="utf-8")
        else:
            raise ValueError("agent has inline instructions; edit the definition instead")

    def write_tool(self, name: str, manifest_text: str = None, handler_text: str = None) -> Tool:
        tdir = self.tools_dir / name
        tdir.mkdir(parents=True, exist_ok=True)
        if manifest_text is not None:
            yaml.safe_load(manifest_text)
            (tdir / "tool.yaml").write_text(manifest_text, encoding="utf-8")
        if handler_text is not None:
            (tdir / "handler.py").write_text(handler_text, encoding="utf-8")
        return self._load_tool(tdir / "tool.yaml")


_registry: Optional[Registry] = None


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry()
    return _registry
