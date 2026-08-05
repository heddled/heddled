"""The turn engine. Deliberately boring — receive → build context → call model →
execute tools → repeat → reply — so these tests mostly pin down the event
sequence, because the trace is the product."""

import pytest

from heddled import config
from heddled.engine import COMPLETED, FAILED, PAUSED, TurnEngine
from heddled.events import new_id


def run_turn(store, agent, text, channel="webchat", **kw):
    sid = store.create_session(agent=agent.name, agent_version=agent.version,
                               channel=channel, trigger_origin={"kind": channel})
    engine = TurnEngine(store, agent, sid, new_id("t"), channel=channel, **kw)
    return engine.run(text), sid


def types_of(store, sid):
    return [e.type for e in store.events_for_session(sid)]


class TestSimpleTurn:
    def test_a_turn_with_no_tool_call_completes(self, store, agent):
        result, sid = run_turn(store, agent, "just say hello")
        assert result.status == COMPLETED and result.reply

    def test_the_event_sequence_is_the_documented_one(self, store, agent):
        _, sid = run_turn(store, agent, "just say hello")
        assert types_of(store, sid) == [
            "message.received", "context.built", "model.invoked",
            "model.responded", "message.sent", "turn.completed",
        ]

    def test_every_event_carries_the_identity_fields(self, store, agent):
        _, sid = run_turn(store, agent, "hello")
        for ev in store.events_for_session(sid):
            assert ev.session_id == sid
            assert ev.turn_id and ev.agent == "support" and ev.agent_version

    def test_sequence_numbers_are_monotonic(self, store, agent):
        _, sid = run_turn(store, agent, "hello")
        seqs = [e.seq for e in store.events_for_session(sid)]
        assert seqs == sorted(seqs)

    def test_context_built_captures_the_exact_model_input(self, store, agent):
        """Replay and evals depend on this (decision 4)."""
        _, sid = run_turn(store, agent, "hello")
        ctx = next(e for e in store.events_for_session(sid) if e.type == "context.built")
        assert "invoice support agent" in ctx.payload["system"]
        assert ctx.payload["messages"][-1]["content"] == "hello"
        assert {t["name"] for t in ctx.payload["tools"]} == {"lookup_invoice", "refund"}

    def test_the_session_ends_and_gets_a_title(self, store, agent):
        _, sid = run_turn(store, agent, "where is invoice F-2231?")
        row = store.get_session(sid)
        assert row["status"] == "ended" and row["title"]

    def test_usage_is_recorded_on_the_ledger(self, store, agent):
        run_turn(store, agent, "hello")
        assert store.spend_today("tokens", agent="support") > 0


class TestToolCalls:
    def test_a_matching_tool_is_called_and_its_result_lands_on_the_spine(self, store, agent):
        result, sid = run_turn(store, agent, "where is invoice F-2231?")
        assert result.status == COMPLETED
        seq = types_of(store, sid)
        assert "tool.called" in seq and "tool.result" in seq

    def test_tool_arguments_are_recorded(self, store, agent):
        _, sid = run_turn(store, agent, "where is invoice F-2231?")
        call = next(e for e in store.events_for_session(sid) if e.type == "tool.called")
        assert call.payload["arguments"]["invoice_number"] == "F-2231"

    def test_ctx_log_appears_as_a_partial_tool_result(self, store, agent):
        _, sid = run_turn(store, agent, "where is invoice F-2231?")
        partials = [e for e in store.events_for_session(sid)
                    if e.type == "tool.result" and e.payload.get("partial")]
        assert partials and "looking up" in partials[0].payload["log"]

    def test_the_answer_is_built_from_the_tool_result(self, store, agent):
        result, _ = run_turn(store, agent, "where is invoice F-2231?")
        assert "unpaid" in result.reply

    def test_a_failing_handler_raises_error_and_still_finishes_the_turn(self, store, registry,
                                                                       project):
        path = project / "agents" / "support.yaml"
        path.write_text(path.read_text().replace("[lookup_invoice, refund]", "[boom]"))
        agent = registry.get_agent("support")
        result, sid = run_turn(store, agent, "boom please")
        seq = types_of(store, sid)
        assert "error.raised" in seq
        assert result.status == COMPLETED  # the model gets the error and answers anyway
        err = next(e for e in store.events_for_session(sid) if e.type == "error.raised")
        assert err.payload["kind"] == "tool_failed"

    def test_invalid_arguments_are_rejected_before_the_handler_runs(self, store, agent):
        sid = store.create_session(agent=agent.name, agent_version=agent.version)
        engine = TurnEngine(store, agent, sid, new_id("t"))
        engine.state = {"messages": [], "pending_calls": [], "approvals": {}}
        engine._execute_tool({"id": "c1", "name": "lookup_invoice", "arguments": {}})
        result = next(e for e in store.events_for_session(sid) if e.type == "tool.result")
        assert result.payload["error"] is True
        assert "missing required field" in result.payload["result"]

    def test_an_unmounted_tool_is_refused(self, store, agent):
        sid = store.create_session(agent=agent.name, agent_version=agent.version)
        engine = TurnEngine(store, agent, sid, new_id("t"))
        engine.state = {"messages": [], "pending_calls": [], "approvals": {}}
        engine._execute_tool({"id": "c1", "name": "boom", "arguments": {}})
        result = next(e for e in store.events_for_session(sid) if e.type == "tool.result")
        assert result.payload["error"] is True and "not mounted" in result.payload["result"]


