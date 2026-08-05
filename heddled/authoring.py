"""Creating and changing agents, tools and policies (§6, §9, decision 9).

One module behind both surfaces: `heddled new agent` and the console's **New agent**
button call the same function and write the same bytes. That is what keeps the
CLI and the UI from drifting into two subtly different products.

Everything here writes files and nothing else. Validation happens before the
file is touched, so a rejected edit leaves the previous version intact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config, gitio, yamlio
from .registry import get_registry, normalize_schema

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class AuthoringError(ValueError):
    """A rejected edit. The message is written for a human to read."""


def check_name(name: str, kind: str = "agent") -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise AuthoringError(
            f"{kind} name must be lowercase letters, digits and underscores, "
            f"starting with a letter — got {name!r}"
        )
    return name


# ------------------------------------------------------------------- agents

AGENT_TEMPLATE = """\
# An agent is one file. Edit it here or in the console — same file either way.
name: {name}
description: {description}
model: {model}
instructions: ./{name}.md

adapters:
  channels: [webchat]
  tools: []

policies: []

memory:
  session: auto
"""

INSTRUCTIONS_TEMPLATE = """\
You are {name}, a helpful agent.

Describe what you do, how you should behave, and when to use each of your tools.
This file is your system prompt — it is sent to the model on every turn.
"""

# Prefilled into the new-agent form. Written as a fill-in-the-blanks briefing
# rather than an empty box, because "describe your system prompt" is not a
# useful thing to say to someone who has never written one.
STARTER_INSTRUCTIONS = """\
You help colleagues with questions about ______.

How to behave:
- Be brief and direct. Give the answer first, then the detail.
- Use the tools you have been given rather than guessing.
- If you do not know something, say so plainly and suggest who to ask.

Never:
- Make up numbers, dates, names or prices.
- Promise anything on behalf of the company.
"""


@dataclass
class Written:
    """What an authoring call did, so the caller can show it."""
    paths: list[Path]
    diff: str = ""
    committed: Optional[str] = None
    # Agents that had to be edited to make this write possible — a delete
    # unmounts itself from whoever was using it, a rename repoints them, and
    # both say so afterwards.
    unmounted_from: list[str] = field(default_factory=list)
    repointed: list[str] = field(default_factory=list)

    @property
    def path(self) -> Optional[Path]:
        return self.paths[0] if self.paths else None


def agent_path(name: str) -> Path:
    for suffix in (".yaml", ".yml"):
        candidate = Path(config.AGENTS_DIR) / f"{name}{suffix}"
        if candidate.exists():
            return candidate
    return Path(config.AGENTS_DIR) / f"{name}.yaml"


def new_agent(name: str, model: str = "mock/echo", description: str = None,
              from_agent: str = None, instructions: str = None,
              tools: list = None, approval_tools: list = None,
              commit: bool = None) -> Written:
    """Scaffold an agent, optionally as a copy of an existing one.

    Most second agents are a variation on the first, so `--from` is not a
    convenience afterthought — it is how the second agent normally gets made.
    """
    name = check_name(name, "agent")
    path = agent_path(name)
    if path.exists():
        raise AuthoringError(f"agent '{name}' already exists at {path}")

    description = description or f"The {name} agent."
    registry = get_registry()

    if from_agent:
        source = registry.get_agent(from_agent)
        if not source:
            raise AuthoringError(f"no agent named '{from_agent}' to copy from")
        data = yamlio.load(source.raw_text())
        data["name"] = name
        data["description"] = description
        if isinstance(data.get("instructions"), str) and data["instructions"].endswith(".md"):
            data["instructions"] = f"./{name}.md"
        text = yamlio.dump(data)
        instructions = (instructions or source.instructions
                        or INSTRUCTIONS_TEMPLATE.format(name=name))
    else:
        text = AGENT_TEMPLATE.format(name=name, model=model, description=description)
        # Tools and approval gates chosen in the form, so a first agent arrives
        # already able to do something rather than needing a second edit.
        if tools or approval_tools:
            data = yamlio.load(text)
            adapters = data.get("adapters") or {}
            adapters["tools"] = list(tools or [])
            data["adapters"] = adapters
            data["policies"] = [
                {"tool": t, "requires_approval": True} for t in (approval_tools or [])
            ]
            text = yamlio.dump(data)
        instructions = instructions or INSTRUCTIONS_TEMPLATE.format(name=name)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    md_path = path.parent / f"{name}.md"
    md_path.write_text(instructions, encoding="utf-8")

    written = Written(paths=[path, md_path], diff=yamlio.diff("", text, str(path)))
    written.committed = gitio.maybe_commit(
        [path, md_path], f"{name}: create agent", enabled=commit)
    return written


def keep_outgoing_version(name: str) -> None:
    """Keep the definition about to be replaced.

    Called before every write, so an edit can always be undone. Relying on
    somebody having opened the agent's page first meant the version you most
    wanted back — the one you just typed over — was usually the one nobody had
    recorded. Bookkeeping never breaks the edit it is describing.
    """
    try:
        from .store import get_store

        agent = get_registry().get_agent(name)
        if agent:
            get_store().record_agent_version(agent)
    except Exception:
        pass


def save_agent(name: str, text: str, commit: bool = None) -> Written:
    """Write a full agent definition, validating before touching the file."""
    ok, error = yamlio.is_valid(text)
    if not ok:
        raise AuthoringError(f"invalid YAML — {error}")
    data = yamlio.load(text)
    if not isinstance(data, dict):
        raise AuthoringError("an agent definition must be a YAML mapping")
    if data.get("name") and data["name"] != name:
        raise AuthoringError(
            f"the definition names '{data['name']}' but this is agent '{name}' — "
            "use Rename, next to the name at the top of the page, so everything "
            "pointing at it moves too"
        )

    path = agent_path(name)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    if not yamlio.has_changes(before, text):
        return Written(paths=[], diff="")

    keep_outgoing_version(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    written = Written(paths=[path], diff=yamlio.diff(before, text, str(path)))
    written.committed = gitio.maybe_commit([path], f"{name}: update agent", enabled=commit)
    return written


def update_agent_fields(name: str, updates: dict, commit: bool = None) -> Written:
    """The structured form's write path: change only the keys it owns."""
    path = agent_path(name)
    if not path.exists():
        raise AuthoringError(f"no agent named '{name}'")
    before = path.read_text(encoding="utf-8")
    return save_agent(name, yamlio.apply_updates(before, updates), commit=commit)


