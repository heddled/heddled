"""Tools without writing code.

The engine must not be able to tell the difference between a tool someone
configured in a form and one a programmer wrote.
"""

import pytest

from heddled import tooltypes
from heddled.tooltypes import ToolTypeError


class Ctx:
    """Stands in for ToolContext."""

    def __init__(self, settings=None):
        self.settings = settings or {}
        self.logs = []

    def log(self, message, **extra):
        self.logs.append(message)


class TestSubstitution:
    def test_a_field_is_filled_from_the_arguments(self):
        assert tooltypes.fill("/invoices/{n}", {"n": "F-1"}, {}) == "/invoices/F-1"

    def test_a_secret_is_filled_from_settings(self):
        assert tooltypes.fill("Bearer {{secret.k}}", {}, {"k": "abc"}) == "Bearer abc"

    def test_secrets_tolerate_whitespace(self):
        assert tooltypes.fill("{{ secret.k }}", {}, {"k": "abc"}) == "abc"

    def test_substitution_recurses_into_structures(self):
        out = tooltypes.fill({"a": ["{n}", {"b": "{n}"}]}, {"n": "1"}, {})
        assert out == {"a": ["1", {"b": "1"}]}

    def test_a_missing_field_says_which_one(self):
        with pytest.raises(ToolTypeError, match="'n'"):
            tooltypes.fill("{n}", {}, {})

    def test_a_missing_secret_points_at_settings(self):
        with pytest.raises(ToolTypeError, match="Settings"):
            tooltypes.fill("{{secret.nope}}", {}, {})

    def test_non_strings_pass_through(self):
        assert tooltypes.fill(42, {}, {}) == 42


class TestLookup:
    def build(self, **config):
        return tooltypes.build_handler({"type": "lookup", "config": {
            "key": "team",
            "table": {"finance": "Rotterdam", "engineering": "Amsterdam"},
            **config,
        }})

    def test_an_exact_match(self):
        assert self.build()({"team": "finance"}, Ctx())["value"] == "Rotterdam"

    def test_matching_ignores_case(self):
        assert self.build()({"team": "FINANCE"}, Ctx())["found"] is True

    def test_a_question_containing_the_key_still_matches(self):
        """People ask for 'the finance team', not 'finance'."""
        out = self.build()({"team": "which office is the finance team in?"}, Ctx())
        assert out["found"] is True and out["matched"] == "finance"

    def test_a_miss_returns_the_default_and_the_options(self):
        out = self.build(default="ask facilities")({"team": "legal"}, Ctx())
        assert out["found"] is False
        assert out["value"] == "ask facilities"
        assert set(out["options"]) == {"finance", "engineering"}

    def test_a_table_that_is_not_a_table_is_refused(self):
        with pytest.raises(ToolTypeError, match="key → value"):
            tooltypes.build_handler({"type": "lookup", "config": {"table": "nope"}})


class TestText:
    def test_it_formats_the_arguments(self):
        handler = tooltypes.build_handler(
            {"type": "text", "config": {"text": "Invoice {n} is due {d}."}})
        assert handler({"n": "F-1", "d": "today"}, Ctx()) == {
            "text": "Invoice F-1 is due today."}

    def test_it_needs_some_text(self):
        with pytest.raises(ToolTypeError, match="needs some text"):
            tooltypes.build_handler({"type": "text", "config": {}})


class TestFixed:
    def test_it_returns_the_configured_value(self):
        handler = tooltypes.build_handler(
            {"type": "fixed", "config": {"value": {"status": "unpaid"}}})
        assert handler({}, Ctx()) == {"status": "unpaid"}


