"""Composition and secrets from the console.

These were the capabilities that made Heddled "extremely powerful" and were
reachable only by hand-editing YAML — which puts them out of reach of exactly
the people the product is for.
"""

import pytest

from heddled import authoring
from heddled.authoring import AuthoringError


class TestSecretsCanActuallyBeCreated:
    """The tool forms tell people to write {{secret.name}} and store the value
    in Settings. Until this existed, Settings had no way to create one — the
    instruction could not be followed."""

    def test_a_secret_can_be_added(self, client, store):
        r = client.post("/settings/secrets",
                        data={"name": "billing_api_key", "value": "s3cret-value"})
        assert r.status_code == 302
        assert store.get_setting("billing_api_key") == "s3cret-value"

    def test_it_appears_on_the_settings_page_with_its_usage(self, client, store):
        client.post("/settings/secrets", data={"name": "billing_api_key", "value": "abc123456"})
        body = client.get("/settings").data.decode()
        assert "billing_api_key" in body
        assert "{{secret.billing_api_key}}" in body

    def test_the_value_is_masked_on_screen(self, client, store):
        client.post("/settings/secrets",
                    data={"name": "billing_api_key", "value": "super-secret-value"})
        assert b"super-secret-value" not in client.get("/settings").data

    def test_a_bad_name_is_refused_with_a_reason(self, client, store):
        r = client.post("/settings/secrets", data={"name": "has spaces", "value": "x"})
        assert "error=" in r.headers["Location"]
        assert store.get_setting("has spaces") is None

    def test_heddled_s_own_settings_cannot_be_overwritten(self, client, store):
        from heddled import auth

        auth.set_password(store, "a good password")
        before = store.get_setting(auth.SETTING)
        client.post("/settings/secrets", data={"name": auth.SETTING, "value": "hijack"})
        assert store.get_setting(auth.SETTING) == before

    def test_a_secret_can_be_removed(self, client, store):
        client.post("/settings/secrets", data={"name": "temp_key", "value": "x"})
        client.post("/settings/secrets", data={"remove": "temp_key"})
        assert store.get_setting("temp_key") is None

    def test_known_settings_are_not_listed_as_secrets(self, client, store):
        from heddled import auth
        from heddled.web.app import KNOWN_SETTINGS

        store.set_setting("anthropic_api_key", "sk-whatever")
        secrets = auth.user_secrets(store, [k for k, _ in KNOWN_SETTINGS])
        assert "anthropic_api_key" not in secrets

    def test_a_secret_actually_reaches_a_tool(self, project, registry, store):
        """End to end: the thing the wizard promised."""
        from heddled import tooltypes

        store.set_setting("billing_api_key", "live-key-value")
        handler = tooltypes.build_handler({"type": "text", "config": {
            "text": "key is {{secret.billing_api_key}}"}})

        class Ctx:
            settings = store.all_settings()

            def log(self, *a, **k):
                pass

        assert handler({}, Ctx())["text"] == "key is live-key-value"


class TestSavingSettingsKeepsSecrets:
    """Regression: credential fields render blank so the page never hands a
    secret back — which made a plain Save look like "clear everything"."""

    def test_saving_the_form_does_not_wipe_stored_keys(self, client, store):
        store.set_setting("anthropic_api_key", "sk-live-key")
        client.post("/settings", data={"setting_anthropic_api_key": "",
                                       "setting_judge_model": "mock/echo"})
        assert store.get_setting("anthropic_api_key") == "sk-live-key"

    def test_a_typed_replacement_still_replaces(self, client, store):
        store.set_setting("anthropic_api_key", "sk-old")
        client.post("/settings", data={"setting_anthropic_api_key": "sk-new"})
        assert store.get_setting("anthropic_api_key") == "sk-new"

    def test_a_non_credential_can_still_be_cleared(self, client, store):
        store.set_setting("judge_model", "mock/echo")
        client.post("/settings", data={"setting_judge_model": ""})
        assert store.get_setting("judge_model") is None

    def test_the_page_never_shows_a_stored_key(self, client, store):
        store.set_setting("anthropic_api_key", "sk-live-secret-value")
        assert b"sk-live-secret-value" not in client.get("/settings").data


