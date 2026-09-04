"""The fences around the part that builds itself.

Jarvis working is not what these are about — a loop that calls a model until it
says DONE is not hard and is not the risk. The risk is the tree it writes into,
the door between that tree and the operator's, and the two numbers that stop it.
So: can it reach `agents/`, can something cross over without a person, does a
promoted tool stay sandboxed, and does a budget actually bite.
"""

from pathlib import Path

import pytest

from heddled import config, jarvis, yamlio


class Ctx:
    """What a builder tool gets. Only `log` is used."""

    def __init__(self):
        self.lines = []

    def log(self, message, **extra):
        self.lines.append(message)


@pytest.fixture()
def run(store):
    """A run record, and the thread-local that says which run we are in."""
    store.execute(
        "INSERT INTO jarvis_runs (id, goal, status, budget_eur, max_steps,"
        " started_by, created_at) VALUES ('j_test','build a thing','running',"
        "5.0, 10, 'tester', 0)")
    jarvis._current.run_id = "j_test"
    jarvis._current.session_id = None
    yield "j_test"
    jarvis._current.run_id = jarvis._current.session_id = None


def make_tool(name, **args):
    return jarvis._make_tool({"name": name, "description": "x", **args}, Ctx())


def make_agent(name, **args):
    return jarvis._make_agent(
        {"name": name, "description": "x", "instructions": "do the thing", **args},
        Ctx())


class TestItWritesOnlyIntoItsOwnTree:
    def test_an_agent_it_makes_lands_in_the_jarvis_tree(self, store, run):
        make_agent("summariser")
        assert (jarvis.agents_dir() / "summariser.yaml").is_file()
        assert not (Path(config.AGENTS_DIR) / "summariser.yaml").exists()

    def test_a_tool_it_makes_lands_in_the_jarvis_tree(self, store, run):
        make_tool("counter", kind="fixed", config={"result": {"n": 1}})
        assert (jarvis.tools_dir() / "counter" / "tool.yaml").is_file()
        assert not (Path(config.TOOLS_DIR) / "counter").exists()

    def test_it_cannot_give_an_agent_a_workspace(self, store, run):
        """The one field that decides which directory the file tools reach. It
        is written by us or not at all — a model that could set it could point
        an agent at the project."""
        make_agent("nosy", workspace="/", policies=[{"tool": "*"}])
        raw = yamlio.load((jarvis.agents_dir() / "nosy.yaml").read_text())
        assert "workspace" not in raw and "policies" not in raw

    def test_the_operators_agents_are_not_in_its_registry(self, store, registry, run):
        """`support` exists on this Heddled. Jarvis reads a different tree and
        does not see it, which is what makes 'it cannot edit them' structural
        rather than a rule it is asked to follow."""
        assert registry.get_agent("support") is not None
        assert jarvis.registry().get_agent("support") is None
        assert "refund" not in jarvis.registry().tools()

    def test_a_path_in_the_name_is_refused(self, store, run):
        for bad in ("../../support", "/etc/passwd", "a b/c"):
            with pytest.raises(ValueError, match="usable"):
                make_agent(bad)

    def test_it_cannot_take_its_own_name(self, store, run):
        with pytest.raises(ValueError):
            make_agent(jarvis.DRIVER)

    def test_it_cannot_mount_a_tool_it_has_not_made(self, store, registry, run):
        """`refund` is the operator's, and naming it must not quietly mount it."""
        with pytest.raises(ValueError, match="no tool called refund"):
            make_agent("greedy", tools=["refund"])


class TestPythonItWrites:
    def test_code_is_marked_for_the_sandbox(self, store, run):
        answer = make_tool("adder", code="def handle(args, ctx):\n    return {}\n")
        assert answer["python"] is True
        raw = yamlio.load((jarvis.tools_dir() / "adder" / "tool.yaml").read_text())
        assert raw["sandboxed"] is True

    def test_a_sandboxed_tool_actually_runs_in_a_child_process(self, store, run):
        """Not just flagged — the flag has to route it. In this process
        `heddled.store` imports and `get_store()` hands over every provider key;
        out there it is not on the path at all."""
        make_tool("peek", code=(
            "def handle(args, ctx):\n"
            "    try:\n"
            "        import heddled.store\n"
            "        return {'reached': True}\n"
            "    except ImportError:\n"
            "        return {'reached': False}\n"))
        assert __import__("heddled.store", fromlist=["x"])   # importable in here
        tool = jarvis.registry().tools()["peek"]
        assert tool.load_handler()({}, Ctx()) == {"reached": False}

    def test_it_cannot_send_email_as_the_operator(self, store, run):
        with pytest.raises(ValueError, match="not yours to send"):
            make_tool("mailer", kind="email", config={"to": "someone@example.com"})


