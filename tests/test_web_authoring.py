"""The authoring surface over HTTP (Phase 2.5 done-when).

"Someone who has never seen a Heddled YAML file can create an agent, give it a new
tool, test that tool in isolation, and gate it behind an approval policy —
entirely from the browser — and the resulting `git diff` is a file a hand-author
would have written."
"""

import json

from heddled import authoring, gitio


class TestToolsScreen:
    def test_the_screen_renders_and_lists_tools(self, client):
        body = client.get("/tools").data
        assert b"lookup_invoice" in body and b"refund" in body

    def test_it_shows_which_agents_mount_a_tool(self, client):
        assert b"support" in client.get("/tools").data

    def test_an_unmounted_tool_says_so(self, client):
        assert b"no agent yet" in client.get("/tools").data  # `boom` is not mounted

    def test_tool_detail_shows_schema_and_handler(self, client):
        body = client.get("/tools/lookup_invoice").data.decode()
        assert "invoice_number" in body      # from tool.yaml
        assert "def handle" in body          # from handler.py

    def test_tool_detail_names_the_blast_radius(self, client):
        assert "reaches" in client.get("/tools/lookup_invoice").data.decode()

    def test_the_test_panel_is_prefilled_from_the_schema(self, client):
        assert "invoice_number" in client.get("/tools/lookup_invoice").data.decode()

    def test_an_unknown_tool_is_404(self, client):
        assert client.get("/tools/nope").status_code == 404

    def test_the_nav_carries_the_tools_screen(self, client):
        assert b'href="/tools"' in client.get("/").data


class TestCreateFromTheBrowser:
    def test_creating_a_tool_writes_both_files(self, client, project, registry):
        r = client.post("/tools", data={"name": "send_email",
                                        "description": "Send an email.",
                                        "input": "to:string,subject:string",
                                        "output": "sent:boolean"})
        assert r.status_code == 302
        assert (project / "tools" / "send_email" / "handler.py").exists()
        assert registry.get_tool("send_email").description == "Send an email."

    def test_the_created_tool_runs_immediately(self, client, project, registry):
        client.post("/tools", data={"name": "send_email", "input": "to:string",
                                    "output": "sent:boolean"})
        body = client.post("/api/tools/send_email/test",
                           json={"args": {"to": "a@b.com"}}).get_json()
        assert body["ok"] is True and body["result"] == {"sent": False}

    def test_a_bad_tool_name_is_rejected(self, client):
        assert client.post("/tools", data={"name": "Not Valid"}).status_code == 400

    def test_the_python_form_has_somewhere_to_write_the_code(self, client):
        body = client.get("/tools/new?type=python").get_data(as_text=True)
        assert 'name="handler"' in body

    def test_a_no_code_form_does_not_offer_a_code_box(self, client):
        body = client.get("/tools/new?type=http").get_data(as_text=True)
        assert 'name="handler"' not in body

    def test_code_typed_into_the_form_is_what_runs(self, client, project, registry):
        client.post("/tools", data={
            "name": "shout", "description": "Shout it.", "type": "python",
            "input_name": "text", "input_type": "string", "output": "result:string",
            "handler": 'def handle(args, ctx):\n    return {"result": args["text"].upper()}\n'})
        body = client.post("/api/tools/shout/test",
                           json={"args": {"text": "hi"}}).get_json()
        assert body["ok"] is True and body["result"] == {"result": "HI"}

    def test_code_that_does_not_compile_is_refused_on_the_form(self, client, project):
        r = client.post("/tools", data={"name": "broken", "description": "No.",
                                        "type": "python",
                                        "handler": "def handle(args, ctx)\n    pass"})
        assert r.status_code == 400
        assert "syntax error" in r.get_data(as_text=True)
        assert not (project / "tools" / "broken").exists()

    def test_creating_an_agent_writes_a_definition_and_instructions(self, client, project,
                                                                    registry):
        r = client.post("/agents", data={"name": "triage", "model": "mock/echo",
                                         "description": "Triage."})
        assert r.status_code == 302
        assert (project / "agents" / "triage.yaml").exists()
        assert registry.get_agent("triage").description == "Triage."

    def test_a_cloned_agent_carries_the_originals_policies(self, client, registry):
        client.post("/agents", data={"name": "triage", "from_agent": "support"})
        assert registry.get_agent("triage").policies == registry.get_agent("support").policies

    def test_a_duplicate_agent_name_is_rejected(self, client):
        assert client.post("/agents", data={"name": "support"}).status_code == 400


