"""OpenTelemetry export — the spine as a consumer (decision 7).

Everything in Heddled is an adapter or a consumer, and OTel is squarely the second
kind: it subscribes to the event stream and writes it somewhere else. Nothing in
the engine knows this module exists.

The wire format is **OTLP/HTTP with JSON encoding**, POSTed with `requests`.
That is a documented part of the OTLP spec and every collector accepts it, so
exporting costs no new dependency — which matters for a self-hosted install
whose whole third-party surface is three packages.

Mapping: one trace per session, one span per turn, child spans for each model
call and each tool call. Events that do not pair off (errors, approvals,
operator injections) are attached to the turn span as span events, so nothing on
the spine is silently dropped.

Enable it in Settings or the environment:

    otel_endpoint      https://collector.example:4318
    otel_headers       {"authorization": "Bearer …"}
    otel_service_name  heddled            (default)
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
import traceback
from typing import Optional

import requests

from .events import (
    ERROR_RAISED,
    MODEL_INVOKED,
    MODEL_RESPONDED,
    TOOL_CALLED,
    TOOL_RESULT,
    TURN_COMPLETED,
)

# OTLP status codes.
STATUS_UNSET, STATUS_OK, STATUS_ERROR = 0, 1, 2

# A turn that never completes must not pin its events in memory forever.
MAX_TURN_AGE_S = 3600


def _hex_id(value: str, length: int) -> str:
    """Stable trace/span ids: the same session always maps to the same trace,
    so a link pasted into a ticket keeps working across restarts."""
    return hashlib.sha256((value or "").encode()).hexdigest()[:length]


def _nanos(ts: float) -> str:
    return str(int(ts * 1_000_000_000))


def _attrs(mapping: dict) -> list[dict]:
    out = []
    for k, v in (mapping or {}).items():
        if v is None:
            continue
        if isinstance(v, bool):
            value = {"boolValue": v}
        elif isinstance(v, int):
            value = {"intValue": str(v)}
        elif isinstance(v, float):
            value = {"doubleValue": v}
        elif isinstance(v, str):
            value = {"stringValue": v}
        else:
            value = {"stringValue": json.dumps(v, default=str)[:4096]}
        out.append({"key": k, "value": value})
    return out


class OtelExporter:
    """Subscribes to the spine and ships completed turns to a collector."""

    def __init__(self, store, endpoint: str, headers: dict = None,
                 service_name: str = "heddled", timeout_s: float = 10):
        self.store = store
        self.endpoint = endpoint.rstrip("/")
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.service_name = service_name
        self.timeout_s = timeout_s
        self._turns: dict[str, list] = {}
        self._queue: queue.Queue = queue.Queue(maxsize=10000)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.exported_turns = 0
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> "OtelExporter":
        self.store.subscribe(self._queue)
        self._thread = threading.Thread(target=self._loop, name="heddled-otel", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.store.unsubscribe(self._queue)

    def _loop(self) -> None:
        last_sweep = time.time()
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=1)
            except queue.Empty:
                event = None
            if event is not None:
                try:
                    self.handle(event)
                except Exception as exc:  # a consumer must never break the spine
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    traceback.print_exc()
            if time.time() - last_sweep > 60:
                last_sweep = time.time()
                self._evict_stale()

    def _evict_stale(self) -> None:
        cutoff = time.time() - MAX_TURN_AGE_S
        for turn_id, events in list(self._turns.items()):
            if events and events[-1].ts < cutoff:
                self._turns.pop(turn_id, None)

    # ---------------------------------------------------------------- intake

    def handle(self, event) -> None:
        if not event.turn_id:
            return
        self._turns.setdefault(event.turn_id, []).append(event)
        if event.type == TURN_COMPLETED:
            events = self._turns.pop(event.turn_id, [])
            self.export(events)

    # ----------------------------------------------------------------- build

    def build_payload(self, events: list) -> Optional[dict]:
        """Turn one turn's events into an OTLP ResourceSpans payload."""
        if not events:
            return None
        first, last = events[0], events[-1]
        trace_id = _hex_id(first.session_id, 32)
        turn_span_id = _hex_id(first.turn_id, 16)

        failed = any(e.type == ERROR_RAISED for e in events)
        turn_span = {
            "traceId": trace_id,
            "spanId": turn_span_id,
            "name": f"turn {first.agent or 'agent'}",
            "kind": 1,  # INTERNAL
            "startTimeUnixNano": _nanos(first.ts),
            "endTimeUnixNano": _nanos(last.ts),
            "attributes": _attrs({
                "heddled.session_id": first.session_id,
                "heddled.turn_id": first.turn_id,
                "heddled.agent": first.agent,
                "heddled.agent_version": first.agent_version,
                "heddled.status": (last.payload or {}).get("status"),
            }),
            "status": {"code": STATUS_ERROR if failed else STATUS_OK},
            "events": [],
        }

        spans = [turn_span]
        open_model: Optional[dict] = None
        open_tools: dict[str, dict] = {}

        for ev in events:
            payload = ev.payload or {}

            if ev.type == MODEL_INVOKED:
                open_model = {
                    "traceId": trace_id,
                    "spanId": _hex_id(f"{ev.turn_id}:model:{ev.seq}", 16),
                    "parentSpanId": turn_span_id,
                    "name": f"model {payload.get('model', '')}".strip(),
                    "kind": 3,  # CLIENT
                    "startTimeUnixNano": _nanos(ev.ts),
                    "endTimeUnixNano": _nanos(ev.ts),
                    "attributes": _attrs({"gen_ai.request.model": payload.get("model")}),
                    "status": {"code": STATUS_UNSET},
                }
                spans.append(open_model)

            elif ev.type == MODEL_RESPONDED and open_model is not None:
                usage = payload.get("usage") or {}
                open_model["endTimeUnixNano"] = _nanos(ev.ts)
                open_model["attributes"] += _attrs({
                    "gen_ai.usage.input_tokens": usage.get("input_tokens"),
                    "gen_ai.usage.output_tokens": usage.get("output_tokens"),
                    "gen_ai.response.finish_reason": payload.get("stop_reason"),
                    "heddled.cost_eur": payload.get("cost_eur"),
                    "heddled.duration_ms": payload.get("duration_ms"),
                })
                open_model["status"] = {"code": STATUS_OK}
                open_model = None

            elif ev.type == TOOL_CALLED:
                span = {
                    "traceId": trace_id,
                    "spanId": _hex_id(f"{ev.turn_id}:tool:{ev.seq}", 16),
                    "parentSpanId": turn_span_id,
                    "name": f"tool {payload.get('tool', '')}".strip(),
                    "kind": 3,
                    "startTimeUnixNano": _nanos(ev.ts),
                    "endTimeUnixNano": _nanos(ev.ts),
                    "attributes": _attrs({
                        "heddled.tool": payload.get("tool"),
                        "heddled.tool.mocked": payload.get("mocked"),
                    }),
                    "status": {"code": STATUS_UNSET},
                }
                spans.append(span)
                open_tools[payload.get("call_id") or payload.get("tool")] = span

            elif ev.type == TOOL_RESULT and not payload.get("partial"):
                key = payload.get("call_id") or payload.get("tool")
                span = open_tools.pop(key, None)
                if span is not None:
                    span["endTimeUnixNano"] = _nanos(ev.ts)
                    span["status"] = {
                        "code": STATUS_ERROR if payload.get("error") else STATUS_OK
                    }
                    span["attributes"] += _attrs(
                        {"heddled.duration_ms": payload.get("duration_ms")})

            else:
                # Everything with no natural span — approvals, operator
                # injections, errors, messages — rides along as a span event so
                # the trace stays complete.
                turn_span["events"].append({
                    "timeUnixNano": _nanos(ev.ts),
                    "name": ev.type,
                    "attributes": _attrs({"heddled.summary": ev.summary, "heddled.seq": ev.seq}),
                })

        return {
            "resourceSpans": [{
                "resource": {"attributes": _attrs({
                    "service.name": self.service_name,
                    "service.version": first.agent_version,
                })},
                "scopeSpans": [{
                    "scope": {"name": "heddled"},
                    "spans": spans,
                }],
            }]
        }

    # ------------------------------------------------------------------ send

    def export(self, events: list) -> bool:
        payload = self.build_payload(events)
        if not payload:
            return False
        try:
            resp = requests.post(f"{self.endpoint}/v1/traces", json=payload,
                                 headers=self.headers, timeout=self.timeout_s)
            if resp.status_code >= 400:
                self.last_error = f"collector {resp.status_code}: {resp.text[:200]}"
                return False
            self.exported_turns += 1
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False


_exporter: Optional[OtelExporter] = None
_lock = threading.Lock()


def configure(store) -> Optional[OtelExporter]:
    """Start the exporter if an endpoint is configured. Called once by whatever
    process owns the spine; a no-op when OTel is not set up."""
    global _exporter
    if _exporter is not None:
        return _exporter

    settings = store.all_settings()
    endpoint = (settings.get("otel_endpoint")
                or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
    if not endpoint:
        return None

    headers = settings.get("otel_headers") or {}
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except json.JSONDecodeError:
            headers = {}

    with _lock:
        if _exporter is None:
            _exporter = OtelExporter(
                store,
                endpoint=endpoint,
                headers=headers,
                service_name=(settings.get("otel_service_name")
                              or os.environ.get("OTEL_SERVICE_NAME") or "heddled"),
            ).start()
    return _exporter


def status() -> dict:
    if _exporter is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "endpoint": _exporter.endpoint,
        "service_name": _exporter.service_name,
        "exported_turns": _exporter.exported_turns,
        "last_error": _exporter.last_error,
    }