class TestPromoting:
    def test_promoting_copies_an_agent_across(self, store, run):
        make_agent("summariser")
        path = jarvis.promote("agent", "summariser")
        assert Path(path) == Path(config.AGENTS_DIR) / "summariser.yaml"
        raw = yamlio.load(Path(path).read_text())
        assert raw["name"] == "summariser" and "made_by" not in raw
        assert (Path(config.AGENTS_DIR) / "summariser.md").is_file()

    def test_promoting_never_overwrites_something_of_yours(self, store, registry, run):
        """The interesting attack, and the reason promotion is by name: build an
        agent called `support` and let a tired administrator press the button."""
        make_agent("support")
        with pytest.raises(ValueError, match="already have an agent"):
            jarvis.promote("agent", "support")
        assert "Invoice and billing" in (Path(config.AGENTS_DIR) / "support.yaml").read_text()

    def test_promoting_never_overwrites_a_tool_of_yours(self, store, registry, run):
        make_tool("refund", kind="fixed", config={"result": {"refunded": True}})
        with pytest.raises(ValueError, match="already have a tool"):
            jarvis.promote("tool", "refund")

    def test_a_promoted_python_tool_stays_sandboxed(self, store, run):
        """Promoting says a person wants it, not that a person wrote it."""
        make_tool("adder", code="def handle(args, ctx):\n    return {}\n")
        path = jarvis.promote("tool", "adder")
        raw = yamlio.load((Path(path) / "tool.yaml").read_text())
        assert raw["sandboxed"] is True and "made_by" not in raw

    def test_promoting_something_that_is_not_there(self, store, run):
        with pytest.raises(ValueError, match="not one of Jarvis"):
            jarvis.promote("agent", "imaginary")

    def test_only_agents_and_tools(self, store, run):
        with pytest.raises(ValueError):
            jarvis.promote("policy", "anything")


class TestDiscarding:
    def test_it_takes_everything_the_run_made(self, store, run):
        make_agent("summariser")
        make_tool("counter", kind="fixed", config={"result": {}})
        what = jarvis.discard(run)
        assert [a["name"] for a in what["agents"]] == ["summariser"]
        assert not (jarvis.agents_dir() / "summariser.yaml").exists()
        assert not (jarvis.tools_dir() / "counter").exists()
        assert jarvis.get_run(run)["status"] == "discarded"

    def test_what_was_promoted_is_yours_and_stays(self, store, run):
        make_agent("summariser")
        jarvis.promote("agent", "summariser")
        jarvis.discard(run)
        assert (Path(config.AGENTS_DIR) / "summariser.yaml").is_file()

    def test_it_leaves_another_runs_work_alone(self, store, run):
        make_agent("mine")
        jarvis._current.run_id = "j_other"
        make_agent("theirs")
        jarvis._current.run_id = run
        jarvis.discard(run)
        assert not (jarvis.agents_dir() / "mine.yaml").exists()
        assert (jarvis.agents_dir() / "theirs.yaml").is_file()


