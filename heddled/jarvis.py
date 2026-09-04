"""The part that builds itself, kept somewhere it can only hurt itself.

Jarvis is a drift from what the rest of Heddled is for, and it is fenced
accordingly. Three rules carry the whole design:

**It writes only into its own tree.** `jarvis/agents`, `jarvis/tools`,
`jarvis/work`. The operator's `agents/` and `tools/` are not merely
policy-protected from it — they are a different directory, reached through a
different `Registry`, and nothing here holds a path to them. The rule stands
untouched: an agent that could rewrite `agents/support.yaml` could delete the
approval gate on `refund`, and no setting anywhere turns that off.

**It may reach the operator's agents, but only to ask them.** `ask_agent` runs
one and returns its reply. Governance travels with the agent exactly as it does
for an MCP caller: if `support` gates `refund` behind approval, a question from
Jarvis stops for a person like any other. Reading and invoking, never writing.
Every such turn arrives on the `jarvis` channel, so a policy can say
`deny_channels: [jarvis]` and mean it.

**Everything it makes is inert until a person moves it.** A Jarvis agent runs
only inside a Jarvis run. `promote` is the one door between the two trees, it
refuses to overwrite anything already there, and Python it wrote stays
sandboxed on the far side.

And a run is the unit, not an agent: a goal, a budget, a step cap. Discard the
run and what it made goes with it — which is what makes it safe to stop caring
about one.
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from . import config, yamlio
from .events import new_id
from .registry import Registry, Tool, normalize_schema
from .store import get_store

SETTING = "jarvis_enabled"
MODEL_SETTING = "jarvis_model"
DEFAULT_MODEL = "mock/echo"

#: The channel every Jarvis-driven turn arrives on, including questions put to
#: the operator's own agents. Nameable in a policy: `deny_channels: [jarvis]`.
CHANNEL = "jarvis"

#: A run costs money and takes steps, and both are required rather than
#: defaulted. An autonomous loop with no ceiling is the one shape worth making
#: impossible to ask for.
MAX_BUDGET_EUR = 50.0
MAX_STEPS = 200

#: The agent that drives a run. It is the only one with the builder tools: an
#: agent Jarvis makes is an ordinary agent and does not get to make more of
#: itself.
DRIVER = "jarvis"

_lock = threading.Lock()
_running: dict[str, threading.Event] = {}

#: Which run the current thread is working on. A run is one thread, so this is
#: the honest place for it — passing a run id through the model's tool
#: arguments would let the model claim to be a different run.
_current = threading.local()


def enabled(store=None) -> bool:
    return bool((store or get_store()).get_setting(SETTING))


def model(store=None) -> str:
    return (store or get_store()).get_setting(MODEL_SETTING) or DEFAULT_MODEL


# --------------------------------------------------------------- its tree


def root() -> Path:
    return Path(config.ROOT) / "jarvis"


def agents_dir() -> Path:
    return root() / "agents"


def tools_dir() -> Path:
    return root() / "tools"


def work_dir() -> Path:
    return root() / "work"


def ensure_tree() -> None:
    for path in (agents_dir(), tools_dir(), work_dir()):
        path.mkdir(parents=True, exist_ok=True)


def registry() -> Registry:
    """A registry over Jarvis's own tree, never the operator's.

    Built fresh each time rather than cached: Jarvis writes agents and tools
    mid-run and has to be able to see what it just made.
    """
    ensure_tree()
    reg = Registry(agents_dir=agents_dir(), tools_dir=tools_dir())
    for tool in _builder_tools():
        reg.register_tool(tool)
    return reg


# ------------------------------------------------------------------- runs


def start_run(goal: str, budget_eur, max_steps, who: str) -> str:
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("A run needs something to work on.")
    try:
        budget, steps = float(budget_eur), int(max_steps)
    except (TypeError, ValueError):
        raise ValueError("The budget is an amount in euros and the cap a number of steps.")
    if not 0 < budget <= MAX_BUDGET_EUR:
        raise ValueError(f"The budget has to be between 0 and {MAX_BUDGET_EUR:.0f} euros.")
    if not 0 < steps <= MAX_STEPS:
        raise ValueError(f"The step cap has to be between 1 and {MAX_STEPS}.")

    run_id = new_id("j")
    get_store().execute(
        "INSERT INTO jarvis_runs (id, goal, status, budget_eur, max_steps,"
        " started_by, created_at) VALUES (?,?,?,?,?,?,?)",
        (run_id, goal, "running", budget, steps, who, time.time()),
    )
    with _lock:
        _running[run_id] = threading.Event()
    threading.Thread(target=_supervise, args=(run_id,),
                     name=f"jarvis-{run_id}", daemon=True).start()
    return run_id


def stop_run(run_id: str) -> None:
    """Ask a run to stop. It notices between steps rather than mid-turn — a
    turn already talking to a provider is paid for either way."""
    with _lock:
        event = _running.get(run_id)
    if event:
        event.set()
    get_store().execute(
        "UPDATE jarvis_runs SET status='stopped', note=?, ended_at=?"
        " WHERE id=? AND status='running'",
        ("you stopped it", time.time(), run_id))


def get_run(run_id: str):
    return get_store().one("SELECT * FROM jarvis_runs WHERE id=?", (run_id,))


def runs(limit: int = 50):
    return get_store().query(
        "SELECT * FROM jarvis_runs ORDER BY created_at DESC LIMIT ?", (limit,))


def is_running(run_id: str) -> bool:
    with _lock:
        return run_id in _running


def spend(run_id: str) -> float:
    """What a run has cost, including every session it started underneath.

    Counting only the driver's own session would leave the loop free: it could
    spend the afternoon inside `run_own_agent` against a budget that never
    moved.
    """
    row = get_run(run_id)
    if not row or not row["session_id"]:
        return 0.0
    return float(get_store().one(
        "WITH RECURSIVE tree(id) AS ("
        "  SELECT ? UNION"
        "  SELECT s.id FROM sessions s JOIN tree ON s.parent_session_id = tree.id)"
        " SELECT COALESCE(SUM(amount),0) t FROM ledger"
        " WHERE kind='eur' AND session_id IN (SELECT id FROM tree)",
        (row["session_id"],))["t"])


def made(run_id: str) -> dict:
    """What a run has left behind, so it can be promoted or binned."""
    reg = registry()
    tag = f"run: {run_id}"
    agents = [{"name": n, "description": a.description}
              for n, a in sorted(reg.agents().items())
              if a.raw.get("made_by") == tag]
    tools = [{"name": n, "description": t.description,
              "kind": t.raw.get("type") or ("python" if t.raw.get("sandboxed") else "—"),
              "python": bool(t.raw.get("sandboxed"))}
             for n, t in sorted(reg.tools().items())
             if t.raw.get("made_by") == tag]
    return {"agents": agents, "tools": tools}


def discard(run_id: str) -> dict:
    """Bin a run and everything it made. The whole point of `expendable`."""
    stop_run(run_id)
    what = made(run_id)
    for agent in what["agents"]:
        for suffix in (".yaml", ".yml", ".md"):
            path = agents_dir() / f"{agent['name']}{suffix}"
            if path.exists():
                path.unlink()
    for tool in what["tools"]:
        path = tools_dir() / tool["name"]
        if path.is_dir():
            shutil.rmtree(path)
    get_store().execute(
        "UPDATE jarvis_runs SET status='discarded', note=?, ended_at=? WHERE id=?",
        (f"discarded, with {len(what['agents'])} agent(s) and "
         f"{len(what['tools'])} tool(s)", time.time(), run_id))
    return what


def promote(kind: str, name: str) -> str:
    """Move one thing Jarvis made into the operator's estate.

    The only door between the two trees, and a person is the only thing that
    opens it. It refuses to write over something already there: promoting must
    not become a way to replace `support.yaml` by choosing its name.
    """
    if kind == "agent":
        source = agents_dir() / f"{name}.yaml"
        if not source.is_file():
            raise ValueError(f"'{name}' is not one of Jarvis's agents.")
        target = Path(config.AGENTS_DIR) / f"{name}.yaml"
        if target.exists() or target.with_suffix(".yml").exists():
            raise ValueError(
                f"You already have an agent called '{name}'. Rename Jarvis's "
                "copy first — promoting never overwrites your own.")
        target.parent.mkdir(parents=True, exist_ok=True)
        data = yamlio.load(source.read_text(encoding="utf-8"))
        data.pop("made_by", None)
        target.write_text(yamlio.dump(data), encoding="utf-8")
        notes = source.with_suffix(".md")
        if notes.is_file():
            shutil.copy2(notes, target.with_suffix(".md"))
        return str(target)

    if kind == "tool":
        source = tools_dir() / name
        if not (source / "tool.yaml").is_file():
            raise ValueError(f"'{name}' is not one of Jarvis's tools.")
        target = Path(config.TOOLS_DIR) / name
        if target.exists():
            raise ValueError(
                f"You already have a tool called '{name}'. Rename Jarvis's "
                "copy first — promoting never overwrites your own.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        manifest = target / "tool.yaml"
        data = yamlio.load(manifest.read_text(encoding="utf-8"))
        # `made_by` goes; `sandboxed` deliberately stays. Promoting says a
        # person read the code and wants it — not that the code stopped being
        # something a model wrote, which is the reason it runs in a child
        # process at all. Delete that line by hand if you truly want it in here.
        data.pop("made_by", None)
        manifest.write_text(yamlio.dump(data), encoding="utf-8")
        return str(target)

    raise ValueError("Promote an 'agent' or a 'tool'.")


# --------------------------------------------------------------- the loop


DRIVER_INSTRUCTIONS = """\
You are Jarvis. You build things for the person running this Heddled: small
agents, and the tools those agents need, in a workspace of your own.