class TestHttp:
    def _fake_request(self, monkeypatch, status=200, payload=None, text=""):
        captured = {}

        class Response:
            status_code = status

            def json(self):
                if payload is None:
                    raise ValueError("no json")
                return payload

            @property
            def text(self):
                return text

        def fake(method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return Response()

        monkeypatch.setattr(tooltypes.requests, "request", fake)
        # These exercise the HTTP tool's behaviour, not where it may connect —
        # the egress guard has its own tests, and `api.test` does not resolve.
        monkeypatch.setattr(tooltypes, "guard_destination", lambda url, settings: None)
        return captured

    def test_the_url_is_filled_from_the_arguments(self, monkeypatch):
        captured = self._fake_request(monkeypatch, payload={"ok": 1})
        handler = tooltypes.build_handler({"type": "http", "config": {
            "url": "https://api.test/invoices/{n}"}})
        handler({"n": "F-1"}, Ctx())
        assert captured["url"] == "https://api.test/invoices/F-1"

    def test_a_secret_header_is_resolved(self, monkeypatch):
        captured = self._fake_request(monkeypatch, payload={})
        handler = tooltypes.build_handler({"type": "http", "config": {
            "url": "https://api.test/", "headers": {"Authorization": "Bearer {{secret.k}}"}}})
        handler({}, Ctx({"k": "s3cret"}))
        assert captured["headers"]["Authorization"] == "Bearer s3cret"

    def test_a_default_user_agent_is_sent(self, monkeypatch):
        """Public APIs reject unidentified clients, and a bare 403 is a baffling
        first experience for someone who just typed a URL."""
        captured = self._fake_request(monkeypatch, payload={})
        tooltypes.build_handler({"type": "http", "config": {"url": "https://api.test/"}})(
            {}, Ctx())
        assert "heddled" in captured["headers"]["User-Agent"]

    def test_an_explicit_user_agent_wins(self, monkeypatch):
        captured = self._fake_request(monkeypatch, payload={})
        tooltypes.build_handler({"type": "http", "config": {
            "url": "https://api.test/", "headers": {"User-Agent": "mine"}}})({}, Ctx())
        assert captured["headers"]["User-Agent"] == "mine"

    def test_a_failed_call_is_a_result_not_a_crash(self, monkeypatch):
        self._fake_request(monkeypatch, status=404, text="nope")
        out = tooltypes.build_handler(
            {"type": "http", "config": {"url": "https://api.test/"}})({}, Ctx())
        assert out["ok"] is False and out["status"] == 404

    def test_result_path_narrows_the_response(self, monkeypatch):
        self._fake_request(monkeypatch, payload={"data": {"invoice": {"status": "unpaid"}}})
        out = tooltypes.build_handler({"type": "http", "config": {
            "url": "https://api.test/", "result_path": "data.invoice"}})({}, Ctx())
        assert out["result"] == {"status": "unpaid"}

    def test_a_non_json_response_still_comes_back(self, monkeypatch):
        self._fake_request(monkeypatch, payload=None, text="plain words")
        out = tooltypes.build_handler(
            {"type": "http", "config": {"url": "https://api.test/"}})({}, Ctx())
        assert out["result"]["text"] == "plain words"

    def test_a_resolved_secret_never_reaches_the_result(self, monkeypatch):
        """Whatever the API echoes back, the trace store must not learn the key."""
        self._fake_request(monkeypatch, payload={"echo": "Bearer s3cretvalue"})
        out = tooltypes.build_handler({"type": "http", "config": {
            "url": "https://api.test/"}})({}, Ctx({"k": "s3cretvalue"}))
        assert "s3cretvalue" not in str(out)

    def test_the_log_line_names_the_host_not_the_whole_url(self, monkeypatch):
        self._fake_request(monkeypatch, payload={})
        ctx = Ctx()
        tooltypes.build_handler({"type": "http", "config": {
            "url": "https://api.test/very/long/path?token=x"}})({}, ctx)
        assert ctx.logs == ["GET api.test"]

    def test_a_url_is_required(self):
        with pytest.raises(ToolTypeError, match="needs a URL"):
            tooltypes.build_handler({"type": "http", "config": {}})


class TestEgressGuard:
    """A tool whose URL is templated lets the *model* pick the destination. With
    content ingested from outside (a polled mailbox, a webhook), an injected
    instruction can steer it at cloud metadata or an internal admin page."""

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://127.0.0.1:5001/api/sessions",         # Heddled itself
        "http://10.0.0.5/admin",                      # internal network
        "http://192.168.1.1/",
    ])
    def test_internal_destinations_are_refused(self, url):
        with pytest.raises(ToolTypeError, match="private or internal"):
            tooltypes.guard_destination(url, {})

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://x/"])
    def test_only_http_and_https_are_allowed(self, url):
        with pytest.raises(ToolTypeError, match="http and https"):
            tooltypes.guard_destination(url, {})

    def test_a_public_address_is_fine(self):
        tooltypes.guard_destination("https://example.com/things", {})

    def test_it_can_be_turned_off_deliberately(self):
        tooltypes.guard_destination("http://10.0.0.5/admin", {"allow_internal_http": True})

    def test_an_unresolvable_host_is_refused_rather_than_attempted(self):
        with pytest.raises(ToolTypeError, match="could not find"):
            tooltypes.guard_destination("https://nx.invalid/", {})


class TestUnknownType:
    def test_it_lists_the_valid_choices(self):
        with pytest.raises(ToolTypeError, match="lookup"):
            tooltypes.build_handler({"type": "telepathy"})


class TestCatalog:
    def test_every_catalog_entry_has_a_builder(self):
        for entry in tooltypes.CATALOG:
            assert entry["type"] in tooltypes.BUILDERS

    def test_every_entry_is_described_for_a_non_developer(self):
        for entry in tooltypes.CATALOG:
            assert entry["label"] and entry["blurb"]
            assert not entry["label"].islower()   # a sentence, not an identifier

    def test_describe_summarises_each_type(self):
        assert "GET" in tooltypes.describe(
            {"type": "http", "config": {"url": "https://x/", "method": "get"}})
        assert "2 entries" in tooltypes.describe(
            {"type": "lookup", "config": {"table": {"a": 1, "b": 2}}})