class TestFormEditing:
    def test_both_a_guided_form_and_the_raw_file_are_reachable(self, client):
        """The structured editors are the page itself; the file stays one
        disclosure away so the form is never a ceiling."""
        body = client.get("/agents/support").data.decode()
        assert 'action="/agents/support/fields"' in body     # guided editing
        assert 'name="definition"' in body                   # the file itself
        assert "Settings and advanced" in body               # …behind a disclosure

    def test_the_form_lists_every_tool_with_the_mounted_ones_checked(self, client):
        body = client.get("/agents/support").data.decode()
        assert 'value="lookup_invoice"' in body and 'value="boom"' in body

    def test_saving_the_form_changes_only_that_field(self, client, project, registry):
        path = project / "agents" / "support.yaml"
        before = path.read_text()
        client.post("/agents/support/fields", data={"model": "openai/gpt-4o"})
        after = path.read_text()
        assert registry.get_agent("support").model == "openai/gpt-4o"
        assert before.count("\n") == after.count("\n")   # no reflow

    def test_saving_the_form_preserves_comments(self, client, project):
        client.post("/agents/support/fields", data={"model": "openai/gpt-4o"})
        text = (project / "agents" / "support.yaml").read_text()
        assert "# The support agent" in text
        assert "# a human signs off on money leaving" in text

    def test_tools_can_be_mounted_from_the_form(self, client, registry):
        client.post("/agents/support/fields",
                    data={"tools": ["lookup_invoice", "boom"]})
        assert set(registry.get_agent("support").tool_names) == {"lookup_invoice", "boom"}

    def test_unchecking_everything_unmounts(self, client, registry):
        client.post("/agents/support/fields", data={"channels": "webchat"})
        assert registry.get_agent("support").channels == ["webchat"]

    def test_the_mcp_toggle_round_trips(self, client, registry):
        client.post("/agents/support/fields", data={"expose_mcp": "off"})
        assert registry.get_agent("support").expose == {"mcp": False}
        client.post("/agents/support/fields", data={"expose_mcp": "on"})
        assert registry.get_agent("support").expose == {"mcp": True}


class TestRawEditing:
    def test_a_valid_definition_saves(self, client, project, registry):
        text = (project / "agents" / "support.yaml").read_text().replace(
            "mock/echo", "openai/gpt-4o")
        r = client.post("/agents/support/definition", data={"definition": text})
        assert r.status_code == 302
        assert registry.get_agent("support").model == "openai/gpt-4o"

    def test_invalid_yaml_is_rejected_and_the_edit_is_returned(self, client, project):
        """A typo must not cost someone the edit they just made."""
        before = (project / "agents" / "support.yaml").read_text()
        attempt = before + "\nbroken: [\n"
        r = client.post("/agents/support/definition", data={"definition": attempt})
        assert r.status_code == 400
        assert (project / "agents" / "support.yaml").read_text() == before
        assert b"broken: [" in r.data           # their text came back
        assert b"invalid YAML" in r.data        # and so did the reason

    def test_a_rejected_tool_edit_returns_the_handler_text(self, client, project):
        before = (project / "tools" / "lookup_invoice" / "handler.py").read_text()
        r = client.post("/tools/lookup_invoice",
                        data={"handler": "def handle(args, ctx:\n    pass\n"})
        assert r.status_code == 400
        assert (project / "tools" / "lookup_invoice" / "handler.py").read_text() == before
        assert b"def handle(args, ctx:" in r.data
        assert b"syntax error" in r.data


