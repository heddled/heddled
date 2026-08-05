"""Caller identity (§12).

Governance travels with the agent: an external orchestrator driving a Heddled
agent is still subject to *your* policies, and a policy can key on *which*
orchestrator it is.
"""

import json

from heddled import policies, runtime


class TestCallerPolicies:
    def test_an_allowed_caller_may_use_the_tool(self, agent, store):
        agent.policies = [{"tool": "refund", "allow_callers": ["copilot-studio"]}]
        d = policies.check_tool_call(agent, "refund", "mcp", store, "s_1",
                                     caller="copilot-studio")
        assert d.allowed

    def test_an_unlisted_caller_is_refused(self, agent, store):
        agent.policies = [{"tool": "refund", "allow_callers": ["copilot-studio"]}]
        d = policies.check_tool_call(agent, "refund", "mcp", store, "s_1", caller="stranger")
        assert not d.allowed and "stranger" in d.reason

    def test_an_anonymous_caller_is_refused_when_a_list_is_set(self, agent, store):
        agent.policies = [{"tool": "refund", "allow_callers": ["copilot-studio"]}]
        d = policies.check_tool_call(agent, "refund", "mcp", store, "s_1")
        assert not d.allowed and "anonymous" in d.reason

    def test_a_denied_caller_is_refused(self, agent, store):
        agent.policies = [{"tool": "refund", "deny_callers": ["untrusted"]}]
        assert not policies.check_tool_call(agent, "refund", "mcp", store, "s_1",
                                            caller="untrusted").allowed
        assert policies.check_tool_call(agent, "refund", "mcp", store, "s_1",
                                        caller="trusted").allowed

    def test_no_caller_policy_means_callers_are_irrelevant(self, agent, store):
        assert policies.check_tool_call(agent, "lookup_invoice", "mcp", store, "s_1",
                                        caller="anyone").allowed


class TestCallerScopedApproval:
    """`approval_callers` gates only the listed callers — an internal channel
    runs straight through while an external orchestrator pauses for a human."""

    def test_a_listed_caller_is_gated(self, agent, store):
        agent.policies = [{"tool": "refund", "approval_callers": ["copilot-studio"]}]
        d = policies.check_tool_call(agent, "refund", "mcp", store, "s_1",
                                     caller="copilot-studio")
        assert d.requires_approval and "copilot-studio" in d.reason

    def test_an_unlisted_caller_runs_without_a_gate(self, agent, store):
        agent.policies = [{"tool": "refund", "approval_callers": ["copilot-studio"]}]
        d = policies.check_tool_call(agent, "refund", "webchat", store, "s_1", caller=None)
        assert d.allowed and not d.requires_approval

    def test_plain_requires_approval_still_gates_everyone(self, agent, store):
        d = policies.check_tool_call(agent, "refund", "mcp", store, "s_1", caller="anyone")
        assert d.requires_approval


class TestMcpAuthentication:
    def test_a_credential_is_required_once_accounts_exist(self, client):
        """Heddled used to leave this open; that became the way around the console
        sign-in the moment accounts were introduced."""
        r = client.post("/mcp/support", json={"jsonrpc": "2.0", "id": 1,
                                              "method": "tools/list"},
                        headers={"X-Heddled-Caller": "anybody"})
        assert r.status_code == 401
        assert b"credential" in r.data

    def test_a_shared_key_still_works(self, client, store):
        store.set_setting("mcp_api_key", "secret")
        assert client.post("/mcp/support", json={"method": "tools/list"}).status_code == 401
        assert client.post("/mcp/support", json={"jsonrpc": "2.0", "id": 1,
                                                 "method": "tools/list"},
                           headers={"Authorization": "Bearer secret"}).status_code == 200

    def test_per_caller_keys_authorize_each_caller(self, client, store):
        store.set_setting("mcp_callers", {"key-a": "copilot-studio", "key-b": "claude"})
        for key in ("key-a", "key-b"):
            r = client.post("/mcp/support", json={"jsonrpc": "2.0", "id": 1,
                                                  "method": "tools/list"},
                            headers={"Authorization": f"Bearer {key}"})
            assert r.status_code == 200

    def test_an_unknown_key_is_refused(self, client, store):
        store.set_setting("mcp_callers", {"key-a": "copilot-studio"})
        assert client.post("/mcp/support", json={"method": "tools/list"},
                           headers={"Authorization": "Bearer key-zzz"}).status_code == 401

    def test_the_key_names_the_caller_not_the_header(self, client, store, worker):
        """A caller must not be able to impersonate another by setting a header."""
        store.set_setting("mcp_callers", {"key-a": "copilot-studio"})
        r = client.post("/mcp/support", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ask_support", "arguments": {"message": "just say hello"}}},
            headers={"Authorization": "Bearer key-a", "X-Heddled-Caller": "pretending-to-be-admin"})
        sid = r.get_json()["result"]["structuredContent"]["session_id"]
        origin = json.loads(store.get_session(sid)["trigger_origin"])
        assert origin["caller"] == "copilot-studio"


class TestCallerReachesThePolicy:
    def test_an_external_caller_is_denied_end_to_end(self, store, registry, worker, project):
        """The whole path: MCP credential → session origin → engine → policy."""
        from heddled import authoring

        authoring.remove_policy("support", "refund")
        authoring.add_policy("support", {"tool": "refund",
                                         "deny_callers": ["copilot-studio"]})

        result = runtime.submit_message(
            "support", "refund invoice F-2231 for 249 eur", channel="mcp",
            caller="copilot-studio", sync=True, timeout_s=20)

        assert result["status"] == "completed"
        denials = [e for e in store.events_for_session(result["session_id"])
                   if e.type == "error.raised" and e.payload.get("kind") == "policy_denied"]
        assert denials and "copilot-studio" in denials[0].payload["message"]
        assert not [e for e in store.events_for_session(result["session_id"])
                    if e.type == "tool.called" and e.payload["tool"] == "refund"]

    def test_a_permitted_caller_runs_the_tool(self, store, registry, worker, project):
        from heddled import authoring

        authoring.remove_policy("support", "refund")
        authoring.add_policy("support", {"tool": "refund",
                                         "allow_callers": ["copilot-studio"]})

        result = runtime.submit_message(
            "support", "refund invoice F-2231 for 249 eur", channel="mcp",
            caller="copilot-studio", sync=True, timeout_s=20)
        assert result["status"] == "completed"
        assert [e for e in store.events_for_session(result["session_id"])
                if e.type == "tool.called" and e.payload["tool"] == "refund"]

    def test_the_caller_survives_an_approval_pause_and_resume(self, store, registry,
                                                              worker, project):
        """A turn resumed days later must evaluate policies against the same
        caller, so the identity lives on the session rather than the request."""
        path = project / "agents" / "support.yaml"
        path.write_text(path.read_text().replace(
            "requires_approval: true", "approval_callers: [copilot-studio]"))

        result = runtime.submit_message(
            "support", "refund invoice F-2231 for 249 eur", channel="mcp",
            caller="copilot-studio", sync=True, timeout_s=20)
        assert result["status"] == "waiting-approval"

        approval = store.pending_approvals()[0]
        assert "copilot-studio" in approval["reason"]
        runtime.resolve_approval(approval["id"], "approved", resolver="ralph")
        final = runtime.wait_for_turn(result["turn_id"], timeout_s=20)
        assert final["status"] == "completed" and "R-TEST" in final["reply"]