def rename_agent(name: str, new_name: str, commit: bool = None) -> Written:
    """Give an agent a different name, and follow every reference to it.

    A name typed wrong used to be permanent from the console: the file editor
    refused the change ("rename the file instead") and the only cure was to
    delete the agent and build it again, losing its instructions, gates and
    triggers with it.
    """
    new_name = check_name(new_name, "agent")
    path = agent_path(name)
    if not path.exists():
        raise AuthoringError(f"no agent named '{name}'")
    if new_name == name:
        return Written(paths=[])
    if agent_path(new_name).exists():
        raise AuthoringError(f"there is already an agent called '{new_name}'")

    text = path.read_text(encoding="utf-8")
    data = yamlio.load(text)
    data["name"] = new_name
    paths = [agent_path(new_name)]

    # The instructions travel with it, but only when they are this agent's own
    # sibling file — a shared or absolute path is left where it is.
    md = path.parent / f"{name}.md"
    if (isinstance(data.get("instructions"), str)
            and data["instructions"].strip().lstrip("./") == f"{name}.md" and md.exists()):
        new_md = path.parent / f"{new_name}.md"
        md.rename(new_md)
        data["instructions"] = f"./{new_name}.md"
        paths.append(new_md)

    paths[0].write_text(yamlio._restore_cosmetic(text, yamlio.dump(data)), encoding="utf-8")
    path.unlink()

    touched = _repoint("agent:" + name, "agent:" + new_name,
                       policy_from=f"ask_{name}", policy_to=f"ask_{new_name}",
                       commit=commit)
    written = Written(paths=paths)
    written.repointed = touched
    written.committed = gitio.maybe_commit(
        paths + [path], f"{name}: rename to {new_name}", enabled=commit)
    return written


def _repoint(ref: str, new_ref: str, policy_from: str = None, policy_to: str = None,
             commit: bool = None) -> list[str]:
    """Point every agent that mounts `ref` at its new name instead."""
    touched = []
    for agent in sorted(get_registry().agents().values(), key=lambda a: a.name):
        if ref not in [r for r in agent.tool_names if isinstance(r, str)]:
            continue
        path = agent_path(agent.name)
        before = path.read_text(encoding="utf-8")
        data = yamlio.load(before)
        adapters = data.get("adapters") or {}
        adapters["tools"] = [new_ref if r == ref else r
                             for r in (adapters.get("tools") or [])]
        for policy in data.get("policies") or []:
            if isinstance(policy, dict) and policy.get("tool") == policy_from:
                policy["tool"] = policy_to
        save_agent(agent.name, yamlio._restore_cosmetic(before, yamlio.dump(data)),
                   commit=commit)
        touched.append(agent.name)
    return touched


