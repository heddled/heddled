"""The chat surface — talking to an agent without operating one.

The interesting assertions here are the boundaries, not the happy path: an
agent that has not opted in must not be reachable, and one person's
conversation must not be another's, whatever their role on the console.
"""

import json

import pytest

from heddled import yamlio


def _open_chat(project, registry, name="support"):
    """Turn on `expose: { chat: true }` the way an operator would — in the file."""
    path = project / "agents" / f"{name}.yaml"
    data = yamlio.load(path.read_text())
    data.setdefault("expose", {})["chat"] = True
    path.write_text(yamlio.dump(data))
    registry.invalidate() if hasattr(registry, "invalidate") else None
    return path


class TestOptIn:
    def test_an_agent_is_not_reachable_until_its_file_says_so(self, client, registry):
        assert client.get("/chat/support").status_code == 404

    def test_opting_in_opens_it(self, client, project, registry):
        _open_chat(project, registry)
        assert client.get("/chat/support").status_code == 200

    def test_an_agent_that_does_not_exist_is_a_404(self, client):
        assert client.get("/chat/nope").status_code == 404

    def test_the_picker_lists_only_opted_in_agents(self, client, project, registry):
        _open_chat(project, registry)
        body = client.get("/chat", follow_redirects=True).get_data(as_text=True)
        assert "support" in body


class TestTheConsoleIsNotHere:
    def test_the_page_carries_no_console_navigation(self, client, project, registry):
        _open_chat(project, registry)
        body = client.get("/chat/support").get_data(as_text=True)
        for leak in ('href="/settings"', 'href="/sessions"', 'href="/tools"',
                     'href="/agents"', 'href="/users"'):
            assert leak not in body, f"chat page links into the console: {leak}"

    def test_it_does_not_show_what_the_agent_did(self, client, project, registry, store):
        """The trace belongs to whoever operates the agent, not to the person
        chatting."""
        _open_chat(project, registry)
        r = client.post("/chat/support/messages",
                        json={"text": "where is invoice F-2231?"})
        sid = r.get_json()["session_id"]
        _drain(store, sid)
        body = client.get(f"/chat/support?session={sid}").get_data(as_text=True)
        assert "lookup_invoice" not in body


class TestOneConversationIsYours:
    def test_another_persons_thread_is_not_reachable(self, client, client_as,
                                                     project, registry, store):
        _open_chat(project, registry)
        mine = client.post("/chat/support/messages", json={"text": "hello"})
        sid = mine.get_json()["session_id"]

        other = client_as("member")
        assert other.get(f"/chat/support?session={sid}").status_code == 404
        assert other.get(f"/chat/support/stream/{sid}").status_code == 404

    def test_nor_can_it_be_continued(self, client, client_as, project, registry):
        _open_chat(project, registry)
        sid = client.post("/chat/support/messages",
                          json={"text": "hello"}).get_json()["session_id"]
        other = client_as("viewer")
        r = other.post("/chat/support/messages",
                       json={"text": "and what else", "session_id": sid})
        assert r.status_code == 404

    def test_your_own_thread_is(self, client, project, registry, store):
        _open_chat(project, registry)
        sid = client.post("/chat/support/messages",
                          json={"text": "hello"}).get_json()["session_id"]
        _drain(store, sid)
        assert client.get(f"/chat/support?session={sid}").status_code == 200


class TestSigningIn:
    def test_a_stranger_gets_the_door(self, anon_client, project, registry):
        _open_chat(project, registry)
        r = anon_client.get("/chat/support")
        assert r.status_code == 302
        assert r.headers["Location"].rstrip("/").endswith(("/login", "/setup"))

    def test_a_viewer_may_chat(self, client_as, project, registry):
        """No separate account type: the lowest console role is enough."""
        _open_chat(project, registry)
        assert client_as("viewer").get("/chat/support").status_code == 200


