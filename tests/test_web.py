"""The console, the JSON API, the SSE trace stream and the MCP server.

Everything deep-linkable, server-rendered, no build step (decision 8).
"""

import json

import pytest


class TestConsoleScreens:
    def test_the_five_top_level_screens_render(self, client):
        for path in ("/", "/sessions", "/evals", "/deployments", "/settings"):
            assert client.get(path).status_code == 200, path

    def test_the_home_page_lists_agents(self, client):
        assert b"support" in client.get("/").data

    def test_agent_detail_renders_with_its_raw_definition(self, client):
        body = client.get("/agents/support").data
        assert b"support" in body and b"lookup_invoice" in body

    def test_the_test_tab_renders(self, client):
        assert client.get("/agents/support/test").status_code == 200

    def test_an_unknown_agent_is_a_404_page(self, client):
        assert client.get("/agents/nope").status_code == 404

    def test_an_unknown_session_is_a_404_page(self, client):
        assert client.get("/sessions/s_nope").status_code == 404

    def test_a_session_is_deep_linkable(self, client, store):
        from heddled.events import Event

        sid = store.create_session(agent="support", agent_version="v1", channel="webchat")
        store.append(Event(type="message.received", session_id=sid, payload={"text": "hi"}))
        assert client.get(f"/sessions/{sid}").status_code == 200

    def test_editing_a_definition_writes_the_file(self, client, project):
        text = (project / "agents" / "support.yaml").read_text().replace(
            "mock/echo", "mock/other")
        resp = client.post("/agents/support/definition", data={"definition": text})
        assert resp.status_code == 302
        assert "mock/other" in (project / "agents" / "support.yaml").read_text()

    def test_invalid_yaml_is_rejected_with_an_error_page(self, client, project):
        before = (project / "agents" / "support.yaml").read_text()
        resp = client.post("/agents/support/definition", data={"definition": "a:\n  b: [\n"})
        assert resp.status_code == 400
        assert (project / "agents" / "support.yaml").read_text() == before


class TestJsonApi:
    def test_health_reports_the_admin_strip_numbers(self, client):
        h = client.get("/api/health").get_json()
        assert {"queue_depth", "errors_last_hour", "worker_alive"} <= set(h)

    def test_agents_endpoint_describes_the_agent(self, client):
        agents = client.get("/api/agents").get_json()
        a = next(x for x in agents if x["name"] == "support")
        assert a["tools"] == ["lookup_invoice", "refund"]
        assert a["expose"] == {"mcp": True}
        assert len(a["triggers"]) == 2

    def test_tools_endpoint_exposes_schemas(self, client):
        tools = {t["name"]: t for t in client.get("/api/tools").get_json()}
        assert "invoice_number" in tools["lookup_invoice"]["input"]["properties"]

    def test_a_tool_is_testable_in_isolation(self, client):
        r = client.post("/api/tools/lookup_invoice/test",
                        json={"args": {"invoice_number": "F-2231"}})
        body = r.get_json()
        assert body["ok"] and body["result"]["status"] == "unpaid"
        assert "looking up F-2231" in body["logs"]

    def test_a_tool_test_reports_failure_rather_than_raising(self, client):
        body = client.post("/api/tools/lookup_invoice/test", json={"args": {}}).get_json()
        assert body["ok"] is False

    def test_an_unknown_tool_is_404(self, client):
        assert client.post("/api/tools/nope/test", json={"args": {}}).status_code == 404

    def test_posting_a_message_enqueues_a_turn(self, client, store):
        body = client.post("/api/agents/support/messages", json={"text": "hello"}).get_json()
        assert body["status"] == "queued" and body["session_id"]
        assert store.health()["queue_depth"] == 1

    def test_an_empty_message_is_rejected(self, client):
        assert client.post("/api/agents/support/messages", json={"text": "  "}).status_code == 400

    def test_messaging_an_unknown_agent_is_404(self, client):
        assert client.post("/api/agents/nope/messages", json={"text": "hi"}).status_code == 404

    def test_the_inbound_webhook_accepts_a_push_trigger(self, client, store):
        r = client.post("/api/agents/support/webhook", json={"text": "invoice F-1"})
        assert r.status_code == 202
        assert store.get_session(r.get_json()["session_id"])["channel"] == "webhook"

    def test_sessions_and_events_are_queryable(self, client, store):
        from heddled.events import Event

        sid = store.create_session(agent="support", agent_version="v1", channel="webchat")
        store.append(Event(type="message.received", session_id=sid, payload={"text": "hi"}))
        assert client.get("/api/sessions").get_json()
        events = client.get(f"/api/sessions/{sid}/events").get_json()
        assert events[0]["type"] == "message.received"

    def test_triggers_endpoint_reports_cursors(self, client):
        rows = client.get("/api/triggers").get_json()
        assert {r["kind"] for r in rows} == {"schedule", "poll"}


