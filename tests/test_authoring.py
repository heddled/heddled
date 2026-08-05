"""Creating and changing agents, tools and policies (Phase 2.5).

One module behind both the CLI and the console, so the two surfaces cannot
drift. These tests exercise it directly; test_web_authoring.py drives the same
functions through HTTP.
"""

import pytest

from heddled import authoring, yamlio
from heddled.authoring import AuthoringError


class TestNames:
    @pytest.mark.parametrize("name", ["support", "lookup_invoice", "a1", "x_2_y"])
    def test_good_names_pass(self, name):
        assert authoring.check_name(name) == name

    @pytest.mark.parametrize("name", ["Support", "1st", "with-dash", "with space", "", "_x"])
    def test_bad_names_are_rejected(self, name):
        with pytest.raises(AuthoringError):
            authoring.check_name(name)


class TestNewAgent:
    def test_it_writes_a_definition_and_instructions(self, project, registry):
        written = authoring.new_agent("triage", description="Triage agent.")
        assert [p.name for p in written.paths] == ["triage.yaml", "triage.md"]
        assert all(p.exists() for p in written.paths)

    def test_the_new_agent_is_immediately_loadable(self, project, registry):
        authoring.new_agent("triage")
        agent = registry.get_agent("triage")
        assert agent and agent.model == "mock/echo"

    def test_it_can_run_a_turn_with_no_further_editing(self, store, registry, worker,
                                                       project):
        from heddled import runtime

        authoring.new_agent("triage", description="Triage agent.")
        result = runtime.submit_message("triage", "hello", sync=True, timeout_s=20)
        assert result["status"] == "completed"

    def test_the_model_is_configurable(self, project, registry):
        authoring.new_agent("triage", model="anthropic/claude-sonnet-4-6")
        assert registry.get_agent("triage").model == "anthropic/claude-sonnet-4-6"

    def test_a_duplicate_name_is_refused(self, project, registry):
        with pytest.raises(AuthoringError, match="already exists"):
            authoring.new_agent("support")

    def test_the_scaffold_carries_no_stale_comment(self, project, registry):
        """A comment echoing a field goes stale the moment the field changes."""
        authoring.new_agent("triage", description="Triage agent.")
        text = authoring.agent_path("triage").read_text()
        assert text.count("Triage agent.") == 1


class TestCloneAgent:
    def test_a_clone_carries_the_adapters_and_policies(self, project, registry):
        authoring.new_agent("triage", from_agent="support", description="A variant.")
        clone = registry.get_agent("triage")
        source = registry.get_agent("support")
        assert clone.tool_names == source.tool_names
        assert clone.channels == source.channels
        assert clone.policies == source.policies

    def test_a_clone_is_renamed_throughout(self, project, registry):
        authoring.new_agent("triage", from_agent="support")
        clone = registry.get_agent("triage")
        assert clone.name == "triage"
        assert clone.raw["instructions"] == "./triage.md"
        assert (project / "agents" / "triage.md").exists()

    def test_a_clone_keeps_the_originals_comments(self, project, registry):
        path = authoring.agent_path("support")
        path.write_text("# why this agent exists\n" + path.read_text())
        authoring.new_agent("triage", from_agent="support")
        assert "# why this agent exists" in authoring.agent_path("triage").read_text()

    def test_cloning_something_that_does_not_exist_is_refused(self, project, registry):
        with pytest.raises(AuthoringError, match="copy from"):
            authoring.new_agent("triage", from_agent="nope")

    def test_the_original_is_untouched(self, project, registry):
        before = authoring.agent_path("support").read_text()
        authoring.new_agent("triage", from_agent="support")
        assert authoring.agent_path("support").read_text() == before


