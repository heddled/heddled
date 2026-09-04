"""Runtime orchestration — the API every surface goes through.

The HTTP layer, the CLI, the worker and the MCP endpoint all call these
functions; none of them touches the engine directly. That's what keeps a
YAML-defined agent, a Python-defined agent and an externally-driven agent the
same object to the platform.
"""

from __future__ import annotations

import hmac
import importlib
import importlib.util
import json
import sys
import time
from typing import Any, Optional

from . import config
from .adapters import channel_names
from .engine import COMPLETED, FAILED, PAUSED, TurnEngine, TurnResult
from .events import (
    APPROVAL_RESOLVED,
    OPERATOR_INJECTED,
    TRIGGER_FIRED,
    Event,
    new_id,
)
from .registry import build_agent, get_registry
from .store import get_store


class AgentNotFound(LookupError):
    pass


def registry_for(channel: str = None):
    """Which registry a turn on this channel resolves its agents and tools in.

    Almost always the operator's. The exception is Jarvis, which works in a tree
    of its own: a turn on its channel must find its agents there and must never
    find the operator's, or the separation the whole feature rests on would be
    one shared lookup away from nothing.

    Gated on the setting as well as the channel, so an instance with Jarvis
    switched off cannot reach that tree by any route at all.
    """
    if channel == "jarvis":
        from . import jarvis

        if jarvis.enabled():
            return jarvis.registry()
    return get_registry()


def _agent_or_raise(name: str, env: str = None, channel: str = None):
    agent = resolve_agent(name, env, channel)
    if not agent:
        raise AgentNotFound(f"no agent named '{name}' in {config.AGENTS_DIR}")
    return agent


def inbound_env(asked: str = None) -> str:
    """Which environment work arriving from outside belongs to.

    The caller decides if it says so; otherwise the instance's default, which a
    setting can override so it can be changed without a restart.
    """
    if asked:
        return asked
    try:
        stored = get_store().get_setting("default_env")
    except Exception:
        stored = None
    env = (stored or config.DEFAULT_ENV or "dev").strip()
    return env if env in config.ENVIRONMENTS else "dev"


def resolve_agent(name: str, env: str = None, channel: str = None):
    """The definition a run in this environment should use.

    `dev` is the working file — editing an agent and immediately trying it is
    the whole point of dev. Every other environment runs the version that was
    published to it, which is what makes publishing mean something: editing an
    agent no longer silently changes what production is running.
    """
    live = registry_for(channel).get_agent(name)
    if not env or env == "dev":
        return live
    if channel == "jarvis":
        # Jarvis's agents are never published anywhere, so there is no version
        # to pin and nothing to look up. The file is the definition.
        return live

    store = get_store()
    pin = store.deployment(name, env)
    if not pin:
        return live
    if live and live.version == pin["version"]:
        return live
    snapshot = store.agent_version(name, pin["version"])
    if not snapshot:
        # Published before versions were kept, or the row was pruned. The file
        # is the only definition there is; running it beats not running.
        return live
    return build_agent(snapshot["definition"], snapshot["instructions"],
                       path=live.path if live else None, pinned_for=env)


# --------------------------------------------------------------- start a turn


def start_session(agent_name: str, channel: str = "webchat", origin: dict = None,
                  env: str = "dev", parent_session_id: str = None,
                  call_chain: list = None) -> str:
    agent = _agent_or_raise(agent_name, env, channel)
    store = get_store()
    if channel != "jarvis":
        # Versions back the Publish screen, which is about the operator's
        # estate. A Jarvis agent is never published anywhere — it is promoted
        # or it is deleted — so recording one there would put a thing nobody
        # can deploy into the list of things you deploy.
        store.record_agent_version(agent)
    return store.create_session(
        agent=agent.name,
        agent_version=agent.version,
        channel=channel,
        trigger_origin=origin or {"kind": channel},
        env=env,
        parent_session_id=parent_session_id,
        call_chain=call_chain or [],
    )


