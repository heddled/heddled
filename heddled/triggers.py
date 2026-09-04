"""Pull triggers — Heddled itself as the active party (concept §7).

Push triggers need nothing here: they are inbound channel adapters. What lives
in this module is the half where Heddled does the calling — a cron scheduler and
interval pollers — both running inside the same background worker that drains
the turn queue.

Cursors are persisted in SQLite so a restart resumes where it left off, and
`trigger.fired` is emitted as the first event of the session so a scheduled or
polled run is exactly as traceable and replayable as a user-driven one.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Optional

from .adapters import get_poller
from .registry import get_registry
from .store import get_store

# ------------------------------------------------------------------ cron


def _expand(field: str, lo: int, hi: int, names: dict = None) -> set[int]:
    out: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if names:
            for k, v in names.items():
                part = re.sub(k, str(v), part, flags=re.I)
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part in ("*", "?"):
            start, end = lo, hi
        elif "-" in part.lstrip("-"):
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        out.update(range(start, end + 1, step))
    return {v for v in out if lo <= v <= hi}


_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_MON_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
}


def cron_matches(expr: str, when: datetime) -> bool:
    """Standard 5-field cron: minute hour day-of-month month day-of-week."""
    expr = _ALIASES.get(expr.strip().lower(), expr).strip()
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields, got {len(fields)}: {expr!r}")
    minute, hour, dom, month, dow = fields

    if when.minute not in _expand(minute, 0, 59):
        return False
    if when.hour not in _expand(hour, 0, 23):
        return False
    if when.month not in _expand(month, 1, 12, _MON_NAMES):
        return False

    # cron semantics: when both dom and dow are restricted, either may match
    dom_set = _expand(dom, 1, 31)
    dow_set = {d % 7 for d in _expand(dow, 0, 7, _DOW_NAMES)}
    dom_restricted = dom.strip() not in ("*", "?")
    dow_restricted = dow.strip() not in ("*", "?")
    weekday = (when.weekday() + 1) % 7  # python Mon=0 → cron Sun=0

    if dom_restricted and dow_restricted:
        if when.day not in dom_set and weekday not in dow_set:
            return False
    elif dom_restricted:
        if when.day not in dom_set:
            return False
    elif dow_restricted:
        if weekday not in dow_set:
            return False
    return True


def parse_interval(value) -> float:
    """`60s`, `5m`, `2h`, `1d`, or a bare number of seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(value), re.I)
    if not m:
        raise ValueError(f"cannot parse interval {value!r}")
    n = float(m.group(1))
    return n * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2).lower()]


# ---------------------------------------------------------------- scheduling


def tick(now: datetime = None) -> int:
    """One scheduler pass. Called about once a second by the worker; cheap
    because the per-minute guard short-circuits almost every call."""
    now = now or datetime.now()
    store = get_store()
    fired = 0
    for agent, channel in _scheduled_agents(store):
        for trigger in agent.triggers:
            try:
                if trigger.kind == "schedule":
                    fired += _maybe_fire_schedule(store, agent, trigger, now, channel)
                elif trigger.kind == "poll":
                    # Jarvis cannot write a poller — it has no tool for one —
                    # so this only ever runs for the operator's own agents.
                    fired += _maybe_fire_poll(store, agent, trigger)
            except Exception as exc:
                store.set_cursor(
                    f"trigger:error:{agent.name}:{trigger.key}",
                    {"error": str(exc), "at": time.time()},
                )
    return fired


def _scheduled_agents(store):
    """Every agent whose triggers this pass should consider, with the channel
    its runs belong on.

    Jarvis's agents live in a different tree and must keep running there: the
    `jarvis` channel is what routes a turn to its registry and strips the
    operator's credentials out of what its tools can see. A scheduled Jarvis run
    on the `schedule` channel would resolve against the operator's estate, which
    is the one thing the whole feature is built to prevent.
    """
    from . import jarvis

    out = [(a, "schedule") for a in get_registry().agents().values()]
    if not jarvis.enabled(store):
        return out
    if not jarvis.schedules_affordable(store):
        # Nobody is watching these. When the day's budget is gone they simply
        # do not fire, rather than failing one turn at a time inside the engine.
        store.set_cursor("jarvis:schedules", {
            "held": "the day's schedule budget is used up",
            "spent": jarvis.schedule_spend_today(store), "at": time.time()})
        return out
    store.set_cursor("jarvis:schedules", {"held": None, "at": time.time()})
    for name, agent in jarvis.registry().agents().items():
        if name != jarvis.DRIVER:      # never the one holding the builders
            out.append((agent, jarvis.CHANNEL))
    return out


