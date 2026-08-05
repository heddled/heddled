"""Which service a `provider/model` string resolves to, and with whose key.

Every OpenAI-compatible service carries its own `<name>_api_key` and
`<name>_base_url`, so an organisation can have DeepSeek and OpenAI and a local
Ollama configured at once. They used to share `openai_api_key`, which quietly
made that a choice of one.
"""

import pytest

from heddled.providers import (
    DEFAULT_PRICING,
    OPENAI_COMPATIBLE,
    ProviderError,
    estimate_cost_eur,
    get_provider,
    known_providers,
    split_model,
)


@pytest.fixture(autouse=True)
def no_ambient_keys(monkeypatch):
    """A key in the developer's own shell must not decide a test's outcome."""
    for key in OPENAI_COMPATIBLE:
        monkeypatch.delenv(f"{key.upper()}_API_KEY", raising=False)
        monkeypatch.delenv(f"{key.upper()}_BASE_URL", raising=False)


class TestResolution:
    def test_deepseek_resolves(self):
        p = get_provider("deepseek/deepseek-chat", {"deepseek_api_key": "sk-d"})
        assert p.provider == "deepseek" and p.model == "deepseek-chat"

    @pytest.mark.parametrize("service", sorted(OPENAI_COMPATIBLE))
    def test_every_listed_service_resolves(self, service):
        spec = OPENAI_COMPATIBLE[service]
        p = get_provider(f"{service}/{spec['example']}", {f"{service}_api_key": "k"})
        assert p._base() == spec["base"]

    def test_a_bare_model_still_means_anthropic(self):
        assert split_model("claude-sonnet-4-6") == ("anthropic", "claude-sonnet-4-6")

    def test_the_provider_half_is_case_insensitive(self):
        assert get_provider("DeepSeek/deepseek-chat", {"deepseek_api_key": "k"}).provider \
            == "deepseek"

    @pytest.mark.parametrize("alias", ["openai-compatible", "local"])
    def test_the_old_aliases_still_work(self, alias):
        """Agent files written before the table existed used these."""
        assert get_provider(f"{alias}/gpt-4o", {"openai_api_key": "k"}).provider == "openai"

    def test_an_unknown_provider_says_what_is_valid(self):
        with pytest.raises(ProviderError) as e:
            get_provider("deepsek/chat")
        assert "deepseek" in str(e.value) and "deepsek" in str(e.value)


