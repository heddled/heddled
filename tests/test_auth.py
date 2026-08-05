"""Who may open the console, and what each of them may do.

Heddled used to have one shared password and no notion of people. A shared secret
cannot say who approved the refund, cannot be revoked for one leaver, and gives
a contractor the same reach as the person who runs the place.
"""

import pytest

from heddled import users
from heddled.users import UserError


class TestFirstRun:
    """Nothing is reachable until somebody has claimed the console."""

    def test_a_fresh_install_needs_setup(self, store):
        assert users.needs_setup(store)

    def test_every_page_redirects_to_setup(self, blank_client):
        for path in ("/", "/sessions", "/tools", "/settings", "/users"):
            r = blank_client.get(path)
            assert r.status_code == 302 and "/setup" in r.headers["Location"], path

    def test_the_api_says_so_rather_than_redirecting(self, blank_client):
        assert blank_client.get("/api/sessions").status_code == 503

    def test_setup_creates_the_first_administrator(self, blank_client, store):
        r = blank_client.post("/setup", data={
            "username": "ralph", "display_name": "Ralph",
            "password": "a-good-long-password", "confirm": "a-good-long-password"})
        assert r.status_code == 302
        person = users.get(store, "ralph")
        assert person and person["role"] == "admin" and person["active"]

    def test_it_signs_you_straight_in(self, blank_client, store):
        blank_client.post("/setup", data={
            "username": "ralph", "password": "a-good-long-password",
            "confirm": "a-good-long-password"})
        assert blank_client.get("/").status_code == 200

    def test_mismatched_passwords_are_refused(self, blank_client, store):
        r = blank_client.post("/setup", data={
            "username": "ralph", "password": "a-good-long-password",
            "confirm": "something-else-entirely"})
        assert r.status_code == 400
        assert users.needs_setup(store)

    def test_a_short_password_is_refused(self, blank_client, store):
        r = blank_client.post("/setup", data={
            "username": "ralph", "password": "short", "confirm": "short"})
        assert r.status_code == 400 and users.needs_setup(store)

    def test_setup_closes_once_somebody_has_claimed_it(self, client):
        r = client.get("/setup")
        assert r.status_code == 409
        assert b"already been set up" in r.data

    def test_a_second_person_cannot_claim_it_through_setup(self, store, blank_client):
        blank_client.post("/setup", data={"username": "first",
                                          "password": "a-good-long-password",
                                          "confirm": "a-good-long-password"})
        r = blank_client.post("/setup", data={"username": "usurper",
                                              "password": "a-good-long-password",
                                              "confirm": "a-good-long-password"})
        # Told plainly, not silently bounced: somebody who fills this in and is
        # redirected to a login page assumes their account was made, then
        # cannot sign in with credentials that never existed.
        assert r.status_code == 409
        assert b"already been set up" in r.data
        assert users.get(store, "usurper") is None


class TestTheBannerReflectsReality:
    """The warning used to read the shared-password setting that accounts
    replaced, so it shouted "this console has no password" at every signed-in
    user forever. An alarm that is always on teaches people to ignore alarms."""

    def test_no_warning_once_somebody_has_an_account(self, store, admin, monkeypatch):
        from heddled import auth

        monkeypatch.setenv("HEDDLED_HOST", "0.0.0.0")
        assert auth.status(store)["at_risk"] is False

    def test_it_does_warn_while_still_unclaimed(self, store, monkeypatch):
        from heddled import auth

        monkeypatch.setenv("HEDDLED_HOST", "0.0.0.0")
        assert auth.status(store)["at_risk"] is True

    def test_the_console_is_quiet_for_a_signed_in_user(self, client):
        assert b"no password" not in client.get("/").data


