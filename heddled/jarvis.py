"""The part that builds itself, kept somewhere it can only hurt itself.

Jarvis is a drift from what the rest of Heddled is for, and it is fenced
accordingly. Three rules carry the whole design:

**It writes only into its own tree.** `jarvis/agents`, `jarvis/tools`,
`jarvis/memory`, `jarvis/work`. The operator's `agents/` and `tools/` are not
merely policy-protected from it — they are a different directory, reached
through a different `Registry`, and nothing here holds a path to them. The rule
stands untouched: an agent that could rewrite `agents/support.yaml` could delete
the approval gate on `refund`, and no setting anywhere turns that off.

**It may reach the operator's agents, but only to ask them.** `ask_agent` runs
one and returns its reply. Governance travels with the agent exactly as it does
for an MCP caller: if `support` gates `refund` behind approval, a question from
Jarvis stops for a person like any other. Reading and invoking, never writing.
Every such turn arrives on the `jarvis` channel, so a policy can say
`deny_channels: [jarvis]` and mean it.

**Everything it makes is inert until a person moves it.** A Jarvis agent runs
only inside a Jarvis conversation. `promote` is the one door between the two
trees, it refuses to overwrite anything already there, and Python it wrote stays
sandboxed on the far side.

The unit is a **conversation**: you talk to it, it builds, you watch what it
built appear beside the thread. There is no autonomous loop with a step cap,
because you are the step cap — it stops and talks to you. What it cannot spend
past is the conversation's budget.

It also keeps notes. `jarvis/memory/*.md`, one file per thing worth knowing,
with the one-line summaries carried into every conversation and the bodies read
only when they matter. That is what makes the second conversation cheaper than
the first, and it is plain Markdown you can read and delete.
"""

from __future__ import annotations

import re
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
BUDGET_SETTING = "jarvis_budget_eur"
DEFAULT_MODEL = "mock/echo"
DEFAULT_BUDGET_EUR = 5.0
MAX_BUDGET_EUR = 100.0

#: The channel every Jarvis-driven turn arrives on, including questions put to
#: the operator's own agents. Nameable in a policy: `deny_channels: [jarvis]`.
CHANNEL = "jarvis"

#: A conversation that has run this long is one to start again rather than
#: continue. Not the real rail — the budget is — but a loop that somehow drives
#: itself should still hit a wall.
MAX_TURNS = 200

#: The agent you are talking to. It is the only one with the builder tools: an
#: agent Jarvis makes is an ordinary agent and does not get to make more of
#: itself.
DRIVER = "jarvis"

#: Provenance, kept rather than scrubbed. It survives promotion — an agent in
#: your own tree that a model wrote is a thing you want to be able to tell.
MADE_BY = "made_by"

_current = threading.local()


def enabled(store=None) -> bool:
    return bool((store or get_store()).get_setting(SETTING))


def model(store=None) -> str:
    return (store or get_store()).get_setting(MODEL_SETTING) or DEFAULT_MODEL


def default_budget(store=None) -> float:
    try:
        return float((store or get_store()).get_setting(BUDGET_SETTING)
                     or DEFAULT_BUDGET_EUR)
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_EUR


# --------------------------------------------------------------- its tree


def root() -> Path:
    return Path(config.ROOT) / "jarvis"


def agents_dir() -> Path:
    return root() / "agents"


def tools_dir() -> Path:
    return root() / "tools"


def memory_dir() -> Path:
    return root() / "memory"


def work_dir() -> Path:
    return root() / "work"


def ensure_tree() -> None:
    for path in (agents_dir(), tools_dir(), memory_dir(), work_dir()):
        path.mkdir(parents=True, exist_ok=True)


def registry() -> Registry:
    """A registry over Jarvis's own tree, never the operator's.

    Built fresh each time rather than cached: Jarvis writes agents and tools
    mid-conversation and has to be able to see what it just made.
    """
    ensure_tree()
    reg = Registry(agents_dir=agents_dir(), tools_dir=tools_dir())
    for tool in _builder_tools():
        reg.register_tool(tool)
    return reg


# ------------------------------------------------------------------ memory


FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.S)


