"""Evals — closing the loop (concept §11).

The trace store makes regression testing nearly free: any recorded session is
promoted to a golden trace by extracting its inbound messages and its recorded
tool results. An eval run replays those messages against a candidate agent
version with tools in mock mode and reports two things:

  1. did the agent call the same tools with equivalent arguments?
  2. does the final answer pass its assertions (exact / contains / regex /
     llm-judged)?

The replay is itself a session on the spine, so an eval result opens in the same
trace view — in diff mode against the golden.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from .engine import TurnEngine
from .events import new_id
from .registry import get_registry
from .store import get_store


# ------------------------------------------------------------------ promote


def extract_spec(session_id: str) -> dict:
    """Turn a recorded session into a replayable case."""
    store = get_store()
    events = store.events_for_session(session_id)
    inbound, tool_calls, tool_results, final = [], [], {}, ""
    for ev in events:
        p = ev.payload or {}
        if ev.type == "message.received":
            inbound.append(p.get("text", ""))
        elif ev.type == "tool.called":
            tool_calls.append({"tool": p.get("tool"), "arguments": p.get("arguments") or {}})
        elif ev.type == "tool.result" and not p.get("partial"):
            tool_results[p.get("tool")] = p.get("result")
        elif ev.type == "message.sent" and not p.get("receipt"):
            final = p.get("text", "")
    session = store.get_session(session_id)
    return {
        "inbound": inbound,
        "expected_tool_calls": tool_calls,
        "tool_results": tool_results,
        "expected_answer": final,
        "recorded_version": session["agent_version"] if session else None,
        "assertions": [{"type": "contains", "value": ""}] if not final else
                      [{"type": "similar", "value": final}],
    }


def promote_session(session_id: str, name: str = None, reported: dict = None) -> str:
    """Turn a recorded conversation into a test.

    `reported` carries who said it was wrong and what they said about it, when
    the promotion came from somebody using the agent rather than an operator
    curating tests. It rides in the spec because that is what travels with the
    trace — the note explains the test, and a note kept somewhere else is a
    note nobody reads next to the thing it is about.
    """
    store = get_store()
    session = store.get_session(session_id)
    if not session:
        raise LookupError("unknown session")
    spec = extract_spec(session_id)
    if reported:
        spec["reported"] = reported
    label = name or session["title"] or session_id
    return store.add_golden(label, session["agent"], session_id, spec)


# ---------------------------------------------------------------- assertions


def check_assertion(assertion: dict, answer: str) -> tuple[bool, str]:
    kind = assertion.get("type", "contains")
    value = assertion.get("value", "")
    if kind == "exact":
        return answer.strip() == str(value).strip(), "exact match"
    if kind == "contains":
        return str(value).lower() in (answer or "").lower(), f"contains {value!r}"
    if kind == "not_contains":
        return str(value).lower() not in (answer or "").lower(), f"does not contain {value!r}"
    if kind == "regex":
        return bool(re.search(str(value), answer or "", re.I | re.S)), f"matches /{value}/"
    if kind == "similar":
        return _similar(answer or "", str(value)) >= float(assertion.get("threshold", 0.6)), (
            "similar to the recorded answer"
        )
    if kind == "judge":
        return _judge(answer, assertion)
    return False, f"unknown assertion type {kind!r}"


def _similar(a: str, b: str) -> float:
    """Token overlap — deliberately crude and dependency-free. Use a `judge`
    assertion when you need real semantics."""
    ta = set(re.findall(r"\w+", a.lower()))
    tb = set(re.findall(r"\w+", b.lower()))
    if not ta or not tb:
        return 1.0 if ta == tb else 0.0
    return len(ta & tb) / len(ta | tb)


def _judge(answer: str, assertion: dict) -> tuple[bool, str]:
    """LLM-as-judge. Uses whichever model the assertion names, falling back to
    the platform's configured judge model."""
    from .providers import get_provider

    store = get_store()
    settings = store.all_settings()
    model = assertion.get("model") or settings.get("judge_model") or "mock/echo"
    criterion = assertion.get("value") or "The answer is correct and helpful."
    provider = get_provider(model, settings)
    resp = provider.complete(
        system="You grade agent answers. Reply with exactly PASS or FAIL, then one short line of reasoning.",
        messages=[
            {
                "role": "user",
                "content": f"Criterion: {criterion}\n\nAnswer to grade:\n{answer}",
            }
        ],
    )
    text = (resp.text or "").strip()
    return text.upper().startswith("PASS"), f"judge({model}): {text[:160]}"


# ------------------------------------------------------------------ compare


def compare_tool_calls(expected: list[dict], actual: list[dict]) -> dict:
    """Same tools, same order, equivalent arguments."""
    diffs = []
    for i in range(max(len(expected), len(actual))):
        e = expected[i] if i < len(expected) else None
        a = actual[i] if i < len(actual) else None
        if e is None:
            diffs.append({"index": i, "kind": "extra", "actual": a})
        elif a is None:
            diffs.append({"index": i, "kind": "missing", "expected": e})
        elif e["tool"] != a["tool"]:
            diffs.append({"index": i, "kind": "different_tool", "expected": e, "actual": a})
        elif _norm(e.get("arguments")) != _norm(a.get("arguments")):
            diffs.append({"index": i, "kind": "different_arguments", "expected": e, "actual": a})
    return {"match": not diffs, "diffs": diffs,
            "first_divergence": diffs[0]["index"] if diffs else None}


