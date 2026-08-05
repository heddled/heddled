"""The path a non-developer takes.

The product is only usable by an organisation if someone who does not write code
can get from an empty console to a working, governed agent. These tests walk
exactly that path and assert it never requires YAML, Python, or a terminal.
"""

import pytest


@pytest.fixture()
def blank(tmp_path, monkeypatch):
    """A console with nothing in it — no agents, no tools."""
    from heddled import config
    from heddled import registry as registry_mod
    from heddled import store as store_mod

    for name in ("agents", "tools", "data", "var"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(config, "TOOLS_DIR", tmp_path / "tools")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "VAR_DIR", tmp_path / "var")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "heddled.db")
    monkeypatch.setattr(store_mod, "_store", store_mod.Store(tmp_path / "data" / "heddled.db"))
    monkeypatch.setattr(registry_mod, "_registry",
                        registry_mod.Registry(tmp_path / "agents", tmp_path / "tools"))

    from heddled import users
    from heddled.web.app import create_app

    # An empty *project* — but somebody has claimed the console, which is the
    # state a real newcomer is in the moment after first-run setup.
    person = users.create(store_mod.get_store(), "newcomer", "a-good-test-password",
                          role="admin", created_by="tests")

    app = create_app(start_worker=False)
    app.config["TESTING"] = True
    with app.test_client() as c:
        c.environ_base = {**c.environ_base, "HTTP_ORIGIN": "http://localhost"}
        with c.session_transaction() as sess:
            sess["uid"] = person["id"]
        yield c, tmp_path


class TestEmptyConsoleTeaches:
    def test_the_home_page_explains_the_shape_of_the_product(self, blank):
        client, _ = blank
        body = client.get("/").data.decode()
        assert "Make a tool" in body and "Make an agent" in body

    def test_it_says_no_provider_account_is_needed(self, blank):
        client, _ = blank
        assert "mock/echo" in client.get("/").data.decode()

    def test_the_empty_tools_screen_invites_rather_than_apologises(self, blank):
        client, _ = blank
        body = client.get("/tools").data.decode()
        assert "Make your first tool" in body
        assert "without" in body or "don't need to write any code" in body


class TestNoJargonOnTheWayIn:
    """The words someone meets first should be words they already know."""

    JARGON = ["adapter", "spine", "turn engine", "golden trace", "registry",
              "system prompt", "YAML", "handler"]

    def test_the_new_agent_form_avoids_jargon(self, blank):
        client, _ = blank
        body = client.get("/agents/new").data.decode().lower()
        for word in self.JARGON:
            assert word.lower() not in body, f"'{word}' appears in the new-agent form"

    def test_the_tool_gallery_avoids_jargon(self, blank):
        client, _ = blank
        body = client.get("/tools/new").data.decode().lower()
        for word in self.JARGON:
            if word == "handler":
                continue  # the Python option may name it; that option is opt-in
            assert word.lower() not in body, f"'{word}' appears in the tool gallery"

    def test_the_tool_gallery_describes_outcomes_not_mechanisms(self, blank):
        client, _ = blank
        body = client.get("/tools/new").data.decode()
        assert "Call an API" in body and "Look something up in a list" in body

    def test_the_navigation_uses_plain_words(self, blank):
        client, _ = blank
        nav = client.get("/").data.decode()
        assert ">Activity" in nav and ">Tests" in nav and ">Publish" in nav


class TestNoJargonAnywhereItMatters:
    """A blunt instrument, on purpose. Without it this drifts straight back:
    every one of these words is natural to write and meaningless to a reader
    who did not build the thing."""

    # Top-level screens plus the *detail* pages, which is where jargon hides:
    # a fixed list of section URLs let the test-run page keep saying "Eval run",
    # "golden" and "assertions" through several passes that claimed to have
    # cleaned the vocabulary up.
    SCREENS = ["/", "/sessions", "/tools", "/tools/new", "/agents/new",
               "/evals", "/deployments", "/settings", "/users", "/account"]

    BANNED = [
        "adapter", "spine", "turn engine", "golden trace", "eval run",
        "session id", "promote to golden", "inbound message", "mock mode",
        "dev harness", "agent version ×", "eval run", "assertions",
    ]

    def _detail_pages(self, client):
        """Every screen you can actually reach by clicking, not just the ones
        in the nav."""
        from heddled import authoring

        authoring.new_tool("scratch", description="A tool.", input_spec="q:string")
        authoring.new_agent("scratch_agent", description="An agent.")
        return ["/tools/scratch", "/agents/scratch_agent",
                "/agents/scratch_agent/test", "/tools/new?type=http",
                "/tools/new?type=lookup"]

    def test_no_banned_word_survives_on_a_detail_page_either(self, blank):
        client, _ = blank
        offences = []
        import re

        for path in self._detail_pages(client):
            body = client.get(path).data.decode()
            # A raw-file view is *meant* to show the platform's own vocabulary;
            # that is the point of having one. Everything around it is not.
            body = re.sub(r"<textarea[^>]*>.*?</textarea>", "", body, flags=re.S).lower()
            for word in self.BANNED:
                if word in body:
                    offences.append(f"{path}: '{word}'")
        assert not offences, "jargon on a detail page: " + "; ".join(offences)

    def test_no_banned_word_survives_on_any_screen(self, blank):
        client, _ = blank
        offences = []
        for path in self.SCREENS:
            body = client.get(path).data.decode().lower()
            for word in self.BANNED:
                if word in body:
                    offences.append(f"{path}: '{word}'")
        assert not offences, "jargon leaked back: " + "; ".join(offences)

    def test_an_agent_page_is_clean_outside_the_raw_file_box(self, blank):
        """The raw file keeps the platform's own vocabulary — that is the
        point of it — so it is excluded rather than sanitised."""
        import re

        from heddled import authoring

        client, _ = blank
        authoring.new_agent("checker", description="Checks things.")
        body = client.get("/agents/checker").data.decode()
        outside = re.sub(r'<textarea name="definition".*?</textarea>', "", body, flags=re.S)
        for word in ["Adapters", "adapters:", "golden", "Promote"]:
            assert word not in outside, f"'{word}' appears on the agent page"


