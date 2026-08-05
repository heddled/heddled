"""The console at three hundred agents and three hundred tools.

Every list screen was written for the handful of things you have on day one.
At estate size the Tools screen took over ten minutes to render a single page —
it asked the store when each tool last ran, decoding up to 500 event payloads
per tool, and walked every agent once per tool to find out who mounted it.
"""

import time

import pytest

from heddled import authoring


@pytest.fixture()
def estate(project, registry, store):
    """Sixty agents and sixty tools — enough to cross every page boundary and
    catch a per-row query, without making the suite slow."""
    tools = project / "tools"
    for i in range(60):
        d = tools / f"bulk_tool_{i:03d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "tool.yaml").write_text(
            f"name: bulk_tool_{i:03d}\ndescription: Handles case {i} for the "
            f"{'finance' if i % 2 else 'legal'} team.\n"
            "type: fixed\nconfig:\n  result: {ok: true}\n"
            "input: {reference: string}\noutput: {ok: boolean}\n")
    for i in range(60):
        team = "finance" if i % 2 else "legal"
        (project / "agents" / f"bulk_agent_{i:03d}.yaml").write_text(
            f"name: bulk_agent_{i:03d}\ndescription: The {team} assistant {i}.\n"
            "model: mock/echo\nadapters:\n  tools:\n"
            f"    - bulk_tool_{i:03d}\n")
    return {"agents": 60, "tools": 60}


class TestListsArePaged:
    def test_the_agents_screen_shows_a_page_not_everything(self, client, estate):
        body = client.get("/").data.decode()
        assert body.count("bulk_agent_") < 60
        assert "page 1 of" in body

    def test_the_second_page_shows_different_agents(self, client, estate):
        first = client.get("/").data.decode()
        second = client.get("/?page=2").data.decode()
        assert "bulk_agent_000" in first and "bulk_agent_000" not in second
        assert "page 2 of" in second

    def test_a_page_past_the_end_lands_on_the_last_one(self, client, estate):
        body = client.get("/?page=9999").data.decode()
        assert "bulk_agent_" in body

    def test_nonsense_in_the_page_number_does_not_break_it(self, client, estate):
        assert client.get("/?page=banana").status_code == 200

    def test_the_tools_screen_is_paged_too(self, client, estate):
        import re

        body = client.get("/tools").data.decode()
        assert len(set(re.findall(r"bulk_tool_\d+", body))) < 60
        assert "page 1 of" in body

    def test_publish_is_paged_too(self, client, estate):
        body = client.get("/deployments").data.decode()
        assert "page 1 of" in body

    def test_activity_pages_without_counting_everything(self, client, store, estate):
        for i in range(30):
            store.create_session(agent="support", agent_version="v1")
        first = client.get("/sessions").data.decode()
        assert "older" in first
        assert client.get("/sessions?page=2").status_code == 200


class TestSearch:
    def test_agents_can_be_found_by_name(self, client, estate):
        body = client.get("/?q=bulk_agent_042").data.decode()
        assert "bulk_agent_042" in body and "bulk_agent_041" not in body

    def test_agents_can_be_found_by_description(self, client, estate):
        body = client.get("/?q=legal").data.decode()
        assert "bulk_agent_000" in body      # even i → legal
        assert "bulk_agent_001" not in body

    def test_agents_can_be_found_by_the_tool_they_use(self, client, estate):
        """"Who can do this?" is the question behind most searches."""
        body = client.get("/?q=bulk_tool_007").data.decode()
        assert "bulk_agent_007" in body and "bulk_agent_008" not in body

    def test_tools_can_be_found_by_what_they_do(self, client, estate):
        body = client.get("/tools?q=case 42").data.decode()
        assert "bulk_tool_042" in body and "bulk_tool_041" not in body

    def test_a_search_with_no_matches_offers_a_way_back(self, client, estate):
        body = client.get("/?q=nothing_matches_this").data.decode()
        assert "No agent matches" in body and 'href="?"' in body

    def test_an_empty_search_result_is_not_the_onboarding_screen(self, client, estate):
        """A search that finds nothing must not claim the console is empty."""
        body = client.get("/?q=nothing_matches_this").data.decode()
        assert "Nothing here yet" not in body

    def test_the_search_survives_into_the_pager(self, client, estate):
        body = client.get("/?q=legal").data.decode()
        assert "q=legal" in body      # the next-page link keeps the search