def delete_agent(name: str, force: bool = False, commit: bool = None) -> Written:
    path = agent_path(name)
    if not path.exists():
        raise AuthoringError(f"no agent named '{name}'")
    agent = get_registry().get_agent(name)
    paths = [path]
    if agent and isinstance(agent.raw.get("instructions"), str):
        md = (path.parent / agent.raw["instructions"].strip()).resolve()
        if md.exists() and md.parent == path.parent.resolve():
            paths.append(md)

    dependents = agents_delegating_to(name)
    if dependents and not force:
        raise AuthoringError(
            f"'{name}' is mounted as a tool by {', '.join(dependents)} — "
            "unmount it there first, or delete with force"
        )
    unmounted = unmount_everywhere(f"agent:{name}", commit=commit) if dependents else []
    for p in paths:
        p.unlink()
    written = Written(paths=paths)
    written.unmounted_from = unmounted
    written.committed = gitio.maybe_commit(paths, f"{name}: delete agent", enabled=commit)
    return written


def agents_delegating_to(name: str) -> list[str]:
    """Which agents mount this agent as a tool (`agent:<name>`)."""
    out = []
    for other in get_registry().agents().values():
        if other.name == name:
            continue
        for ref in other.tool_names:
            if isinstance(ref, str) and ref == f"agent:{name}":
                out.append(other.name)
    return sorted(out)


# -------------------------------------------------------------------- tools

TOOL_TEMPLATE = """\
name: {name}
description: {description}
input:  {{{input_fields}}}
output: {{{output_fields}}}
handler: ./handler.py
"""

HANDLER_TEMPLATE = '''\
"""{description}

`handle(args, ctx)` is the whole tool contract:
  args  — validated against the `input:` schema in tool.yaml
  ctx   — .log(msg) puts a line on the trace, .memory() is per-session state
"""


def handle(args, ctx):
    ctx.log("running {name}")
    return {{{output_stub}}}
'''


def check_handler(handler: str, name: str) -> None:
    """The two things that make a handler file usable at all.

    Applied on create as well as save — a tool that only fails the first time an
    agent reaches for it is a worse bug report than one that fails on the form.
    """
    try:
        compile(handler, f"{name}/handler.py", "exec")
    except SyntaxError as exc:
        raise AuthoringError(f"handler has a syntax error on line {exc.lineno}: {exc.msg}")
    if "def handle" not in handler:
        raise AuthoringError("a handler must define `handle(args, ctx)`")


def tool_dir(name: str) -> Path:
    return Path(config.TOOLS_DIR) / name


def parse_field_spec(spec: str) -> dict:
    """`invoice_number:string,amount_eur:number` → the shorthand tool.yaml uses."""
    fields = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        field, _, ftype = part.partition(":")
        field = field.strip()
        if not field:
            raise AuthoringError(f"cannot parse field spec {part!r}")
        fields[field] = (ftype.strip() or "string")
    return fields


_STUB_VALUES = {
    "number": "0.0", "float": "0.0", "integer": "0", "int": "0",
    "boolean": "False", "bool": "False",
    "array": "[]", "list": "[]", "object": "{}", "dict": "{}",
}


def _output_stub(outputs: dict) -> str:
    """A return statement the handler can actually run on day one."""
    parts = [f'"{field}": {_STUB_VALUES.get(ftype, chr(34) * 2)}'
             for field, ftype in outputs.items()]
    return ", ".join(parts)