class TestEngineCannotTell:
    """The whole point: a form-built tool behaves like any other."""

    def _install(self, project, name="office_location"):
        (project / "tools" / name).mkdir(parents=True)
        (project / "tools" / name / "tool.yaml").write_text(
            f"name: {name}\n"
            "description: Find which office a team sits in.\n"
            "input: {team: string}\n"
            "type: lookup\n"
            "config:\n"
            "  key: team\n"
            "  table: {finance: Rotterdam}\n"
        )

    def test_it_loads_as_an_ordinary_tool(self, project, registry):
        self._install(project)
        tool = registry.get_tool("office_location")
        assert tool.is_no_code
        assert tool.input_schema["properties"]["team"]["type"] == "string"

    def test_no_handler_file_is_needed(self, project, registry):
        self._install(project)
        assert not (project / "tools" / "office_location" / "handler.py").exists()
        assert registry.get_tool("office_location").load_handler() is not None

    def test_it_runs_through_heddled_tool_test(self, project, registry, store):
        from heddled.tooltest import run_tool_standalone

        self._install(project)
        out = run_tool_standalone("office_location", {"team": "finance"})
        assert out["ok"] and out["result"]["value"] == "Rotterdam"

    def test_argument_validation_still_applies(self, project, registry, store):
        from heddled.tooltest import run_tool_standalone

        self._install(project)
        assert run_tool_standalone("office_location", {})["ok"] is False

    def test_an_agent_calls_it_and_the_spine_looks_normal(self, project, registry, store,
                                                          worker):
        from heddled import runtime

        self._install(project)
        path = project / "agents" / "support.yaml"
        path.write_text(path.read_text().replace(
            "tools: [lookup_invoice, refund]", "tools: [office_location]"))

        result = runtime.submit_message(
            "support", "which office is the finance team in?", sync=True, timeout_s=20)
        assert result["status"] == "completed"
        types = [e.type for e in store.events_for_session(result["session_id"])]
        assert "tool.called" in types and "tool.result" in types

    def test_a_policy_gates_it_like_any_other_tool(self, project, registry, store, worker):
        from heddled import authoring, runtime

        self._install(project)
        path = project / "agents" / "support.yaml"
        path.write_text(path.read_text().replace(
            "tools: [lookup_invoice, refund]", "tools: [office_location]"))
        authoring.add_policy("support", {"tool": "office_location",
                                         "requires_approval": True})

        result = runtime.submit_message(
            "support", "which office is the finance team in?", sync=True, timeout_s=20)
        assert result["status"] == "waiting-approval"


class TestAuthoringNoCodeTools:
    def test_a_form_built_tool_writes_no_python(self, project, registry):
        from heddled import authoring

        written = authoring.new_tool(
            "office_location", description="Find an office.",
            input_spec="team:string", tool_type="lookup",
            config={"key": "team", "table": {"finance": "Rotterdam"}})
        assert [p.name for p in written.paths] == ["tool.yaml"]
        assert registry.get_tool("office_location").is_no_code

    def test_a_misconfiguration_is_caught_at_creation(self, project, registry):
        from heddled import authoring

        with pytest.raises(authoring.AuthoringError, match="needs a URL"):
            authoring.new_tool("broken", tool_type="http", config={})
        assert registry.get_tool("broken") is None

    def test_an_unknown_type_lists_the_choices(self, project, registry):
        from heddled import authoring

        with pytest.raises(authoring.AuthoringError, match="lookup"):
            authoring.new_tool("broken", tool_type="telepathy", config={})

    def test_the_config_can_be_edited_afterwards(self, project, registry):
        from heddled import authoring

        authoring.new_tool("office_location", input_spec="team:string", tool_type="lookup",
                           config={"key": "team", "table": {"finance": "Rotterdam"}})
        authoring.update_tool_config(
            "office_location",
            config={"key": "team", "table": {"finance": "Rotterdam", "legal": "Den Haag"}})
        handler = registry.get_tool("office_location").load_handler()
        assert handler({"team": "legal"}, Ctx())["value"] == "Den Haag"

    def test_a_broken_edit_never_reaches_the_file(self, project, registry):
        from heddled import authoring

        authoring.new_tool("api_call", input_spec="q:string", tool_type="http",
                           config={"url": "https://api.test/{q}"})
        before = (project / "tools" / "api_call" / "tool.yaml").read_text()
        with pytest.raises(authoring.AuthoringError):
            authoring.update_tool_config("api_call", config={})
        assert (project / "tools" / "api_call" / "tool.yaml").read_text() == before