class TestKeysDoNotCollide:
    def test_each_service_reads_its_own_key(self):
        settings = {"openai_api_key": "sk-openai", "deepseek_api_key": "sk-deepseek"}
        assert get_provider("openai/gpt-4o", settings)._key() == "sk-openai"
        assert get_provider("deepseek/deepseek-chat", settings)._key() == "sk-deepseek"

    def test_an_openai_key_does_not_stand_in_for_deepseek(self):
        with pytest.raises(ProviderError, match="DeepSeek"):
            get_provider("deepseek/deepseek-chat", {"openai_api_key": "sk-openai"})._key()

    def test_the_missing_key_message_names_the_setting_to_add(self):
        with pytest.raises(ProviderError) as e:
            get_provider("deepseek/deepseek-chat", {})._key()
        assert "deepseek_api_key" in str(e.value)
        assert "DEEPSEEK_API_KEY" in str(e.value)

    def test_the_environment_is_a_fallback(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
        assert get_provider("deepseek/deepseek-chat", {})._key() == "sk-from-env"

    def test_a_setting_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
        assert get_provider("deepseek/deepseek-chat",
                            {"deepseek_api_key": "sk-set"})._key() == "sk-set"


class TestBaseUrls:
    def test_each_service_has_its_own_default(self):
        assert get_provider("deepseek/deepseek-chat", {"deepseek_api_key": "k"})._base() \
            == "https://api.deepseek.com/v1"
        assert get_provider("openai/gpt-4o", {"openai_api_key": "k"})._base() \
            == "https://api.openai.com/v1"

    def test_a_gateway_can_be_pointed_elsewhere(self):
        p = get_provider("deepseek/deepseek-chat",
                         {"deepseek_api_key": "k",
                          "deepseek_base_url": "https://proxy.example.com/v1/"})
        assert p._base() == "https://proxy.example.com/v1"     # trailing slash trimmed


class TestLocalServices:
    @pytest.mark.parametrize("service", ["ollama", "vllm"])
    def test_a_local_service_needs_no_key(self, service):
        """Nothing on localhost issues API keys; asking for one is a dead end."""
        assert get_provider(f"{service}/whatever", {})._key()

    def test_a_local_service_still_accepts_one(self, service="ollama"):
        assert get_provider("ollama/llama3.2", {"ollama_api_key": "k"})._key() == "k"

    def test_a_hosted_service_does_not_get_the_exemption(self):
        with pytest.raises(ProviderError):
            get_provider("groq/llama-3.3-70b-versatile", {})._key()


class TestPricing:
    @pytest.mark.parametrize("service", sorted(OPENAI_COMPATIBLE))
    def test_every_service_has_a_price(self, service):
        """Without one the budget ledger silently counts a paid model as free."""
        assert service in DEFAULT_PRICING

    def test_deepseek_is_costed(self):
        cost = estimate_cost_eur("deepseek/deepseek-chat",
                                 {"input_tokens": 1_000_000, "output_tokens": 0})
        assert cost == pytest.approx(DEFAULT_PRICING["deepseek"]["input"])

    def test_a_local_model_costs_nothing(self):
        assert estimate_cost_eur("ollama/llama3.2",
                                 {"input_tokens": 5_000_000,
                                  "output_tokens": 5_000_000}) == 0.0

    def test_a_settings_override_beats_the_built_in_table(self):
        cost = estimate_cost_eur(
            "deepseek/deepseek-chat", {"input_tokens": 1_000_000, "output_tokens": 0},
            {"pricing": {"deepseek/deepseek-chat": {"input": 9.0, "output": 9.0}}})
        assert cost == pytest.approx(9.0)


class TestPicker:
    def test_it_offers_anthropic_the_stand_in_and_every_compatible_service(self):
        keys = [p["key"] for p in known_providers()]
        assert keys[:2] == ["anthropic", "mock"]
        assert set(keys[2:]) == set(OPENAI_COMPATIBLE)

    def test_every_entry_carries_what_the_form_needs(self):
        for p in known_providers():
            assert p["label"] and p["example"] and isinstance(p["local"], bool)

    def test_the_examples_are_real_model_strings(self):
        """The picker's example is pasted straight into an agent file."""
        for p in known_providers():
            provider, name = split_model(f"{p['key']}/{p['example']}")
            assert provider == p["key"] and name


class TestConsoleWiring:
    """The picker and the settings page have to agree with the table above."""

    def test_settings_groups_cover_every_setting(self):
        from heddled.web.app import KNOWN_SETTINGS, SETTING_GROUPS

        grouped = [k for _, _, keys in SETTING_GROUPS for k in keys]
        assert sorted(grouped) == sorted(k for k, _ in KNOWN_SETTINGS)
        assert len(grouped) == len(set(grouped)), "a setting is in two groups"

    def test_every_hosted_service_has_a_key_field(self):
        from heddled.web.app import KNOWN_SETTINGS

        keys = dict(KNOWN_SETTINGS)
        for name, spec in OPENAI_COMPATIBLE.items():
            assert f"{name}_base_url" in keys
            assert (f"{name}_api_key" in keys) is not bool(spec.get("local"))

    def test_deepseek_is_offered_in_the_models_group(self):
        from heddled.web.app import SETTING_GROUPS

        models = next(keys for title, _, keys in SETTING_GROUPS if title == "Models")
        assert "deepseek_api_key" in models

    def test_every_offered_model_resolves(self, monkeypatch):
        """A model in the picker that no provider can serve is a trap."""
        from heddled.web.app import KNOWN_MODELS

        for model in KNOWN_MODELS:
            service = model.split("/")[0]
            get_provider(model, {f"{service}_api_key": "k"})

    def test_the_picker_marks_which_services_are_ready(self, store):
        from heddled.web.app import model_groups

        by_provider = {g["provider"]: g for g in model_groups(store)}
        assert by_provider["mock"]["ready"] is True          # needs no account
        assert by_provider["ollama"]["ready"] is True        # local
        assert by_provider["deepseek"]["ready"] is False

        store.set_setting("deepseek_api_key", "sk-d")
        assert {g["provider"] for g in model_groups(store) if g["ready"]} >= {"deepseek"}

    def test_ready_services_are_offered_first(self, store):
        from heddled.web.app import model_groups

        store.set_setting("deepseek_api_key", "sk-d")
        ready = [g["ready"] for g in model_groups(store)]
        assert ready == sorted(ready, reverse=True)

    def test_every_group_offers_at_least_one_model(self, store):
        from heddled.web.app import model_groups

        for g in model_groups(store):
            assert g["models"], g["provider"]


class TestTheScreens:
    def test_the_new_agent_picker_groups_models_by_service(self, client):
        body = client.get("/agents/new").data.decode()
        assert "<optgroup" in body
        assert "DeepSeek" in body and "deepseek/deepseek-chat" in body

    def test_it_says_which_services_still_need_a_key(self, client):
        assert "needs a key" in client.get("/agents/new").data.decode()

    def test_a_configured_service_is_not_labelled_as_missing(self, client, store):
        store.set_setting("deepseek_api_key", "sk-d")
        body = client.get("/agents/new").data.decode()
        assert 'label="DeepSeek"' in body

    def test_an_agent_on_an_unkeyed_service_is_warned_about(self, client, project):
        from heddled import authoring

        authoring.update_agent_fields("support", {"model": "deepseek/deepseek-chat"})
        body = client.get("/agents/support").data.decode()
        assert "DeepSeek has no key yet" in body

    def test_the_warning_goes_away_once_the_key_is_set(self, client, project, store):
        from heddled import authoring

        authoring.update_agent_fields("support", {"model": "deepseek/deepseek-chat"})
        store.set_setting("deepseek_api_key", "sk-d")
        assert "has no key yet" not in client.get("/agents/support").data.decode()

    def test_a_misspelt_service_is_pointed_out(self, client, project):
        from heddled import authoring

        authoring.update_agent_fields("support", {"model": "deepsek/chat"})
        body = client.get("/agents/support").data.decode()
        assert "doesn't recognise" in body and "deepsek" in body

    def test_settings_offers_a_deepseek_key_field(self, client):
        body = client.get("/settings").data.decode()
        assert "setting_deepseek_api_key" in body and "DeepSeek API key" in body

    def test_settings_does_not_ask_for_a_key_no_local_service_issues(self, client):
        assert "setting_ollama_api_key" not in client.get("/settings").data.decode()

    def test_saving_a_deepseek_key_keeps_the_openai_one(self, client, store):
        store.set_setting("openai_api_key", "sk-openai")
        client.post("/settings", data={"setting_deepseek_api_key": "sk-deepseek"})
        assert store.get_setting("openai_api_key") == "sk-openai"
        assert store.get_setting("deepseek_api_key") == "sk-deepseek"


class TestRemovingAKey:
    """A key you can set but never unset is a key you can never rotate away from."""

    def test_a_credential_can_be_cleared_explicitly(self, client, store):
        store.set_setting("deepseek_api_key", "sk-old")
        client.post("/settings", data={"setting_deepseek_api_key": "",
                                       "clear": "deepseek_api_key"})
        assert store.get_setting("deepseek_api_key") is None

    def test_clearing_one_leaves_the_others_alone(self, client, store):
        store.set_setting("deepseek_api_key", "sk-d")
        store.set_setting("openai_api_key", "sk-o")
        client.post("/settings", data={"setting_deepseek_api_key": "",
                                       "setting_openai_api_key": "",
                                       "clear": "deepseek_api_key"})
        assert store.get_setting("deepseek_api_key") is None
        assert store.get_setting("openai_api_key") == "sk-o"

    def test_a_blank_field_without_the_tick_still_means_unchanged(self, client, store):
        store.set_setting("deepseek_api_key", "sk-d")
        client.post("/settings", data={"setting_deepseek_api_key": ""})
        assert store.get_setting("deepseek_api_key") == "sk-d"

    def test_the_tick_only_appears_for_a_credential_that_is_set(self, client, store):
        assert 'value="deepseek_api_key"' not in client.get("/settings").data.decode()
        store.set_setting("deepseek_api_key", "sk-d")
        assert 'value="deepseek_api_key"' in client.get("/settings").data.decode()

    def test_the_picker_stops_offering_it_once_cleared(self, client, store):
        from heddled.web.app import model_groups

        store.set_setting("deepseek_api_key", "sk-d")
        client.post("/settings", data={"setting_deepseek_api_key": "",
                                       "clear": "deepseek_api_key"})
        assert not next(g for g in model_groups(store)
                        if g["provider"] == "deepseek")["ready"]


class TestTheWarningIsWhereYouLook:
    """Buried in a collapsed section, a missing key is found by pressing Try it
    and reading a stack trace. It belongs where the eye already is."""

    def _on_deepseek(self):
        from heddled import authoring

        authoring.update_agent_fields("support", {"model": "deepseek/deepseek-chat"})

    def test_the_agent_page_leads_with_it(self, client, project):
        self._on_deepseek()
        body = " ".join(client.get("/agents/support").data.decode().split())
        assert "there is no DeepSeek key yet" in body
        assert body.index("no DeepSeek key yet") < body.index("How it behaves")

    def test_the_model_fact_is_badged(self, client, project):
        self._on_deepseek()
        assert "no key" in client.get("/agents/support").data.decode()

    def test_the_try_it_page_says_so_before_you_type(self, client, project):
        self._on_deepseek()
        body = " ".join(client.get("/agents/support/test").data.decode().split())
        assert "This won't answer yet" in body and "DeepSeek key" in body

    def test_it_offers_the_stand_in_as_a_way_out(self, client, project):
        self._on_deepseek()
        assert "mock/echo" in client.get("/agents/support/test").data.decode()

    def test_a_working_agent_gets_no_banner(self, client):
        for path in ("/agents/support", "/agents/support/test"):
            body = client.get(path).data.decode()
            assert "won't answer yet" not in body and "no key yet" not in body

    def test_the_banner_clears_once_the_key_is_added(self, client, project, store):
        self._on_deepseek()
        store.set_setting("deepseek_api_key", "sk-d")
        for path in ("/agents/support", "/agents/support/test"):
            assert "no key yet" not in client.get(path).data.decode()

    def test_an_unknown_service_is_named_as_such(self, client, project):
        from heddled import authoring

        authoring.update_agent_fields("support", {"model": "deepsek/chat"})
        for path in ("/agents/support", "/agents/support/test"):
            body = " ".join(client.get(path).data.decode().split())
            assert "doesn't recognise" in body and "deepsek" in body

    def test_the_helper_reports_what_the_templates_rely_on(self, store):
        from heddled.web.app import model_service

        assert model_service(store, "mock/echo") == {
            "provider": "mock", "label": "Built-in stand-in (no account needed)",
            "ready": True, "known": True, "models": ["mock/echo"]}
        unknown = model_service(store, "nope/x")
        assert unknown["known"] is False and unknown["ready"] is False

    def test_a_bare_model_name_is_read_as_anthropic(self, store):
        from heddled.web.app import model_service

        store.set_setting("anthropic_api_key", "sk-a")
        # split_model() defaults a prefix-less model to anthropic; the page must
        # not then claim the service is unknown.
        assert model_service(store, "anthropic/claude-sonnet-4-6")["ready"]
