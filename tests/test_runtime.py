"""End-to-end through the real background worker.

This is where the concept's Phase 1 done-when is pinned: an agent with a
`requires_approval` tool runs, a human approves through the adapter path, and
the turn resumes — all without the console. Plus delegation (§12, inside) and
the CLI surface from §6.
"""

import json
import time

import pytest

from heddled import runtime


def wait_until(predicate, timeout=15, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestTurnsSurviveTheirRequest:
    def test_a_queued_turn_is_executed_by_the_worker(self, store, registry, worker):
        result = runtime.submit_message("support", "just say hello", sync=True, timeout_s=15)
        assert result["status"] == "completed" and result["reply"]

    def test_an_unknown_agent_raises(self, store, registry, worker):
        with pytest.raises(runtime.AgentNotFound):
            runtime.submit_message("nope", "hi")

    def test_an_async_submit_returns_immediately_then_settles(self, store, registry, worker):
        result = runtime.submit_message("support", "just say hello")
        assert result["status"] == "queued"
        assert wait_until(lambda: store.get_session(result["session_id"])["status"] == "ended")

    def test_a_second_message_continues_the_same_session(self, store, registry, worker):
        first = runtime.submit_message("support", "just say hello", sync=True, timeout_s=15)
        second = runtime.submit_message("support", "and again",
                                        session_id=first["session_id"], sync=True, timeout_s=15)
        assert second["session_id"] == first["session_id"]
        received = [e for e in store.events_for_session(first["session_id"])
                    if e.type == "message.received"]
        assert len(received) == 2

    def test_a_failing_job_is_retried_rather_than_dropped(self, store, registry, worker):
        # The full give-up schedule is covered in test_worker.py, which shortens
        # the backoff; here we only need to see the retry happen at all.
        store.enqueue("nonexistent_job_kind", {})
        assert wait_until(
            lambda: store.one(
                "SELECT * FROM jobs WHERE kind='nonexistent_job_kind'")["attempts"] >= 1)


class TestApprovalRoundTrip:
    """Phase 1's done-when, end to end."""

    def test_the_full_pause_approve_resume_cycle(self, store, registry, worker):
        result = runtime.submit_message(
            "support", "refund invoice F-2231 for 249 eur", sync=True, timeout_s=15)
        assert result["status"] == "waiting-approval"

        approval = store.pending_approvals()[0]
        assert approval["tool"] == "refund"

        resolved = runtime.resolve_approval(approval["id"], "approved", resolver="ralph")
        assert resolved["resumed"] is True

        assert wait_until(
            lambda: store.get_session(result["session_id"])["status"] == "ended")

        types = [e.type for e in store.events_for_session(result["session_id"])]
        assert types.index("approval.requested") < types.index("approval.resolved")
        assert "turn.completed" in types
        refunds = [e for e in store.events_for_session(result["session_id"])
                   if e.type == "tool.called" and e.payload["tool"] == "refund"]
        assert len(refunds) == 1

    def test_denial_resumes_without_running_the_tool(self, store, registry, worker):
        result = runtime.submit_message(
            "support", "refund invoice F-2231 for 249 eur", sync=True, timeout_s=15)
        approval = store.pending_approvals()[0]
        runtime.resolve_approval(approval["id"], "denied", resolver="ralph",
                                 note="not this time")
        assert wait_until(
            lambda: store.get_session(result["session_id"])["status"] == "ended")
        assert not [e for e in store.events_for_session(result["session_id"])
                    if e.type == "tool.called" and e.payload["tool"] == "refund"]

    def test_a_bad_token_is_refused(self, store, registry, worker):
        runtime.submit_message("support", "refund invoice F-2231 for 249 eur",
                               sync=True, timeout_s=15)
        approval = store.pending_approvals()[0]
        with pytest.raises(PermissionError):
            runtime.resolve_approval(approval["id"], "approved", resolver="x", token="wrong")

    def test_resolving_an_unknown_approval_raises(self, store, registry, worker):
        with pytest.raises(LookupError):
            runtime.resolve_approval("a_nope", "approved", resolver="x")

    def test_the_paused_turn_is_not_reported_as_settled_twice(self, store, registry, worker):
        """Regression: the session must leave 'waiting-approval' before the
        resume job is enqueued, or a waiting caller reads a stale status."""
        result = runtime.submit_message(
            "support", "refund invoice F-2231 for 249 eur", sync=True, timeout_s=15)
        approval = store.pending_approvals()[0]
        runtime.resolve_approval(approval["id"], "approved", resolver="ralph")
        final = runtime.wait_for_turn(result["turn_id"], timeout_s=15)
        assert final["status"] == "completed"
        assert "R-TEST" in final["reply"]


class TestTriggerFiring:
    def test_a_fired_trigger_starts_a_traceable_session(self, store, registry, worker):
        runtime.fire_trigger("support", kind="schedule",
                             message="Summarize overnight invoices.",
                             reason="cron 0 8 * * 1-5 at 2026-08-03T08:00")
        sid = store.list_sessions(channel="schedule")[0]["id"]
        assert wait_until(lambda: store.get_session(sid)["status"] in ("ended", "error"))

        first = store.events_for_session(sid)[0]
        assert first.type == "trigger.fired"
        assert first.payload["kind"] == "schedule"
        assert "cron" in first.payload["reason"]
        assert json.loads(store.get_session(sid)["trigger_origin"])["kind"] == "schedule"


class TestOperatorInjection:
    def test_injection_resumes_the_session_with_the_note(self, store, registry, worker):
        first = runtime.submit_message("support", "just say hello", sync=True, timeout_s=15)
        sid = first["session_id"]
        out = runtime.inject_operator_message(sid, "also check the credit note",
                                              operator="ralph")
        assert out["resumed"] is True
        assert wait_until(lambda: store.get_session(sid)["status"] in ("ended", "error"))
        assert any(e.type == "operator.injected" for e in store.events_for_session(sid))

    def test_injecting_into_an_unknown_session_raises(self, store, registry, worker):
        with pytest.raises(LookupError):
            runtime.inject_operator_message("s_nope", "hi")


class TestDelegation:
    """Inside the walls, delegation is a tool.called whose handler is another
    agent's turn engine (§12)."""

    def _make_router(self, project, tools="['agent:support']"):
        (project / "agents" / "router.yaml").write_text(
            "name: router\n"
            "description: Routes to specialists.\n"
            "model: mock/echo\n"
            f"adapters:\n  channels: [webchat]\n  tools: {tools}\n"
        )

    def test_a_sub_session_links_to_its_parent(self, store, registry, worker, project):
        self._make_router(project)
        result = runtime.submit_message("router", "ask support about invoice F-2231",
                                        sync=True, timeout_s=20)
        children = store.query("SELECT * FROM sessions WHERE parent_session_id=?",
                               (result["session_id"],))
        assert len(children) == 1
        assert children[0]["agent"] == "support"
        assert json.loads(children[0]["call_chain"]) == ["router"]

    def test_the_delegated_reply_comes_back_as_a_tool_result(self, store, registry,
                                                             worker, project):
        self._make_router(project)
        result = runtime.submit_message("router", "ask support about invoice F-2231",
                                        sync=True, timeout_s=20)
        results = [e for e in store.events_for_session(result["session_id"])
                   if e.type == "tool.result" and not e.payload.get("partial")]
        assert results and "reply" in results[0].payload["result"]

    def test_a_delegation_cycle_is_refused(self, store, registry, project, agent):
        from heddled.subagent import make_agent_tool_handler

        sid = store.create_session(agent="support", agent_version="v1", channel="webchat",
                                   call_chain=["support"])

        class Ctx:
            session_id = sid
            agent = "support"

        out = make_agent_tool_handler("support")({"message": "loop forever"}, Ctx())
        assert "cycle detected" in out["error"]

    def test_the_call_depth_limit_is_enforced(self, store, registry, project, monkeypatch):
        from heddled import config
        from heddled.subagent import make_agent_tool_handler

        monkeypatch.setattr(config, "MAX_CALL_DEPTH", 2)
        sid = store.create_session(agent="a", agent_version="v1", channel="webchat",
                                   call_chain=["x", "y", "z"])

        class Ctx:
            session_id = sid
            agent = "a"

        out = make_agent_tool_handler("support")({"message": "go"}, Ctx())
        assert "depth limit" in out["error"]


class TestHealth:
    def test_worker_alive_flips_once_the_worker_beats(self, store, registry, worker):
        assert wait_until(lambda: runtime.platform_health()["worker_alive"] is True)


class TestCli:
    """The dev loop is the heart of 'easy' (§6)."""

    def test_agents_command_lists_the_agent(self, store, registry, capsys):
        from heddled.cli import main

        assert main(["agents"]) == 0
        assert "support" in capsys.readouterr().out

    def test_tool_list_shows_fields(self, store, registry, capsys):
        from heddled.cli import main

        assert main(["tool", "list"]) == 0
        assert "invoice_number" in capsys.readouterr().out

    def test_tool_test_runs_a_tool_in_isolation(self, store, registry, capsys):
        from heddled.cli import main

        code = main(["tool", "test", "lookup_invoice", "--args",
                     '{"invoice_number":"F-2231"}'])
        assert code == 0
        assert "unpaid" in capsys.readouterr().out

    def test_tool_test_reports_a_failure_with_a_nonzero_exit(self, store, registry, capsys):
        from heddled.cli import main

        assert main(["tool", "test", "lookup_invoice", "--args", "{}"]) == 1

    def test_trace_of_an_unknown_session_exits_nonzero(self, store, registry, capsys):
        from heddled.cli import main

        assert main(["trace", "s_nope"]) == 1

    def test_sessions_command_lists_sessions(self, store, registry, worker, capsys):
        from heddled.cli import main

        runtime.submit_message("support", "just say hello", sync=True, timeout_s=15)
        assert main(["sessions", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)

    def test_deploy_to_prod_is_refused_without_a_green_eval(self, store, registry, capsys):
        from heddled.cli import main

        assert main(["deploy", "support", "prod"]) == 1
        assert "green eval run" in capsys.readouterr().err

    def test_deploy_to_dev_is_allowed(self, store, registry, capsys):
        from heddled.cli import main

        assert main(["deploy", "support", "dev"]) == 0
        assert store.deployment("support", "dev")

    def test_init_scaffolds_a_runnable_project(self, tmp_path, store, registry, capsys):
        from heddled.cli import main

        assert main(["init", str(tmp_path / "fresh")]) == 0
        assert (tmp_path / "fresh" / "agents" / "assistant.yaml").exists()
        assert (tmp_path / "fresh" / "tools" / "echo" / "handler.py").exists()
