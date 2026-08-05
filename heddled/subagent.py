"""Agents as tools — the whole composition model (concept §12, inside).

Delegation is a `tool.called` event whose handler is another agent's turn
engine. The sub-session links to its parent, so the trace view can render the
tree. No new orchestration language: the spine already expresses it.

Because the sub-turn is executed inline (not enqueued) the parent's worker
thread is the one doing the work, which keeps the parent's `tool.result` timing
honest and avoids a deadlock when the pool is small.
"""

from __future__ import annotations

import json

from . import config
from .events import new_id


def make_agent_tool_handler(agent_name: str):
    def handle(args: dict, ctx):
        from .engine import TurnEngine
        from .runtime import resolve_agent
        from .store import get_store

        store = get_store()
        parent = store.get_session(ctx.session_id)
        # The specialist runs the version published to the environment the
        # caller is in — a prod turn must not reach a colleague's draft.
        sub = resolve_agent(agent_name, parent["env"] if parent else "dev")
        if not sub:
            return {"error": f"agent '{agent_name}' not found"}
        chain = json.loads(parent["call_chain"] or "[]") if parent else []
        chain = list(chain) + [ctx.agent]

        # Loop protection (§12): depth limit + cycle detection, failing fast and
        # visibly in the trace rather than silently recursing.
        if len(chain) > config.MAX_CALL_DEPTH:
            return {"error": f"call depth limit {config.MAX_CALL_DEPTH} exceeded",
                    "call_chain": chain}
        if agent_name in chain:
            return {"error": f"delegation cycle detected: {' → '.join(chain + [agent_name])}",
                    "call_chain": chain}

        session_id = args.get("session_id") or store.create_session(
            agent=sub.name,
            agent_version=sub.version,
            channel="agent",
            trigger_origin={"kind": "agent", "reason": f"delegated by {ctx.agent}",
                            "parent_session": ctx.session_id},
            env=parent["env"] if parent else "dev",
            parent_session_id=ctx.session_id,
            call_chain=chain,
        )
        turn_id = new_id("t")
        engine = TurnEngine(store, sub, session_id, turn_id, channel="agent",
                            call_chain=chain)
        result = engine.run(
            args.get("message") or "",
            origin={"kind": "agent", "parent_session": ctx.session_id, "via": chain},
            sender=ctx.agent,
        )
        return {
            "reply": result.reply,
            "session_id": session_id,
            "status": result.status,
            "trace_url": f"/sessions/{session_id}",
        }

    return handle