class TestApprovalsOverHttp:
    def _pending(self, store, agent):
        from heddled.engine import TurnEngine
        from heddled.events import new_id

        sid = store.create_session(agent=agent.name, agent_version=agent.version,
                                   channel="webchat")
        TurnEngine(store, agent, sid, new_id("t")).run("refund invoice F-2231 for 249 eur")
        return store.pending_approvals()[0]

    def test_pending_approvals_are_listed(self, client, store, agent):
        self._pending(store, agent)
        assert client.get("/api/approvals").get_json()[0]["tool"] == "refund"

    def test_the_approve_page_renders_the_proposed_action(self, client, store, agent):
        a = self._pending(store, agent)
        body = client.get(f"/approve/{a['id']}?token={a['token']}").data
        assert b"refund" in body and b"F-2231" in body

    def test_the_page_cannot_be_read_without_the_token(self, client, store, agent):
        """It shows the tool and its arguments — routinely customer data."""
        a = self._pending(store, agent)
        assert client.get(f"/approve/{a['id']}").status_code == 404

    def test_a_signed_link_resolves_the_approval(self, client, store, agent):
        a = self._pending(store, agent)
        r = client.get(f"/approve/{a['id']}?token={a['token']}&decision=approved")
        assert r.status_code == 200
        assert store.get_approval(a["id"])["status"] == "approved"

    def test_a_wrong_token_is_refused(self, client, store, agent):
        a = self._pending(store, agent)
        # 404 rather than 403: a wrong token should not confirm that the
        # approval exists at all.
        assert client.get(
            f"/approve/{a['id']}?token=wrong&decision=approved").status_code == 404
        assert store.get_approval(a["id"])["status"] == "pending"

    def test_resolving_twice_is_idempotent(self, client, store, agent):
        a = self._pending(store, agent)
        client.post(f"/api/approvals/{a['id']}", json={"decision": "approved",
                                                       "token": a["token"]})
        second = client.post(f"/api/approvals/{a['id']}",
                             json={"decision": "denied", "token": a["token"]}).get_json()
        assert second["already_resolved"] is True
        assert store.get_approval(a["id"])["status"] == "approved"

    def test_a_nonsense_decision_is_rejected(self, client, store, agent):
        a = self._pending(store, agent)
        r = client.post(f"/api/approvals/{a['id']}", json={"decision": "maybe",
                                                           "token": a["token"]})
        assert r.status_code == 400

    def test_approval_resolved_lands_on_the_spine(self, client, store, agent):
        a = self._pending(store, agent)
        client.post(f"/api/approvals/{a['id']}", json={"decision": "approved",
                                                       "token": a["token"],
                                                       "resolver": "ralph"})
        resolved = [e for e in store.events_for_session(a["session_id"])
                    if e.type == "approval.resolved"]
        assert resolved and resolved[0].payload["resolver"] == "ralph"


class TestOperatorTakeover:
    def test_injecting_a_message_puts_it_on_the_spine(self, client, store, agent):
        sid = store.create_session(agent="support", agent_version=agent.version,
                                   channel="webchat")
        r = client.post(f"/api/sessions/{sid}/inject",
                        json={"text": "check the credit note too", "resume": False})
        assert r.get_json()["injected"] is True
        injected = [e for e in store.events_for_session(sid) if e.type == "operator.injected"]
        assert injected and injected[0].payload["text"] == "check the credit note too"

    def test_injecting_into_an_unknown_session_is_404(self, client):
        assert client.post("/api/sessions/s_nope/inject", json={"text": "x"}).status_code == 404


