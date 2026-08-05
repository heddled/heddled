"""OpenTelemetry export (decision 7): the spine as a consumer.

A consumer observes the stream and must never be able to affect it.
"""

import json

import pytest

from heddled import otel, runtime
from heddled.events import Event


@pytest.fixture()
def exporter(store):
    e = otel.OtelExporter(store, endpoint="http://collector.test:4318")
    yield e
    e.stop()


def turn_events(store, agent, text="where is invoice F-2231?"):
    from heddled.engine import TurnEngine
    from heddled.events import new_id

    sid = store.create_session(agent=agent.name, agent_version=agent.version,
                               channel="webchat")
    TurnEngine(store, agent, sid, new_id("t")).run(text)
    return store.events_for_session(sid)


class TestIdStability:
    def test_the_same_session_always_maps_to_the_same_trace(self):
        assert otel._hex_id("s_abc", 32) == otel._hex_id("s_abc", 32)

    def test_different_sessions_map_to_different_traces(self):
        assert otel._hex_id("s_abc", 32) != otel._hex_id("s_def", 32)

    def test_ids_are_the_right_width(self):
        assert len(otel._hex_id("s_abc", 32)) == 32   # 16-byte trace id
        assert len(otel._hex_id("t_abc", 16)) == 16   # 8-byte span id


class TestAttributeEncoding:
    def test_each_python_type_maps_to_its_otlp_value(self):
        attrs = {a["key"]: a["value"] for a in otel._attrs(
            {"s": "x", "i": 3, "f": 1.5, "b": True})}
        assert attrs["s"] == {"stringValue": "x"}
        assert attrs["i"] == {"intValue": "3"}
        assert attrs["f"] == {"doubleValue": 1.5}
        assert attrs["b"] == {"boolValue": True}

    def test_bools_are_not_mistaken_for_ints(self):
        assert otel._attrs({"b": False})[0]["value"] == {"boolValue": False}

    def test_none_is_dropped_rather_than_sent_as_null(self):
        assert otel._attrs({"a": None, "b": 1}) == [
            {"key": "b", "value": {"intValue": "1"}}]

    def test_structured_values_are_json_encoded(self):
        v = otel._attrs({"d": {"a": 1}})[0]["value"]
        assert json.loads(v["stringValue"]) == {"a": 1}


class TestPayloadShape:
    def test_a_completed_turn_becomes_one_trace(self, store, agent, exporter):
        payload = exporter.build_payload(turn_events(store, agent))
        spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len({s["traceId"] for s in spans}) == 1

    def test_the_turn_span_is_the_root(self, store, agent, exporter):
        spans = exporter.build_payload(
            turn_events(store, agent))["resourceSpans"][0]["scopeSpans"][0]["spans"]
        root = spans[0]
        assert root["name"].startswith("turn ")
        assert "parentSpanId" not in root
        assert all(s["parentSpanId"] == root["spanId"] for s in spans[1:])

    def test_model_and_tool_calls_become_child_spans(self, store, agent, exporter):
        spans = exporter.build_payload(
            turn_events(store, agent))["resourceSpans"][0]["scopeSpans"][0]["spans"]
        names = [s["name"] for s in spans]
        assert any(n.startswith("model ") for n in names)
        assert "tool lookup_invoice" in names

    def test_a_tool_span_is_closed_by_its_result(self, store, agent, exporter):
        spans = exporter.build_payload(
            turn_events(store, agent))["resourceSpans"][0]["scopeSpans"][0]["spans"]
        tool = next(s for s in spans if s["name"] == "tool lookup_invoice")
        assert int(tool["endTimeUnixNano"]) >= int(tool["startTimeUnixNano"])
        assert tool["status"]["code"] == otel.STATUS_OK

    def test_token_usage_lands_on_the_model_span(self, store, agent, exporter):
        spans = exporter.build_payload(
            turn_events(store, agent))["resourceSpans"][0]["scopeSpans"][0]["spans"]
        model = next(s for s in spans if s["name"].startswith("model "))
        keys = {a["key"] for a in model["attributes"]}
        assert "gen_ai.usage.input_tokens" in keys
        assert "gen_ai.usage.output_tokens" in keys

    def test_unpaired_events_ride_along_as_span_events(self, store, agent, exporter):
        payload = exporter.build_payload(turn_events(store, agent))
        root = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        names = {e["name"] for e in root["events"]}
        assert "message.received" in names and "message.sent" in names

    def test_a_failed_turn_is_marked_error(self, store, agent, exporter):
        events = turn_events(store, agent)
        events.append(Event(type="error.raised", session_id=events[0].session_id,
                            turn_id=events[0].turn_id, payload={"kind": "tool_failed"}))
        root = exporter.build_payload(events)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert root["status"]["code"] == otel.STATUS_ERROR

    def test_the_service_name_is_on_the_resource(self, store, agent, exporter):
        res = exporter.build_payload(
            turn_events(store, agent))["resourceSpans"][0]["resource"]
        assert {"key": "service.name", "value": {"stringValue": "heddled"}} in res["attributes"]

    def test_an_empty_turn_produces_nothing(self, exporter):
        assert exporter.build_payload([]) is None