def _maybe_fire_schedule(store, agent, trigger, now: datetime,
                        channel: str = "schedule") -> int:
    expr = trigger.raw.get("schedule")
    if not expr or not cron_matches(expr, now):
        return 0
    minute_key = now.strftime("%Y-%m-%dT%H:%M")
    cursor_key = f"schedule:{channel}:{agent.name}:{trigger.key}"
    if store.get_cursor(cursor_key) == minute_key:
        return 0  # already fired this minute
    store.set_cursor(cursor_key, minute_key)
    store.enqueue(
        "schedule_trigger",
        {"agent": agent.name, "trigger": trigger.raw, "at": minute_key,
         "cron": expr, "channel": channel},
    )
    return 1


def _maybe_fire_poll(store, agent, trigger) -> int:
    every = parse_interval(trigger.raw.get("every", "60s"))
    cursor_key = f"poll:{agent.name}:{trigger.key}:last_tick"
    last = float(store.get_cursor(cursor_key) or 0)
    if time.time() - last < every:
        return 0
    store.set_cursor(cursor_key, time.time())
    store.enqueue("poll_trigger", {"agent": agent.name, "trigger": trigger.raw,
                                   "key": trigger.key})
    return 1


# ------------------------------------------------------------- job handlers


def run_schedule_trigger(payload: dict) -> None:
    from .runtime import fire_trigger, inbound_env

    trigger = payload["trigger"]
    fire_trigger(
        payload["agent"],
        kind="schedule",
        message=trigger.get("message") or "Scheduled run.",
        reason=f"cron {payload.get('cron')} at {payload.get('at')}",
        origin_extra={"cron": payload.get("cron"), "fired_at": payload.get("at")},
        env=inbound_env(trigger.get("env")),
        # What started it and where it runs are different questions: the origin
        # still says `schedule`, so the trace reads the same, while the channel
        # decides which tree the agent comes from.
        channel=payload.get("channel", "schedule"),
    )


def run_poll_trigger(payload: dict) -> None:
    """Check the source, and start one turn per new item.

    Reliability is a per-trigger choice: `at_least_once` (default) advances the
    cursor only after the turn is durably enqueued.
    """
    from .runtime import fire_trigger, inbound_env

    store = get_store()
    agent_name = payload["agent"]
    trigger = payload["trigger"]
    source = trigger.get("poll")
    cfg = dict(trigger.get("config") or {})
    if isinstance(source, dict):
        cfg.update(source)
        source = cfg.pop("source_name", "mailbox")

    poller = get_poller(source, store.all_settings())
    if poller is None:
        store.set_cursor(f"trigger:error:{agent_name}:{payload.get('key')}",
                         {"error": f"unknown poller '{source}'", "at": time.time()})
        return

    cursor_key = f"poll:{agent_name}:{payload.get('key')}:cursor"
    cursor = store.get_cursor(cursor_key)
    semantics = trigger.get("delivery", "at_least_once")

    items, new_cursor = poller.poll(cursor, cfg)
    if semantics == "at_most_once":
        store.set_cursor(cursor_key, new_cursor)

    template = trigger.get("on_new") or "Handle this item."
    for item in items:
        message = f"{template}\n\n{item.get('text') or item.get('body') or ''}".strip()
        fire_trigger(
            agent_name,
            kind="poll",
            message=message,
            reason=f"{source}: {item.get('subject') or item.get('id')}",
            origin_extra={"source": source, "item_id": item.get("id"),
                          "subject": item.get("subject"), "from": item.get("from")},
            env=inbound_env(trigger.get("env")),
        )

    if semantics != "at_most_once":
        store.set_cursor(cursor_key, new_cursor)


def _watching(spec: dict) -> dict:
    """What a poll trigger is actually watching, in words.

    Every poller used to describe itself as "whenever something arrives", which
    told you nothing about whether it was a folder or a mailbox, or which one.
    """
    config = spec.get("config") or {}
    if config.get("source") == "imap":
        folder = config.get("folder") or "INBOX"
        return {"watching": "mailbox", "where": folder,
                "words": f"Every email arriving in {folder}"}
    path = config.get("path") or "./var/mailbox"
    return {"watching": "folder", "where": path,
            "words": f"Every file arriving in {path}"}


def trigger_status() -> list[dict]:
    """What the Agents screen shows under Triggers."""
    store = get_store()
    out = []
    for agent in get_registry().agents().values():
        for t in agent.triggers:
            row = {"agent": agent.name, "kind": t.kind, "spec": t.raw}
            if t.kind == "schedule":
                row["last_fired"] = store.get_cursor(f"schedule:{agent.name}:{t.key}")
                row["cron"] = t.raw.get("schedule")
            else:
                last = store.get_cursor(f"poll:{agent.name}:{t.key}:last_tick")
                row["last_tick"] = float(last) if last else None
                row["cursor"] = store.get_cursor(f"poll:{agent.name}:{t.key}:cursor")
                row["every"] = t.raw.get("every")
                row.update(_watching(t.raw))
            row["error"] = store.get_cursor(f"trigger:error:{agent.name}:{t.key}")
            out.append(row)
    return out