def submit_message(
    agent_name: str,
    text: str,
    session_id: str = None,
    channel: str = "webchat",
    origin: dict = None,
    env: str = "dev",
    sender: str = None,
    parent_session_id: str = None,
    call_chain: list = None,
    caller: str = None,
    sync: bool = False,
    timeout_s: float = 120,
) -> dict:
    """Enqueue a turn. This is the single inbound door: webchat, webhook, MCP,
    poller items and scheduled runs all arrive here.

    `sync=True` blocks until the turn ends or pauses — used by the CLI, by
    agents-as-tools, and by the MCP endpoint.
    """
    agent = _agent_or_raise(agent_name, env, channel)
    store = get_store()

    origin = dict(origin or {"kind": channel})
    if caller:
        origin.setdefault("caller", caller)

    if session_id:
        session = store.get_session(session_id)
        if not session:
            raise LookupError(f"unknown session '{session_id}'")
        store.update_session(session_id, status="running")
    else:
        session_id = start_session(
            agent.name, channel=channel, origin=origin, env=env,
            parent_session_id=parent_session_id, call_chain=call_chain,
        )

    turn_id = new_id("t")
    job_id = store.enqueue(
        "run_turn",
        {
            "agent": agent.name,
            "session_id": session_id,
            "turn_id": turn_id,
            "text": text,
            "channel": channel,
            "origin": origin,
            "sender": sender,
        },
    )
    result = {"session_id": session_id, "turn_id": turn_id, "job_id": job_id,
              "status": "queued"}
    if sync:
        result.update(wait_for_turn(turn_id, timeout_s=timeout_s))
    return result


def wait_for_turn(turn_id: str, timeout_s: float = 120, poll_s: float = 0.05) -> dict:
    """Block until a turn reaches a terminal-or-paused state."""
    store = get_store()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        row = store.one("SELECT * FROM turns WHERE id=?", (turn_id,))
        if row and row["status"] in ("completed", "error", "waiting-approval"):
            reply = ""
            for ev in store.events_for_turn(turn_id):
                if ev.type == "message.sent" and not (ev.payload or {}).get("receipt"):
                    reply = ev.payload.get("text", "")
            return {
                "status": row["status"],
                "reply": reply,
                "error": row["error"],
                "session_id": row["session_id"],
            }
        time.sleep(poll_s)
    return {"status": "timeout", "reply": "", "error": f"turn did not settle in {timeout_s}s"}


# ------------------------------------------------------------- job execution


def execute_job(job) -> None:
    """Called by the worker for each claimed job."""
    kind = job["kind"]
    payload = json.loads(job["payload"])
    if kind == "run_turn":
        _run_turn_job(payload)
    elif kind == "resume_turn":
        _resume_turn_job(payload)
    elif kind == "poll_trigger":
        from .triggers import run_poll_trigger

        run_poll_trigger(payload)
    elif kind == "schedule_trigger":
        from .triggers import run_schedule_trigger

        run_schedule_trigger(payload)
    elif kind == "eval_run":
        from .evals import execute_eval_run

        execute_eval_run(payload)
    else:
        raise ValueError(f"unknown job kind '{kind}'")


def _engine_for(agent, session_id: str, turn_id: str, channel: str) -> TurnEngine:
    store = get_store()
    session = store.get_session(session_id)
    call_chain = json.loads(session["call_chain"] or "[]") if session else []
    # The caller identity is recorded on the session when it is created, so a
    # turn resumed days later still evaluates policies against the same caller.
    origin = json.loads(session["trigger_origin"] or "{}") if session else {}
    caller = origin.get("caller")
    kwargs = dict(channel=channel, call_chain=call_chain, caller=caller)
    namespace = registry_for(channel)
    if namespace is not get_registry():
        # The same tree the agent came from. An engine resolving its tools in
        # the operator's registry would hand a Jarvis agent the operator's
        # tools. Passed only when it differs, so a Level 3 engine that took the
        # documented arguments and no more still loads.
        kwargs["registry"] = namespace
    if agent.handler:
        # Level 3: a custom turn engine. Same events, same adapters, same
        # policies — the platform can't tell the difference.
        return load_engine_class(agent)(store, agent, session_id, turn_id, **kwargs)
    return TurnEngine(store, agent, session_id, turn_id, **kwargs)