class TestSigningIn:
    def test_the_right_credentials_work(self, anon_client, admin):
        r = anon_client.post("/login", data={"username": "tester",
                                             "password": "a-good-test-password"})
        assert r.status_code == 302
        assert anon_client.get("/").status_code == 200

    def test_the_wrong_password_does_not(self, anon_client, admin):
        assert anon_client.post("/login", data={"username": "tester",
                                                "password": "wrong"}).status_code == 401

    def test_an_unknown_user_gets_the_same_message(self, anon_client, admin):
        """Never reveal which half was wrong."""
        a = anon_client.post("/login", data={"username": "tester", "password": "wrong"})
        b = anon_client.post("/login", data={"username": "nobody", "password": "wrong"})
        assert a.status_code == b.status_code == 401
        # Byte-identical: a real account and an imaginary one are indistinguishable.
        assert a.data == b.data

    def test_signing_out_closes_the_door(self, client):
        client.get("/logout")
        assert client.get("/").status_code == 302

    def test_login_never_redirects_off_site(self, anon_client, admin):
        for target in ("https://evil.example/steal", "//evil.example"):
            r = anon_client.post("/login", data={"username": "tester",
                                                 "password": "a-good-test-password",
                                                 "next": target})
            assert r.headers["Location"].endswith("/")

    def test_repeated_guesses_are_throttled(self, anon_client, admin):
        from heddled.web import app as app_mod

        app_mod._ATTEMPTS.clear()
        for _ in range(app_mod._MAX_ATTEMPTS):
            anon_client.post("/login", data={"username": "tester", "password": "wrong"})
        r = anon_client.post("/login", data={"username": "tester", "password": "wrong"})
        assert r.status_code == 429
        app_mod._ATTEMPTS.clear()

    def test_a_suspended_account_cannot_sign_in(self, anon_client, store, admin):
        users.create(store, "leaver", "a-good-test-password", role="member")
        users.set_active(store, "leaver", False)
        assert anon_client.post("/login", data={"username": "leaver",
                                                "password": "a-good-test-password"}).status_code == 401

    def test_suspending_somebody_ends_their_session_immediately(self, client_as, store):
        c = client_as("member")
        assert c.get("/").status_code == 200
        users.set_active(store, "member_person", False)
        assert c.get("/").status_code == 302


class TestClosedDoor:
    def test_reading_conversations_needs_a_session(self, anon_client, admin):
        assert anon_client.get("/api/sessions").status_code == 401

    def test_creating_an_agent_writes_nothing(self, anon_client, registry, admin):
        anon_client.post("/agents", data={"name": "sneaky"})
        assert registry.get_agent("sneaky") is None

    def test_editing_an_agent_writes_nothing(self, anon_client, project, admin):
        before = (project / "agents" / "support.yaml").read_text()
        anon_client.post("/agents/support/fields", data={"model": "openai/gpt-4o"})
        assert (project / "agents" / "support.yaml").read_text() == before

    def test_a_program_can_use_a_bearer_credential(self, anon_client, admin):
        r = anon_client.get("/api/sessions",
                            headers={"Authorization": "Bearer tester:a-good-test-password"})
        assert r.status_code == 200

    def test_a_wrong_bearer_credential_is_refused(self, anon_client, admin):
        assert anon_client.get(
            "/api/sessions",
            headers={"Authorization": "Bearer tester:wrong"}).status_code == 401


class TestRoles:
    def test_a_viewer_can_look(self, client_as):
        c = client_as("viewer")
        for path in ("/", "/sessions", "/tools"):
            assert c.get(path).status_code == 200

    def test_a_viewer_cannot_change_anything(self, client_as, registry):
        c = client_as("viewer")
        r = c.post("/agents", data={"name": "from_a_viewer"})
        assert r.status_code == 403
        assert registry.get_agent("from_a_viewer") is None

    def test_a_viewer_cannot_change_an_agent(self, client_as, project):
        c = client_as("viewer")
        before = (project / "agents" / "support.yaml").read_text()
        c.post("/agents/support/fields", data={"model": "openai/gpt-4o"})
        assert (project / "agents" / "support.yaml").read_text() == before

    def test_a_member_can_build(self, client_as, registry):
        c = client_as("member")
        assert c.post("/agents", data={"name": "from_a_member"}).status_code == 302
        assert registry.get_agent("from_a_member")

    def test_a_member_cannot_manage_people(self, client_as, store):
        c = client_as("member")
        r = c.post("/users", data={"action": "create", "username": "smuggled",
                                   "password": "a-good-test-password",
                                   "confirm": "a-good-test-password"})
        assert r.status_code == 403
        assert users.get(store, "smuggled") is None

    def test_a_member_cannot_change_settings(self, client_as, store):
        c = client_as("member")
        c.post("/settings/secrets", data={"name": "sneaky_key", "value": "x"})
        assert store.get_setting("sneaky_key") is None

    def test_a_non_admin_cannot_even_open_the_people_page(self, client_as):
        """The nav hides it; a page you cannot see in the menu should not be
        reachable by typing its address either."""
        assert client_as("viewer").get("/users").status_code == 403
        assert client_as("member").get("/users").status_code == 403

    def test_a_non_admin_cannot_read_settings(self, client_as):
        """Settings holds every API key and webhook URL."""
        assert client_as("member").get("/settings").status_code == 403

    def test_only_admins_see_the_people_link(self, client_as):
        assert b'href="/users"' in client_as("admin", "another_admin").get("/").data
        assert b'href="/users"' not in client_as("member").get("/").data

    def test_an_admin_can_do_everything(self, client, store):
        assert client.get("/users").status_code == 200
        assert client.post("/users", data={
            "action": "create", "username": "colleague", "role": "member",
            "password": "a-good-test-password",
            "confirm": "a-good-test-password"}).status_code == 302
        assert users.get(store, "colleague")