How to work:

1. Call `list_agents` first, to see what you already have and what the operator
   has that you may ask.
2. Build tools with `make_tool`. Prefer the no-code kinds — an `http` call, a
   `lookup` table, a `fixed` answer, a `template`, a `webhook` — because they
   are data, need no code, and anyone can read them. Write Python only when
   none of them fits.
3. Build agents with `make_agent`, mounting the tools they need by name.
4. Try what you built with `run_own_agent`, read what came back, and fix what
   does not work. Building something and never running it is not finishing.
5. Use `ask_agent` when one of the operator's own agents already knows
   something. You cannot change them, only ask, and one may stop for a person's
   approval — if it does, work around it.
6. Keep notes in your workspace files if the run is a long one.
7. When the goal is met, reply with DONE on the first line, then a short plain
   account of what you built and how to try it.

You have a budget and a step cap and you cannot see either running down, so do
the useful thing early rather than exploring. Everything you make stays in your
own tree until a person promotes it, so build for somebody who will read it
before they trust it. If you need something outside your reach, say what and
why rather than trying to get at it.
"""

CARRY_ON = ("Carry on. If the goal is met, reply with DONE on the first line "
            "and what you built. If you are stuck, say so and why.")


def _write_driver() -> None:
    """The driver is written to disk like anything else, so the registry
    resolves it by the ordinary path and its file can be read."""
    ensure_tree()
    (agents_dir() / f"{DRIVER}.md").write_text(DRIVER_INSTRUCTIONS, encoding="utf-8")
    (agents_dir() / f"{DRIVER}.yaml").write_text(yamlio.dump({
        "name": DRIVER,
        "description": "Builds agents and tools of its own.",
        "model": model(),
        "instructions": f"./{DRIVER}.md",
        "adapters": {"channels": [CHANNEL], "tools": list(BUILDERS)},
        # Its own corner of its own tree. `workspace` is the one field that
        # decides which directory the file tools reach, so it is set here and
        # nowhere else — no Jarvis agent gets to choose one.
        "workspace": str(work_dir()),
    }), encoding="utf-8")


def _supervise(run_id: str) -> None:
    """One run: turns until the goal is met, the money runs out, the steps run
    out, or somebody stops it. Each step is an ordinary turn on the ordinary
    spine, which is what makes a run readable in Activity like anything else.
    """
    from .engine import TurnEngine

    store = get_store()
    row = get_run(run_id)
    if not row:
        return
    with _lock:
        stopping = _running.get(run_id) or threading.Event()

    try:
        _write_driver()
        reg = registry()
        agent = reg.get_agent(DRIVER)
        session_id = store.create_session(
            agent=DRIVER, agent_version=agent.version, channel=CHANNEL,
            title=row["goal"][:80],
            trigger_origin={"kind": CHANNEL, "run": run_id,
                            "who": row["started_by"]})
        store.execute("UPDATE jarvis_runs SET session_id=? WHERE id=?",
                      (session_id, run_id))
        _current.run_id, _current.session_id = run_id, session_id

        message = f"Your goal: {row['goal']}\n\nBegin."
        for step in range(1, int(row["max_steps"]) + 1):
            if stopping.is_set():
                return _finish(run_id, "stopped", "you stopped it")
            if spend(run_id) >= float(row["budget_eur"]):
                return _finish(run_id, "spent",
                               f"the €{row['budget_eur']:.2f} budget is used up")

            engine = TurnEngine(store, agent, session_id, new_id("t"),
                                channel=CHANNEL, registry=reg)
            result = engine.run(message, origin={"kind": CHANNEL, "run": run_id})
            store.execute("UPDATE jarvis_runs SET steps=? WHERE id=?", (step, run_id))

            if result.status == "waiting-approval":
                return _finish(run_id, "waiting",
                               "something it did needs your approval")
            if result.status == "error":
                return _finish(run_id, "failed", result.error or "a turn failed")

            reply = (result.reply or "").strip()
            if reply.upper().startswith("DONE"):
                return _finish(run_id, "done", reply[:1000])

            # Reloaded every step: it has been writing agents and tools, and a
            # registry from before those files existed cannot see them.
            reg = registry()
            agent = reg.get_agent(DRIVER) or agent
            message = CARRY_ON
        _finish(run_id, "spent", f"it used all {row['max_steps']} steps")
    except Exception as exc:                                    # noqa: BLE001
        _finish(run_id, "failed", f"{type(exc).__name__}: {exc}")
    finally:
        _current.run_id = _current.session_id = None
        with _lock:
            _running.pop(run_id, None)


def _finish(run_id: str, status: str, note: str) -> None:
    get_store().execute(
        "UPDATE jarvis_runs SET status=?, note=?, ended_at=? WHERE id=?",
        (status, note, time.time(), run_id))


# -------------------------------------------------------------- its tools


def _slug(given, what: str) -> str:
    name = str(given or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not name or not name.replace("_", "").isalnum():
        raise ValueError(
            f"'{given}' is not a usable {what} name — lowercase letters, "
            "digits and underscores.")
    return name


def _tag() -> str:
    return f"run: {getattr(_current, 'run_id', None) or 'unknown'}"


def _make_agent(args, ctx) -> dict:
    name = _slug(args.get("name"), "agent")
    if name == DRIVER:
        raise ValueError("That is your own name. Pick another.")
    ensure_tree()
    known = set(registry().tools())
    wanted = [str(t) for t in (args.get("tools") or [])]
    missing = [t for t in wanted if t not in known]
    if missing:
        raise ValueError(
            f"You have no tool called {', '.join(missing)}. Make it with "
            "make_tool first, or leave it out.")

    (agents_dir() / f"{name}.md").write_text(
        str(args.get("instructions") or ""), encoding="utf-8")
    # Written field by field rather than from anything the model hands over
    # whole: `workspace`, `policies` and `triggers` are not its to set, and the
    # way to keep them out is not to have a path for them.
    (agents_dir() / f"{name}.yaml").write_text(yamlio.dump({
        "name": name,
        "description": str(args.get("description") or ""),
        "model": model(),
        "instructions": f"./{name}.md",
        "adapters": {"channels": [CHANNEL], "tools": wanted},
        "made_by": _tag(),
    }), encoding="utf-8")
    ctx.log(f"made the agent {name}")
    return {"name": name, "made": True}


def _make_tool(args, ctx) -> dict:
    name = _slug(args.get("name"), "tool")
    if name in BUILDERS:
        raise ValueError(f"'{name}' is one of your own tools. Pick another name.")
    ensure_tree()
    folder = tools_dir() / name
    folder.mkdir(parents=True, exist_ok=True)

    manifest = {
        "name": name,
        "description": str(args.get("description") or ""),
        "input": dict(args.get("input") or {"text": "string"}),
        "made_by": _tag(),
    }
    code = args.get("code")
    if code:
        # Python it wrote itself, so it runs as a child process rather than in
        # here, where the store and every provider key live. The flag is what
        # routes it there, and it survives promotion.
        (folder / "handler.py").write_text(str(code), encoding="utf-8")
        manifest["handler"] = "./handler.py"
        manifest["sandboxed"] = True
        ctx.log(f"made the tool {name} — Python, run in a sandbox")
    else:
        kind = str(args.get("kind") or "fixed")
        if kind in ("python", "email"):
            raise ValueError(
                "Give `code` for Python. Email goes out through the operator's "
                "own credentials and is not yours to send.")
        manifest["type"] = kind
        manifest["config"] = dict(args.get("config") or {})
        ctx.log(f"made the tool {name} — {kind}")
    (folder / "tool.yaml").write_text(yamlio.dump(manifest), encoding="utf-8")
    return {"name": name, "made": True, "python": bool(code)}


def _list_agents(args, ctx) -> dict:
    from .registry import get_registry

    def lines(agents, skip=()):
        return "\n".join(f"{n} — {a.description or 'no description'}"
                         for n, a in sorted(agents.items()) if n not in skip)

    reg = registry()
    return {
        "yours": lines(reg.agents(), skip=(DRIVER,)) or "none yet",
        "your_tools": ", ".join(sorted(set(reg.tools()) - set(BUILDERS))) or "none yet",
        "theirs": lines(get_registry().agents()) or "none",
    }


def _run_own_agent(args, ctx) -> dict:
    """Try one of its own, in its own namespace."""
    from .engine import TurnEngine

    name = str(args.get("name") or "")
    reg = registry()
    agent = reg.get_agent(name)
    if not agent or name == DRIVER:
        raise ValueError(f"'{name}' is not one of the agents you made.")
    store = get_store()
    sid = store.create_session(
        agent=name, agent_version=agent.version, channel=CHANNEL,
        parent_session_id=getattr(_current, "session_id", None),
        trigger_origin={"kind": CHANNEL, "by": DRIVER})
    result = TurnEngine(store, agent, sid, new_id("t"), channel=CHANNEL,
                        registry=reg).run(str(args.get("message") or ""))
    ctx.log(f"ran {name}")
    return {"reply": result.reply or f"({result.status})", "status": result.status}


def _ask_agent(args, ctx) -> dict:
    """Ask one of the operator's agents. Their rules still apply."""
    from . import runtime

    name = str(args.get("name") or "")
    try:
        result = runtime.submit_message(
            name, str(args.get("message") or ""), channel=CHANNEL,
            origin={"kind": CHANNEL, "run": getattr(_current, "run_id", None)},
            caller=DRIVER, parent_session_id=getattr(_current, "session_id", None),
            sync=True, timeout_s=120)
    except LookupError:
        raise ValueError(f"The operator has no agent called '{name}'.")
    ctx.log(f"asked {name}")
    if result.get("status") == "waiting-approval":
        return {"reply": "That agent has stopped and is waiting for a person to "
                         "approve something, so it has not answered. Do not wait "
                         "for it."}
    return {"reply": result.get("reply") or f"({result.get('status')})"}


