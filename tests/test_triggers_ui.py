"""Setting an agent going without being asked, and the ways in from outside.

The console offered two of the five ways work reaches an agent — a clock and a
folder — so the other three were invisible: a mailbox the platform has always
been able to watch, and the webhook and MCP addresses other systems can send to.
"""

import pytest

from heddled import authoring


class TestWhatTheFormOffers:
    def test_all_three_self_starting_kinds_are_offered(self, client, registry):
        body = client.get("/agents/support").data.decode()
        for label in ("A time of day or week", "A file arriving in a folder",
                      "An email arriving in a mailbox"):
            assert label in body

    def test_a_folder_trigger_is_written(self, client, registry):
        client.post("/agents/support/triggers",
                    data={"kind": "folder", "path": "./var/invoices", "every": "30s",
                          "message": "Handle this invoice."})
        trigger = registry.get_agent("support").triggers[-1].raw
        assert trigger["poll"] == "mailbox"
        assert trigger["config"] == {"source": "folder", "path": "./var/invoices"}
        assert trigger["every"] == "30s"

    def test_a_mailbox_trigger_is_written(self, client, registry):
        client.post("/agents/support/triggers",
                    data={"kind": "email", "imap_host": "imap.example.com",
                          "imap_user": "invoices@example.com",
                          "imap_password": "hunter2-and-then-some",
                          "mailbox_folder": "Invoices", "every": "5m",
                          "message": "Handle this email."})
        trigger = registry.get_agent("support").triggers[-1].raw
        assert trigger["poll"] == "mailbox"
        assert trigger["config"] == {"source": "imap", "folder": "Invoices"}
        assert trigger["on_new"] == "Handle this email."

    def test_the_mailbox_password_stays_out_of_the_agent_file(self, client, store,
                                                              registry, project):
        client.post("/agents/support/triggers",
                    data={"kind": "email", "imap_host": "imap.example.com",
                          "imap_user": "invoices@example.com",
                          "imap_password": "hunter2-and-then-some",
                          "message": "Handle this."})
        text = (project / "agents" / "support.yaml").read_text()
        assert "hunter2" not in text
        assert store.get_setting("imap_password") == "hunter2-and-then-some"
        assert store.get_setting("imap_host") == "imap.example.com"

    def test_a_schedule_still_works(self, client, registry):
        client.post("/agents/support/triggers",
                    data={"kind": "schedule", "when": "weekdays", "at": "07:30",
                          "message": "Morning summary."})
        assert registry.get_agent("support").triggers[-1].raw["schedule"] == "30 7 * * 1-5"