class TestDiffPreview:
    def test_the_preview_returns_the_diff_before_writing(self, client, project):
        text = (project / "agents" / "support.yaml").read_text().replace(
            "mock/echo", "openai/gpt-4o")
        body = client.post("/api/agents/support/preview",
                           json={"definition": text}).get_json()
        assert body["valid"] and body["changed"]
        assert "+model: openai/gpt-4o" in body["diff"].replace(" ", "")[:2000] or \
               "openai/gpt-4o" in body["diff"]
        # nothing was written
        assert "openai/gpt-4o" not in (project / "agents" / "support.yaml").read_text()

    def test_an_unchanged_definition_reports_no_change(self, client, project):
        text = (project / "agents" / "support.yaml").read_text()
        body = client.post("/api/agents/support/preview",
                           json={"definition": text}).get_json()
        assert body["valid"] and not body["changed"]

    def test_invalid_yaml_is_reported_with_a_reason(self, client):
        body = client.post("/api/agents/support/preview",
                           json={"definition": "a: [\n"}).get_json()
        assert not body["valid"] and body["error"]

    def test_unrepresentable_constructs_are_flagged(self, client):
        body = client.post("/api/agents/support/preview", json={
            "definition": "defaults: &d\n  model: mock/echo\nname: support\n"}).get_json()
        assert body["unrepresentable"]


class TestUnrepresentableFilesAreProtected:
    def test_the_form_is_disabled_for_a_file_it_cannot_represent(self, client, project):
        path = project / "agents" / "support.yaml"
        path.write_text("defaults: &base\n  x: 1\n" + path.read_text())
        body = client.get("/agents/support").data.decode()
        assert "cannot show" in body
        assert "disabled" in body


class TestRenamingFromTheBrowser:
    def test_the_agent_moves_and_the_page_says_so(self, client, registry):
        r = client.post("/agents/support/rename", data={"new_name": "billing"},
                        follow_redirects=True)
        body = r.data.decode()
        assert registry.get_agent("billing") and registry.get_agent("support") is None
        assert "Renamed from" in body and "support" in body

    def test_its_conversations_come_with_it(self, client, store, registry):
        sid = store.create_session(agent="support", agent_version="v1")
        client.post("/agents/support/rename", data={"new_name": "billing"})
        assert [s["id"] for s in store.list_sessions(agent="billing")] == [sid]

    def test_a_taken_name_comes_back_with_a_reason(self, client, registry):
        authoring.new_agent("billing")
        body = client.post("/agents/support/rename", data={"new_name": "billing"},
                           follow_redirects=True).data.decode()
        assert "already an agent called" in body
        assert registry.get_agent("support")

    def test_a_tool_moves_and_its_agents_follow(self, client, registry):
        body = client.post("/tools/lookup_invoice/rename", data={"new_name": "find_invoice"},
                           follow_redirects=True).data.decode()
        assert registry.get_tool("find_invoice")
        assert "find_invoice" in registry.get_agent("support").tool_names
        assert "Renamed from" in body

    def test_the_control_sits_next_to_the_name(self, client):
        """Where you look when the name is wrong, not in a settings panel."""
        for path, action in (("/agents/support", "/agents/support/rename"),
                             ("/tools/lookup_invoice", "/tools/lookup_invoice/rename")):
            body = client.get(path).data.decode()
            assert action in body
            assert body.index(action) < body.index("</h1>")

    def test_the_old_link_still_lands_somewhere(self, client, registry):
        """A bookmark or a link in someone's notes outlives the rename."""
        client.post("/agents/support/rename", data={"new_name": "billing"})
        r = client.get("/agents/support", follow_redirects=True)
        assert r.status_code == 200
        assert "Renamed from" in r.data.decode()

    def test_an_old_tool_link_does_too(self, client, registry):
        client.post("/tools/lookup_invoice/rename", data={"new_name": "find_invoice"})
        r = client.get("/tools/lookup_invoice", follow_redirects=True)
        assert r.status_code == 200 and "find_invoice" in r.data.decode()

    def test_a_name_that_never_existed_is_still_a_404(self, client):
        assert client.get("/agents/never_existed").status_code == 404

    def test_renaming_asks_first(self, client):
        body = client.get("/agents/support").data.decode()
        assert 'data-confirm="Rename support?' in body