class TestSaveAgent:
    def test_a_valid_definition_is_written(self, project, registry):
        text = authoring.agent_path("support").read_text().replace(
            "mock/echo", "anthropic/claude-sonnet-4-6")
        authoring.save_agent("support", text)
        assert registry.get_agent("support").model == "anthropic/claude-sonnet-4-6"

    def test_invalid_yaml_never_touches_the_file(self, project, registry):
        before = authoring.agent_path("support").read_text()
        with pytest.raises(AuthoringError, match="invalid YAML"):
            authoring.save_agent("support", "adapters:\n  tools: [\n")
        assert authoring.agent_path("support").read_text() == before

    def test_a_mismatched_name_is_refused_and_points_at_rename(self, project, registry):
        """Editing `name:` by hand would leave every reference to it dangling,
        so the message sends you to the control that moves them all."""
        with pytest.raises(AuthoringError, match="use Rename"):
            authoring.save_agent("support", "name: something_else\nmodel: mock/echo\n")

    def test_a_non_mapping_document_is_refused(self, project, registry):
        with pytest.raises(AuthoringError, match="mapping"):
            authoring.save_agent("support", "- just\n- a\n- list\n")

    def test_saving_identical_content_is_a_no_op(self, project, registry):
        text = authoring.agent_path("support").read_text()
        assert authoring.save_agent("support", text).paths == []

    def test_the_diff_is_reported(self, project, registry):
        text = authoring.agent_path("support").read_text().replace("mock/echo", "openai/gpt-4o")
        assert "openai/gpt-4o" in authoring.save_agent("support", text).diff