#: name → (what it does, what it takes, what it gives back, how it does it)
BUILDERS = {
    "list_agents": (
        "See what exists: the agents and tools you have made, and the "
        "operator's own agents, which you may ask but never change.",
        {"type": "object", "properties": {}},
        {"yours": "string", "your_tools": "string", "theirs": "string"},
        _list_agents),
    "make_tool": (
        "Make a tool of your own. Prefer a no-code kind — http, lookup, fixed, "
        "template or webhook — which is data and needs no code at all. Give "
        "`code` only when none of them fits: it runs in a locked-down child "
        "process with no keys, no store and no way back into Heddled.",
        {"type": "object", "properties": {
            "name": {"type": "string",
                     "description": "Lowercase with underscores, e.g. postcode_lookup."},
            "description": {"type": "string",
                            "description": "What it does, as the agent using it will read it."},
            "kind": {"type": "string",
                     "description": "http, lookup, fixed, template or webhook."},
            "config": {"type": "object",
                       "description": "Settings for that kind — url and method for "
                                      "http, the table for lookup, and so on."},
            "input": {"type": "object",
                      "description": 'What it takes, as name: type, e.g. '
                                     '{"postcode": "string"}.'},
            "code": {"type": "string",
                     "description": "Python defining handle(args, ctx). Last resort."},
        }, "required": ["name", "description"]},
        {"name": "string", "made": "boolean", "python": "boolean"},
        _make_tool),
    "make_agent": (
        "Make an agent of your own, mounting tools you have already made. It "
        "exists only in your tree until a person promotes it.",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "Lowercase with underscores."},
            "description": {"type": "string", "description": "What it is for, in one line."},
            "instructions": {"type": "string",
                             "description": "What it should do, addressed to it, in plain words."},
            "tools": {"type": "array", "items": {"type": "string"},
                      "description": "Names of tools you have made."},
        }, "required": ["name", "description", "instructions"]},
        {"name": "string", "made": "boolean"},
        _make_agent),
    "run_own_agent": (
        "Try one of the agents you made: send it a message and read the reply. "
        "Do this before you say you are finished.",
        {"type": "object", "properties": {
            "name": {"type": "string"},
            "message": {"type": "string", "description": "What to send it."},
        }, "required": ["name", "message"]},
        {"reply": "string", "status": "string"},
        _run_own_agent),
    "ask_agent": (
        "Ask one of the operator's own agents something. You cannot change "
        "them, and their own rules still apply — one may stop for approval "
        "rather than answer.",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "As list_agents gives it."},
            "message": {"type": "string"},
        }, "required": ["name", "message"]},
        {"reply": "string"},
        _ask_agent),
}


def _builder_tools() -> list[Tool]:
    return [
        Tool(name=name, description=description, input_schema=schema,
             output_schema=normalize_schema(output), handler_path=None,
             dir=root(), raw={"jarvis": name}, source="jarvis")
        for name, (description, schema, output, _fn) in BUILDERS.items()
    ]


def make_builder_handler(name: str):
    """Called by `Tool.load_handler` for `source: jarvis`."""
    try:
        fn = BUILDERS[name][3]
    except KeyError:
        raise ValueError(f"'{name}' is not one of Jarvis's own tools")

    def handle(args, ctx):
        return fn(args or {}, ctx)

    return handle