class TestTheMailboxPollerFindsItsCredentials:
    """The poller read config and environment only, so a mailbox set up in the
    console could never sign in."""

    def _poller(self, settings):
        from heddled.adapters import get_poller

        return get_poller("mailbox", settings)

    def test_settings_are_used_when_the_trigger_does_not_carry_them(self, monkeypatch):
        for var in ("HEDDLED_IMAP_HOST", "HEDDLED_IMAP_USER", "HEDDLED_IMAP_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        seen = {}

        import heddled.adapters.mailbox as mailbox

        class FakeIMAP:
            def __init__(self, host, port):
                seen["host"] = host

            def login(self, user, password):
                seen["user"], seen["password"] = user, password

            def select(self, folder):
                seen["folder"] = folder

            def uid(self, *a):
                return "OK", [b""]

            def logout(self):
                pass

        monkeypatch.setattr(mailbox.imaplib, "IMAP4_SSL", FakeIMAP)
        poller = self._poller({"imap_host": "imap.example.com",
                               "imap_user": "someone@example.com",
                               "imap_password": "a-password"})
        poller.poll(None, {"source": "imap", "folder": "Invoices"})
        assert seen == {"host": "imap.example.com", "user": "someone@example.com",
                        "password": "a-password", "folder": "Invoices"}

    def test_a_missing_detail_says_which_one(self, monkeypatch):
        for var in ("HEDDLED_IMAP_HOST", "HEDDLED_IMAP_USER", "HEDDLED_IMAP_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        poller = self._poller({"imap_host": "imap.example.com"})
        with pytest.raises(RuntimeError, match="user, password"):
            poller.poll(None, {"source": "imap"})

    def test_a_folder_poll_needs_no_credentials(self, project):
        from heddled.adapters import get_poller

        watched = project / "var" / "drop"
        watched.mkdir(parents=True, exist_ok=True)
        (watched / "note.txt").write_text("an invoice")
        items, cursor = get_poller("mailbox", {}).poll(
            None, {"source": "folder", "path": str(watched)})
        assert len(items) == 1


class TestTheListSaysWhatItWatches:
    """Every poller described itself as "whenever something arrives", which said
    nothing about whether it was a folder or a mailbox, or which one."""

    def test_a_folder_trigger_names_the_folder(self, client, registry):
        client.post("/agents/support/triggers",
                    data={"kind": "folder", "path": "./var/invoices",
                          "message": "Handle it."})
        body = client.get("/agents/support").data.decode()
        assert "Every file arriving in ./var/invoices" in body

    def test_a_mailbox_trigger_names_the_mailbox(self, client, registry):
        client.post("/agents/support/triggers",
                    data={"kind": "email", "mailbox_folder": "Invoices",
                          "imap_host": "imap.example.com", "imap_user": "a@b.c",
                          "imap_password": "a-password", "message": "Handle it."})
        body = client.get("/agents/support").data.decode()
        assert "Every email arriving in Invoices" in body

    def test_a_schedule_shows_both_the_words_and_the_expression(self, client, registry):
        body = client.get("/agents/support").data.decode()
        assert "Weekdays at 08:00" in body and "0 8 * * 1-5" in body

    def test_removing_one_says_what_stops(self, client, registry):
        client.post("/agents/support/triggers",
                    data={"kind": "folder", "path": "./var/invoices", "message": "Go."})
        body = client.get("/agents/support").data.decode()
        assert "will no longer start a conversation" in body

    def test_a_poller_that_has_never_run_says_so(self, client, registry):
        client.post("/agents/support/triggers",
                    data={"kind": "folder", "path": "./var/fresh", "message": "Go."})
        assert "not checked yet" in client.get("/agents/support").data.decode()


class TestApprovalsOnlyOfferWhatWasChosen:
    """Step 4 listed every tool in the console, including the ones the new agent
    was never given — a question about nothing, buried among hundreds."""

    def test_the_gate_list_starts_empty(self, client, registry):
        body = client.get("/agents/new").data.decode()
        assert 'id="gate-tools" data-mirrors="#new-tools"' in body
        assert "Pick what it can do in step 3 first" in body

    def test_its_rows_start_hidden(self, client, registry):
        body = client.get("/agents/new").data.decode()
        gates = body[body.index('id="gate-tools"'):]
        assert gates.count("<li hidden>") >= 1

    def test_what_was_chosen_is_summarised(self, client, registry):
        body = client.get("/agents/new").data.decode()
        assert 'data-chosen-for="#new-tools"' in body

    def test_the_form_still_carries_every_tool(self, client, registry):
        """Hiding is presentation — the server decides what a submission means."""
        body = client.get("/agents/new").data.decode()
        for name in ("lookup_invoice", "refund"):
            assert body.count(f'value="{name}"') >= 2   # once per list


class TestTheChatHandlesItsOwnMishaps:
    def test_a_paused_turn_links_to_the_approval(self, client):
        """It used to say "look under Activity" — a dead end from the one screen
        where you are already looking at the thing that paused."""
        body = client.get("/agents/support/test").data.decode()
        assert "Approve or refuse it" in body
        assert "/sessions/${d.session_id}" in body

    def test_losing_the_connection_is_reported(self, client):
        body = client.get("/agents/support/test").data.decode()
        assert "Could not reach Heddled" in body

    def test_the_form_closes_while_a_turn_is_running(self, client):
        """A second message into a running conversation races the first."""
        body = client.get("/agents/support/test").data.decode()
        assert "function busy(" in body and "sendBtn.disabled = on" in body

    def test_starting_fresh_keeps_the_empty_state(self, client):
        """Emptying the whole chat took the openers and the explanation with it,
        leaving a blank white box."""
        body = client.get("/agents/support/test").data.decode()
        reset = body.split("function newSession")[1][:400]
        assert "showEmpty()" in reset
        assert "#chat .bubble" in reset          # bubbles go, the empty state stays
        assert "getElementById('chat').innerHTML = ''" not in reset


class TestEmptyScreensSayWhatToDo:
    def test_activity_distinguishes_empty_from_filtered(self, client, store):
        blank = client.get("/sessions").data.decode()
        assert "No conversations yet" in blank
        filtered = client.get("/sessions?agent=support").data.decode()
        assert "Nothing matches those filters" in filtered
        assert "Show everything" in filtered

    def test_a_page_past_the_end_offers_the_way_back(self, client, store):
        store.create_session(agent="support", agent_version="v1")
        body = client.get("/sessions?page=4").data.decode()
        assert "Nothing this far back" in body


class TestWaysInAreVisible:
    def test_the_webhook_address_is_shown(self, client, registry):
        body = client.get("/agents/support").data.decode()
        assert "/api/agents/support/webhook" in body

    def test_the_mcp_address_is_shown(self, client, registry):
        body = client.get("/agents/support").data.decode()
        assert "/mcp/support" in body

    def test_each_one_says_whether_it_is_open(self, client, registry):
        body = client.get("/agents/support").data.decode()
        assert "How other systems reach it" in body
        # support mounts webchat and webhook, and exposes MCP.
        assert "mount <b>webhook</b> to open it" not in body

    def test_an_unmounted_way_in_says_how_to_open_it(self, client, project, registry):
        (project / "agents" / "quiet.yaml").write_text(
            "name: quiet\nmodel: mock/echo\nadapters:\n  channels: [webchat]\n")
        body = client.get("/agents/quiet").data.decode()
        assert "mount <b>webhook</b> to open it" in body
        assert "let other systems use it" in body

    def test_the_addresses_can_be_copied(self, client):
        body = client.get("/agents/support").data.decode()
        assert "data-copy" in body


class TestChoosingToolsIsNotAWallOfBoxes:
    def test_what_it_can_do_is_summarised_before_the_picker(self, client):
        body = client.get("/agents/support").data.decode()
        assert 'data-chosen-for="#mount-tools"' in body
        assert body.index("mount-tools-chosen") < body.index('id="mount-tools"')

    def test_a_long_list_starts_folded(self, client, project, registry):
        for i in range(12):
            d = project / "tools" / f"extra_{i}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "tool.yaml").write_text(
                f"name: extra_{i}\ndescription: One of many.\ntype: fixed\n"
                "config:\n  result: {ok: true}\ninput: {}\noutput: {ok: boolean}\n")
        body = client.get("/agents/support").data.decode()
        assert '<details class="picker" >' in body or '<details class="picker">' in body

    def test_a_short_list_starts_open(self, client, registry):
        body = client.get("/agents/support").data.decode()
        assert '<details class="picker" open>' in body

    def test_a_hidden_row_is_actually_hidden(self, client):
        """`.checks li { display: flex }` beats the browser's own [hidden] rule,
        so filtering marked every row hidden and changed nothing on screen."""
        css = (client.application.static_folder + "/console.css")
        with open(css, encoding="utf-8") as fh:
            assert "[hidden] { display: none !important; }" in fh.read()

    def test_specialists_are_summarised_and_folded_too(self, client, project, registry):
        for i in range(12):
            (project / "agents" / f"helper_{i}.yaml").write_text(
                f"name: helper_{i}\nmodel: mock/echo\n")
        body = client.get("/agents/support").data.decode()
        assert 'data-chosen-for="#mount-agents"' in body
        assert "Choose specialists" in body

    def test_every_tool_is_still_submitted(self, client, registry):
        """Folding is presentation. The form still carries every checkbox, so a
        save does not silently unmount what is out of sight."""
        body = client.get("/agents/support").data.decode()
        for name in ("lookup_invoice", "refund"):
            assert f'value="{name}"' in body


class TestTheTraceIsHonestAboutItself:
    def test_the_placeholder_goes_when_steps_arrive(self, client):
        """"Each step appears here as it happens" stayed on screen above the
        steps once they started appearing."""
        js = (client.application.static_folder + "/trace.js")
        with open(js, encoding="utf-8") as fh:
            source = fh.read()
        assert "timeline-empty" in source.split("function appendEvent")[1][:400]

    def test_a_dropped_stream_stops_claiming_to_be_live(self, client):
        js = (client.application.static_folder + "/trace.js")
        with open(js, encoding="utf-8") as fh:
            source = fh.read()
        assert "source.onerror = () => setLive(false)" in source
        assert "reconnecting" in source

    def test_filtering_everything_away_says_so(self, client):
        js = (client.application.static_folder + "/trace.js")
        with open(js, encoding="utf-8") as fh:
            assert "No step matches" in fh.read()


class TestTestsScreenOffersOnlyWhatCanRun:
    def test_an_agent_with_no_saved_tests_cannot_be_chosen(self, client, registry):
        body = client.get("/evals").data.decode()
        assert "nothing saved yet" in body
        assert "disabled" in body

    def test_the_run_button_is_off_until_something_is_saved(self, client):
        body = client.get("/evals").data.decode()
        assert 'id="run-tests"' in body
        assert "Nothing to run yet" in body

    def test_an_agent_with_tests_shows_how_many(self, client, store, registry):
        sid = store.create_session(agent="support", agent_version="v1")
        store.add_golden("a saved chat", "support", sid, {"messages": []})
        body = client.get("/evals").data.decode()
        assert "1 test" in body
        assert "Nothing to run yet" not in body

    def test_a_running_test_can_be_watched(self, client, store, registry):
        store.create_eval_run("support", "v1")
        body = client.get("/evals").data.decode()
        assert "running-eval" in body
        r = client.get("/api/evals/runs?limit=1")
        assert r.status_code == 200
        assert r.get_json()["runs"][0]["status"] == "running"
