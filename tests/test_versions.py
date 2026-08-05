"""Publishing binds a version to an environment (§4: "Deployment — an agent
version bound to an environment").

It used to bind nothing. `promote` wrote a version string into a table and
every run, in every environment, loaded whatever the file said at that moment.
Editing an agent therefore changed production silently, and "published version
4f2a" named bytes that no longer existed anywhere.
"""

import time

import pytest

from heddled import authoring, runtime
from heddled.registry import build_agent


def edit(project, name: str, line: str) -> None:
    path = project / "agents" / f"{name}.yaml"
    path.write_text(path.read_text() + line)
    time.sleep(0.01)  # the registry caches on mtime


class TestVersionsAreKept:
    def test_looking_at_an_agent_records_its_definition(self, client, store, registry):
        client.get("/agents/support")
        agent = registry.get_agent("support")
        row = store.agent_version("support", agent.version)
        assert row["definition"] == agent.raw_text()
        assert row["instructions"] == agent.instructions

    def test_recording_the_same_version_twice_is_free(self, store, registry):
        agent = registry.get_agent("support")
        store.record_agent_version(agent)
        store.record_agent_version(agent)
        assert len(store.agent_versions("support")) == 1

    def test_an_edit_makes_a_new_version_without_losing_the_old(self, client, store,
                                                               project, registry):
        client.get("/agents/support")
        before = registry.get_agent("support").version
        edit(project, "support", "\n# a change\n")
        client.get("/agents/support")
        after = registry.get_agent("support").version
        assert before != after
        assert {r["version"] for r in store.agent_versions("support")} == {before, after}


class TestThereIsAlwaysSomethingToRewindTo:
    """Recording a version only when somebody opened the page meant the one you
    most wanted back — the one you just typed over — was usually missing."""

    def test_saving_keeps_the_definition_it_replaced(self, store, project, registry):
        before = registry.get_agent("support").version
        authoring.save_agent("support", registry.get_agent("support").raw_text()
                             + "\n# changed\n")
        after = registry.get_agent("support").version
        assert before != after
        assert store.agent_version("support", before) is not None

    def test_editing_the_instructions_keeps_the_old_ones(self, client, store, registry):
        original = registry.get_agent("support")
        client.post("/agents/support/definition", data={"instructions": "Completely new."})
        assert registry.get_agent("support").instructions == "Completely new."
        kept = store.agent_version("support", original.version)
        assert kept is not None and kept["instructions"] == original.instructions

    def test_a_rewind_is_possible_without_ever_visiting_the_page(self, client, store,
                                                                 registry):
        """The whole point: an edit made and regretted, with no prior visit."""
        original = registry.get_agent("support")
        client.post("/agents/support/definition", data={"instructions": "A bad rewrite."})
        client.post(f"/agents/support/versions/{original.version}/restore")
        assert registry.get_agent("support").instructions == original.instructions

    def test_saving_the_same_bytes_adds_nothing(self, store, registry):
        agent = registry.get_agent("support")
        authoring.save_agent("support", agent.raw_text())
        assert len(store.agent_versions("support")) <= 1

    def test_the_list_is_on_the_page_not_behind_the_disclosure(self, client):
        body = client.get("/agents/support").data.decode()
        assert body.index("Earlier versions") > body.index("</details>")

    def test_the_current_version_is_visible_without_scrolling(self, client, registry):
        body = client.get("/agents/support").data.decode()
        assert 'href="#versions"' in body
        assert registry.get_agent("support").short_version in body


class TestWhatEachEnvironmentRuns:
    def test_dev_always_runs_the_working_file(self, store, project, registry):
        agent = registry.get_agent("support")
        store.record_agent_version(agent)
        store.promote("support", "dev", agent.version)
        edit(project, "support", "\n# edited after publishing\n")
        assert runtime.resolve_agent("support", "dev").version \
            == registry.get_agent("support").version

    def test_prod_keeps_running_the_published_version_while_you_edit(self, store, project,
                                                                    registry):
        published = registry.get_agent("support")
        store.record_agent_version(published)
        store.promote("support", "prod", published.version)

        edit(project, "support", "\n# still editing\n")
        assert registry.get_agent("support").version != published.version

        running = runtime.resolve_agent("support", "prod")
        assert running.version == published.version
        assert running.raw_text() == published.raw_text()
        assert running.pinned_for == "prod"

    def test_publishing_again_moves_prod_forward(self, store, project, registry):
        first = registry.get_agent("support")
        store.record_agent_version(first)
        store.promote("support", "prod", first.version)
        edit(project, "support", "\n# a considered change\n")
        second = registry.get_agent("support")
        store.record_agent_version(second)
        store.promote("support", "prod", second.version)
        assert runtime.resolve_agent("support", "prod").version == second.version

    def test_an_unpublished_environment_falls_back_to_the_file(self, store, registry):
        assert runtime.resolve_agent("support", "staging").version \
            == registry.get_agent("support").version

    def test_a_version_whose_bytes_are_gone_falls_back_rather_than_failing(
            self, store, project, registry):
        """Promoted before versions were kept: running the file beats not running."""
        store.promote("support", "prod", "a" * 64)
        assert runtime.resolve_agent("support", "prod").version \
            == registry.get_agent("support").version

    def test_a_pinned_version_is_a_working_agent(self, store, project, registry):
        published = registry.get_agent("support")
        store.record_agent_version(published)
        store.promote("support", "prod", published.version)
        edit(project, "support", "\n# edited\n")
        running = runtime.resolve_agent("support", "prod")
        assert running.name == "support"
        assert running.model == published.model
        assert running.tool_names == published.tool_names
        assert running.policies == published.policies
        assert running.instructions == published.instructions