class TestManagingPeople:
    def test_a_username_must_be_sensible(self, store):
        for bad in ("a", "has space", "x" * 40, "semi;colon"):
            with pytest.raises(UserError):
                users.create(store, bad, "a-good-test-password")

    def test_names_are_unique_regardless_of_case(self, store, admin):
        with pytest.raises(UserError, match="already"):
            users.create(store, "TESTER", "a-good-test-password")

    def test_a_password_is_never_stored_in_the_clear(self, store):
        users.create(store, "someone", "correct-horse-battery")
        row = users.get(store, "someone")
        assert "correct-horse-battery" not in str(dict(row))

    def test_two_people_with_the_same_password_hash_differently(self, store):
        users.create(store, "one", "the-same-password")
        users.create(store, "two", "the-same-password")
        assert users.get(store, "one")["password_hash"] != \
               users.get(store, "two")["password_hash"]

    def test_the_last_administrator_cannot_be_demoted(self, store, admin):
        with pytest.raises(UserError, match="only administrator"):
            users.set_role(store, "tester", "member")

    def test_the_last_administrator_cannot_be_suspended(self, store, admin):
        with pytest.raises(UserError, match="only administrator"):
            users.set_active(store, "tester", False)

    def test_the_last_administrator_cannot_be_removed(self, store, admin):
        with pytest.raises(UserError, match="only administrator"):
            users.delete(store, "tester")

    def test_demotion_is_allowed_once_there_are_two(self, store, admin):
        users.create(store, "second", "a-good-test-password", role="admin")
        users.set_role(store, "tester", "member")
        assert users.get(store, "tester")["role"] == "member"

    def test_changing_your_own_password_needs_the_current_one(self, client):
        r = client.post("/account", data={"current": "wrong",
                                          "password": "a-new-good-password",
                                          "confirm": "a-new-good-password"})
        assert r.status_code == 400

    def test_and_works_with_it(self, client, store):
        r = client.post("/account", data={"current": "a-good-test-password",
                                          "password": "a-new-good-password",
                                          "confirm": "a-new-good-password"})
        assert r.status_code == 200
        assert users.authenticate(store, "tester", "a-new-good-password")


class TestAuditTrail:
    def test_signing_in_is_recorded(self, anon_client, store, admin):
        anon_client.post("/login", data={"username": "tester",
                                         "password": "a-good-test-password"})
        assert any(e["action"] == "signed.in" for e in users.audit_log(store))

    def test_a_failed_attempt_is_recorded(self, anon_client, store, admin):
        anon_client.post("/login", data={"username": "tester", "password": "wrong"})
        assert any(e["action"] == "signin.failed" for e in users.audit_log(store))

    def test_creating_a_person_is_recorded_with_who_did_it(self, client, store):
        client.post("/users", data={"action": "create", "username": "colleague",
                                    "role": "member",
                                    "password": "a-good-test-password",
                                    "confirm": "a-good-test-password"})
        entry = next(e for e in users.audit_log(store) if e["action"] == "user.created")
        assert entry["target"] == "colleague" and entry["username"] == "tester"

    def test_a_refused_change_is_recorded(self, client_as, store):
        client_as("viewer").post("/agents", data={"name": "nope"})
        assert any(e["action"] == "denied.write" for e in users.audit_log(store))

    def test_the_trail_survives_removing_the_person(self, client, store):
        client.post("/users", data={"action": "create", "username": "temp",
                                    "role": "member",
                                    "password": "a-good-test-password",
                                    "confirm": "a-good-test-password"})
        users.delete(store, "temp", by="tester")
        actions = [e["action"] for e in users.audit_log(store)]
        assert "user.created" in actions and "user.removed" in actions