class TestApprovalPause:
    """A paused turn must survive: state is persisted, and `resume` re-enters
    the same loop from a different door."""

    def test_a_gated_tool_pauses_the_turn(self, store, agent):
        result, sid = run_turn(store, agent, "refund invoice F-2231 for 249 eur")
        assert result.status == PAUSED
        assert "approval.requested" in types_of(store, sid)
        assert "turn.completed" not in types_of(store, sid)

    def test_the_tool_does_not_run_while_paused(self, store, agent):
        _, sid = run_turn(store, agent, "refund invoice F-2231 for 249 eur")
        called = [e for e in store.events_for_session(sid)
                  if e.type == "tool.called" and e.payload["tool"] == "refund"]
        assert called == []

    def test_the_approval_records_the_proposed_action(self, store, agent):
        run_turn(store, agent, "refund invoice F-2231 for 249 eur")
        approval = store.pending_approvals()[0]
        assert approval["tool"] == "refund" and approval["token"]

    def test_session_and_turn_are_marked_waiting(self, store, agent):
        result, sid = run_turn(store, agent, "refund invoice F-2231 for 249 eur")
        assert store.get_session(sid)["status"] == "waiting-approval"
        turn = store.one("SELECT * FROM turns WHERE id=?", (result.turn_id,))
        assert turn["status"] == "waiting-approval"

    def test_approving_lets_the_resumed_turn_run_the_tool(self, store, agent):
        result, sid = run_turn(store, agent, "refund invoice F-2231 for 249 eur")
        state = store.get_state(sid)
        state["approvals"] = {state["awaiting_call_id"]: "approved"}
        store.set_state(sid, state)

        engine = TurnEngine(store, agent, sid, result.turn_id)
        resumed = engine.resume()
        assert resumed.status == COMPLETED
        calls = [e for e in store.events_for_session(sid)
                 if e.type == "tool.called" and e.payload["tool"] == "refund"]
        assert len(calls) == 1
        assert "R-TEST" in resumed.reply

    def test_denying_feeds_the_refusal_back_to_the_model(self, store, agent):
        result, sid = run_turn(store, agent, "refund invoice F-2231 for 249 eur")
        state = store.get_state(sid)
        state["approvals"] = {state["awaiting_call_id"]: "denied"}
        state["approval_notes"] = {state["awaiting_call_id"]: "not this time"}
        store.set_state(sid, state)

        resumed = TurnEngine(store, agent, sid, result.turn_id).resume()
        assert resumed.status == COMPLETED
        denied = [e for e in store.events_for_session(sid)
                  if e.type == "tool.result" and e.payload.get("error")
                  and "Denied by approver" in str(e.payload.get("result"))]
        assert denied and "not this time" in denied[0].payload["result"]
        assert not [e for e in store.events_for_session(sid)
                    if e.type == "tool.called" and e.payload["tool"] == "refund"]


