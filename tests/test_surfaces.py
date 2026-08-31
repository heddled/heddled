"""Three surfaces over things the platform already did but never showed.

The approvals queue, reporting a bad reply as a test, and where the money went.
Each one is a view over machinery that existed — pending approvals, golden
traces, and a ledger that had been written to since the first turn and read
back by nothing at all.
"""

import json

from heddled import yamlio


def _open_chat(project, name="support"):
    path = project / "agents" / f"{name}.yaml"
    data = yamlio.load(path.read_text())
    data.setdefault("expose", {})["chat"] = True
    path.write_text(yamlio.dump(data))


def _seed_chat(agent="support", who="tester", text="where is invoice F-2231?"):
    from heddled import runtime

    return runtime.submit_message(
        agent, text, channel="chat", origin={"kind": "chat", "who": who},
        sender=who, sync=True, timeout_s=25)


def _waiting(store, agent="support", tool="refund"):
    """One approval, as the engine would leave it."""
    return store.create_approval(
        session_id="s_x", turn_id="t_x", agent=agent, tool=tool,
        # create_approval serialises for you; passing JSON here would store a
        # string of a string, which is how this bit has been got wrong before.
        args={"invoice_number": "F-2231", "amount_eur": 249.0},
        reason="over the limit that needs a person",
    )


# ------------------------------------------------------------------ approvals


class TestTheApprovalsQueue:
    def test_it_lists_what_is_waiting(self, client, store):
        _waiting(store)
        body = client.get("/approvals").get_data(as_text=True)
        assert "refund" in body
        assert "F-2231" in body, "an approver has to see what they are deciding"

    def test_it_says_so_when_nothing_is(self, client):
        assert "Nothing is waiting" in client.get("/approvals").get_data(as_text=True)

    def test_it_carries_none_of_the_console(self, client, store):
        """Same reasoning as the chat page: somebody whose job is signing things
        off should not be handed the whole estate to do it."""
        _waiting(store)
        body = client.get("/approvals").get_data(as_text=True)
        for leak in ('href="/settings"', 'href="/sessions"', 'href="/tools"',
                     'href="/users"', 'href="/deployments"'):
            assert leak not in body, f"the console reached the inbox: {leak}"

    def test_approving_resolves_it(self, client, store):
        aid = _waiting(store)
        client.post(f"/approvals/{aid}", data={"decision": "approved"})
        assert store.get_approval(aid)["status"] == "approved"

    def test_refusing_resolves_it_too(self, client, store):
        aid = _waiting(store)
        client.post(f"/approvals/{aid}", data={"decision": "denied"})
        assert store.get_approval(aid)["status"] == "denied"

    def test_the_decision_is_recorded_against_a_name(self, client, store, admin):
        aid = _waiting(store)
        client.post(f"/approvals/{aid}", data={"decision": "approved",
                                               "note": "checked with finance"})
        row = store.get_approval(aid)
        assert row["resolver"] == admin["username"]
        assert row["note"] == "checked with finance"

    def test_a_second_decision_does_not_overwrite_the_first(self, client, store):
        aid = _waiting(store)
        client.post(f"/approvals/{aid}", data={"decision": "approved"})
        client.post(f"/approvals/{aid}", data={"decision": "denied"})
        assert store.get_approval(aid)["status"] == "approved"

    def test_a_decision_that_is_not_one_is_refused(self, client, store):
        aid = _waiting(store)
        client.post(f"/approvals/{aid}", data={"decision": "maybe"})
        assert store.get_approval(aid)["status"] == "pending"

    def test_a_viewer_may_decide(self, client_as, store):
        """The read-only rule is about not changing Heddled's configuration.
        Requiring `member` to sign something off would hand an approver the
        agent files as well — more power, not less."""
        aid = _waiting(store)
        client_as("viewer").post(f"/approvals/{aid}", data={"decision": "approved"})
        assert store.get_approval(aid)["status"] == "approved"

    def test_but_a_viewer_still_cannot_change_anything(self, client_as):
        viewer = client_as("viewer")
        assert viewer.post("/agents", data={"name": "sneaky"}).status_code == 403

    def test_a_stranger_gets_the_door(self, anon_client, store):
        _waiting(store)
        r = anon_client.get("/approvals")
        assert r.status_code == 302
        assert r.headers["Location"].rstrip("/").endswith(("/login", "/setup"))


# -------------------------------------------------------------------- report