class TestATurnUsesItsEnvironment:
    def test_a_prod_turn_runs_the_published_instructions(self, store, project, registry,
                                                         worker):
        (project / "agents" / "support.md").write_text("Published behaviour.")
        time.sleep(0.01)
        published = registry.get_agent("support")
        store.record_agent_version(published)
        store.promote("support", "prod", published.version)

        (project / "agents" / "support.md").write_text("Draft behaviour, not published.")
        time.sleep(0.01)

        result = runtime.submit_message("support", "hello", env="prod", sync=True,
                                        timeout_s=15)
        context = [e for e in store.events_for_session(result["session_id"])
                   if e.type == "context.built"]
        assert context, "the turn should have built a context"
        rendered = str(context[0].payload)
        assert "Published behaviour." in rendered
        assert "Draft behaviour" not in rendered

    def test_the_session_records_the_version_it_actually_ran(self, store, project,
                                                             registry, worker):
        published = registry.get_agent("support")
        store.record_agent_version(published)
        store.promote("support", "prod", published.version)
        edit(project, "support", "\n# edited after publishing\n")

        result = runtime.submit_message("support", "hello", env="prod", sync=True,
                                        timeout_s=15)
        session = store.get_session(result["session_id"])
        assert session["agent_version"] == published.version
        assert session["agent_version"] != registry.get_agent("support").version


class TestDelegationStaysInItsEnvironment:
    def test_a_prod_turn_reaches_the_published_specialist(self, store, project, registry):
        """A specialist is mounted like any other capability, so a prod turn
        delegating to one must not reach whatever draft is on disk."""
        (project / "agents" / "office_helper.yaml").write_text(
            "name: office_helper\nmodel: mock/echo\ndescription: published\n")
        time.sleep(0.01)
        published = registry.get_agent("office_helper")
        store.record_agent_version(published)
        store.promote("office_helper", "prod", published.version)

        (project / "agents" / "office_helper.yaml").write_text(
            "name: office_helper\nmodel: mock/echo\ndescription: draft\n")
        time.sleep(0.01)

        assert runtime.resolve_agent("office_helper", "prod").description == "published"
        assert runtime.resolve_agent("office_helper", "dev").description == "draft"


class TestPublishingFromTheConsole:
    def test_publishing_stores_the_bytes_it_pins(self, client, store, registry):
        agent = registry.get_agent("support")
        r = client.post("/api/deployments/promote",
                        json={"agent": "support", "env": "staging"})
        assert r.status_code == 200
        assert store.agent_version("support", agent.version) is not None
        assert store.deployment("support", "staging")["version"] == agent.version

    def test_publishing_an_unknown_version_is_refused(self, client, store):
        r = client.post("/api/deployments/promote",
                        json={"agent": "support", "env": "staging", "version": "b" * 64})
        assert r.status_code == 404
        assert store.deployment("support", "staging") is None

    def test_prod_still_asks_for_a_green_test_run(self, client, store):
        r = client.post("/api/deployments/promote", json={"agent": "support", "env": "prod"})
        assert r.status_code == 412
        assert store.deployment("support", "prod") is None

    def test_the_gate_can_be_overridden_deliberately(self, client, store, registry):
        r = client.post("/api/deployments/promote",
                        json={"agent": "support", "env": "prod", "force": True})
        assert r.status_code == 200
        assert store.deployment("support", "prod")["version"] \
            == registry.get_agent("support").version

    def test_both_screens_publish_the_same_way(self, client):
        """The agent page used to report the prod gate and offer nothing
        further, so prod could not be published from there at all."""
        for path in ("/agents/support", "/deployments"):
            body = client.get(path).data.decode()
            assert "heddledPublish" in body

    def test_it_records_who_published(self, client, store, admin):
        client.post("/api/deployments/promote", json={"agent": "support", "env": "staging"})
        assert store.deployment("support", "staging")["promoted_by"] == admin["username"]