class TestTheWholePathWithoutCode:
    """Empty console → a governed agent that works. No file editing anywhere."""

    def _make_tool(self, client):
        return client.post("/tools", data={
            "type": "lookup", "name": "office_location",
            "description": "Find which office a team sits in.",
            "input_name": "team", "input_type": "string",
            "config__key": "team",
            "config__table__key": ["finance", "engineering"],
            "config__table__value": ["Rotterdam", "Amsterdam"],
        })

    def _make_agent(self, client, **extra):
        data = {
            "name": "office_helper",
            "description": "Answers questions about our offices.",
            "instructions": "You help colleagues find which office a team sits in.",
            "tools": "office_location",
            "model": "mock/echo",
        }
        data.update(extra)
        return client.post("/agents", data=data)

    def test_a_tool_is_created_from_a_form_with_no_code(self, blank):
        client, root = blank
        assert self._make_tool(client).status_code == 302
        assert (root / "tools" / "office_location" / "tool.yaml").exists()
        assert not (root / "tools" / "office_location" / "handler.py").exists()

    def test_the_new_tool_works_straight_away(self, blank):
        client, _ = blank
        self._make_tool(client)
        body = client.post("/api/tools/office_location/test",
                           json={"args": {"team": "finance"}}).get_json()
        assert body["ok"] and body["result"]["value"] == "Rotterdam"

    def test_an_agent_is_created_with_its_tools_already_attached(self, blank):
        client, _ = blank
        self._make_tool(client)
        self._make_agent(client)

        from heddled.registry import get_registry

        agent = get_registry().get_agent("office_helper")
        assert agent.tool_names == ["office_location"]
        assert "find which office" in agent.instructions.lower()

    def test_ticking_ask_me_first_writes_a_real_approval_gate(self, blank):
        client, _ = blank
        self._make_tool(client)
        self._make_agent(client, approval_tools="office_location")

        from heddled.registry import get_registry

        policy = get_registry().get_agent("office_helper").policy_for_tool("office_location")
        assert policy["requires_approval"] is True

    def test_creating_an_agent_lands_you_in_a_chat_window(self, blank):
        client, _ = blank
        self._make_tool(client)
        response = self._make_agent(client)
        assert "/test" in response.headers["Location"]

    def test_that_chat_window_explains_itself(self, blank):
        client, _ = blank
        self._make_tool(client)
        self._make_agent(client)
        body = client.get("/agents/office_helper/test?created=1").data.decode()
        assert "is ready" in body

    def test_the_agent_then_actually_answers(self, blank, monkeypatch):
        """The end of the path: it works, and the gate fires."""
        from heddled import runtime
        from heddled import worker as worker_mod

        client, _ = blank
        self._make_tool(client)
        self._make_agent(client, approval_tools="office_location")

        w = worker_mod.Worker(concurrency=1, run_triggers=False)
        monkeypatch.setattr(worker_mod, "_worker", w)
        w.start()
        try:
            result = runtime.submit_message(
                "office_helper", "which office is the finance team in?",
                sync=True, timeout_s=20)
            # Gated, because they ticked "ask me before".
            assert result["status"] == "waiting-approval"
        finally:
            w.stop()

    def test_and_without_the_gate_it_answers_outright(self, blank, monkeypatch):
        from heddled import runtime
        from heddled import worker as worker_mod

        client, _ = blank
        self._make_tool(client)
        self._make_agent(client)

        w = worker_mod.Worker(concurrency=1, run_triggers=False)
        monkeypatch.setattr(worker_mod, "_worker", w)
        w.start()
        try:
            result = runtime.submit_message(
                "office_helper", "which office is the finance team in?",
                sync=True, timeout_s=20)
            assert result["status"] == "completed"
            assert "Rotterdam" in result["reply"]
        finally:
            w.stop()


class TestStarterInstructions:
    def test_the_instructions_box_is_prefilled_with_a_briefing(self, blank):
        client, _ = blank
        body = client.get("/agents/new").data.decode()
        assert "How to behave" in body and "Never" in body

    def test_it_reads_as_guidance_not_a_template_to_decode(self, blank):
        from heddled.authoring import STARTER_INSTRUCTIONS

        assert "{" not in STARTER_INSTRUCTIONS  # no placeholder syntax to learn
        assert "______" in STARTER_INSTRUCTIONS  # an obvious blank to fill in