def _memory_path(name: str) -> Path:
    return memory_dir() / f"{_slug(name, 'memory')}.md"


def remember(name: str, description: str, note: str) -> dict:
    """One fact, one file. Markdown with a little front matter, because the
    summary has to be readable without loading the body."""
    ensure_tree()
    slug = _slug(name, "memory")
    body = (note or "").strip()
    if not body:
        raise ValueError("A memory needs something in it.")
    summary = " ".join((description or "").split()) or body.splitlines()[0][:120]
    _memory_path(slug).write_text(
        f"---\nname: {slug}\ndescription: {summary}\n---\n\n{body}\n",
        encoding="utf-8")
    return {"name": slug, "description": summary}


def recall(name: str) -> str:
    path = _memory_path(name)
    if not path.is_file():
        raise ValueError(f"There is no memory called '{name}'.")
    text = path.read_text(encoding="utf-8")
    return FRONT_MATTER.sub("", text, count=1).strip()


def forget(name: str) -> bool:
    path = _memory_path(name)
    if not path.is_file():
        return False
    path.unlink()
    return True


def memories() -> list[dict]:
    """Every note, newest first, with its summary and its body.

    Read from the files themselves rather than from a separate index: an index
    kept alongside the thing it indexes is an index that drifts.
    """
    ensure_tree()
    out = []
    for path in memory_dir().glob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        head = FRONT_MATTER.match(text)
        fields = {}
        if head:
            for line in head.group(1).splitlines():
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
        body = FRONT_MATTER.sub("", text, count=1).strip()
        out.append({
            "name": fields.get("name") or path.stem,
            "description": fields.get("description") or body.splitlines()[0][:120],
            "body": body,
            "changed_at": path.stat().st_mtime,
        })
    return sorted(out, key=lambda m: m["changed_at"], reverse=True)


def memory_index() -> str:
    """What goes into every conversation: the summaries, never the bodies.

    A dozen notes read in full would be most of the context window before you
    have said anything. A dozen one-liners is nothing, and it can pull the one
    it needs with `recall`.
    """
    notes = memories()
    if not notes:
        return "You have no notes yet."
    return "\n".join(f"- **{m['name']}** — {m['description']}" for m in notes)


# ---------------------------------------------------------- conversations
#
# Stored in `jarvis_runs`, which is what the table was called when the unit was
# a run. `goal` is the conversation's subject, `steps` the turns it has taken,
# `max_steps` the wall a runaway conversation hits.


def start_chat(title: str, who: str, budget_eur=None) -> str:
    budget = float(budget_eur if budget_eur is not None else default_budget())
    if not 0 < budget <= MAX_BUDGET_EUR:
        raise ValueError(f"The budget has to be between 0 and {MAX_BUDGET_EUR:.0f} euros.")
    chat_id = new_id("j")
    session_id = get_store().create_session(
        agent=DRIVER, channel=CHANNEL, title=(title or "")[:80],
        trigger_origin={"kind": CHANNEL, "chat": chat_id, "who": who})
    get_store().execute(
        "INSERT INTO jarvis_runs (id, goal, status, budget_eur, max_steps,"
        " session_id, started_by, created_at) VALUES (?,?,'open',?,?,?,?,?)",
        (chat_id, (title or "New conversation")[:200], budget, MAX_TURNS,
         session_id, who, time.time()))
    return chat_id


def get_chat(chat_id: str):
    return get_store().one("SELECT * FROM jarvis_runs WHERE id=?", (chat_id,))


def chat_for_session(session_id: str):
    return get_store().one(
        "SELECT * FROM jarvis_runs WHERE session_id=?", (session_id,))


def chats(limit: int = 60):
    return get_store().query(
        "SELECT * FROM jarvis_runs ORDER BY created_at DESC LIMIT ?", (limit,))


def record_turn(chat_id: str) -> None:
    get_store().execute(
        "UPDATE jarvis_runs SET steps = steps + 1 WHERE id=?", (chat_id,))