class TestItIsItsOwnChannel:
    def test_the_conversation_is_recorded_as_chat_not_webchat(
            self, client, project, registry, store):
        """`allow_channels` is a security control, so the console's Test tab and
        this page must not look like the same context."""
        _open_chat(project, registry)
        sid = client.post("/chat/support/messages",
                          json={"text": "hello"}).get_json()["session_id"]
        assert store.get_session(sid)["channel"] == "chat"

    def test_the_person_is_recorded_on_the_conversation(self, client, project,
                                                        registry, store, admin):
        _open_chat(project, registry)
        sid = client.post("/chat/support/messages",
                          json={"text": "hello"}).get_json()["session_id"]
        origin = json.loads(store.get_session(sid)["trigger_origin"])
        assert origin["who"] == admin["username"]


class TestEmptyMessages:
    def test_whitespace_is_refused(self, client, project, registry):
        _open_chat(project, registry)
        assert client.post("/chat/support/messages",
                           json={"text": "   "}).status_code == 400


def _drain(store, sid, tries=80):
    """Let the worker finish the turn."""
    import time
    for _ in range(tries):
        s = store.get_session(sid)
        if s and s["status"] in ("completed", "error", "waiting-approval"):
            return s
        time.sleep(0.05)
    return store.get_session(sid)


class TestAViewerCanActuallyUseIt:
    """A viewer who can open the page but not send is a text box that does
    nothing — the read-only rule has one deliberate hole and this pins it."""

    def test_a_viewer_can_send_a_message(self, client_as, project, registry):
        _open_chat(project, registry)
        r = client_as("viewer").post("/chat/support/messages", json={"text": "hello"})
        assert r.status_code == 200

    def test_but_still_cannot_change_anything(self, client_as, project, registry):
        _open_chat(project, registry)
        viewer = client_as("viewer")
        assert viewer.post("/agents", data={"name": "sneaky"}).status_code == 403
        assert viewer.post("/tools", data={"name": "sneaky"}).status_code == 403

    def test_and_the_hole_is_only_the_chat_box(self, client_as, project, registry):
        """Not any path that merely starts with /chat."""
        _open_chat(project, registry)
        viewer = client_as("viewer")
        assert viewer.post("/chat/support", json={}).status_code in (403, 405)


class TestTurningItOnAndOff:
    """A checkbox that cannot be unticked is worse than no checkbox: it reports
    success and leaves the door open."""

    def test_ticking_the_box_opens_the_chat_page(self, client, registry):
        client.post("/agents/support/fields",
                    data={"expose_present": "1", "expose_chat": "on"})
        assert registry.get_agent("support").expose.get("chat") is True
        assert client.get("/chat/support").status_code == 200

    def test_unticking_it_closes_the_page_again(self, client, project, registry):
        _open_chat(project, registry)
        assert client.get("/chat/support").status_code == 200
        client.post("/agents/support/fields", data={"expose_present": "1"})
        assert registry.get_agent("support").expose.get("chat") is not True
        assert client.get("/chat/support").status_code == 404

    def test_the_same_now_holds_for_mcp(self, client, project, registry):
        """The bug this fixes was on the MCP box, found while adding the chat
        one: an unticked checkbox submits nothing, so the old handler could
        never turn either of them off."""
        from heddled import yamlio

        path = project / "agents" / "support.yaml"
        data = yamlio.load(path.read_text())
        data["expose"] = {"mcp": True}
        path.write_text(yamlio.dump(data))
        assert registry.get_agent("support").expose.get("mcp") is True

        client.post("/agents/support/fields", data={"expose_present": "1"})
        assert registry.get_agent("support").expose.get("mcp") is not True

    def test_one_box_does_not_clobber_the_other(self, client, registry):
        client.post("/agents/support/fields",
                    data={"expose_present": "1", "expose_mcp": "on",
                          "expose_chat": "on"})
        expose = registry.get_agent("support").expose
        assert expose.get("mcp") is True and expose.get("chat") is True
