"""What happened, in plain language.

The event log is the truth, but "what happened" and "every record on the spine"
are different questions. These tests pin the answer to the first one.
"""

from heddled import story
from heddled.events import Event


def ev(type_, payload=None, seq=1, ts=1000.0):
    return Event(type=type_, session_id="s_1", turn_id="t_1", agent="support",
                 payload=payload or {}, seq=seq, ts=ts)


class TestMechanismIsHidden:
    def test_model_plumbing_never_appears_in_the_story(self):
        events = [
            ev("message.received", {"text": "hi"}),
            ev("context.built", {"system": "…", "messages": []}),
            ev("model.invoked", {"model": "mock/echo"}),
            ev("model.responded", {"usage": {}}),
            ev("message.sent", {"text": "hello"}),
        ]
        kinds = [s.kind for s in story.build(events)]
        assert kinds == ["ask", "replied"]

    def test_every_hidden_type_is_a_real_event_type(self):
        from heddled.events import EVENT_TYPES

        assert story.MECHANISM <= set(EVENT_TYPES)


class TestReadings:
    def test_an_inbound_message_names_the_asker(self):
        step = story.build([ev("message.received", {"text": "where is F-1?",
                                                    "sender": "user"})])[0]
        assert step.headline == "You asked" and "where is F-1?" in step.detail

    def test_a_named_sender_is_used_instead(self):
        step = story.build([ev("message.received", {"text": "hi",
                                                    "sender": "copilot-studio"})])[0]
        assert "copilot-studio" in step.headline

    def test_a_tool_call_and_its_result_become_one_line(self):
        events = [
            ev("tool.called", {"tool": "lookup_invoice", "call_id": "c1",
                               "arguments": {"invoice_number": "F-1"}}, seq=1),
            ev("tool.result", {"tool": "lookup_invoice", "call_id": "c1",
                               "result": {"status": "unpaid"}}, seq=2),
        ]
        steps = story.build(events)
        assert len(steps) == 1
        assert steps[0].headline == "Used lookup invoice — invoice number: F-1"
        assert steps[0].detail == "status: unpaid"

    def test_a_flat_result_is_read_out_rather_than_dumped_as_json(self):
        step = story.build([ev("tool.result", {"tool": "t", "result":
                                               {"status": "unpaid", "amount_eur": 249}})])[0]
        assert step.detail == "status: unpaid, amount eur: 249"
        assert "{" not in step.detail

    def test_a_lookup_hit_reads_as_found(self):
        step = story.build([ev("tool.result", {"tool": "t", "result":
                                               {"found": True, "value": "Rotterdam"}})])[0]
        assert step.detail == "Rotterdam" and step.tone == "good"

    def test_a_lookup_miss_is_a_warning_not_a_failure(self):
        step = story.build([ev("tool.result", {"tool": "t", "result":
                                               {"found": False, "message": "not in the list"}})])[0]
        assert step.tone == "warn"

    def test_a_failed_api_call_reads_as_bad(self):
        step = story.build([ev("tool.result", {"tool": "t", "result":
                                               {"ok": False, "error": "404"}})])[0]
        assert step.tone == "bad"

    def test_progress_logs_are_not_separate_steps(self):
        events = [ev("tool.result", {"tool": "t", "partial": True, "log": "working"})]
        assert story.build(events) == []

    def test_an_approval_says_what_it_wants_to_do(self):
        step = story.build([ev("approval.requested", {
            "tool": "refund", "routed_to": "slack",
            "arguments": {"amount_eur": 249}})])[0]
        assert "needs approval" in step.headline and "refund" in step.headline
        assert "amount eur: 249" in step.detail and "slack" in step.detail
        assert step.tone == "warn"

    def test_a_resolution_names_the_person(self):
        step = story.build([ev("approval.resolved", {"decision": "approved",
                                                     "resolver": "ralph"})])[0]
        assert step.headline == "Ralph approved it" and step.tone == "good"

    def test_a_refusal_reads_as_refused(self):
        step = story.build([ev("approval.resolved", {"decision": "denied",
                                                     "resolver": "ralph"})])[0]
        assert "refused" in step.headline and step.tone == "bad"

    def test_a_scheduled_start_says_why(self):
        step = story.build([ev("trigger.fired", {"kind": "schedule",
                                                 "reason": "cron 0 8 * * 1-5"})])[0]
        assert step.headline == "Started automatically" and "cron" in step.detail

    def test_a_short_turn_does_not_claim_zero_seconds(self):
        step = story.build([ev("turn.completed", {"status": "ok", "duration_ms": 4})])[0]
        assert step.detail == "Took under a second."

    def test_a_longer_turn_reports_seconds(self):
        step = story.build([ev("turn.completed", {"status": "ok", "duration_ms": 3400})])[0]
        assert "3.4 seconds" in step.detail