def new_tool(name: str, description: str = None, input_spec=None, output_spec=None,
             from_tool: str = None, tool_type: str = None, config: dict = None,
             handler: str = None, commit: bool = None) -> Written:
    name = check_name(name, "tool")
    tdir = tool_dir(name)
    if tdir.exists():
        raise AuthoringError(f"tool '{name}' already exists at {tdir}")

    description = description or f"The {name} tool."

    if tool_type and tool_type != "python":
        return _new_no_code_tool(name, description, input_spec, tool_type,
                                 config or {}, commit)

    # Code written on the create screen wins over anything generated: it is the
    # one thing here the author typed themselves.
    written_by_hand = (handler or "").strip()
    if written_by_hand:
        check_handler(written_by_hand, name)

    if from_tool:
        source = get_registry().get_tool(from_tool)
        if not source or not source.dir:
            raise AuthoringError(f"no tool named '{from_tool}' to copy from")
        data = yamlio.load((source.dir / "tool.yaml").read_text(encoding="utf-8"))
        data["name"] = name
        data["description"] = description
        manifest = yamlio.dump(data)
        handler = written_by_hand or (
            (source.dir / "handler.py").read_text(encoding="utf-8")
            if (source.dir / "handler.py").exists() else HANDLER_TEMPLATE.format(
                name=name, description=description, output_stub=""))
    else:
        inputs = input_spec if isinstance(input_spec, dict) else parse_field_spec(input_spec)
        outputs = output_spec if isinstance(output_spec, dict) else parse_field_spec(output_spec)
        inputs = inputs or {"text": "string"}
        outputs = outputs or {"result": "string"}
        manifest = TOOL_TEMPLATE.format(
            name=name,
            description=description,
            input_fields=", ".join(f"{k}: {v}" for k, v in inputs.items()),
            output_fields=", ".join(f"{k}: {v}" for k, v in outputs.items()),
        )
        handler = written_by_hand or HANDLER_TEMPLATE.format(
            name=name, description=description, output_stub=_output_stub(outputs))

    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "tool.yaml").write_text(manifest, encoding="utf-8")
    (tdir / "handler.py").write_text(handler, encoding="utf-8")

    paths = [tdir / "tool.yaml", tdir / "handler.py"]
    written = Written(paths=paths, diff=yamlio.diff("", manifest, str(paths[0])))
    written.committed = gitio.maybe_commit(paths, f"{name}: create tool", enabled=commit)
    return written


def _new_no_code_tool(name: str, description: str, input_spec, tool_type: str,
                      config: dict, commit: bool = None) -> Written:
    """A tool built from a form. No handler file is written — there is no code."""
    from . import tooltypes

    if tool_type not in tooltypes.BUILDERS:
        raise AuthoringError(
            f"'{tool_type}' is not a kind of tool Heddled knows. Choose one of: "
            + ", ".join(sorted(tooltypes.BUILDERS))
        )
    inputs = input_spec if isinstance(input_spec, dict) else parse_field_spec(input_spec)
    inputs = inputs or {"text": "string"}

    manifest = yamlio.dump({
        "name": name,
        "description": description,
        "input": dict(inputs),
        "type": tool_type,
        "config": config,
    })

    # Build it once now, so a misconfiguration is reported on the form that
    # caused it rather than in the middle of someone's conversation.
    try:
        tooltypes.build_handler(yamlio.load(manifest))
    except tooltypes.ToolTypeError as exc:
        raise AuthoringError(str(exc))

    tdir = tool_dir(name)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "tool.yaml").write_text(manifest, encoding="utf-8")
    written = Written(paths=[tdir / "tool.yaml"],
                      diff=yamlio.diff("", manifest, str(tdir / "tool.yaml")))
    written.committed = gitio.maybe_commit(written.paths, f"{name}: create tool",
                                           enabled=commit)
    return written


def update_tool_config(name: str, description: str = None, input_spec=None,
                       config: dict = None, commit: bool = None) -> Written:
    """The no-code tool form's write path."""
    from . import tooltypes

    tdir = tool_dir(name)
    path = tdir / "tool.yaml"
    if not path.exists():
        raise AuthoringError(f"no tool named '{name}'")

    before = path.read_text(encoding="utf-8")
    updates: dict = {}
    if description is not None:
        updates["description"] = description
    if input_spec is not None:
        fields = input_spec if isinstance(input_spec, dict) else parse_field_spec(input_spec)
        updates["input"] = dict(fields)
    if config is not None:
        updates["config"] = config

    text = yamlio.apply_updates(before, updates)
    try:
        tooltypes.build_handler(yamlio.load(text))
    except tooltypes.ToolTypeError as exc:
        raise AuthoringError(str(exc))
    return save_tool(name, manifest=text, commit=commit)