def top_up(chat_id: str, extra_eur) -> float:
    """More money for this conversation. Deliberately a gesture rather than a
    setting: the point of the cap is that continuing is a decision."""
    row = get_chat(chat_id)
    if not row:
        raise ValueError("That conversation is gone.")
    try:
        extra = float(extra_eur)
    except (TypeError, ValueError):
        raise ValueError("Say how much to add, in euros.")
    total = float(row["budget_eur"]) + extra
    if not 0 < extra or total > MAX_BUDGET_EUR:
        raise ValueError(
            f"Top up by something above zero, and no more than "
            f"€{MAX_BUDGET_EUR:.0f} on one conversation.")
    get_store().execute("UPDATE jarvis_runs SET budget_eur=? WHERE id=?",
                        (total, chat_id))
    return total


def spend(chat_id: str) -> float:
    """What a conversation has cost, including every session it started
    underneath. Counting only its own would leave it free to spend the
    afternoon inside `run_own_agent` against a budget that never moved."""
    row = get_chat(chat_id)
    if not row or not row["session_id"]:
        return 0.0
    return float(get_store().one(
        "WITH RECURSIVE tree(id) AS ("
        "  SELECT ? UNION"
        "  SELECT s.id FROM sessions s JOIN tree ON s.parent_session_id = tree.id)"
        " SELECT COALESCE(SUM(amount),0) t FROM ledger"
        " WHERE kind='eur' AND session_id IN (SELECT id FROM tree)",
        (row["session_id"],))["t"])


def budget_state(chat_id: str) -> dict:
    row = get_chat(chat_id)
    if not row:
        return {"spent": 0.0, "budget": 0.0, "left": 0.0, "spent_up": True}
    spent, budget = spend(chat_id), float(row["budget_eur"])
    return {"spent": spent, "budget": budget, "left": max(0.0, budget - spent),
            "spent_up": spent >= budget,
            "pct": min(100, round(spent / budget * 100)) if budget else 0}


# ------------------------------------------------------------- inventory


def _tag(chat_id: str = None) -> str:
    return f"chat: {chat_id or getattr(_current, 'chat_id', None) or 'unknown'}"


def inventory() -> dict:
    """Everything Jarvis has, for the panel beside the conversation.

    Not scoped to one conversation: what it built last week is still what it
    has, and a panel that forgot it would send it building a second one.
    """
    reg = registry()
    agents = [{"name": n, "description": a.description,
               "made_in": (a.raw.get(MADE_BY) or "").replace("chat: ", "")}
              for n, a in sorted(reg.agents().items()) if n != DRIVER]
    tools = [{"name": n, "description": t.description,
              "kind": t.raw.get("type") or ("python" if t.raw.get("sandboxed") else "—"),
              "python": bool(t.raw.get("sandboxed")),
              "made_in": (t.raw.get(MADE_BY) or "").replace("chat: ", "")}
             for n, t in sorted(reg.tools().items()) if n not in BUILDERS]
    return {"agents": agents, "tools": tools, "memories": memories(),
            "schedules": schedules()}


def schedules() -> list[dict]:
    """Schedules on things that came from Jarvis and were promoted.

    Jarvis cannot write one — a trigger means running with nobody there, and
    that stays your decision, made on the agent's own page after you have read
    what it does. This lists the ones you decided on, so the panel shows what is
    actually firing rather than implying Jarvis arranged it.
    """
    from . import story
    from .registry import get_registry

    out = []
    for name, agent in sorted(get_registry().agents().items()):
        if not str(agent.raw.get(MADE_BY) or "").startswith("jarvis"):
            continue
        for trigger in agent.triggers or []:
            words = (story.cron_words(trigger.raw.get("schedule"))
                     if trigger.kind == "schedule"
                     else f"when something lands in {trigger.raw.get('poll')}")
            out.append({"agent": name, "kind": trigger.kind, "words": words})
    return out


def made(chat_id: str) -> dict:
    """What one conversation left behind, so it can be binned as a unit."""
    have = inventory()
    return {"agents": [a for a in have["agents"] if a["made_in"] == chat_id],
            "tools": [t for t in have["tools"] if t["made_in"] == chat_id]}


