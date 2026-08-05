"""Files first, UI second: the registry is the only thing that reads agent and
tool files, so this is where the file format is pinned down."""

import pytest

from heddled.registry import normalize_schema, validate_args


class TestSchemaShorthand:
    """The concept doc writes `input: { invoice_number: string }`; the model
    needs JSON Schema. Both must work."""

    def test_shorthand_becomes_json_schema(self):
        s = normalize_schema({"invoice_number": "string", "amount_eur": "number"})
        assert s["type"] == "object"
        assert s["properties"]["invoice_number"]["type"] == "string"
        assert s["properties"]["amount_eur"]["type"] == "number"

    def test_fields_are_required_by_default(self):
        assert normalize_schema({"a": "string"})["required"] == ["a"]

    def test_trailing_question_mark_marks_optional(self):
        s = normalize_schema({"a": "string", "b": "string?"})
        assert s["required"] == ["a"]

    def test_type_aliases_are_accepted(self):
        s = normalize_schema({"a": "str", "b": "int", "c": "bool", "d": "list", "e": "dict"})
        types = {k: v["type"] for k, v in s["properties"].items()}
        assert types == {"a": "string", "b": "integer", "c": "boolean",
                         "d": "array", "e": "object"}

    def test_a_real_json_schema_passes_through_untouched(self):
        raw = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
        assert normalize_schema(raw) is raw

    def test_empty_schema_is_an_empty_object(self):
        assert normalize_schema(None) == {"type": "object", "properties": {}}


class TestValidateArgs:
    """Enough structural validation to catch a model hallucinating a field."""

    schema = normalize_schema({"invoice_number": "string", "amount_eur": "number"})

    def test_valid_args_pass(self):
        assert validate_args(self.schema, {"invoice_number": "F-1", "amount_eur": 10.0}) == []

    def test_missing_required_field_is_reported(self):
        errors = validate_args(self.schema, {"invoice_number": "F-1"})
        assert any("amount_eur" in e for e in errors)

    def test_wrong_type_is_reported(self):
        errors = validate_args(self.schema, {"invoice_number": 42, "amount_eur": 1})
        assert any("invoice_number" in e for e in errors)

    def test_int_is_accepted_where_number_is_wanted(self):
        assert validate_args(self.schema, {"invoice_number": "F-1", "amount_eur": 10}) == []

    def test_bool_is_rejected_where_number_is_wanted(self):
        errors = validate_args(self.schema, {"invoice_number": "F-1", "amount_eur": True})
        assert any("amount_eur" in e for e in errors)

    def test_unknown_fields_are_ignored(self):
        assert validate_args(self.schema,
                             {"invoice_number": "F-1", "amount_eur": 1, "extra": "x"}) == []


class TestAgentLoading:
    def test_agent_loads_with_its_parts(self, registry):
        a = registry.get_agent("support")
        assert a.name == "support"
        assert a.model == "mock/echo"
        assert a.channels == ["webchat", "webhook"]
        assert a.tool_names == ["lookup_invoice", "refund"]
        assert a.expose == {"mcp": True}

    def test_instructions_are_read_from_the_referenced_file(self, registry):
        assert "invoice support agent" in registry.get_agent("support").instructions

    def test_triggers_are_classified(self, registry):
        kinds = [t.kind for t in registry.get_agent("support").triggers]
        assert kinds == ["schedule", "poll"]

    def test_version_is_the_hash_of_definition_plus_instructions(self, registry, project):
        before = registry.get_agent("support").version
        assert len(before) == 64
        (project / "agents" / "support.md").write_text("Different instructions.\n")
        assert registry.get_agent("support").version != before

    def test_editing_the_definition_changes_the_version(self, registry, project):
        before = registry.get_agent("support").version
        path = project / "agents" / "support.yaml"
        path.write_text(path.read_text().replace("mock/echo", "mock/other"))
        after = registry.get_agent("support")
        assert after.version != before and after.model == "mock/other"

    def test_unknown_agent_is_none(self, registry):
        assert registry.get_agent("nope") is None

    def test_a_broken_agent_file_does_not_hide_the_good_ones(self, registry, project):
        (project / "agents" / "broken.yaml").write_text("name: broken\n  bad: [indent\n")
        assert "support" in registry.agents()


class TestPolicyResolution:
    def test_wildcard_and_specific_policies_merge(self, agent):
        p = agent.policy_for_tool("refund")
        assert p["requires_approval"] is True
        assert p["redact"] == ["iban", "creditcard"]

    def test_wildcard_applies_to_a_tool_with_no_block_of_its_own(self, agent):
        p = agent.policy_for_tool("lookup_invoice")
        assert p["redact"] == ["iban", "creditcard"]
        assert not p.get("requires_approval")


class TestToolLoading:
    def test_tools_are_discovered_from_directories(self, registry):
        assert {"lookup_invoice", "refund", "boom"} <= set(registry.tools())

    def test_handler_is_loadable_and_callable(self, registry):
        tool = registry.get_tool("lookup_invoice")

        class Ctx:
            def log(self, *a, **k):
                pass

        result = tool.load_handler()({"invoice_number": "F-1"}, Ctx())
        assert result["status"] == "unpaid"

    def test_model_schema_shape(self, registry):
        schema = registry.get_tool("lookup_invoice").to_model_schema()
        assert set(schema) == {"name", "description", "input_schema"}

    def test_agent_tools_resolve_only_what_is_mounted(self, registry, agent):
        mounted = registry.agent_tools(agent)
        assert set(mounted) == {"lookup_invoice", "refund"}
        assert "boom" not in mounted

    def test_agent_prefixed_mount_becomes_a_delegation_tool(self, registry, project):
        (project / "agents" / "router.yaml").write_text(
            "name: router\nmodel: mock/echo\nadapters:\n  tools: ['agent:support']\n"
        )
        tools = registry.agent_tools(registry.get_agent("router"))
        assert "ask_support" in tools
        assert tools["ask_support"].source == "agent"


class TestWritePaths:
    """The console's only write path is the file itself, so `git diff` stays
    the truth (principle 3)."""

    def test_write_agent_updates_the_file_on_disk(self, registry, project):
        text = (project / "agents" / "support.yaml").read_text().replace(
            "mock/echo", "anthropic/claude-sonnet-4-6")
        registry.write_agent("support", text)
        assert "anthropic" in (project / "agents" / "support.yaml").read_text()
        assert registry.get_agent("support").model == "anthropic/claude-sonnet-4-6"

    def test_invalid_yaml_is_rejected_before_touching_disk(self, registry, project):
        before = (project / "agents" / "support.yaml").read_text()
        with pytest.raises(Exception):
            registry.write_agent("support", "name: x\n  bad: [\n")
        assert (project / "agents" / "support.yaml").read_text() == before

    def test_write_instructions_updates_the_markdown_file(self, registry, agent, project):
        registry.write_instructions(agent, "New instructions.")
        assert (project / "agents" / "support.md").read_text() == "New instructions."
