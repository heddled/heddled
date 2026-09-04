"""The trust layer: declarative rules over adapters and sessions.

Policies attach to an agent (and, via `tool: "*"`, to all its tools). Every
decision a policy makes lands on the spine, so the audit log is a query rather
than a feature.

Supported keys inside a policy block:

    tool: refund | "*"            which tool the block applies to
    requires_approval: true       pause the turn and route out for a human
    approval_adapter: webhook     which approval adapter delivers it
    allow_channels: [webchat]     tool may only be used from these channels
    deny_channels: [webhook]      tool may never be used from these channels
    allow_callers: [copilot]      tool may only be used by these external callers
    deny_callers: [untrusted]     tool may never be used by these callers
    approval_callers: [copilot]   gate applies only to these callers (instead of
                                  requires_approval, which gates everyone)
    budget: {max_eur_per_day: 500, max_eur_per_session: 50,
             max_tokens_per_session: 200000}
    rate_limit: {max_calls: 5, per_seconds: 60}
    redact: [email, iban, creditcard]   applied at the trace-store boundary
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

# ------------------------------------------------------------------ redaction

REDACTORS = {
    "email": (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "«email»"),
    "iban": (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"), "«iban»"),
    "creditcard": (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "«card»"),
    "phone": (re.compile(r"\+?\d[\d\s().-]{7,}\d"), "«phone»"),
    "bsn": (re.compile(r"\b\d{9}\b"), "«bsn»"),
}


def redact_value(value, rules: list[str]):
    """Recursively redact a payload. Operate on live data, store the redacted
    form (concept §10)."""
    if not rules:
        return value
    if isinstance(value, str):
        out = value
        for rule in rules:
            pair = REDACTORS.get(rule)
            if pair:
                out = pair[0].sub(pair[1], out)
        return out
    if isinstance(value, dict):
        return {k: redact_value(v, rules) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, rules) for v in value]
    return value


def strip_secrets(value, secrets: list[str]):
    """Remove stored secret values from anything on its way to the store.

    Pattern redaction (`redact: [iban, …]`) is a per-agent choice about customer
    data. This is different and unconditional: a credential must never be
    *storable*, whatever the agent is configured to redact.

    It matters because the leak is rarely deliberate. A handler that raises
    `RuntimeError("GET https://api/x?api_key=… failed")` puts the key straight
    into `error.raised`, tracebacks and all, where any viewer can read it.
    """
    if not secrets:
        return value
    if isinstance(value, str):
        out = value
        for secret in secrets:
            if secret and secret in out:
                out = out.replace(secret, "«secret»")
        return out
    if isinstance(value, dict):
        return {k: strip_secrets(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_secrets(v, secrets) for v in value]
    return value


#: Setting names that hold something nobody should be handed back. Lives here
#: rather than in `auth` because the engine needs it too and must not import a
#: web module to get it; `auth.is_credential` is the same function.
CREDENTIAL_HINTS = ("key", "token", "password", "secret", "webhook_url")


def is_credential(name: str) -> bool:
    """Whether a setting holds something that should stay write-only."""
    lowered = (name or "").lower()
    return any(hint in lowered for hint in CREDENTIAL_HINTS)


#: Settings that are not credentials but are still not an untrusted tool's to
#: inherit. `allow_internal_http` is the operator saying "*my* tools may reach
#: my own network" — a decision about their tools, made before anything of
#: Jarvis's existed. Handing it on would give a model-chosen URL the run of
#: 192.168.x.x and the cloud metadata endpoint, which is the exact attack
#: `tooltypes.guard_destination` was written to stop.
NOT_INHERITED = {"allow_internal_http"}


def for_untrusted_tools(settings: dict) -> dict:
    """What a tool in a namespace that is not the operator's may see.

    Every credential removed, because a no-code tool resolves `{{secret.name}}`
    out of this and a model can write one in four lines. The ordinary settings
    stay — a user agent string is not a secret.
    """
    return {k: v for k, v in (settings or {}).items()
            if not is_credential(k) and k not in NOT_INHERITED}


def secret_values(settings: dict) -> list[str]:
    """The stored values worth scrubbing. Short ones are skipped: a two-letter
    setting would blank out ordinary words everywhere it appeared."""
    return [
        str(v) for v in (settings or {}).values()
        if isinstance(v, str) and len(v) >= 8
    ]


def agent_redaction_rules(agent) -> list[str]:
    rules: list[str] = []
    for p in agent.policies:
        for r in p.get("redact") or []:
            if r not in rules:
                rules.append(r)
    return rules


# ------------------------------------------------------------------ decisions


@dataclass
class Decision:
    allowed: bool = True
    requires_approval: bool = False
    reason: str = ""
    approval_adapter: str = "webhook"
    policy: dict = None


def check_tool_call(agent, tool_name: str, channel: str, store, session_id: str,
                    caller: str = None) -> Decision:
    """Evaluate every policy that applies to this tool call, in order:
    channel allow/deny → caller allow/deny → rate limit → budget → approval gate.

    `caller` is the external identity driving the turn (an MCP caller, an API
    key holder, a parent agent). Governance travels with the agent: even when a
    foreign orchestrator is driving, these rules still apply (§12).
    """
    policy = agent.policy_for_tool(tool_name)
    if not policy:
        return Decision(policy={})

    if policy.get("deny_channels") and channel in policy["deny_channels"]:
        return Decision(False, reason=f"tool '{tool_name}' is denied on channel '{channel}'",
                        policy=policy)
    if policy.get("allow_channels") and channel not in policy["allow_channels"]:
        return Decision(
            False,
            reason=f"tool '{tool_name}' is only allowed on {policy['allow_channels']}, not '{channel}'",
            policy=policy,
        )

    if policy.get("deny_callers") and caller in policy["deny_callers"]:
        return Decision(False, reason=f"tool '{tool_name}' is denied for caller '{caller}'",
                        policy=policy)
    if policy.get("allow_callers") and caller not in policy["allow_callers"]:
        return Decision(
            False,
            reason=f"tool '{tool_name}' is only allowed for {policy['allow_callers']}, "
                   f"not '{caller or 'anonymous'}'",
            policy=policy,
        )

    rl = policy.get("rate_limit") or {}
    if rl.get("max_calls"):
        window = float(rl.get("per_seconds", 60))
        since = time.time() - window
        used = store.count_tool_calls(agent.name, tool_name, since)
        if used >= int(rl["max_calls"]):
            return Decision(
                False,
                reason=f"rate limit: {tool_name} used {used}× in the last {int(window)}s"
                f" (max {rl['max_calls']})",
                policy=policy,
            )

    budget = policy.get("budget") or {}
    if budget.get("max_eur_per_day"):
        spent = store.spend_today("eur", agent=agent.name)
        if spent >= float(budget["max_eur_per_day"]):
            return Decision(
                False,
                reason=f"daily budget exhausted: €{spent:.2f} of €{budget['max_eur_per_day']}",
                policy=policy,
            )
    if budget.get("max_eur_per_session"):
        spent = store.spend_today("eur", session_id=session_id)
        if spent >= float(budget["max_eur_per_session"]):
            return Decision(
                False,
                reason=f"session budget exhausted: €{spent:.2f}",
                policy=policy,
            )

    # `approval_callers` narrows the gate to specific external callers — e.g.
    # trusted internal channels run straight through while an external
    # orchestrator still pauses for a human.
    gated_callers = policy.get("approval_callers")
    if gated_callers is not None:
        if caller in gated_callers:
            return Decision(
                True,
                requires_approval=True,
                reason=policy.get("approval_reason")
                or f"policy requires approval for '{tool_name}' when called by '{caller}'",
                approval_adapter=policy.get("approval_adapter", "webhook"),
                policy=policy,
            )
        return Decision(policy=policy)

    if policy.get("requires_approval"):
        return Decision(
            True,
            requires_approval=True,
            reason=policy.get("approval_reason") or f"policy requires approval for '{tool_name}'",
            approval_adapter=policy.get("approval_adapter", "webhook"),
            policy=policy,
        )
    return Decision(policy=policy)


def check_turn_budget(agent, store, session_id: str) -> Optional[str]:
    """Called before each model invocation. Returns a reason string to abort."""
    for p in agent.policies:
        budget = p.get("budget") or {}
        if p.get("tool") not in (None, "*"):
            continue
        if budget.get("max_tokens_per_session"):
            used = store.spend_today("tokens", session_id=session_id)
            if used >= float(budget["max_tokens_per_session"]):
                return f"session token budget exhausted ({int(used)} tokens)"
        if budget.get("max_eur_per_day"):
            spent = store.spend_today("eur", agent=agent.name)
            if spent >= float(budget["max_eur_per_day"]):
                return f"daily spend budget exhausted (€{spent:.2f})"
    return None
