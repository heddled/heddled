"""Evals close the loop: the trace store makes regression testing nearly free
(concept §11)."""

import json

from heddled import evals
from heddled.engine import TurnEngine
from heddled.events import new_id


def record_session(store, agent, text="where is invoice F-2231?"):
    sid = store.create_session(agent=agent.name, agent_version=agent.version,
                               channel="webchat", trigger_origin={"kind": "webchat"})
    TurnEngine(store, agent, sid, new_id("t")).run(text)
    return sid


class TestAssertions:
    def test_exact(self):
        assert evals.check_assertion({"type": "exact", "value": "hello"}, " hello ")[0]
        assert not evals.check_assertion({"type": "exact", "value": "hello"}, "hello!")[0]

    def test_contains_is_case_insensitive(self):
        assert evals.check_assertion({"type": "contains", "value": "UNPAID"}, "it is unpaid")[0]

    def test_not_contains(self):
        assert evals.check_assertion({"type": "not_contains", "value": "error"}, "all good")[0]
        assert not evals.check_assertion({"type": "not_contains", "value": "error"}, "an error")[0]

    def test_regex(self):
        assert evals.check_assertion({"type": "regex", "value": r"F-\d{4}"}, "invoice F-2231")[0]
        assert not evals.check_assertion({"type": "regex", "value": r"F-\d{4}"}, "no match")[0]

    def test_similar_accepts_a_reworded_answer(self):
        a = "Invoice F-2231 is unpaid, 249 euro, due 2026-08-14"
        b = "F-2231 is unpaid: 249 euro due on 2026-08-14"
        assert evals.check_assertion({"type": "similar", "value": a, "threshold": 0.5}, b)[0]

    def test_similar_rejects_an_unrelated_answer(self):
        assert not evals.check_assertion(
            {"type": "similar", "value": "invoice unpaid 249 euro"},
            "the weather in Rotterdam is mild")[0]

    def test_judge_uses_the_mock_provider_offline(self, store):
        ok, desc = evals.check_assertion(
            {"type": "judge", "value": "The answer is helpful.", "model": "mock/echo"},
            "Invoice F-2231 is unpaid.")
        assert isinstance(ok, bool) and "judge" in desc

    def test_an_unknown_assertion_type_fails_loudly(self):
        ok, desc = evals.check_assertion({"type": "telepathy"}, "anything")
        assert not ok and "unknown assertion" in desc


class TestToolCallComparison:
    def test_identical_calls_match(self):
        calls = [{"tool": "lookup_invoice", "arguments": {"invoice_number": "F-1"}}]
        assert evals.compare_tool_calls(calls, list(calls))["match"]

    def test_argument_comparison_ignores_case_and_padding(self):
        expected = [{"tool": "lookup_invoice", "arguments": {"invoice_number": "F-1"}}]
        actual = [{"tool": "lookup_invoice", "arguments": {"invoice_number": " f-1 "}}]
        assert evals.compare_tool_calls(expected, actual)["match"]

    def test_a_different_tool_is_a_divergence(self):
        diff = evals.compare_tool_calls(
            [{"tool": "lookup_invoice", "arguments": {}}], [{"tool": "refund", "arguments": {}}])
        assert not diff["match"]
        assert diff["diffs"][0]["kind"] == "different_tool"
        assert diff["first_divergence"] == 0

    def test_different_arguments_are_a_divergence(self):
        diff = evals.compare_tool_calls(
            [{"tool": "lookup_invoice", "arguments": {"invoice_number": "F-1"}}],
            [{"tool": "lookup_invoice", "arguments": {"invoice_number": "F-2"}}])
        assert diff["diffs"][0]["kind"] == "different_arguments"

    def test_a_missing_call_is_reported(self):
        diff = evals.compare_tool_calls([{"tool": "refund", "arguments": {}}], [])
        assert diff["diffs"][0]["kind"] == "missing"

    def test_an_extra_call_is_reported(self):
        diff = evals.compare_tool_calls([], [{"tool": "refund", "arguments": {}}])
        assert diff["diffs"][0]["kind"] == "extra"

    def test_first_divergence_points_at_the_earliest_difference(self):
        expected = [{"tool": "a", "arguments": {}}, {"tool": "b", "arguments": {}},
                    {"tool": "c", "arguments": {}}]
        actual = [{"tool": "a", "arguments": {}}, {"tool": "X", "arguments": {}},
                  {"tool": "Y", "arguments": {}}]
        assert evals.compare_tool_calls(expected, actual)["first_divergence"] == 1


