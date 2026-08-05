"""The event contract is the platform's most important API — versioned and
stable. These tests are deliberately strict: breaking them should require a
deliberate decision, not an accident."""

from heddled import events
from heddled.events import EVENT_CLASS, EVENT_TYPES, Event


def test_canonical_event_set_is_exactly_the_documented_thirteen():
    assert EVENT_TYPES == [
        "trigger.fired",
        "message.received",
        "context.built",
        "model.invoked",
        "model.responded",
        "tool.called",
        "tool.result",
        "approval.requested",
        "approval.resolved",
        "operator.injected",
        "message.sent",
        "turn.completed",
        "error.raised",
    ]


def test_every_event_type_has_a_render_class():
    assert set(EVENT_CLASS) == set(EVENT_TYPES)


def test_event_carries_the_required_identity_fields():
    ev = Event(type="message.received", session_id="s_1", turn_id="t_1",
               agent="support", agent_version="abc", payload={"text": "hi"})
    d = ev.to_dict()
    for field in ("session_id", "turn_id", "agent_version", "seq", "contract"):
        assert field in d
    assert d["contract"] == events.CONTRACT_VERSION


def test_new_id_is_prefixed_and_unique():
    a, b = events.new_id("s"), events.new_id("s")
    assert a.startswith("s_") and b.startswith("s_") and a != b


class TestSummary:
    """The timeline column is generated, so a payload shape change shows up
    here rather than as a blank row in the console."""

    def test_message_summary_is_the_text(self):
        ev = Event(type="message.received", session_id="s", payload={"text": "hello"})
        assert ev.summary == "hello"

    def test_long_message_is_truncated(self):
        ev = Event(type="message.sent", session_id="s", payload={"text": "x" * 200})
        assert len(ev.summary) == 91 and ev.summary.endswith("…")

    def test_tool_result_reports_ok_and_duration(self):
        ev = Event(type="tool.result", session_id="s",
                   payload={"tool": "refund", "error": False, "duration_ms": 12})
        assert "refund" in ev.summary and "ok" in ev.summary and "12 ms" in ev.summary

    def test_tool_result_reports_error(self):
        ev = Event(type="tool.result", session_id="s",
                   payload={"tool": "refund", "error": True})
        assert "error" in ev.summary

    def test_partial_tool_result_is_a_log_line(self):
        ev = Event(type="tool.result", session_id="s",
                   payload={"tool": "lookup", "partial": True, "log": "working"})
        assert "log: working" in ev.summary

    def test_approval_summaries_name_the_tool_and_decision(self):
        req = Event(type="approval.requested", session_id="s",
                    payload={"tool": "refund", "routed_to": "slack"})
        res = Event(type="approval.resolved", session_id="s",
                    payload={"tool": "refund", "decision": "approved", "resolver": "ralph"})
        assert "refund" in req.summary and "slack" in req.summary
        assert "approved" in res.summary and "ralph" in res.summary

    def test_trigger_summary_names_what_fired(self):
        ev = Event(type="trigger.fired", session_id="s",
                   payload={"kind": "schedule", "reason": "cron 0 8 * * 1-5"})
        assert "schedule" in ev.summary and "cron" in ev.summary

    def test_every_event_type_produces_a_string_summary(self):
        for t in EVENT_TYPES:
            assert isinstance(Event(type=t, session_id="s", payload={}).summary, str)