class TestSse:
    def test_the_session_stream_replays_existing_events(self, client, store):
        from heddled.events import Event

        sid = store.create_session(agent="support", agent_version="v1", channel="webchat")
        store.append(Event(type="message.received", session_id=sid, payload={"text": "hi"}))

        resp = client.get(f"/sessions/{sid}/stream")
        assert resp.mimetype == "text/event-stream"
        chunk = next(resp.response).decode()
        assert "event: message.received" in chunk
        data = json.loads(chunk.split("data: ", 1)[1])
        assert data["summary"] == "hi" and data["css"] == "ev-in"
        resp.close()


class TestMcpServer:
    @pytest.fixture(autouse=True)
    def _key(self, mcp_key):
        self.headers = mcp_key

    def test_the_descriptor_advertises_the_agent(self, client):
        d = client.get("/mcp/support", headers=self.headers).get_json()
        assert d["name"] == "heddled/support"
        assert {t["name"] for t in d["tools"]} == {"ask_support", "continue_support"}

    def test_initialize_returns_server_info(self, client):
        r = client.post("/mcp/support", json={"jsonrpc": "2.0", "id": 1,
                                              "method": "initialize"}, headers=self.headers).get_json()
        assert r["result"]["serverInfo"]["name"] == "heddled/support"

    def test_tools_list_carries_the_input_schema(self, client):
        r = client.post("/mcp/support", json={"jsonrpc": "2.0", "id": 1,
                                              "method": "tools/list"}, headers=self.headers).get_json()
        ask = next(t for t in r["result"]["tools"] if t["name"] == "ask_support")
        assert ask["inputSchema"]["required"] == ["message"]
        assert "session_id" in ask["inputSchema"]["properties"]

    def test_an_agent_that_does_not_expose_mcp_is_404(self, client, project):
        (project / "agents" / "private.yaml").write_text("name: private\nmodel: mock/echo\n")
        assert client.post("/mcp/private", json={"method": "tools/list"}).status_code == 404

    def test_an_unknown_method_returns_a_jsonrpc_error(self, client):
        r = client.post("/mcp/support", json={"jsonrpc": "2.0", "id": 9,
                                              "method": "telepathy/read"}, headers=self.headers).get_json()
        assert r["error"]["code"] == -32601

    def test_an_unknown_tool_is_an_error_result(self, client):
        r = client.post("/mcp/support", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "nope", "arguments": {}}}, headers=self.headers).get_json()
        assert r["result"]["isError"] is True

    def test_an_api_key_is_enforced_when_configured(self, client, store):
        store.execute("DELETE FROM settings WHERE key='mcp_callers'")
        store.set_setting("mcp_api_key", "secret")
        assert client.post("/mcp/support", json={"method": "tools/list"}, headers=self.headers).status_code == 401
        ok = client.post("/mcp/support", json={"jsonrpc": "2.0", "id": 1,
                                               "method": "tools/list"},
                         headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200

    def test_a_call_becomes_a_traceable_session(self, client, store, worker):
        r = client.post("/mcp/support", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ask_support",
                       "arguments": {"message": "where is invoice F-2231?"}}},
            headers={**self.headers, "X-Heddled-Caller": "copilot-studio"})
        body = r.get_json()["result"]
        sid = body["structuredContent"]["session_id"]

        assert body["structuredContent"]["status"] == "completed"
        assert "unpaid" in body["content"][0]["text"]
        session = store.get_session(sid)
        assert session["channel"] == "mcp"
        # The credential names the caller; a header cannot override it.
        assert json.loads(session["trigger_origin"])["via"] == ["test-caller"]

    def test_an_external_call_still_pauses_for_approval(self, client, store, worker):
        """Governance travels with the agent (§12): even when a foreign
        orchestrator drives it, the approval gate still applies."""
        r = client.post("/mcp/support", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ask_support",
                       "arguments": {"message": "refund invoice F-2231 for 249 eur",
                                     "timeout_s": 20}}}, headers=self.headers).get_json()
        payload = json.loads(r["result"]["content"][0]["text"])
        assert payload["status"] == "pending"
        assert payload["continue_with"] == "continue_support"
        assert store.pending_approvals()[0]["tool"] == "refund"