class TestMountingOtherAgents:
    def test_the_page_offers_the_other_agents(self, client, project, registry):
        authoring.new_agent("billing", description="Handles billing.")
        body = client.get("/agents/support").data.decode()
        assert 'name="agents" value="billing"' in body

    def test_it_never_offers_the_agent_itself(self, client, registry):
        body = client.get("/agents/support").data.decode()
        assert 'name="agents" value="support"' not in body

    def test_mounting_writes_the_delegation_reference(self, client, project, registry):
        authoring.new_agent("billing")
        client.post("/agents/support/mounted",
                    data={"submitted": "1", "tools": "lookup_invoice", "agents": "billing"})
        agent = registry.get_agent("support")
        assert "agent:billing" in agent.tool_names
        assert "lookup_invoice" in agent.tool_names

    def test_the_delegation_becomes_a_usable_tool(self, client, project, registry):
        authoring.new_agent("billing", description="Handles billing questions.")
        client.post("/agents/support/mounted", data={"submitted": "1", "agents": "billing"})
        tools = registry.agent_tools(registry.get_agent("support"))
        assert "ask_billing" in tools
        assert tools["ask_billing"].source == "agent"

    def test_an_agent_cannot_be_given_itself(self, project, registry):
        with pytest.raises(AuthoringError, match="itself"):
            authoring.set_mounted("support", tools=[], agents=["support"])

    def test_unticking_removes_the_delegation(self, client, project, registry):
        authoring.new_agent("billing")
        client.post("/agents/support/mounted", data={"submitted": "1", "agents": "billing"})
        client.post("/agents/support/mounted", data={"submitted": "1"})
        assert "agent:billing" not in registry.get_agent("support").tool_names

    def test_delegation_runs_for_real(self, client, store, project, registry, worker):
        """The composition story, exercised rather than asserted."""
        from heddled import runtime

        authoring.new_agent("billing", description="Handles billing questions.")
        client.post("/agents/support/mounted", data={"submitted": "1", "agents": "billing"})

        result = runtime.submit_message("support", "ask billing about invoice F-2231",
                                        sync=True, timeout_s=25)
        children = store.query("SELECT * FROM sessions WHERE parent_session_id=?",
                               (result["session_id"],))
        assert children and children[0]["agent"] == "billing"


class TestConnectingAnMcpServer:
    def test_the_form_is_offered(self, client):
        assert b'name="mcp_url"' in client.get("/agents/support").data

    def test_an_unreachable_server_is_refused_before_saving(self, client, registry):
        r = client.post("/agents/support/mounted",
                        data={"mcp_name": "billing",
                              "mcp_url": "http://127.0.0.1:9/mcp"})
        assert "error=" in r.headers["Location"]
        assert not authoring.mounted_breakdown(registry.get_agent("support"))["mcp"]

    def test_a_url_is_required(self):
        with pytest.raises(AuthoringError, match="address"):
            authoring.check_mcp_server({})

    def test_a_working_server_is_saved_with_its_tools_counted(self, client, registry,
                                                              monkeypatch, project):
        """Heddled's own MCP endpoint is a real server, so point at that."""
        from heddled import authoring as auth_mod
        from heddled.registry import Tool, normalize_schema

        monkeypatch.setattr(auth_mod, "check_mcp_server", lambda spec: [
            Tool(name="billing_lookup", description="", input_schema=normalize_schema(None),
                 output_schema=normalize_schema(None), handler_path=None, dir=None,
                 raw={}, source="mcp")])
        r = client.post("/agents/support/mounted",
                        data={"mcp_name": "billing", "mcp_url": "https://example/mcp"})
        assert "Connected" in r.headers["Location"]
        servers = authoring.mounted_breakdown(registry.get_agent("support"))["mcp"]
        assert servers and servers[0]["url"] == "https://example/mcp"

    def test_a_connected_server_can_be_disconnected(self, client, registry, monkeypatch):
        from heddled import authoring as auth_mod

        monkeypatch.setattr(auth_mod, "check_mcp_server", lambda spec: [])
        client.post("/agents/support/mounted",
                    data={"mcp_name": "billing", "mcp_url": "https://example/mcp"})
        client.post("/agents/support/mounted", data={"remove_mcp": "billing"})
        assert not authoring.mounted_breakdown(registry.get_agent("support"))["mcp"]

    def test_connecting_does_not_disturb_the_existing_tools(self, client, registry,
                                                            monkeypatch):
        from heddled import authoring as auth_mod

        monkeypatch.setattr(auth_mod, "check_mcp_server", lambda spec: [])
        before = list(registry.get_agent("support").tool_names)
        client.post("/agents/support/mounted",
                    data={"mcp_name": "billing", "mcp_url": "https://example/mcp"})
        after = authoring.mounted_breakdown(registry.get_agent("support"))["tools"]
        assert after == [r for r in before if isinstance(r, str)]


class TestBreakdown:
    def test_it_separates_the_three_kinds(self, project, registry):
        authoring.set_mounted("support", tools=["lookup_invoice"], agents=["billing"],
                              mcp_servers=[{"url": "https://x/mcp", "name": "x"}])
        out = authoring.mounted_breakdown(registry.get_agent("support"))
        assert out == {"tools": ["lookup_invoice"], "agents": ["billing"],
                       "mcp": [{"url": "https://x/mcp", "name": "x"}]}
