"""The trust layer. Every decision a policy makes lands on the spine, so the
audit log is a query rather than a feature (concept §10)."""

import time

from heddled import policies
from heddled.events import Event


class TestRedaction:
    def test_iban_is_replaced(self):
        assert policies.redact_value("pay NL91ABNA0417164300", ["iban"]) == "pay «iban»"

    def test_credit_card_is_replaced(self):
        assert "«card»" in policies.redact_value("4111 1111 1111 1111", ["creditcard"])

    def test_email_is_replaced(self):
        assert policies.redact_value("a@b.com", ["email"]) == "«email»"

    def test_redaction_recurses_into_nested_payloads(self):
        payload = {"a": {"b": ["iban NL91ABNA0417164300"]}}
        assert policies.redact_value(payload, ["iban"]) == {"a": {"b": ["iban «iban»"]}}

    def test_non_strings_pass_through(self):
        assert policies.redact_value({"n": 42, "f": 1.5, "b": True}, ["iban"]) == \
            {"n": 42, "f": 1.5, "b": True}

    def test_no_rules_is_a_no_op(self):
        assert policies.redact_value("NL91ABNA0417164300", []) == "NL91ABNA0417164300"

    def test_unknown_rule_names_are_ignored(self):
        assert policies.redact_value("hello", ["nonsense"]) == "hello"

    def test_rules_are_collected_from_every_policy_block(self, agent):
        assert policies.agent_redaction_rules(agent) == ["iban", "creditcard"]


class TestToolPolicies:
    def test_an_ungoverned_tool_is_simply_allowed(self, agent, store):
        d = policies.check_tool_call(agent, "lookup_invoice", "webchat", store, "s_1")
        assert d.allowed and not d.requires_approval

    def test_a_gated_tool_requires_approval(self, agent, store):
        d = policies.check_tool_call(agent, "refund", "webchat", store, "s_1")
        assert d.allowed and d.requires_approval
        assert d.approval_adapter == "webhook"

    def test_deny_channels_blocks_the_call(self, agent, store):
        agent.policies = [{"tool": "refund", "deny_channels": ["webhook"]}]
        d = policies.check_tool_call(agent, "refund", "webhook", store, "s_1")
        assert not d.allowed and "denied on channel" in d.reason

    def test_allow_channels_blocks_everything_else(self, agent, store):
        agent.policies = [{"tool": "refund", "allow_channels": ["webchat"]}]
        assert policies.check_tool_call(agent, "refund", "webchat", store, "s_1").allowed
        assert not policies.check_tool_call(agent, "refund", "mcp", store, "s_1").allowed


class TestBudgets:
    def test_a_call_under_the_daily_budget_is_allowed(self, agent, store):
        store.record_spend("eur", 10.0, agent="support", session_id="s_1")
        assert policies.check_tool_call(agent, "refund", "webchat", store, "s_1").allowed

    def test_an_exhausted_daily_budget_denies_the_call(self, agent, store):
        store.record_spend("eur", 500.0, agent="support", session_id="s_1")
        d = policies.check_tool_call(agent, "refund", "webchat", store, "s_1")
        assert not d.allowed and "daily budget exhausted" in d.reason

    def test_session_budget_is_enforced(self, agent, store):
        agent.policies = [{"tool": "refund", "budget": {"max_eur_per_session": 5}}]
        store.record_spend("eur", 6.0, agent="support", session_id="s_1")
        assert not policies.check_tool_call(agent, "refund", "webchat", store, "s_1").allowed

    def test_the_turn_budget_stops_the_loop_before_the_next_model_call(self, agent, store):
        agent.policies = [{"tool": "*", "budget": {"max_tokens_per_session": 100}}]
        store.record_spend("tokens", 250, agent="support", session_id="s_1")
        assert "token budget exhausted" in policies.check_turn_budget(agent, store, "s_1")

    def test_no_budget_means_no_block(self, agent, store):
        agent.policies = []
        assert policies.check_turn_budget(agent, store, "s_1") is None


class TestRateLimits:
    def _record_calls(self, store, n, tool="refund", ts=None):
        for _ in range(n):
            store.append(Event(type="tool.called", session_id="s_1", agent="support",
                               payload={"tool": tool}, ts=ts or time.time()))

    def test_under_the_limit_is_allowed(self, agent, store):
        agent.policies = [{"tool": "refund", "rate_limit": {"max_calls": 3, "per_seconds": 60}}]
        self._record_calls(store, 2)
        assert policies.check_tool_call(agent, "refund", "webchat", store, "s_1").allowed

    def test_at_the_limit_is_denied(self, agent, store):
        agent.policies = [{"tool": "refund", "rate_limit": {"max_calls": 3, "per_seconds": 60}}]
        self._record_calls(store, 3)
        d = policies.check_tool_call(agent, "refund", "webchat", store, "s_1")
        assert not d.allowed and "rate limit" in d.reason

    def test_calls_outside_the_window_do_not_count(self, agent, store):
        agent.policies = [{"tool": "refund", "rate_limit": {"max_calls": 3, "per_seconds": 60}}]
        self._record_calls(store, 5, ts=time.time() - 3600)
        assert policies.check_tool_call(agent, "refund", "webchat", store, "s_1").allowed

    def test_another_tools_calls_do_not_count(self, agent, store):
        agent.policies = [{"tool": "refund", "rate_limit": {"max_calls": 2, "per_seconds": 60}}]
        self._record_calls(store, 10, tool="lookup_invoice")
        assert policies.check_tool_call(agent, "refund", "webchat", store, "s_1").allowed

    def test_the_limit_holds_beyond_a_short_event_tail(self, agent, store):
        """Regression: counting used to scan only the most recent 200 events, so
        a busy agent could push its own calls out of view and evade the limit."""
        agent.policies = [{"tool": "refund", "rate_limit": {"max_calls": 2, "per_seconds": 300}}]
        self._record_calls(store, 3)
        self._record_calls(store, 400, tool="lookup_invoice")
        assert not policies.check_tool_call(agent, "refund", "webchat", store, "s_1").allowed


class TestPolicyDenialOnTheSpine:
    def test_a_denied_tool_call_emits_error_raised_and_tells_the_model(self, store, agent):
        from heddled.engine import TurnEngine
        from heddled.events import new_id

        agent.policies = [{"tool": "lookup_invoice", "deny_channels": ["webchat"]}]
        sid = store.create_session(agent=agent.name, agent_version=agent.version,
                                   channel="webchat")
        engine = TurnEngine(store, agent, sid, new_id("t"), channel="webchat")
        result = engine.run("where is invoice F-2231?")

        errors = [e for e in store.events_for_session(sid)
                  if e.type == "error.raised" and e.payload.get("kind") == "policy_denied"]
        assert errors and errors[0].payload["tool"] == "lookup_invoice"
        assert result.status == "completed"
        denied = [e for e in store.events_for_session(sid)
                  if e.type == "tool.result" and "Denied by policy" in str(e.payload.get("result"))]
        assert denied