class TestErrorsInPlainLanguage:
    def test_a_provider_failure_is_explained_not_dumped(self):
        step = story.build([ev("error.raised", {"kind": "provider_error",
                                                "message": "401 Unauthorized",
                                                "trace": "Traceback…"})])[0]
        assert step.headline == "Couldn't reach the AI model"
        assert "API key" in step.detail
        assert "Traceback" not in step.detail

    def test_a_policy_denial_says_a_rule_stopped_it(self):
        step = story.build([ev("error.raised", {"kind": "policy_denied",
                                                "tool": "refund"})])[0]
        assert "rule" in step.headline.lower() and "refund" in step.detail

    def test_a_loop_is_explained_with_what_to_do(self):
        step = story.build([ev("error.raised", {"kind": "iteration_limit"})])[0]
        assert "circles" in step.headline and "instructions" in step.detail

    def test_an_unknown_error_kind_still_reads_as_a_sentence(self):
        step = story.build([ev("error.raised", {"kind": "something_new",
                                                "message": "the details"})])[0]
        assert step.headline == "Something went wrong"
        assert step.tone == "bad"

    def test_every_error_reading_names_a_real_error_kind(self):
        """Guards against the readings drifting away from what the engine emits."""
        emitted = {
            "provider_error", "model_call_failed", "tool_failed", "policy_denied",
            "iteration_limit", "approval_routing_failed", "outbound_delivery_failed",
        }
        assert set(story.ERROR_READINGS) == emitted


class TestHeadline:
    def _session(self, status):
        return {"status": status}

    def test_a_waiting_session_says_what_it_waits_for(self):
        steps = story.build([ev("approval.requested", {"tool": "refund"})])
        assert "refund" in story.headline(self._session("waiting-approval"), steps)

    def test_a_finished_session_names_the_tools_it_used(self):
        steps = story.build([
            ev("tool.result", {"tool": "lookup_invoice", "result": {}}),
            ev("turn.completed", {"status": "ok"}),
        ])
        assert "lookup invoice" in story.headline(self._session("ended"), steps)

    def test_a_toolless_answer_says_so(self):
        steps = story.build([ev("message.sent", {"text": "hi"})])
        assert "without needing any tools" in story.headline(self._session("ended"), steps)

    def test_a_broken_session_leads_with_the_problem(self):
        steps = story.build([ev("error.raised", {"kind": "provider_error"})])
        assert story.headline(self._session("error"), steps) == "Couldn't reach the AI model"

    def test_a_running_session_says_it_is_working(self):
        assert story.headline(self._session("running"), []) == "Working on it right now"


class TestAgainstRealTurns:
    def test_a_real_gated_turn_reads_as_a_story(self, store, agent, worker, registry):
        from heddled import runtime

        result = runtime.submit_message(
            "support", "refund invoice F-2231 for 249 eur", sync=True, timeout_s=20)
        approval = store.pending_approvals()[0]
        runtime.resolve_approval(approval["id"], "approved", resolver="ralph")
        runtime.wait_for_turn(result["turn_id"], timeout_s=20)

        steps = story.build(store.events_for_session(result["session_id"]))
        headlines = [s.headline for s in steps]

        assert headlines[0] == "You asked"
        assert any("needs approval" in h for h in headlines)
        assert "Ralph approved it" in headlines
        assert any(h.startswith("Used refund") for h in headlines)
        assert "Finished" in headlines
        # And none of the plumbing leaked in.
        assert not any("context" in h or "model" in h for h in headlines)

    def test_no_step_is_ever_blank(self, store, agent, worker):
        from heddled import runtime

        result = runtime.submit_message("support", "where is invoice F-2231?",
                                        sync=True, timeout_s=20)
        for step in story.build(store.events_for_session(result["session_id"])):
            assert step.headline.strip()