class TestTheTwoNumbers:
    def test_a_run_needs_a_goal(self, store):
        with pytest.raises(ValueError, match="work on"):
            jarvis.start_run("   ", 1.0, 5, "tester")

    def test_a_budget_of_nothing_is_refused(self, store):
        with pytest.raises(ValueError, match="budget"):
            jarvis.start_run("something", 0, 5, "tester")

    def test_an_enormous_budget_is_refused(self, store):
        with pytest.raises(ValueError, match="budget"):
            jarvis.start_run("something", 10_000, 5, "tester")

    def test_an_endless_step_cap_is_refused(self, store):
        with pytest.raises(ValueError, match="step cap"):
            jarvis.start_run("something", 1.0, jarvis.MAX_STEPS + 1, "tester")

    def test_nothing_is_recorded_when_a_run_is_refused(self, store):
        with pytest.raises(ValueError):
            jarvis.start_run("something", 0, 5, "tester")
        assert jarvis.runs() == []

    def test_spending_counts_the_sessions_it_started_underneath(self, store, run):
        """Counting only the driver's own session would leave the loop free to
        spend the afternoon inside run_own_agent against a budget that never
        moved."""
        parent = store.create_session(agent="jarvis", channel="jarvis")
        child = store.create_session(agent="made_one", channel="jarvis",
                                     parent_session_id=parent)
        store.execute("UPDATE jarvis_runs SET session_id=? WHERE id=?", (parent, run))
        store.record_spend(agent="jarvis", session_id=parent, kind="eur", amount=0.40)
        store.record_spend(agent="made_one", session_id=child, kind="eur", amount=1.10)
        assert jarvis.spend(run) == pytest.approx(1.50)


class TestAskingTheOperatorsAgents:
    def test_it_can_ask_one(self, store, registry, worker, run):
        answer = jarvis._ask_agent({"name": "support", "message": "hello"}, Ctx())
        assert answer["reply"]

    def test_asking_one_that_does_not_exist_says_so(self, store, registry, run):
        with pytest.raises(ValueError, match="no agent called"):
            jarvis._ask_agent({"name": "nope", "message": "hi"}, Ctx())

    def test_it_arrives_on_the_jarvis_channel(self, store, registry, worker, run):
        """So `deny_channels: [jarvis]` on a policy means something."""
        jarvis._ask_agent({"name": "support", "message": "hello"}, Ctx())
        session = store.query(
            "SELECT * FROM sessions WHERE agent='support' ORDER BY created_at DESC")[0]
        assert session["channel"] == "jarvis"

    def test_running_one_of_its_own_refuses_the_operators(self, store, registry, run):
        with pytest.raises(ValueError, match="not one of the agents you made"):
            jarvis._run_own_agent({"name": "support", "message": "hi"}, Ctx())

    def test_and_refuses_itself(self, store, run):
        jarvis._write_driver()
        with pytest.raises(ValueError):
            jarvis._run_own_agent({"name": jarvis.DRIVER, "message": "hi"}, Ctx())

    def test_listing_shows_both_trees_apart(self, store, registry, run):
        make_agent("summariser")
        listing = jarvis._list_agents({}, Ctx())
        assert "summariser" in listing["yours"] and "support" not in listing["yours"]
        assert "support" in listing["theirs"]


class TestTheDriver:
    def test_it_gets_the_builders_and_a_workspace_of_its_own(self, store):
        jarvis._write_driver()
        reg = jarvis.registry()
        agent = reg.get_agent(jarvis.DRIVER)
        assert set(jarvis.BUILDERS) <= set(reg.agent_tools(agent))
        assert Path(agent.workspace) == jarvis.work_dir()

    def test_its_file_tools_reach_its_own_workspace_not_the_operators(self, store):
        """The workspace handler resolves the agent through the registry running
        the turn. Against the process-wide one it would find no `jarvis` at all."""
        from heddled import workspace

        jarvis._write_driver()
        agent = jarvis.registry().get_agent(jarvis.DRIVER)
        root = workspace.resolve_root(agent)
        assert root == jarvis.work_dir().resolve()
        assert Path(config.AGENTS_DIR) not in [root, *root.parents]

    def test_an_agent_it_makes_does_not_get_the_builders(self, store, run):
        """Otherwise the thing it built could build more of itself, which is a
        different and much larger promise than the one on the screen."""
        make_agent("summariser")
        reg = jarvis.registry()
        mounted = reg.agent_tools(reg.get_agent("summariser"))
        assert not set(jarvis.BUILDERS) & set(mounted)