class TestBuffering:
    def test_events_are_held_until_the_turn_completes(self, store, agent, exporter):
        events = turn_events(store, agent)
        for ev in events[:-1]:
            exporter.handle(ev)
        assert exporter._turns and exporter.exported_turns == 0

    def test_events_without_a_turn_are_ignored(self, exporter):
        exporter.handle(Event(type="trigger.fired", session_id="s_1", payload={}))
        assert exporter._turns == {}

    def test_a_stale_turn_is_evicted_rather_than_leaking(self, store, agent, exporter):
        import time as _time

        ev = Event(type="message.received", session_id="s_1", turn_id="t_1",
                   payload={}, ts=_time.time() - otel.MAX_TURN_AGE_S - 10)
        exporter.handle(ev)
        assert "t_1" in exporter._turns
        exporter._evict_stale()
        assert "t_1" not in exporter._turns


class TestDelivery:
    def test_a_completed_turn_is_posted_to_the_collector(self, store, agent, exporter,
                                                         monkeypatch):
        sent = {}

        class Resp:
            status_code = 200
            text = ""

        def fake_post(url, json=None, headers=None, timeout=None):
            sent["url"] = url
            sent["payload"] = json
            return Resp()

        monkeypatch.setattr(otel.requests, "post", fake_post)
        for ev in turn_events(store, agent):
            exporter.handle(ev)

        assert sent["url"] == "http://collector.test:4318/v1/traces"
        assert sent["payload"]["resourceSpans"]
        assert exporter.exported_turns == 1

    def test_a_collector_error_is_recorded_not_raised(self, store, agent, exporter,
                                                      monkeypatch):
        class Resp:
            status_code = 503
            text = "unavailable"

        monkeypatch.setattr(otel.requests, "post",
                            lambda *a, **k: Resp())
        assert exporter.export(turn_events(store, agent)) is False
        assert "503" in exporter.last_error

    def test_an_unreachable_collector_is_recorded_not_raised(self, store, agent,
                                                             exporter, monkeypatch):
        def boom(*a, **k):
            raise ConnectionError("no route to host")

        monkeypatch.setattr(otel.requests, "post", boom)
        assert exporter.export(turn_events(store, agent)) is False
        assert "ConnectionError" in exporter.last_error


class TestConsumerIsolation:
    def test_the_spine_runs_fine_with_no_collector_configured(self, store, registry,
                                                              worker):
        assert otel.configure(store) is None
        result = runtime.submit_message("support", "just say hello", sync=True,
                                        timeout_s=15)
        assert result["status"] == "completed"

    def test_an_exporter_that_throws_cannot_break_a_turn(self, store, registry, worker,
                                                         monkeypatch):
        """A consumer observes the stream; it must never affect it."""
        def boom(*a, **k):
            raise RuntimeError("exporter is broken")

        monkeypatch.setattr(otel.OtelExporter, "handle", boom)
        e = otel.OtelExporter(store, endpoint="http://collector.test:4318").start()
        try:
            result = runtime.submit_message("support", "where is invoice F-2231?",
                                            sync=True, timeout_s=15)
            assert result["status"] == "completed"
        finally:
            e.stop()

    def test_status_reports_disabled_when_unconfigured(self):
        assert otel.status() == {"enabled": False}