def save_tool(name: str, manifest: str = None, handler: str = None,
              commit: bool = None) -> Written:
    tdir = tool_dir(name)
    if not tdir.exists():
        raise AuthoringError(f"no tool named '{name}'")

    paths, diffs = [], []
    if manifest is not None:
        ok, error = yamlio.is_valid(manifest)
        if not ok:
            raise AuthoringError(f"invalid YAML — {error}")
        data = yamlio.load(manifest)
        if not isinstance(data, dict):
            raise AuthoringError("a tool manifest must be a YAML mapping")
        if data.get("name") and data["name"] != name:
            raise AuthoringError(
                f"the manifest names '{data['name']}' but this is tool '{name}' — "
                "use Rename, next to the name at the top of the page, so the "
                "agents using it move too")
        # A schema the engine cannot turn into a model schema is not savable.
        normalize_schema(data.get("input"))
        normalize_schema(data.get("output"))

        path = tdir / "tool.yaml"
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        if yamlio.has_changes(before, manifest):
            path.write_text(manifest, encoding="utf-8")
            paths.append(path)
            diffs.append(yamlio.diff(before, manifest, str(path)))

    if handler is not None:
        check_handler(handler, name)

        path = tdir / "handler.py"
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        if yamlio.has_changes(before, handler):
            path.write_text(handler, encoding="utf-8")
            paths.append(path)
            diffs.append(yamlio.diff(before, handler, str(path)))

    written = Written(paths=paths, diff="\n".join(diffs))
    if paths:
        written.committed = gitio.maybe_commit(paths, f"{name}: update tool", enabled=commit)
    return written


def rename_tool(name: str, new_name: str, commit: bool = None) -> Written:
    """Rename a tool, and repoint every agent that mounts it."""
    new_name = check_name(new_name, "tool")
    tdir = tool_dir(name)
    if not tdir.exists():
        raise AuthoringError(f"no tool named '{name}'")
    if new_name == name:
        return Written(paths=[])
    if tool_dir(new_name).exists():
        raise AuthoringError(f"there is already a tool called '{new_name}'")

    new_dir = tool_dir(new_name)
    tdir.rename(new_dir)
    manifest = new_dir / "tool.yaml"
    text = manifest.read_text(encoding="utf-8")
    data = yamlio.load(text)
    data["name"] = new_name
    manifest.write_text(yamlio._restore_cosmetic(text, yamlio.dump(data)), encoding="utf-8")

    touched = _repoint(name, new_name, policy_from=name, policy_to=new_name, commit=commit)
    paths = sorted(p for p in new_dir.rglob("*") if p.is_file())
    written = Written(paths=paths)
    written.repointed = touched
    written.committed = gitio.maybe_commit(paths, f"{name}: rename to {new_name}",
                                           enabled=commit)
    return written


def delete_tool(name: str, force: bool = False, commit: bool = None) -> Written:
    tdir = tool_dir(name)
    if not tdir.exists():
        raise AuthoringError(f"no tool named '{name}'")
    mounted = agents_using_tool(name)
    if mounted and not force:
        raise AuthoringError(
            f"'{name}' is mounted by {', '.join(mounted)} — unmount it there first, "
            "or delete with force"
        )
    unmounted = unmount_everywhere(name, commit=commit) if mounted else []
    paths = sorted(p for p in tdir.rglob("*") if p.is_file())
    for p in paths:
        p.unlink()
    for d in sorted((p for p in tdir.rglob("*") if p.is_dir()), reverse=True):
        d.rmdir()
    tdir.rmdir()
    written = Written(paths=paths)
    written.unmounted_from = unmounted
    written.committed = gitio.maybe_commit(paths, f"{name}: delete tool", enabled=commit)
    return written


def unmount_everywhere(ref: str, commit: bool = None) -> list[str]:
    """Take a tool (or `agent:<name>`) out of every agent that mounts it.

    Deleting something that is in use used to be a dead end: the console said
    "unmount it there first" and gave you nowhere to do it. Forcing it was no
    better — the files went, the `tools:` entries stayed, and the agent kept a
    reference to a tool that no longer exists. So a forced delete now does the
    unmounting itself, and the caller reports which agents it had to change.
    """
    bare = ref.split(":", 1)[1] if ref.startswith("agent:") else ref
    touched = []
    for agent in sorted(get_registry().agents().values(), key=lambda a: a.name):
        if ref not in [r for r in agent.tool_names if isinstance(r, str)]:
            continue
        path = agent_path(agent.name)
        before = path.read_text(encoding="utf-8")
        data = yamlio.load(before)
        adapters = data.get("adapters") or {}
        adapters["tools"] = [r for r in (adapters.get("tools") or []) if r != ref]
        # A policy naming a tool the agent can no longer reach is dead weight,
        # and it would show up in the console as a rule about nothing.
        policies = data.get("policies")
        if policies is not None:
            data["policies"] = [p for p in policies
                                if not (isinstance(p, dict) and p.get("tool") == bare)]
        save_agent(agent.name, yamlio._restore_cosmetic(before, yamlio.dump(data)),
                   commit=commit)
        touched.append(agent.name)
    return touched