class TestTheDoor:
    def test_the_tab_is_hidden_until_it_is_turned_on(self, client):
        assert "/jarvis" not in client.get("/").get_data(as_text=True)

    def test_and_the_screen_is_not_there_either(self, client):
        assert client.get("/jarvis").status_code == 404

    def test_turning_it_on_shows_the_tab(self, client, store):
        client.post("/settings/jarvis", data={"enabled": "on"})
        assert store.get_setting(jarvis.SETTING) is True
        assert "/jarvis" in client.get("/").get_data(as_text=True)
        assert client.get("/jarvis").status_code == 200

    def test_turning_it_off_closes_the_route_not_just_the_tab(self, client, store):
        store.set_setting(jarvis.SETTING, True)
        assert client.get("/jarvis").status_code == 200
        client.post("/settings/jarvis", data={})
        assert client.get("/jarvis").status_code == 404

    def test_a_member_cannot_open_it_even_when_it_is_on(self, client_as, store):
        store.set_setting(jarvis.SETTING, True)
        assert client_as("member").get("/jarvis").status_code == 403

    def test_a_viewer_cannot_either(self, client_as, store):
        store.set_setting(jarvis.SETTING, True)
        assert client_as("viewer").get("/jarvis").status_code == 403

    def test_a_member_cannot_start_a_run(self, client_as, store):
        store.set_setting(jarvis.SETTING, True)
        assert client_as("member").post(
            "/jarvis", data={"goal": "x", "budget": "1", "steps": "2"}).status_code == 403
        assert jarvis.runs() == []

    def test_a_bad_budget_comes_back_as_a_sentence(self, client, store):
        store.set_setting(jarvis.SETTING, True)
        answer = client.post("/jarvis", data={"goal": "x", "budget": "0", "steps": "2"},
                             follow_redirects=True)
        assert "budget has to be" in answer.get_data(as_text=True)

    def test_the_screen_says_what_it_can_and_cannot_reach(self, client, store):
        """The warnings are the feature. A page that starts an autonomous loop
        without them is the thing we said we would not ship."""
        store.set_setting(jarvis.SETTING, True)
        page = client.get("/jarvis").get_data(as_text=True)
        assert "promote" in page.lower()
        assert "child process" in page

    def test_a_run_screen_offers_what_it_made(self, client, store, run):
        store.set_setting(jarvis.SETTING, True)
        make_agent("summariser")
        page = client.get(f"/jarvis/{run}").get_data(as_text=True)
        assert "summariser" in page and "Promote" in page

    def test_promoting_goes_through_the_console(self, client, store, run):
        store.set_setting(jarvis.SETTING, True)
        make_agent("summariser")
        client.post(f"/jarvis/{run}/promote",
                    data={"kind": "agent", "name": "summariser"})
        assert (Path(config.AGENTS_DIR) / "summariser.yaml").is_file()

    def test_a_clash_is_reported_rather_than_resolved(self, client, store, registry, run):
        store.set_setting(jarvis.SETTING, True)
        make_agent("support")
        answer = client.post(f"/jarvis/{run}/promote",
                             data={"kind": "agent", "name": "support"},
                             follow_redirects=True)
        assert "already have an agent" in answer.get_data(as_text=True)

    def test_discarding_goes_through_the_console(self, client, store, run):
        store.set_setting(jarvis.SETTING, True)
        make_tool("counter", kind="fixed", config={"result": {}})
        client.post(f"/jarvis/{run}/discard")
        assert not (jarvis.tools_dir() / "counter").exists()

    def test_an_unknown_run_is_not_there(self, client, store):
        store.set_setting(jarvis.SETTING, True)
        assert client.get("/jarvis/j_nope").status_code == 404


class TestARunEndToEnd:
    def test_it_records_a_session_and_finishes(self, store, registry):
        """The stand-in model replies to everything, so this proves the loop
        runs, stops, and leaves a readable session behind — not that it builds
        anything useful."""
        import time

        run_id = jarvis.start_run("say hello", 1.0, 2, "tester")
        for _ in range(100):
            row = jarvis.get_run(run_id)
            if row["status"] != "running":
                break
            time.sleep(0.05)
        row = jarvis.get_run(run_id)
        assert row["status"] in ("done", "spent"), row["note"]
        assert row["steps"] >= 1 and row["session_id"]
        assert store.get_session(row["session_id"])["channel"] == "jarvis"
        # The turns are on the ordinary spine, which is what makes a run
        # readable in Activity like anything else.
        assert store.events_for_session(row["session_id"])