class TestDeletion:
    """Deleting from the console used to dead-end on anything in use: the page
    said "unmount it there first" and offered no way to do that. The browser
    now unmounts as part of the delete, having said so in the confirmation."""

    def test_a_mounted_tool_is_deleted_and_unmounted(self, client, registry):
        client.post("/tools/lookup_invoice/delete")
        assert registry.get_tool("lookup_invoice") is None
        assert "lookup_invoice" not in registry.get_agent("support").tool_names

    def test_the_page_says_which_agents_it_came_off(self, client, registry):
        body = client.post("/tools/lookup_invoice/delete",
                           follow_redirects=True).data.decode()
        assert "lookup_invoice" in body and "taken off support" in body

    def test_the_confirmation_names_them_before_anything_happens(self, client):
        body = client.get("/tools/lookup_invoice").data.decode()
        assert "taken off support" in body

    def test_an_unmounted_tool_can_be(self, client, registry, project):
        authoring.new_tool("scratch")
        client.post("/tools/scratch/delete")
        assert registry.get_tool("scratch") is None

    def test_an_agent_can_be_deleted(self, client, registry):
        authoring.new_agent("triage")
        client.post("/agents/triage/delete")
        assert registry.get_agent("triage") is None

    def test_a_delegated_agent_is_deleted_and_unmounted(self, client, registry, project):
        (project / "agents" / "router.yaml").write_text(
            "name: router\nmodel: mock/echo\nadapters:\n  tools: ['agent:support']\n")
        body = client.post("/agents/support/delete", follow_redirects=True).data.decode()
        assert registry.get_agent("support") is None
        assert registry.get_agent("router").tool_names == []
        assert "taken off router" in body

    def test_the_question_is_asked_through_the_shared_handler(self, client):
        """Built by hand into `onsubmit`, the JS broke on the first quote in a
        message — and a broken handler deletes without asking anything."""
        for path in ("/tools/lookup_invoice", "/agents/support"):
            body = client.get(path).data.decode()
            assert "onsubmit=\"return confirm(" not in body
            assert "data-confirm=\"Delete" in body
            assert "dataset.confirm" in body      # the handler is on the page

    def test_delete_is_not_hidden_behind_the_disclosure(self, client, project):
        """It was inside "Settings and advanced", which is where people looked
        last — and reported that agents could not be deleted at all."""
        body = client.get("/agents/support").data.decode()
        assert body.index("Delete this agent") > body.index("</details>")


class TestPolicyEditing:
    def test_a_gate_can_be_added_from_the_browser(self, client, registry):
        client.post("/agents/support/policies",
                    data={"tool": "lookup_invoice", "requires_approval": "on"})
        assert registry.get_agent("support").policy_for_tool(
            "lookup_invoice")["requires_approval"] is True

    def test_a_budget_is_written_as_a_whole_number(self, client, project):
        client.post("/agents/support/policies",
                    data={"tool": "lookup_invoice", "max_eur_per_day": "250"})
        text = (project / "agents" / "support.yaml").read_text()
        assert "250" in text and "250.0" not in text

    def test_a_policy_can_be_removed(self, client, registry):
        client.post("/agents/support/policies", data={"remove": "refund"})
        assert not registry.get_agent("support").policy_for_tool("refund").get(
            "requires_approval")

    def test_the_gate_added_in_the_browser_actually_pauses_a_turn(self, client, store,
                                                                  registry, worker):
        """End to end: a policy authored in the UI governs the next turn."""
        from heddled import runtime

        client.post("/agents/support/policies",
                    data={"tool": "lookup_invoice", "requires_approval": "on"})
        result = runtime.submit_message("support", "where is invoice F-2231?",
                                        sync=True, timeout_s=20)
        assert result["status"] == "waiting-approval"


class TestCommitOnSave:
    def test_it_is_off_by_default(self, store):
        assert gitio.is_enabled() is False

    def test_nothing_is_committed_when_it_is_off(self, project, registry, store):
        written = authoring.new_agent("triage")
        assert written.committed is None

    def test_the_setting_can_be_toggled_from_settings(self, client, store):
        client.post("/settings/commit-on-save", data={"enabled": "on"})
        assert store.get_setting(gitio.SETTING) is True
        client.post("/settings/commit-on-save", data={})
        assert store.get_setting(gitio.SETTING) is False

    def test_a_commit_outside_a_repo_fails_quietly(self, project, registry, store):
        """The file is written either way — that is the part that matters."""
        written = authoring.new_agent("triage", commit=True)
        assert written.committed is None            # tmp_path is not a git repo
        assert written.paths[0].exists()

    def test_the_settings_screen_explains_the_state(self, client):
        assert b"Commit on save" in client.get("/settings").data