def load_engine_class(agent):
    """Resolve an agent's `handler:` to a turn-engine class.

    Two spellings, because a project is files first and not necessarily an
    installed package:

        handler: ./engines/planner.py:PlanningEngine   # path, relative to the agent file
        handler: myproject.planner:PlanningEngine      # importable dotted module
    """
    ref, _, cls_name = agent.handler.rpartition(":")
    if not ref or not cls_name:
        raise ValueError(
            f"agent '{agent.name}' handler must be '<module-or-path>:<ClassName>', "
            f"got {agent.handler!r}"
        )

    if ref.endswith(".py") or ref.startswith("."):
        path = (agent.path.parent / ref).resolve()
        if not path.exists():
            raise FileNotFoundError(f"agent '{agent.name}' handler not found at {path}")
        mod_name = f"heddled_engine_{agent.name}_{abs(hash(str(path)))}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(ref)

    cls = getattr(module, cls_name, None)
    if cls is None:
        raise AttributeError(f"{ref} defines no '{cls_name}'")
    if not issubclass(cls, TurnEngine):
        raise TypeError(
            f"agent '{agent.name}' handler {cls_name} must subclass heddled.TurnEngine "
            "so it emits the same events and obeys the same policies"
        )
    return cls


def _session_env(session_id: str) -> str:
    """Which environment this turn belongs to, and so which definition it runs.
    Read from the session rather than the job, so a turn resumed after an
    approval still runs the version its session started on."""
    session = get_store().get_session(session_id)
    return (session["env"] if session else None) or "dev"


def _run_turn_job(payload: dict) -> TurnResult:
    channel = payload.get("channel", "webchat")
    _mark_jarvis_chat(channel, payload["session_id"])
    agent = _agent_or_raise(payload["agent"], _session_env(payload["session_id"]),
                            channel)
    engine = _engine_for(agent, payload["session_id"], payload["turn_id"], channel)
    return engine.run(payload["text"], origin=payload.get("origin"),
                      sender=payload.get("sender"))


def _mark_jarvis_chat(channel: str, session_id: str) -> None:
    """Tell this worker thread which Jarvis conversation it is running, so what
    gets built is stamped with it. Read off the session rather than taken from
    the job payload: the conversation a turn belongs to is not something the
    model or the caller gets to assert."""
    if channel != "jarvis":
        return
    from . import jarvis

    session = get_store().get_session(session_id)
    origin = json.loads(session["trigger_origin"] or "{}") if session else {}
    jarvis.set_current_chat(origin.get("chat"))


def _resume_turn_job(payload: dict) -> TurnResult:
    channel = payload.get("channel", "webchat")
    _mark_jarvis_chat(channel, payload["session_id"])
    agent = _agent_or_raise(payload["agent"], _session_env(payload["session_id"]),
                            channel)
    engine = _engine_for(agent, payload["session_id"], payload["turn_id"], channel)
    return engine.resume()


# ----------------------------------------------------------------- approvals