class TestReportingABadReply:
    def test_it_becomes_a_test(self, client, project, registry, worker, store):
        _open_chat(project)
        sid = _seed_chat()["session_id"]
        r = client.post("/chat/support/report",
                        json={"session_id": sid, "note": "the amount was wrong"})
        assert r.status_code == 200
        goldens = store.goldens("support")
        assert goldens, "reporting should leave a golden trace behind"
        assert goldens[0]["session_id"] == sid

    def test_what_they_said_is_kept_with_it(self, client, project, registry,
                                            worker, store, admin):
        _open_chat(project)
        sid = _seed_chat()["session_id"]
        client.post("/chat/support/report",
                    json={"session_id": sid, "note": "should have said unpaid"})
        spec = json.loads(store.goldens("support")[0]["spec"])
        assert spec["reported"]["note"] == "should have said unpaid"
        assert spec["reported"]["by"] == admin["username"]

    def test_it_is_named_so_an_operator_can_tell_where_it_came_from(
            self, client, project, registry, worker, store, admin):
        _open_chat(project)
        sid = _seed_chat()["session_id"]
        client.post("/chat/support/report", json={"session_id": sid, "note": ""})
        assert admin["username"] in store.goldens("support")[0]["name"]

    def test_somebody_elses_conversation_cannot_be_reported(
            self, client, client_as, project, registry, worker):
        _open_chat(project)
        sid = _seed_chat()["session_id"]
        other = client_as("member")
        assert other.post("/chat/support/report",
                          json={"session_id": sid, "note": "x"}).status_code == 404

    def test_an_agent_not_open_for_chat_has_no_report_endpoint(
            self, client, registry):
        assert client.post("/chat/support/report",
                           json={"session_id": "s_x"}).status_code == 404


# ------------------------------------------------------------------ spending


class TestWhereTheMoneyWent:
    def test_the_screen_renders_with_nothing_recorded(self, client):
        body = client.get("/spending").get_data(as_text=True)
        assert "Spending" in body
        assert "Nothing has been spent yet" in body

    def test_it_adds_up_what_the_ledger_holds(self, client, store):
        store.record_spend("eur", 1.50, agent="support")
        store.record_spend("eur", 2.25, agent="support", tool="lookup_invoice")
        store.record_spend("tokens", 4000, agent="support")
        body = client.get("/spending").get_data(as_text=True)
        assert "3.75" in body, "the total should be the sum of the ledger"
        assert "4,000" in body

    def test_it_breaks_down_by_assistant_and_action(self, client, store):
        store.record_spend("eur", 5.0, agent="support", tool="refund")
        body = client.get("/spending").get_data(as_text=True)
        assert "support" in body and "refund" in body

    def test_the_caps_that_were_set_are_shown_against_the_spend(
            self, client, store, registry, project):
        """The point of the screen: a budget you cannot see is a budget set
        blind, and `support` has capped refunds at 500 since the beginning."""
        store.record_spend("eur", 12.0, agent="support")
        body = client.get("/spending").get_data(as_text=True)
        assert "500.00" in body
        assert "12.00" in body

    def test_the_window_can_be_changed(self, client):
        assert client.get("/spending?days=7").status_code == 200

    def test_a_viewer_can_look(self, client_as):
        assert client_as("viewer").get("/spending").status_code == 200


class TestTheLedgerQueries:
    def test_by_day_is_oldest_first(self, store):
        store.record_spend("eur", 1.0, agent="a")
        rows = store.spend_by_day("eur", days=30)
        assert rows and rows[-1]["total"] >= 1.0

    def test_by_agent_groups_and_sorts(self, store):
        store.record_spend("eur", 1.0, agent="small")
        store.record_spend("eur", 9.0, agent="big")
        rows = store.spend_by_agent("eur")
        assert [r["agent"] for r in rows] == ["big", "small"]

    def test_by_tool_ignores_entries_with_no_tool(self, store):
        store.record_spend("eur", 1.0, agent="a")
        store.record_spend("eur", 2.0, agent="a", tool="refund")
        rows = store.spend_by_tool("eur")
        assert [r["tool"] for r in rows] == ["refund"]

    def test_kinds_do_not_mix(self, store):
        store.record_spend("eur", 3.0, agent="a")
        store.record_spend("tokens", 1000, agent="a")
        assert store.spend_total("eur") == 3.0
        assert store.spend_total("tokens") == 1000