class TestNoQueryPerRow:
    def test_the_agents_screen_does_not_query_per_agent(self, client, estate, store):
        """Four queries per agent is what made this screen take seconds."""
        counted = []
        original = store.query

        def counting(sql, params=()):
            counted.append(sql)
            return original(sql, params)

        store.query = counting
        try:
            client.get("/")
        finally:
            store.query = original
        assert len(counted) < 25, f"{len(counted)} queries for one page"

    def test_the_tools_screen_asks_for_tool_runs_once(self, client, estate, store):
        counted = []
        original = store.last_tool_runs

        def counting(*a, **kw):
            counted.append(1)
            return original(*a, **kw)

        store.last_tool_runs = counting
        try:
            client.get("/tools")
        finally:
            store.last_tool_runs = original
        assert len(counted) == 1

    def test_mounts_are_indexed_once_rather_than_scanned_per_tool(self, registry, estate):
        index = authoring.mount_index()
        assert index["bulk_tool_007"] == ["bulk_agent_007"]
        assert index.get("bulk_tool_999") is None

    def test_the_index_agrees_with_asking_one_at_a_time(self, registry, estate):
        index = authoring.mount_index()
        for name in ("bulk_tool_000", "bulk_tool_030", "lookup_invoice"):
            assert index.get(name, []) == authoring.agents_using_tool(name)


class TestParsingIsNotRepeated:
    """The agent cache was written and never read, so every call re-read and
    re-parsed every agent file and its instructions."""

    def test_a_second_look_does_not_reparse(self, registry, estate, monkeypatch):
        registry.agents()
        parsed = []
        original = registry.__class__._load_agent

        def counting(self, path):
            parsed.append(path)
            return original(self, path)

        monkeypatch.setattr(registry.__class__, "_load_agent", counting)
        registry.agents()
        registry.agents()
        assert parsed, "the loader should still be consulted"
        # Consulted, yes; re-reading the file, no.
        import heddled.registry as reg

        built = []
        monkeypatch.setattr(reg, "build_agent",
                            lambda *a, **k: built.append(1) or pytest.fail("reparsed"))
        registry.agents()

    def test_editing_the_definition_is_picked_up(self, project, registry, estate):
        before = registry.get_agent("bulk_agent_001").description
        path = project / "agents" / "bulk_agent_001.yaml"
        time.sleep(0.01)
        path.write_text(path.read_text().replace("assistant 1", "renamed role"))
        assert registry.get_agent("bulk_agent_001").description != before

    def test_editing_only_the_instructions_still_makes_a_new_version(self, project,
                                                                     registry):
        """Both files decide the version, so both have to be watched."""
        before = registry.get_agent("support").version
        time.sleep(0.01)
        (project / "agents" / "support.md").write_text("Entirely new instructions.")
        after = registry.get_agent("support")
        assert after.version != before
        assert after.instructions == "Entirely new instructions."


class TestLongListsInForms:
    def test_a_long_tool_list_gets_a_filter(self, client, estate):
        body = client.get("/agents/support").data.decode()
        assert 'data-filters="#mount-tools"' in body
        assert "Find a tool" in body

    def test_a_short_tool_list_does_not(self, client, registry):
        body = client.get("/agents/support").data.decode()
        assert 'data-filters="#mount-tools"' not in body

    def test_the_new_agent_form_gets_one_too(self, client, estate):
        body = client.get("/agents/new").data.decode()
        assert 'data-filters="#new-tools"' in body

    def test_every_tool_is_still_in_the_form(self, client, estate):
        """Filtering hides rows in the browser; it must not drop them from the
        form, or saving would silently unmount what is not visible."""
        body = client.get("/agents/support").data.decode()
        for i in (0, 30, 59):
            assert f'value="bulk_tool_{i:03d}"' in body


class TestWideRowsStayReadable:
    def test_a_widely_shared_tool_does_not_render_a_wall_of_chips(self, client, project,
                                                                  registry, store):
        for i in range(40):
            (project / "agents" / f"user_{i:03d}.yaml").write_text(
                f"name: user_{i:03d}\nmodel: mock/echo\n"
                "adapters:\n  tools:\n    - lookup_invoice\n")
        body = client.get("/tools?q=lookup_invoice").data.decode()
        assert "more</a>" in body
        assert body.count('class="chip"') <= 4