class TestPathsThatMustStayOpen:
    def test_an_approver_can_still_use_their_link(self, anon_client, client, store,
                                                  agent, worker):
        from heddled import runtime

        runtime.submit_message("support", "refund invoice F-2231 for 249 eur",
                               sync=True, timeout_s=20)
        approval = store.pending_approvals()[0]
        r = anon_client.get(f"/approve/{approval['id']}?token={approval['token']}")
        assert r.status_code == 200 and b"go ahead" in r.data

    def test_and_can_answer_it_without_an_account(self, anon_client, store, agent, worker):
        from heddled import runtime

        runtime.submit_message("support", "refund invoice F-2231 for 249 eur",
                               sync=True, timeout_s=20)
        approval = store.pending_approvals()[0]
        anon_client.get(f"/approve/{approval['id']}"
                        f"?token={approval['token']}&decision=approved")
        assert store.get_approval(approval["id"])["status"] == "approved"

    def test_another_system_can_still_reach_a_published_agent(self, anon_client, admin,
                                                              mcp_key):
        """With its own credential — the console password is not shared with it."""
        assert anon_client.post("/mcp/support",
                                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                headers=mcp_key).status_code == 200

    def test_but_not_without_one(self, anon_client, admin):
        """An unauthenticated endpoint that can *run* an agent would simply be
        the way around the console sign-in."""
        r = anon_client.post("/mcp/support",
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401

    def test_health_stays_readable(self, anon_client, admin):
        assert anon_client.get("/api/health").status_code == 200

    def test_the_stylesheet_loads_on_the_login_page(self, anon_client, admin):
        assert anon_client.get("/static/console.css").status_code == 200


class TestSecretsNeverReachTheStore:
    """Pattern redaction (`redact: [iban, …]`) is a per-agent choice about
    customer data. Stripping stored credentials is not optional: a viewer
    reading Activity must not be able to read your API keys out of it."""

    def _leaky_tool(self, project):
        d = project / "tools" / "leaky"
        d.mkdir(parents=True)
        (d / "tool.yaml").write_text(
            "name: leaky\ndescription: Calls something.\n"
            "input: {q: string}\nhandler: ./handler.py\n")
        (d / "handler.py").write_text(
            'def handle(args, ctx):\n'
            '    key = ctx.settings.get("billing_api_key")\n'
            '    raise RuntimeError(f"GET https://api/x?api_key={key} failed")\n')
        path = project / "agents" / "support.yaml"
        path.write_text(path.read_text().replace(
            "tools: [lookup_invoice, refund]", "tools: [leaky]"))

    def test_a_handler_traceback_cannot_carry_a_secret_into_the_trace(
            self, store, project, registry):
        """Regression: `RuntimeError(f"...api_key={key}...")` put the key
        straight into error.raised, tracebacks and all."""
        from heddled.engine import TurnEngine
        from heddled.events import new_id

        store.set_setting("billing_api_key", "sk-live-SUPERSECRET-9931")
        self._leaky_tool(project)
        agent = registry.get_agent("support")
        sid = store.create_session(agent="support", agent_version=agent.version,
                                   channel="webchat")
        TurnEngine(store, agent, sid, new_id("t")).run("leaky q please")

        blob = str([e.payload for e in store.events_for_session(sid)])
        assert "sk-live-SUPERSECRET-9931" not in blob
        assert "«secret»" in blob

    def test_it_applies_even_with_no_redaction_configured(self, store, project, registry):
        from heddled.engine import TurnEngine
        from heddled.events import new_id

        store.set_setting("billing_api_key", "sk-live-SUPERSECRET-9931")
        self._leaky_tool(project)
        path = project / "agents" / "support.yaml"
        path.write_text(path.read_text().replace(
            "    redact: [iban, creditcard]       # applied at the trace-store boundary", ""))
        agent = registry.get_agent("support")
        sid = store.create_session(agent="support", agent_version=agent.version,
                                   channel="webchat")
        TurnEngine(store, agent, sid, new_id("t")).run("leaky q please")
        blob = str([e.payload for e in store.events_for_session(sid)])
        assert "sk-live-SUPERSECRET-9931" not in blob

    def test_short_settings_are_not_scrubbed_out_of_ordinary_text(self, store):
        """A two-character setting would blank ordinary words everywhere."""
        from heddled import policies

        values = policies.secret_values({"lang": "en", "key": "a-real-long-secret"})
        assert values == ["a-real-long-secret"]

    def test_stripping_recurses_into_structures(self):
        from heddled import policies

        out = policies.strip_secrets(
            {"a": ["x sk-live-1234567 y", {"b": "sk-live-1234567"}]}, ["sk-live-1234567"])
        assert out == {"a": ["x «secret» y", {"b": "«secret»"}]}


class TestLongLivedStreams:
    """A live trace outlives the request that opened it, often by hours."""

    def test_the_stream_checks_whether_the_watcher_is_still_allowed(self, store):
        from heddled import auth, users

        person = users.create(store, "watcher", "a-good-long-password", role="viewer")
        assert auth.still_allowed(store, person["id"]) is True
        users.set_active(store, "watcher", False)
        assert auth.still_allowed(store, person["id"]) is False

    def test_a_removed_person_is_not_allowed(self, store):
        from heddled import auth, users

        person = users.create(store, "temp", "a-good-long-password", role="viewer")
        users.delete(store, "temp")
        assert auth.still_allowed(store, person["id"]) is False

    def test_an_unknown_id_is_not_allowed(self, store):
        from heddled import auth

        assert auth.still_allowed(store, "u_nonexistent") is False

    def test_a_stream_only_carries_its_own_session(self, client, store):
        """The obvious leak to check: one conversation's stream must not
        deliver another's."""
        from heddled.events import Event

        mine = store.create_session(agent="support", agent_version="v1", channel="webchat")
        theirs = store.create_session(agent="support", agent_version="v1", channel="webchat")
        store.append(Event(type="message.received", session_id=theirs,
                           payload={"text": "somebody else's conversation"}))

        resp = client.get(f"/sessions/{mine}/stream")
        first = next(iter(resp.response), b"").decode()
        resp.close()
        assert "somebody else's conversation" not in first

    def test_a_viewer_may_still_watch(self, client_as, store):
        """Read-only accounts are meant to watch; this must not have broken it."""
        sid = store.create_session(agent="support", agent_version="v1", channel="webchat")
        resp = client_as("viewer").get(f"/sessions/{sid}/stream")
        assert resp.status_code == 200
        resp.close()


class TestIntegrationKeys:
    """A webhook from a ticketing tool is not a person. Requiring it to hold a
    user account was a functionality regression from introducing sign-in."""

    def test_a_push_trigger_works_with_an_integration_key(self, anon_client, store,
                                                          admin, mcp_key):
        r = anon_client.post("/api/agents/support/webhook",
                             json={"text": "invoice F-1 arrived"}, headers=mcp_key)
        assert r.status_code == 202
        assert store.get_session(r.get_json()["session_id"])["channel"] == "webhook"

    def test_without_a_key_it_is_refused(self, anon_client, admin):
        assert anon_client.post("/api/agents/support/webhook",
                                json={"text": "hi"}).status_code == 401

    def test_an_unknown_key_is_refused(self, anon_client, admin, mcp_key):
        assert anon_client.post("/api/agents/support/webhook", json={"text": "hi"},
                                headers={"Authorization": "Bearer not-a-key"}).status_code == 401

    def test_an_integration_may_not_manage_people(self, anon_client, store, admin, mcp_key):
        """It can drive agents; it is not an administrator."""
        r = anon_client.post("/users", data={"action": "create", "username": "smuggled",
                                             "password": "a-good-long-password",
                                             "confirm": "a-good-long-password"},
                             headers=mcp_key)
        assert r.status_code == 403
        assert users.get(store, "smuggled") is None

    def test_an_integration_may_not_read_settings(self, anon_client, admin, mcp_key):
        assert anon_client.get("/settings", headers=mcp_key).status_code == 403

    def test_a_person_can_still_use_their_own_account(self, anon_client, admin):
        assert anon_client.get(
            "/api/sessions",
            headers={"Authorization": "Bearer tester:a-good-test-password"}).status_code == 200