class TestUpdateFields:
    """The structured form's write path."""

    def test_only_the_named_field_changes(self, project, registry):
        path = authoring.agent_path("support")
        before = path.read_text()
        authoring.update_agent_fields("support", {"model": "openai/gpt-4o"})
        after = path.read_text()
        changed = [line for line in yamlio.diff(before, after).splitlines()
                   if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
        assert len(changed) == 2

    def test_comments_survive_a_form_save(self, project, registry):
        authoring.update_agent_fields("support", {"model": "openai/gpt-4o"})
        text = authoring.agent_path("support").read_text()
        assert "# The support agent" in text
        assert "# swap for anthropic when a key is set" in text

    def test_adapters_can_be_replaced(self, project, registry):
        authoring.update_agent_fields(
            "support", {"adapters": {"channels": ["webchat"], "tools": ["lookup_invoice"]}})
        agent = registry.get_agent("support")
        assert agent.tool_names == ["lookup_invoice"] and agent.channels == ["webchat"]

    def test_an_unknown_agent_is_refused(self, project, registry):
        with pytest.raises(AuthoringError, match="no agent"):
            authoring.update_agent_fields("nope", {"model": "x"})


class TestRename:
    """A name typed wrong used to be permanent: the file editor refused to
    change it and the console had no other way in."""

    def test_the_agent_answers_to_the_new_name(self, project, registry):
        authoring.rename_agent("support", "billing")
        assert registry.get_agent("billing")
        assert registry.get_agent("support") is None
        assert registry.get_agent("billing").name == "billing"

    def test_the_instructions_come_with_it(self, project, registry):
        before = (project / "agents" / "support.md").read_text()
        authoring.rename_agent("support", "billing")
        assert not (project / "agents" / "support.md").exists()
        assert (project / "agents" / "billing.md").read_text() == before
        assert registry.get_agent("billing").instructions == before

    def test_everything_else_in_the_file_survives(self, project, registry):
        authoring.rename_agent("support", "billing")
        renamed = registry.get_agent("billing")
        assert renamed.model == "mock/echo"
        assert renamed.tool_names == ["lookup_invoice", "refund"]
        assert renamed.policy_for_tool("refund")["requires_approval"] is True
        assert "# " in (project / "agents" / "billing.yaml").read_text()  # comments kept

    def test_a_delegating_agent_is_repointed(self, project, registry):
        (project / "agents" / "router.yaml").write_text(
            "name: router\nmodel: mock/echo\nadapters:\n  tools: ['agent:support']\n"
            "policies:\n  - tool: ask_support\n    requires_approval: true\n")
        written = authoring.rename_agent("support", "billing")
        assert written.repointed == ["router"]
        router = registry.get_agent("router")
        assert router.tool_names == ["agent:billing"]
        assert router.policy_for_tool("ask_billing")["requires_approval"] is True

    def test_a_taken_name_is_refused(self, project, registry):
        authoring.new_agent("billing")
        with pytest.raises(AuthoringError, match="already an agent called"):
            authoring.rename_agent("support", "billing")
        assert registry.get_agent("support")

    def test_a_bad_name_is_refused_before_anything_moves(self, project, registry):
        with pytest.raises(AuthoringError, match="lowercase"):
            authoring.rename_agent("support", "Billing Team")
        assert registry.get_agent("support")

    def test_renaming_to_the_same_name_does_nothing(self, project, registry):
        assert authoring.rename_agent("support", "support").paths == []
        assert registry.get_agent("support")

    def test_a_tool_is_renamed_and_agents_follow(self, project, registry):
        written = authoring.rename_tool("lookup_invoice", "find_invoice")
        assert registry.get_tool("find_invoice")
        assert registry.get_tool("lookup_invoice") is None
        assert written.repointed == ["support"]
        assert "find_invoice" in registry.get_agent("support").tool_names
        assert "lookup_invoice" not in registry.get_agent("support").tool_names

    def test_a_renamed_tool_still_runs(self, project, registry):
        authoring.rename_tool("lookup_invoice", "find_invoice")
        tool = registry.get_tool("find_invoice")
        assert tool.name == "find_invoice"
        assert tool.handler_path.exists()

    def test_a_policy_naming_the_tool_follows_it(self, project, registry):
        authoring.rename_tool("refund", "issue_refund")
        agent = registry.get_agent("support")
        assert agent.policy_for_tool("issue_refund")["requires_approval"] is True


class TestDeleteAgent:
    def test_it_removes_the_definition_and_instructions(self, project, registry):
        authoring.new_agent("triage")
        authoring.delete_agent("triage")
        assert registry.get_agent("triage") is None
        assert not (project / "agents" / "triage.md").exists()

    def test_an_agent_another_agent_delegates_to_is_protected(self, project, registry):
        (project / "agents" / "router.yaml").write_text(
            "name: router\nmodel: mock/echo\nadapters:\n  tools: ['agent:support']\n")
        with pytest.raises(AuthoringError, match="mounted as a tool by router"):
            authoring.delete_agent("support")
        assert registry.get_agent("support")

    def test_forcing_it_unmounts_it_from_the_delegator(self, project, registry):
        (project / "agents" / "router.yaml").write_text(
            "name: router\nmodel: mock/echo\n"
            "adapters:\n  tools: ['agent:support', 'lookup_invoice']\n")
        written = authoring.delete_agent("support", force=True)
        assert written.unmounted_from == ["router"]
        assert registry.get_agent("support") is None
        # The delegator keeps working: only the reference to the deleted agent
        # is gone, not its other tools.
        assert registry.get_agent("router").tool_names == ["lookup_invoice"]

    def test_deleting_something_absent_is_refused(self, project, registry):
        with pytest.raises(AuthoringError, match="no agent"):
            authoring.delete_agent("nope")


class TestNewTool:
    def test_it_writes_a_manifest_and_handler(self, project, registry):
        written = authoring.new_tool("send_email", input_spec="to:string,body:string")
        assert [p.name for p in written.paths] == ["tool.yaml", "handler.py"]

    def test_the_scaffolded_tool_is_immediately_runnable(self, project, registry):
        from heddled.tooltest import run_tool_standalone

        authoring.new_tool("send_email", input_spec="to:string",
                           output_spec="sent:boolean,count:number")
        result = run_tool_standalone("send_email", {"to": "a@b.com"})
        assert result["ok"] is True
        assert result["result"] == {"sent": False, "count": 0.0}

    def test_the_schema_reaches_the_registry(self, project, registry):
        authoring.new_tool("send_email", input_spec="to:string,cc:string")
        schema = registry.get_tool("send_email").input_schema
        assert set(schema["properties"]) == {"to", "cc"}

    def test_field_specs_are_parsed(self):
        assert authoring.parse_field_spec("a:string, b:number") == {"a": "string", "b": "number"}
        assert authoring.parse_field_spec("bare") == {"bare": "string"}
        assert authoring.parse_field_spec("") == {}

    def test_a_duplicate_tool_is_refused(self, project, registry):
        with pytest.raises(AuthoringError, match="already exists"):
            authoring.new_tool("lookup_invoice")

    def test_a_clone_copies_the_handler(self, project, registry):
        authoring.new_tool("lookup_invoice_v2", from_tool="lookup_invoice")
        assert "looking up" in (project / "tools" / "lookup_invoice_v2" / "handler.py").read_text()

    def test_code_written_on_the_form_is_what_gets_saved(self, project, registry):
        from heddled.tooltest import run_tool_standalone

        authoring.new_tool(
            "shout", input_spec="text:string", output_spec="result:string",
            handler='def handle(args, ctx):\n    return {"result": args["text"].upper()}\n')
        assert run_tool_standalone("shout", {"text": "hi"})["result"] == {"result": "HI"}

    def test_hand_written_code_beats_the_clone_it_started_from(self, project, registry):
        authoring.new_tool("lookup_invoice_v2", from_tool="lookup_invoice",
                           handler="def handle(args, ctx):\n    return {}\n")
        text = (project / "tools" / "lookup_invoice_v2" / "handler.py").read_text()
        assert "looking up" not in text

    def test_blank_code_still_gets_the_scaffold(self, project, registry):
        authoring.new_tool("quiet", input_spec="text:string", handler="   \n  ")
        assert "def handle" in (project / "tools" / "quiet" / "handler.py").read_text()

    def test_broken_code_is_refused_before_anything_is_written(self, project, registry):
        with pytest.raises(AuthoringError, match="syntax error"):
            authoring.new_tool("broken", handler="def handle(args, ctx)\n    return {}")
        assert not (project / "tools" / "broken").exists()

    def test_code_without_a_handle_function_is_refused(self, project, registry):
        with pytest.raises(AuthoringError, match="handle"):
            authoring.new_tool("nohandle", handler="x = 1\n")
        assert not (project / "tools" / "nohandle").exists()


class TestSaveTool:
    def test_a_manifest_change_reaches_the_registry(self, project, registry):
        manifest = (project / "tools" / "lookup_invoice" / "tool.yaml").read_text()
        authoring.save_tool("lookup_invoice",
                            manifest=manifest.replace("Look up an invoice", "Find an invoice"))
        assert registry.get_tool("lookup_invoice").description.startswith("Find an invoice")

    def test_a_handler_syntax_error_never_touches_the_file(self, project, registry):
        path = project / "tools" / "lookup_invoice" / "handler.py"
        before = path.read_text()
        with pytest.raises(AuthoringError, match="syntax error"):
            authoring.save_tool("lookup_invoice", handler="def handle(args, ctx:\n  pass\n")
        assert path.read_text() == before

    def test_a_handler_without_handle_is_refused(self, project, registry):
        with pytest.raises(AuthoringError, match="handle"):
            authoring.save_tool("lookup_invoice", handler="x = 1\n")

    def test_an_invalid_manifest_never_touches_the_file(self, project, registry):
        path = project / "tools" / "lookup_invoice" / "tool.yaml"
        before = path.read_text()
        with pytest.raises(AuthoringError, match="invalid YAML"):
            authoring.save_tool("lookup_invoice", manifest="input: [\n")
        assert path.read_text() == before

    def test_a_renamed_manifest_is_refused(self, project, registry):
        with pytest.raises(AuthoringError, match="names 'other'"):
            authoring.save_tool("lookup_invoice", manifest="name: other\n")

    def test_an_edited_handler_takes_effect_on_the_next_call(self, project, registry):
        from heddled.tooltest import run_tool_standalone

        authoring.save_tool("lookup_invoice", handler=(
            'def handle(args, ctx):\n'
            '    return {"status": "EDITED"}\n'))
        assert run_tool_standalone(
            "lookup_invoice", {"invoice_number": "F-1"})["result"]["status"] == "EDITED"


class TestDeleteTool:
    def test_a_mounted_tool_is_protected(self, project, registry):
        with pytest.raises(AuthoringError, match="mounted by support"):
            authoring.delete_tool("lookup_invoice")

    def test_force_overrides_the_guard(self, project, registry):
        authoring.delete_tool("lookup_invoice", force=True)
        assert registry.get_tool("lookup_invoice") is None

    def test_forcing_it_leaves_no_dangling_reference(self, project, registry):
        """A forced delete used to remove the files and leave the `tools:` entry
        behind, so the agent kept asking for a tool that no longer existed."""
        written = authoring.delete_tool("lookup_invoice", force=True)
        assert written.unmounted_from == ["support"]
        assert "lookup_invoice" not in registry.get_agent("support").tool_names
        assert "refund" in registry.get_agent("support").tool_names

    def test_forcing_it_drops_the_policy_that_named_it(self, project, registry):
        authoring.add_policy("support",
                             {"tool": "lookup_invoice", "requires_approval": True})
        authoring.delete_tool("lookup_invoice", force=True)
        tools = [p.get("tool") for p in registry.get_agent("support").policies]
        assert "lookup_invoice" not in tools
        assert "refund" in tools          # every other rule survives

    def test_an_unmounted_tool_deletes_cleanly(self, project, registry):
        authoring.new_tool("scratch")
        authoring.delete_tool("scratch")
        assert not (project / "tools" / "scratch").exists()


class TestDependencies:
    def test_agents_using_a_tool_are_listed(self, project, registry):
        assert authoring.agents_using_tool("lookup_invoice") == ["support"]

    def test_an_unmounted_tool_lists_nobody(self, project, registry):
        assert authoring.agents_using_tool("boom") == []

    def test_delegating_agents_are_listed(self, project, registry):
        (project / "agents" / "router.yaml").write_text(
            "name: router\nmodel: mock/echo\nadapters:\n  tools: ['agent:support']\n")
        assert authoring.agents_delegating_to("support") == ["router"]


class TestPolicies:
    def test_a_policy_is_appended(self, project, registry):
        authoring.add_policy("support", {"tool": "lookup_invoice", "requires_approval": True})
        assert registry.get_agent("support").policy_for_tool(
            "lookup_invoice")["requires_approval"] is True

    def test_a_second_policy_for_the_same_tool_merges(self, project, registry):
        authoring.add_policy("support", {"tool": "refund", "budget": {"max_eur_per_day": 100}})
        policies = [p for p in registry.get_agent("support").policies
                    if p.get("tool") == "refund"]
        assert len(policies) == 1
        assert policies[0]["requires_approval"] is True   # the original survives
        assert policies[0]["budget"]["max_eur_per_day"] == 100

    def test_adding_a_policy_keeps_the_files_comments(self, project, registry):
        authoring.add_policy("support", {"tool": "lookup_invoice", "requires_approval": True})
        assert "#" in authoring.agent_path("support").read_text()

    def test_a_policy_without_a_tool_is_refused(self, project, registry):
        with pytest.raises(AuthoringError, match="name a tool"):
            authoring.add_policy("support", {"requires_approval": True})

    def test_a_policy_is_removable(self, project, registry):
        authoring.remove_policy("support", "refund")
        assert not registry.get_agent("support").policy_for_tool("refund").get(
            "requires_approval")

    def test_removing_an_absent_policy_is_refused(self, project, registry):
        with pytest.raises(AuthoringError, match="no policy"):
            authoring.remove_policy("support", "nonexistent")

    def test_a_new_gate_actually_pauses_a_turn(self, store, registry, worker, project):
        """The point of the whole feature: the policy you added governs behaviour."""
        from heddled import runtime

        authoring.add_policy("support", {"tool": "lookup_invoice",
                                         "requires_approval": True})
        result = runtime.submit_message("support", "where is invoice F-2231?",
                                        sync=True, timeout_s=20)
        assert result["status"] == "waiting-approval"
        assert store.pending_approvals()[0]["tool"] == "lookup_invoice"