def remove(kind: str, name: str) -> None:
    """Delete one thing out of Jarvis's tree. Nothing here is precious — that
    is the point of it being over there."""
    if kind == "agent":
        for suffix in (".yaml", ".yml", ".md"):
            path = agents_dir() / f"{name}{suffix}"
            if path.exists():
                path.unlink()
    elif kind == "tool":
        path = tools_dir() / name
        if path.is_dir():
            shutil.rmtree(path)
    elif kind == "memory":
        forget(name)
    else:
        raise ValueError("Remove an 'agent', a 'tool' or a 'memory'.")


def discard(chat_id: str) -> dict:
    """Bin a conversation and everything it made that you did not promote."""
    what = made(chat_id)
    for agent in what["agents"]:
        remove("agent", agent["name"])
    for tool in what["tools"]:
        remove("tool", tool["name"])
    get_store().execute(
        "UPDATE jarvis_runs SET status='discarded', note=?, ended_at=? WHERE id=?",
        (f"discarded, with {len(what['agents'])} agent(s) and "
         f"{len(what['tools'])} tool(s)", time.time(), chat_id))
    return what


def promote(kind: str, name: str) -> str:
    """Move one thing Jarvis made into the operator's estate.

    The only door between the two trees, and a person is the only thing that
    opens it. It refuses to write over something already there: promoting must
    not become a way to replace `support.yaml` by choosing its name.
    """
    stamp = f"jarvis, promoted {time.strftime('%Y-%m-%d')}"

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
        # Provenance is kept, not scrubbed: an agent of yours that a model wrote
        # is exactly the thing you want to be able to tell later. It is also
        # what lets the panel show which schedules you put on its work.
        data[MADE_BY] = stamp
        # It arrives on webchat, because a promoted agent is one of yours and
        # `jarvis` is not a channel your console offers.
        data.setdefault("adapters", {})["channels"] = ["webchat"]
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
        # `sandboxed` deliberately stays. Promoting says a person read the code
        # and wants it — not that the code stopped being something a model
        # wrote, which is the reason it runs in a child process at all.
        data[MADE_BY] = stamp
        manifest.write_text(yamlio.dump(data), encoding="utf-8")
        return str(target)

    raise ValueError("Promote an 'agent' or a 'tool'.")


# --------------------------------------------------------------- the agent


INSTRUCTIONS = """\
You are Jarvis. You build things for the person you are talking to: small
agents, and the tools those agents need, in a tree of your own.

You are in a conversation, not on a leash — so work in steps and say what you
are doing. Build something, try it, tell them what happened, and ask when a
choice is theirs to make rather than guessing. They can see everything you
build in a panel beside this conversation, so you do not need to recite it.

How to work:

1. Check `list_agents` before making anything, and read a note with `recall`
   when the index below suggests one is relevant.
2. Build tools with `make_tool`. Prefer the no-code kinds — an `http` call, a
   `lookup` table, a `fixed` answer, a `template`, a `webhook` — because they
   are data, need no code, and anyone can read them. Write Python only when
   none of them fits.
3. Build agents with `make_agent`, mounting the tools they need by name.
4. Try what you built with `run_own_agent` and fix what does not work. Building
   something and never running it is not finishing.
5. Use `ask_agent` when one of their own agents already knows something. You
   cannot change those, only ask, and one may stop for an approval.
6. Use `remember` when you learn something worth having next time — how their
   systems are shaped, what they prefer, what turned out to be a dead end. Not
   a diary: the conversation is already recorded. One fact per note.

You cannot reach anything outside your own tree, and nothing you build is part
of their Heddled until they promote it — so build for somebody who will read it
before they trust it. If you need something out of reach, say what and why.

## What you remember

{memory}
"""


def _instructions() -> str:
    return INSTRUCTIONS.format(memory=memory_index())