class TestRestoringAnEarlierVersion:
    def test_an_earlier_definition_can_be_put_back(self, client, store, project, registry):
        client.get("/agents/support")
        original = registry.get_agent("support")
        original_text = original.raw_text()

        edit(project, "support", "\n# a change I regret\n")
        client.get("/agents/support")
        assert registry.get_agent("support").raw_text() != original_text

        client.post(f"/agents/support/versions/{original.version}/restore")
        assert registry.get_agent("support").raw_text() == original_text
        assert registry.get_agent("support").version == original.version

    def test_the_instructions_are_restored_too(self, client, store, project, registry):
        client.get("/agents/support")
        original = registry.get_agent("support")
        (project / "agents" / "support.md").write_text("Rewritten, badly.")
        time.sleep(0.01)
        client.post(f"/agents/support/versions/{original.version}/restore")
        assert registry.get_agent("support").instructions == original.instructions

    def test_restoring_does_not_republish(self, client, store, project, registry):
        client.get("/agents/support")
        original = registry.get_agent("support")
        edit(project, "support", "\n# newer\n")
        client.get("/agents/support")
        newer = registry.get_agent("support")
        store.promote("support", "prod", newer.version)

        client.post(f"/agents/support/versions/{original.version}/restore")
        assert store.deployment("support", "prod")["version"] == newer.version

    def test_a_version_that_is_not_stored_says_so(self, client, registry):
        r = client.post("/agents/support/versions/" + "c" * 64 + "/restore",
                        follow_redirects=True)
        assert "no longer stored" in r.data.decode()


class TestSeeingWhatChanged:
    def test_the_diff_shows_the_edit(self, client, project, registry):
        client.get("/agents/support")
        original = registry.get_agent("support")
        edit(project, "support", "\n# an added line\n")
        body = client.get(
            f"/api/agents/support/versions/{original.version}/diff").get_json()
        assert "an added line" in body["definition"]
        assert body["same"] is False

    def test_comparing_the_current_version_with_itself_says_so(self, client, registry):
        client.get("/agents/support")
        agent = registry.get_agent("support")
        body = client.get(
            f"/api/agents/support/versions/{agent.version}/diff").get_json()
        assert body["same"] is True

    def test_an_unknown_version_is_a_404(self, client):
        assert client.get(
            "/api/agents/support/versions/" + "d" * 64 + "/diff").status_code == 404


class TestVersionsFollowARename:
    def test_the_history_moves_with_the_agent(self, client, store, registry):
        client.get("/agents/support")
        version = registry.get_agent("support").version
        client.post("/agents/support/rename", data={"new_name": "billing"})
        assert store.agent_version("billing", version) is not None
        assert store.agent_version("support", version) is None


class TestWhereOutsideWorkLands:
    """A webhook, an MCP caller or a scheduled run has to belong to some
    environment, and that choice decides which version it runs."""

    def test_it_defaults_to_dev(self, store):
        assert runtime.inbound_env() == "dev"

    def test_a_caller_can_say_for_itself(self, store):
        assert runtime.inbound_env("prod") == "prod"

    def test_the_setting_changes_it_without_a_restart(self, store):
        store.set_setting("default_env", "prod")
        assert runtime.inbound_env() == "prod"
        assert runtime.inbound_env("dev") == "dev"    # the caller still wins

    def test_nonsense_falls_back_to_dev(self, store):
        store.set_setting("default_env", "wherever")
        assert runtime.inbound_env() == "dev"

    def test_a_webhook_lands_in_the_default_environment(self, client, store, registry,
                                                        worker):
        store.set_setting("default_env", "staging")
        r = client.post("/api/agents/support/webhook", json={"text": "hello", "sync": True})
        session = store.get_session(r.get_json()["session_id"])
        assert session["env"] == "staging"

    def test_the_test_tab_stays_in_dev(self, client, store, registry, worker):
        store.set_setting("default_env", "prod")
        r = client.post("/api/agents/support/messages",
                        json={"text": "hello", "sync": True, "env": "dev"})
        assert store.get_session(r.get_json()["session_id"])["env"] == "dev"


class TestActivityShowsTheDifference:
    def test_a_session_carries_its_environment_in_the_list(self, client, store, registry):
        store.create_session(agent="support", agent_version="v1", env="prod")
        body = client.get("/sessions").data.decode()
        assert 'env-tag env-prod' in body

    def test_it_can_be_filtered_to_one_environment(self, client, store, registry):
        dev = store.create_session(agent="support", agent_version="v1", env="dev")
        prod = store.create_session(agent="support", agent_version="v1", env="prod")
        body = client.get("/sessions?env=prod").data.decode()
        assert prod[:16] in body and dev[:16] not in body

    def test_the_conversation_page_says_which_environment_and_version(self, client, store,
                                                                     registry):
        sid = store.create_session(agent="support", agent_version="c0ffee1234567890",
                                   env="prod")
        body = client.get(f"/sessions/{sid}").data.decode()
        assert "env-prod" in body
        assert "c0ffee12" in body


class TestBuildingFromBytes:
    def test_an_agent_built_from_a_snapshot_hashes_the_same(self, registry):
        agent = registry.get_agent("support")
        rebuilt = build_agent(agent.raw_text(), agent.instructions, path=agent.path)
        assert rebuilt.version == agent.version

    def test_it_answers_with_the_bytes_it_was_built_from(self, project, registry):
        agent = registry.get_agent("support")
        text = agent.raw_text()
        rebuilt = build_agent(text, agent.instructions, path=agent.path)
        (project / "agents" / "support.yaml").write_text("name: support\nmodel: mock/echo\n")
        assert rebuilt.raw_text() == text