def resolve_approval(approval_id: str, decision: str, resolver: str,
                     note: str = None, token: str = None) -> dict:
    """Inbound half of the out-of-Heddled approval flow. Emits `approval.resolved`
    and re-enqueues the paused turn."""
    store = get_store()
    approval = store.get_approval(approval_id)
    if not approval:
        raise LookupError("unknown approval")
    # The approval token is a bearer credential handed to an approver; compare
    # it the way credentials are compared.
    if token is not None and approval["token"] and not hmac.compare_digest(
            str(token), str(approval["token"])):
        raise PermissionError("invalid approval token")
    if approval["status"] != "pending":
        return {"status": approval["status"], "already_resolved": True,
                "session_id": approval["session_id"]}
    if decision not in ("approved", "denied"):
        raise ValueError("decision must be 'approved' or 'denied'")

    store.resolve_approval(approval_id, decision, resolver, note)
    session = store.get_session(approval["session_id"])
    agent = get_registry().get_agent(approval["agent"] or (session["agent"] if session else ""))

    store.append(
        Event(
            type=APPROVAL_RESOLVED,
            session_id=approval["session_id"],
            turn_id=approval["turn_id"],
            agent=approval["agent"],
            agent_version=agent.version if agent else None,
            payload={
                "approval_id": approval_id,
                "tool": approval["tool"],
                "decision": decision,
                "resolver": resolver,
                "note": note,
                "arguments": json.loads(approval["args"]),
            },
        )
    )

    state = store.get_state(approval["session_id"])
    call_id = state.get("awaiting_call_id")
    state.setdefault("approvals", {})
    state.setdefault("approval_notes", {})
    if call_id:
        state["approvals"][call_id] = decision
        if note:
            state["approval_notes"][call_id] = note
    state.pop("awaiting_approval_id", None)
    state.pop("awaiting_call_id", None)
    store.set_state(approval["session_id"], state)

    # Flip out of the paused state before enqueuing, so a caller that is
    # waiting on this turn does not read the stale 'waiting-approval' and
    # conclude the turn has settled.
    store.set_turn_status(approval["turn_id"], "running")
    store.update_session(approval["session_id"], status="running")

    store.enqueue(
        "resume_turn",
        {
            "agent": approval["agent"] or (session["agent"] if session else ""),
            "session_id": approval["session_id"],
            "turn_id": approval["turn_id"],
            "channel": state.get("channel", "webchat"),
        },
    )
    return {"status": decision, "session_id": approval["session_id"],
            "turn_id": approval["turn_id"], "resumed": True}


# -------------------------------------------------------- operator takeover


def inject_operator_message(session_id: str, text: str, operator: str = "operator",
                            resume: bool = True) -> dict:
    """Takeover is a primitive, not a product (§8): put a message on the spine
    as if it came from the agent's own reasoning, and optionally continue."""
    store = get_store()
    session = store.get_session(session_id)
    if not session:
        raise LookupError("unknown session")
    state = store.get_state(session_id)
    turn_id = state.get("turn_id") or new_id("t")
    agent = get_registry().get_agent(session["agent"])

    store.append(
        Event(
            type=OPERATOR_INJECTED,
            session_id=session_id,
            turn_id=turn_id,
            agent=session["agent"],
            agent_version=agent.version if agent else None,
            payload={"text": text, "operator": operator, "resume": resume},
        )
    )
    state.setdefault("messages", []).append(
        {"role": "user", "content": f"[operator note] {text}"}
    )
    store.set_state(session_id, state)

    if not resume:
        return {"injected": True, "resumed": False, "session_id": session_id}

    new_turn = new_id("t")
    state["turn_id"] = new_turn
    store.set_state(session_id, state)
    store.create_turn(new_turn, session_id)
    store.enqueue(
        "resume_turn",
        {"agent": session["agent"], "session_id": session_id, "turn_id": new_turn,
         "channel": state.get("channel", "webchat")},
    )
    return {"injected": True, "resumed": True, "session_id": session_id, "turn_id": new_turn}


# ------------------------------------------------------------------ triggers


def fire_trigger(agent_name: str, kind: str, message: str, reason: str,
                 origin_extra: dict = None, env: str = "dev") -> dict:
    """Common path for schedule and poll triggers: `trigger.fired` is the first
    event of the session, so a scheduled run is as traceable as a user one."""
    agent = _agent_or_raise(agent_name)
    store = get_store()
    origin = {"kind": kind, "reason": reason, **(origin_extra or {})}
    session_id = store.create_session(
        agent=agent.name,
        agent_version=agent.version,
        channel=kind,
        trigger_origin=origin,
        env=env,
    )
    store.append(
        Event(
            type=TRIGGER_FIRED,
            session_id=session_id,
            agent=agent.name,
            agent_version=agent.version,
            payload=origin,
        )
    )
    result = submit_message(
        agent.name, message, session_id=session_id, channel=kind, origin=origin, env=env,
        sender=kind,
    )
    return result


# -------------------------------------------------------------------- health


def platform_health() -> dict:
    store = get_store()
    h = store.health()
    last = store.get_cursor("worker:heartbeat", 0)
    h["worker_alive"] = bool(last and (time.time() - float(last)) < 30)
    h["worker_last_seen"] = last
    return h