class TestPromotion:
    def test_a_recorded_session_becomes_a_replayable_spec(self, store, agent):
        sid = record_session(store, agent)
        spec = evals.extract_spec(sid)
        assert spec["inbound"] == ["where is invoice F-2231?"]
        assert spec["expected_tool_calls"][0]["tool"] == "lookup_invoice"
        assert "lookup_invoice" in spec["tool_results"]
        assert spec["expected_answer"]

    def test_partial_tool_logs_are_not_mistaken_for_results(self, store, agent):
        spec = evals.extract_spec(record_session(store, agent))
        assert isinstance(spec["tool_results"]["lookup_invoice"], dict)

    def test_promote_stores_a_golden(self, store, agent):
        gid = evals.promote_session(record_session(store, agent), "invoice lookup")
        golden = store.get_golden(gid)
        assert golden["name"] == "invoice lookup" and golden["agent"] == "support"

    def test_promoting_an_unknown_session_raises(self, store):
        import pytest

        with pytest.raises(LookupError):
            evals.promote_session("s_nope")


class TestEvalRuns:
    def test_an_unchanged_agent_passes_its_own_golden(self, store, registry, agent):
        evals.promote_session(record_session(store, agent), "invoice lookup")
        result = evals.execute_eval_run(
            {"run_id": store.create_eval_run("support", agent.version), "agent": "support",
             "golden_ids": None})
        assert result["cases"][0]["passed"] is True

    def test_the_replay_is_itself_a_session_on_the_spine(self, store, registry, agent):
        evals.promote_session(record_session(store, agent), "invoice lookup")
        evals.execute_eval_run({"run_id": store.create_eval_run("support", agent.version),
                                "agent": "support", "golden_ids": None})
        assert store.list_sessions(channel="eval")

    def test_tools_are_mocked_during_replay(self, store, registry, agent):
        """An eval must not touch the world."""
        evals.promote_session(record_session(store, agent), "invoice lookup")
        evals.execute_eval_run({"run_id": store.create_eval_run("support", agent.version),
                                "agent": "support", "golden_ids": None})
        eval_sid = store.list_sessions(channel="eval")[0]["id"]
        results = [e for e in store.events_for_session(eval_sid)
                   if e.type == "tool.result" and not e.payload.get("partial")]
        assert results and all(r.payload["mocked"] for r in results)

    def test_a_changed_agent_fails_the_golden(self, store, registry, agent, project):
        evals.promote_session(record_session(store, agent), "invoice lookup")
        # Unmount the tool the golden expects: the agent can no longer call it.
        path = project / "agents" / "support.yaml"
        path.write_text(path.read_text().replace("[lookup_invoice, refund]", "[refund]"))

        changed = registry.get_agent("support")
        run_id = store.create_eval_run("support", changed.version)
        result = evals.execute_eval_run({"run_id": run_id, "agent": "support",
                                         "golden_ids": None})
        case = result["cases"][0]
        assert case["passed"] is False
        assert not case["tool_diff"]["match"]

    def test_run_status_and_counts_are_recorded(self, store, registry, agent):
        evals.promote_session(record_session(store, agent), "invoice lookup")
        run_id = store.create_eval_run("support", agent.version)
        evals.execute_eval_run({"run_id": run_id, "agent": "support", "golden_ids": None})
        run = store.get_eval_run(run_id)
        assert run["status"] == "passed" and run["passed"] == 1 and run["failed"] == 0

    def test_a_case_that_explodes_is_reported_not_raised(self, store, registry, agent):
        store.add_golden("broken", "support", "s_missing", {"inbound": None})
        run_id = store.create_eval_run("support", agent.version)
        result = evals.execute_eval_run({"run_id": run_id, "agent": "support",
                                         "golden_ids": None})
        assert result["cases"][0]["passed"] is False


class TestDeploymentGate:
    def test_no_eval_run_means_not_green(self, store, registry, agent):
        green, why = evals.is_green("support", agent.version)
        assert not green and "no eval run" in why

    def test_a_passing_run_makes_the_version_green(self, store, registry, agent):
        evals.promote_session(record_session(store, agent), "invoice lookup")
        run_id = store.create_eval_run("support", agent.version)
        evals.execute_eval_run({"run_id": run_id, "agent": "support", "golden_ids": None})
        assert evals.is_green("support", agent.version)[0]

    def test_a_green_run_for_another_version_does_not_count(self, store, registry, agent):
        evals.promote_session(record_session(store, agent), "invoice lookup")
        run_id = store.create_eval_run("support", agent.version)
        evals.execute_eval_run({"run_id": run_id, "agent": "support", "golden_ids": None})
        assert not evals.is_green("support", "some-other-version")[0]

    def test_a_failing_run_is_not_green(self, store, registry, agent):
        run_id = store.create_eval_run("support", agent.version)
        store.finish_eval_run(run_id, "failed", 0, 1, {"cases": []})
        assert not evals.is_green("support", agent.version)[0]