class TestRedaction:
    """Redaction happens at the trace-store boundary: operate on live data,
    store the redacted form (concept §10)."""

    def test_the_iban_in_a_tool_result_never_reaches_the_store(self, store, agent):
        _, sid = run_turn(store, agent, "where is invoice F-2231?")
        blob = str([e.payload for e in store.events_for_session(sid)])
        assert "NL91ABNA0417164300" not in blob
        assert "«iban»" in blob


class TestSafetyRails:
    def test_the_iteration_limit_fails_the_turn_instead_of_looping_forever(
            self, store, agent, monkeypatch):
        monkeypatch.setattr(config, "MAX_TOOL_ITERATIONS", 0)
        sid = store.create_session(agent=agent.name, agent_version=agent.version)
        engine = TurnEngine(store, agent, sid, new_id("t"))
        engine.state = store.get_state(sid)
        engine.state["iteration"] = 5
        result = engine._loop()
        assert result.status == FAILED
        err = next(e for e in store.events_for_session(sid) if e.type == "error.raised")
        assert err.payload["kind"] == "iteration_limit"

    def test_a_provider_failure_lands_on_the_spine(self, store, agent, monkeypatch):
        from heddled import engine as engine_mod
        from heddled.providers import ProviderError

        def boom(*a, **k):
            raise ProviderError("no api key")

        monkeypatch.setattr(engine_mod, "get_provider", boom)
        result, sid = run_turn(store, agent, "hello")
        assert result.status == FAILED
        kinds = [e.payload.get("kind") for e in store.events_for_session(sid)
                 if e.type == "error.raised"]
        assert "provider_error" in kinds
        assert store.get_session(sid)["status"] == "error"


class TestSessionMemory:
    """`memory: session: auto` keeps a rolling window and summarises what falls
    out, so a long session does not grow the context forever."""

    def test_the_window_is_capped_and_older_turns_are_summarised(self, store, agent):
        sid = store.create_session(agent=agent.name, agent_version=agent.version)
        engine = TurnEngine(store, agent, sid, new_id("t"))
        engine.agent.memory = {"session": "auto", "max_messages": 4}
        engine.state = {"messages": [{"role": "user", "content": f"msg {i}"}
                                     for i in range(20)]}
        kept = engine._windowed_messages()
        assert len(kept) == 4
        assert kept[-1]["content"] == "msg 19"
        assert "msg 0" in engine.state["summary"]

    def test_memory_off_keeps_only_the_current_message(self, store, agent):
        sid = store.create_session(agent=agent.name, agent_version=agent.version)
        engine = TurnEngine(store, agent, sid, new_id("t"))
        engine.agent.memory = {"session": "none"}
        engine.state = {"messages": [{"role": "user", "content": "old"},
                                     {"role": "assistant", "content": "reply"},
                                     {"role": "user", "content": "current"}]}
        assert engine._windowed_messages() == [{"role": "user", "content": "current"}]

    def test_the_window_never_starts_on_an_orphan_tool_result(self, store, agent):
        sid = store.create_session(agent=agent.name, agent_version=agent.version)
        engine = TurnEngine(store, agent, sid, new_id("t"))
        engine.agent.memory = {"session": "auto", "max_messages": 3}
        engine.state = {"messages": [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "tool", "name": "lookup_invoice", "content": "c"},
            {"role": "tool", "name": "refund", "content": "d"},
            {"role": "assistant", "content": "e"},
        ]}
        assert engine._windowed_messages()[0]["role"] != "tool"


class TestToolMocks:
    """Eval replay plays back recorded results instead of touching the world."""

    def test_a_mocked_tool_handler_is_never_executed(self, store, agent):
        result, sid = run_turn(store, agent, "where is invoice F-2231?",
                               tool_mocks={"lookup_invoice": {"status": "MOCKED"}})
        res = next(e for e in store.events_for_session(sid)
                   if e.type == "tool.result" and not e.payload.get("partial"))
        assert res.payload["mocked"] is True
        assert res.payload["result"] == {"status": "MOCKED"}