def write_driver() -> None:
    """Write the agent you are talking to, instructions and all.

    Called before every turn, because the memory index is part of the
    instructions and it changes as it learns. Written only when something
    actually differs — an identical rewrite would give the agent a new version
    on every message and make the session history unreadable.
    """
    ensure_tree()
    notes, definition = agents_dir() / f"{DRIVER}.md", agents_dir() / f"{DRIVER}.yaml"
    wanted = _instructions()
    if not notes.is_file() or notes.read_text(encoding="utf-8") != wanted:
        notes.write_text(wanted, encoding="utf-8")
    spec = yamlio.dump({
        "name": DRIVER,
        "description": "Builds agents and tools of its own.",
        "model": model(),
        "instructions": f"./{DRIVER}.md",
        "adapters": {"channels": [CHANNEL], "tools": list(BUILDERS)},
        # Its own corner of its own tree. `workspace` is the one field that
        # decides which directory the file tools reach, so it is set here and
        # nowhere else — no Jarvis agent gets to choose one.
        "workspace": str(work_dir()),
    })
    if not definition.is_file() or definition.read_text(encoding="utf-8") != spec:
        definition.write_text(spec, encoding="utf-8")


def set_current_chat(chat_id: str) -> None:
    """Which conversation the turn on this thread belongs to, so what it builds
    can be stamped with it. Not a tool argument — a model that could pass its
    own chat id could claim to be a different conversation."""
    _current.chat_id = chat_id


# -------------------------------------------------------------- its tools


def _slug(given, what: str) -> str:
    name = str(given or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not name or not name.replace("_", "").isalnum():
        raise ValueError(
            f"'{given}' is not a usable {what} name — lowercase letters, "
            "digits and underscores.")
    return name


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
    # way to keep them out is not to have a path for them. A trigger in
    # particular means running with nobody there, which stays a person's call.
    (agents_dir() / f"{name}.yaml").write_text(yamlio.dump({
        "name": name,
        "description": str(args.get("description") or ""),
        "model": model(),
        "instructions": f"./{name}.md",
        "adapters": {"channels": [CHANNEL], "tools": wanted},
        MADE_BY: _tag(),
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
        MADE_BY: _tag(),
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
        parent_session_id=ctx.session_id,
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
            origin={"kind": CHANNEL, "asked_by": DRIVER}, caller=DRIVER,
            parent_session_id=ctx.session_id, sync=True, timeout_s=120)
    except LookupError:
        raise ValueError(f"The operator has no agent called '{name}'.")
    ctx.log(f"asked {name}")
    if result.get("status") == "waiting-approval":
        return {"reply": "That agent has stopped and is waiting for a person to "
                         "approve something, so it has not answered. Do not wait "
                         "for it."}
    return {"reply": result.get("reply") or f"({result.get('status')})"}


def _remember(args, ctx) -> dict:
    saved = remember(args.get("name"), args.get("description"), args.get("note"))
    ctx.log(f"noted {saved['name']}")
    return {**saved, "saved": True}


def _recall(args, ctx) -> dict:
    name = str(args.get("name") or "")
    ctx.log(f"read the note {name}")
    return {"note": recall(name)}


def _forget(args, ctx) -> dict:
    name = str(args.get("name") or "")
    gone = forget(name)
    ctx.log(f"forgot {name}" if gone else f"no note called {name}")
    return {"forgotten": gone}


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
        "Do this before you say it works.",
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
    "remember": (
        "Keep a note for next time — one fact per note, written so it still "
        "makes sense in a month. Writing a note that already exists replaces "
        "it, which is how you correct one.",
        {"type": "object", "properties": {
            "name": {"type": "string",
                     "description": "Lowercase with underscores, e.g. invoice_api_shape."},
            "description": {"type": "string",
                            "description": "One line. This is what you see in every "
                                           "conversation, so make it say whether the "
                                           "note is worth opening."},
            "note": {"type": "string", "description": "The fact itself, in Markdown."},
        }, "required": ["name", "description", "note"]},
        {"name": "string", "saved": "boolean"},
        _remember),
    "recall": (
        "Read one of your notes in full, by name, when its line in the index "
        "looks relevant to what you are doing.",
        {"type": "object", "properties": {"name": {"type": "string"}},
         "required": ["name"]},
        {"note": "string"},
        _recall),
    "forget": (
        "Delete a note that turned out to be wrong or is no longer true. "
        "Better than leaving it: a wrong note is worse than no note.",
        {"type": "object", "properties": {"name": {"type": "string"}},
         "required": ["name"]},
        {"forgotten": "boolean"},
        _forget),
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