def mount_index() -> dict[str, list[str]]:
    """Every mounted reference, mapped back to the agents that mount it.

    Asking per tool meant walking every agent once per tool — 300 tools over
    300 agents is ninety thousand passes to render one page. One walk answers
    for all of them.
    """
    index: dict[str, list[str]] = {}
    for agent in get_registry().agents().values():
        for ref in agent.tool_names:
            if isinstance(ref, str):
                index.setdefault(ref, []).append(agent.name)
    return {ref: sorted(names) for ref, names in index.items()}


def agents_using_tool(name: str) -> list[str]:
    """Who is affected by a change to this tool — shown before you save it,
    because the registry is global and a shared tool's blast radius is invisible
    from inside any one agent (§9)."""
    out = []
    for agent in get_registry().agents().values():
        for ref in agent.tool_names:
            if isinstance(ref, str) and ref == name:
                out.append(agent.name)
                break
    return sorted(out)


# ----------------------------------------------------------------- policies


def add_policy(agent_name: str, policy: dict, commit: bool = None) -> Written:
    """Append a policy block to an agent, or merge into the one already
    governing that tool."""
    path = agent_path(agent_name)
    if not path.exists():
        raise AuthoringError(f"no agent named '{agent_name}'")
    if not policy.get("tool"):
        raise AuthoringError("a policy must name a tool (or \"*\")")

    before = path.read_text(encoding="utf-8")
    data = yamlio.load(before)
    policies = data.get("policies")
    if policies is None:
        policies = []
        data["policies"] = policies

    for existing in policies:
        if existing.get("tool") == policy["tool"]:
            existing.update({k: v for k, v in policy.items() if k != "tool"})
            break
    else:
        policies.append(policy)

    text = yamlio._restore_cosmetic(before, yamlio.dump(data))
    return save_agent(agent_name, text, commit=commit)


# ------------------------------------------------------------- composition


def set_mounted(agent_name: str, tools: list, agents: list = None,
                mcp_servers: list = None, commit: bool = None) -> Written:
    """Replace what an agent may use: file tools, other agents, MCP servers.

    Composition is where the platform gets genuinely powerful — an agent that
    can hand work to a specialist, or reach a third-party MCP server — and all
    of it was reachable only by hand-editing YAML.
    """
    path = agent_path(agent_name)
    if not path.exists():
        raise AuthoringError(f"no agent named '{agent_name}'")

    refs: list = list(tools or [])
    for other in agents or []:
        if other == agent_name:
            raise AuthoringError("an agent cannot be given itself as a tool")
        refs.append(f"agent:{other}")
    for server in mcp_servers or []:
        refs.append({"mcp": server})

    before = path.read_text(encoding="utf-8")
    data = yamlio.load(before)
    adapters = data.get("adapters")
    if adapters is None:
        adapters = {}
        data["adapters"] = adapters
    adapters["tools"] = refs
    return save_agent(agent_name, yamlio._restore_cosmetic(before, yamlio.dump(data)),
                      commit=commit)


def mounted_breakdown(agent) -> dict:
    """Split an agent's mounted tools back into the three kinds, so the form can
    show each in its own place."""
    tools, agents, servers = [], [], []
    for ref in agent.tool_names:
        if isinstance(ref, dict) and ref.get("mcp"):
            servers.append(ref["mcp"])
        elif isinstance(ref, str) and ref.startswith("agent:"):
            agents.append(ref.split(":", 1)[1])
        elif isinstance(ref, str):
            tools.append(ref)
    return {"tools": tools, "agents": agents, "mcp": servers}


def check_mcp_server(spec: dict) -> list:
    """Connect to an MCP server and report the tools it offers, so somebody can
    see it worked before saving a URL they cannot verify by eye."""
    from .mcp_client import discover_tools

    if not (spec or {}).get("url"):
        raise AuthoringError("give the address of the MCP server")
    try:
        return discover_tools({"mcp": spec})
    except Exception as exc:
        raise AuthoringError(f"could not reach that server — {exc}")