def _norm(args) -> str:
    if not isinstance(args, dict):
        return json.dumps(args, sort_keys=True, default=str)
    return json.dumps(
        {k: (v.strip().lower() if isinstance(v, str) else v) for k, v in sorted(args.items())},
        sort_keys=True,
        default=str,
    )


# ---------------------------------------------------------------- eval runs


def queue_eval_run(agent_name: str, golden_ids: list[str] = None,
                   against_version: str = None) -> str:
    store = get_store()
    agent = get_registry().get_agent(agent_name)
    if not agent:
        raise LookupError(f"unknown agent '{agent_name}'")
    run_id = store.create_eval_run(agent.name, against_version or agent.version)
    store.enqueue("eval_run", {"run_id": run_id, "agent": agent.name,
                               "golden_ids": golden_ids})
    return run_id


def execute_eval_run(payload: dict) -> dict:
    store = get_store()
    run_id = payload["run_id"]
    agent = get_registry().get_agent(payload["agent"])
    goldens = [
        store.get_golden(g) for g in (payload.get("golden_ids") or [])
    ] if payload.get("golden_ids") else store.goldens(payload["agent"])
    goldens = [g for g in goldens if g]

    cases, passed, failed = [], 0, 0
    for g in goldens:
        try:
            case = run_case(agent, g)
        except Exception as exc:
            case = {"golden_id": g["id"], "name": g["name"], "passed": False,
                    "error": f"{type(exc).__name__}: {exc}"}
        cases.append(case)
        if case.get("passed"):
            passed += 1
        else:
            failed += 1

    result = {"cases": cases, "agent_version": agent.version}
    store.finish_eval_run(run_id, "passed" if failed == 0 else "failed", passed, failed, result)
    return result


def run_case(agent, golden) -> dict:
    """Replay one golden against the current agent definition."""
    store = get_store()
    spec = json.loads(golden["spec"])

    # A case with nothing to replay exercised nothing, and an empty tool diff
    # plus an empty assertion list would otherwise pass vacuously — a false
    # green on the gate that guards promotion to prod.
    if not (spec.get("inbound") or []):
        return {
            "golden_id": golden["id"],
            "name": golden["name"],
            "passed": False,
            "error": "golden has no inbound messages to replay",
            "tool_diff": {"match": False, "diffs": [], "first_divergence": None},
            "assertions": [],
            "answer": "",
            "expected_answer": spec.get("expected_answer"),
            "replay_session_id": None,
            "golden_session_id": golden["session_id"],
        }

    session_id = store.create_session(
        agent=agent.name,
        agent_version=agent.version,
        channel="eval",
        trigger_origin={"kind": "eval", "reason": f"golden {golden['name']}",
                        "golden_id": golden["id"]},
        env="eval",
        title=f"eval · {golden['name']}",
    )

    reply = ""
    for text in spec.get("inbound") or []:
        turn_id = new_id("t")
        engine = TurnEngine(
            store, agent, session_id, turn_id, channel="eval",
            tool_mocks=spec.get("tool_results") or {},
        )
        result = engine.run(text, origin={"kind": "eval", "golden_id": golden["id"]})
        reply = result.reply or reply
        if result.status == "paused":
            # An approval gate inside an eval is auto-approved: the eval asks
            # "does the agent still decide to do this", not "will a human agree".
            reply = reply or "(paused for approval)"
            break

    actual_calls = []
    for ev in store.events_for_session(session_id):
        if ev.type == "tool.called":
            actual_calls.append({"tool": ev.payload.get("tool"),
                                 "arguments": ev.payload.get("arguments") or {}})

    tool_diff = compare_tool_calls(spec.get("expected_tool_calls") or [], actual_calls)
    assertion_results = []
    for a in spec.get("assertions") or []:
        ok, desc = check_assertion(a, reply)
        assertion_results.append({"assertion": a, "passed": ok, "description": desc})

    passed = tool_diff["match"] and all(r["passed"] for r in assertion_results)
    store.update_session(session_id, status="ended")
    return {
        "golden_id": golden["id"],
        "name": golden["name"],
        "passed": passed,
        "tool_diff": tool_diff,
        "assertions": assertion_results,
        "answer": reply,
        "expected_answer": spec.get("expected_answer"),
        "replay_session_id": session_id,
        "golden_session_id": golden["session_id"],
    }


# ---------------------------------------------------------------- gate check


def latest_run(agent_name: str) -> Optional[dict]:
    store = get_store()
    runs = store.eval_runs(agent_name, limit=1)
    return dict(runs[0]) if runs else None


def is_green(agent_name: str, version: str = None) -> tuple[bool, str]:
    """Deployment promotion is gated on a green eval run for that version."""
    store = get_store()
    for r in store.eval_runs(agent_name, limit=20):
        if version and r["agent_version"] != version:
            continue
        if r["status"] == "running":
            continue
        return r["status"] == "passed", (
            f"eval run {r['id']}: {r['passed']} passed, {r['failed']} failed"
        )
    return False, "no eval run for this version yet"
