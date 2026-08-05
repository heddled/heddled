"""Level 3: a Python turn engine is the same object to the platform (§6).

"It still emits the same events, mounts the same adapters, obeys the same
policies, and appears in the same console. The platform can't tell the
difference, and neither can an external caller."
"""

import shutil
from pathlib import Path

import pytest

from heddled import runtime
from heddled.engine import TurnEngine

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "level3"


@pytest.fixture()
def level3(project, registry):
    """Install the shipped Level 3 example into the test project."""
    for name in ("triage_engine.py", "triage.yaml", "triage.md"):
        shutil.copy(EXAMPLE / name, project / "agents" / name)
    return registry.get_agent("triage")


class TestLoading:
    def test_the_example_agent_loads(self, level3):
        assert level3.handler == "./triage_engine.py:TriageEngine"

    def test_a_file_path_handler_resolves_to_a_class(self, level3):
        cls = runtime.load_engine_class(level3)
        assert cls.__name__ == "TriageEngine" and issubclass(cls, TurnEngine)

    def test_a_dotted_module_handler_resolves_too(self, registry, project):
        (project / "agents" / "dotted.yaml").write_text(
            "name: dotted\nmodel: mock/echo\nhandler: heddled.engine:TurnEngine\n")
        assert runtime.load_engine_class(registry.get_agent("dotted")) is TurnEngine

    def test_a_missing_file_is_reported_clearly(self, registry, project):
        (project / "agents" / "bad.yaml").write_text(
            "name: bad\nmodel: mock/echo\nhandler: ./nope.py:Thing\n")
        with pytest.raises(FileNotFoundError):
            runtime.load_engine_class(registry.get_agent("bad"))

    def test_a_missing_class_is_reported_clearly(self, level3, registry, project):
        (project / "agents" / "bad.yaml").write_text(
            "name: bad\nmodel: mock/echo\nhandler: ./triage_engine.py:NoSuchClass\n")
        with pytest.raises(AttributeError):
            runtime.load_engine_class(registry.get_agent("bad"))

    def test_a_malformed_handler_is_rejected(self, registry, project):
        (project / "agents" / "bad.yaml").write_text(
            "name: bad\nmodel: mock/echo\nhandler: justastring\n")
        with pytest.raises(ValueError):
            runtime.load_engine_class(registry.get_agent("bad"))

    def test_a_class_that_is_not_a_turn_engine_is_refused(self, registry, project):
        (project / "agents" / "engines.py").write_text("class NotAnEngine:\n    pass\n")
        (project / "agents" / "bad.yaml").write_text(
            "name: bad\nmodel: mock/echo\nhandler: ./engines.py:NotAnEngine\n")
        with pytest.raises(TypeError):
            runtime.load_engine_class(registry.get_agent("bad"))


class TestSameObjectToThePlatform:
    def test_the_custom_engine_runs_through_the_normal_door(self, store, level3, worker):
        result = runtime.submit_message("triage", "F-2231", sync=True, timeout_s=20)
        assert result["status"] == "completed"
        assert "Triage fast path" in result["reply"]

    def test_it_emits_the_same_canonical_events(self, store, level3, worker):
        result = runtime.submit_message("triage", "F-2231", sync=True, timeout_s=20)
        types = [e.type for e in store.events_for_session(result["session_id"])]
        assert types == ["message.received", "tool.called", "tool.result",
                         "tool.result", "message.sent", "turn.completed"]

    def test_the_fast_path_skips_the_model_entirely(self, store, level3, worker):
        result = runtime.submit_message("triage", "F-2231", sync=True, timeout_s=20)
        types = [e.type for e in store.events_for_session(result["session_id"])]
        assert "model.invoked" not in types

    def test_anything_else_falls_through_to_the_default_loop(self, store, level3, worker):
        result = runtime.submit_message(
            "triage", "why was invoice F-2231 not paid?", sync=True, timeout_s=20)
        types = [e.type for e in store.events_for_session(result["session_id"])]
        assert "model.invoked" in types and "context.built" in types
        assert result["status"] == "completed"

    def test_the_session_looks_normal_in_the_console(self, store, level3, worker, client):
        result = runtime.submit_message("triage", "F-2231", sync=True, timeout_s=20)
        assert client.get(f"/sessions/{result['session_id']}").status_code == 200
        row = store.get_session(result["session_id"])
        assert row["status"] == "ended" and row["title"]

    def test_policies_still_apply_to_a_custom_engine(self, store, level3, worker):
        """The trust layer is not something an engine can opt out of."""
        result = runtime.submit_message(
            "triage", "refund invoice F-2231 for 249 eur", sync=True, timeout_s=20)
        assert result["status"] == "waiting-approval"
        assert store.pending_approvals()[0]["tool"] == "refund"

    def test_a_gated_turn_still_resumes_after_approval(self, store, level3, worker):
        result = runtime.submit_message(
            "triage", "refund invoice F-2231 for 249 eur", sync=True, timeout_s=20)
        approval = store.pending_approvals()[0]
        runtime.resolve_approval(approval["id"], "approved", resolver="ralph")
        final = runtime.wait_for_turn(result["turn_id"], timeout_s=20)
        assert final["status"] == "completed" and "R-TEST" in final["reply"]

    def test_redaction_still_applies(self, store, level3, worker):
        result = runtime.submit_message("triage", "F-2231", sync=True, timeout_s=20)
        blob = str([e.payload for e in store.events_for_session(result["session_id"])])
        assert "NL91ABNA0417164300" not in blob and "«iban»" in blob

    def test_a_recorded_run_is_promotable_to_a_golden(self, store, level3, worker):
        from heddled import evals

        result = runtime.submit_message("triage", "F-2231", sync=True, timeout_s=20)
        gid = evals.promote_session(result["session_id"], "triage fast path")
        assert store.get_golden(gid)["agent"] == "triage"