# --------------------------------------------------------------- triggers

# The shapes people actually want, expressed as a picker rather than as cron.
# "Agentic workflow" mostly means "it does this on its own" — making that
# YAML-only put the whole point of the product behind a text editor.
SCHEDULE_CHOICES = {
    "every_day":     ("Every day",              "{minute} {hour} * * *"),
    "weekdays":      ("Every weekday",          "{minute} {hour} * * 1-5"),
    "every_monday":  ("Every Monday",           "{minute} {hour} * * 1"),
    "first_of_month": ("First of the month",    "{minute} {hour} 1 * *"),
    "hourly":        ("Every hour",             "{minute} * * * *"),
    "every_15_min":  ("Every 15 minutes",       "*/15 * * * *"),
    "custom":        ("A schedule I type myself", None),
}


def cron_from_choice(choice: str, at: str = "08:00", custom: str = "") -> str:
    """Turn the picker's answer into a cron expression."""
    if choice == "custom":
        expr = (custom or "").strip()
        if len(expr.split()) != 5:
            raise AuthoringError(
                "A schedule needs five parts, like `0 8 * * 1-5` "
                "(minute, hour, day, month, weekday)."
            )
        return expr

    entry = SCHEDULE_CHOICES.get(choice)
    if not entry or not entry[1]:
        raise AuthoringError(f"'{choice}' is not one of the schedules offered")

    hour, _, minute = (at or "08:00").partition(":")
    try:
        hour, minute = int(hour), int(minute or 0)
    except ValueError:
        raise AuthoringError(f"'{at}' is not a time — use something like 08:00")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise AuthoringError(f"'{at}' is not a time of day")
    return entry[1].format(hour=hour, minute=minute)


def add_trigger(agent_name: str, trigger: dict, commit: bool = None) -> Written:
    """Append a trigger. Validated here so a broken schedule is refused on the
    form rather than discovered when it silently never fires."""
    from . import triggers as trigger_mod

    path = agent_path(agent_name)
    if not path.exists():
        raise AuthoringError(f"no agent named '{agent_name}'")

    if trigger.get("schedule"):
        try:
            trigger_mod.cron_matches(trigger["schedule"], __import__("datetime").datetime.now())
        except ValueError as exc:
            raise AuthoringError(str(exc))
    elif trigger.get("poll"):
        try:
            trigger_mod.parse_interval(trigger.get("every", "60s"))
        except ValueError as exc:
            raise AuthoringError(str(exc))
    else:
        raise AuthoringError("a trigger is either a schedule or something to watch")

    if not (trigger.get("message") or trigger.get("on_new")):
        raise AuthoringError("say what the agent should do when this fires")

    before = path.read_text(encoding="utf-8")
    data = yamlio.load(before)
    existing = data.get("triggers")
    if existing is None:
        existing = []
        data["triggers"] = existing
    existing.append(trigger)
    return save_agent(agent_name, yamlio._restore_cosmetic(before, yamlio.dump(data)),
                      commit=commit)


def remove_trigger(agent_name: str, index: int, commit: bool = None) -> Written:
    path = agent_path(agent_name)
    if not path.exists():
        raise AuthoringError(f"no agent named '{agent_name}'")
    before = path.read_text(encoding="utf-8")
    data = yamlio.load(before)
    existing = list(data.get("triggers") or [])
    if not 0 <= index < len(existing):
        raise AuthoringError("that trigger is no longer there")
    existing.pop(index)
    data["triggers"] = existing
    return save_agent(agent_name, yamlio._restore_cosmetic(before, yamlio.dump(data)),
                      commit=commit)


def remove_policy(agent_name: str, tool: str, commit: bool = None) -> Written:
    path = agent_path(agent_name)
    if not path.exists():
        raise AuthoringError(f"no agent named '{agent_name}'")
    before = path.read_text(encoding="utf-8")
    data = yamlio.load(before)
    policies = data.get("policies") or []
    kept = [p for p in policies if p.get("tool") != tool]
    if len(kept) == len(policies):
        raise AuthoringError(f"'{agent_name}' has no policy for tool '{tool}'")
    data["policies"] = kept
    text = yamlio._restore_cosmetic(before, yamlio.dump(data))
    return save_agent(agent_name, text, commit=commit)
