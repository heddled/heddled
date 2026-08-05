"""Comment-preserving round-trip (decision 9).

The forms are a view over the file, which is only honest if writing the file
back is lossless. A form that quietly reformats — or deletes the comments
explaining why a policy exists — is worse than no form at all.
"""

from heddled import yamlio

SAMPLE = """\
# The support agent.
# Edited here or in the console — same file either way.
name: support
description: Invoice support.
model: mock/echo                     # swap for anthropic when you have a key
instructions: ./support.md

adapters:
  channels: [webchat, webhook]
  tools: [lookup_invoice, refund]

policies:
  - tool: refund
    requires_approval: true          # a human signs off on money leaving
    budget: { max_eur_per_day: 500 }

memory:
  session: auto
"""


class TestRoundTrip:
    def test_a_no_op_load_and_dump_keeps_the_comments(self):
        out = yamlio._restore_cosmetic(SAMPLE, yamlio.dump(yamlio.load(SAMPLE)))
        assert "# The support agent." in out
        assert "# a human signs off on money leaving" in out
        assert "# swap for anthropic when you have a key" in out

    def test_a_no_op_update_changes_nothing_at_all(self):
        assert yamlio.apply_updates(SAMPLE, {"model": "mock/echo"}) == SAMPLE

    def test_changing_one_field_changes_one_line(self):
        out = yamlio.apply_updates(SAMPLE, {"model": "anthropic/claude-sonnet-4-6"})
        changed = [line for line in yamlio.diff(SAMPLE, out).splitlines()
                   if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
        assert len(changed) == 2  # one removal, one addition
        assert "anthropic/claude-sonnet-4-6" in out

    def test_an_inline_comment_survives_a_change_to_its_own_line(self):
        out = yamlio.apply_updates(SAMPLE, {"model": "anthropic/claude-sonnet-4-6"})
        assert "# swap for anthropic when you have a key" in out

    def test_key_order_is_preserved(self):
        out = yamlio.apply_updates(SAMPLE, {"description": "Changed."})
        keys = [line.split(":")[0] for line in out.splitlines()
                if line and not line.startswith((" ", "#", "-"))]
        assert keys == ["name", "description", "model", "instructions",
                        "adapters", "policies", "memory"]

    def test_a_new_key_is_appended_without_disturbing_the_rest(self):
        out = yamlio.apply_updates(SAMPLE, {"expose": {"mcp": True}})
        assert "# The support agent." in out
        assert "mcp" in out and "expose" in out

    def test_removing_a_key(self):
        out = yamlio.apply_updates(SAMPLE, {"description": None})
        assert "description:" not in out
        assert "# The support agent." in out

    def test_unknown_keys_the_form_never_touches_survive(self):
        text = SAMPLE + "\ncustom_thing:\n  kept: true   # nobody's business but mine\n"
        out = yamlio.apply_updates(text, {"model": "openai/gpt-4o"})
        assert "custom_thing" in out and "nobody's business but mine" in out

    def test_flow_style_spacing_is_not_normalised_in_untouched_lines(self):
        """ruamel rewrites `{ a: 1 }` as `{a: 1}`; a form edit must not put an
        untouched line in the diff."""
        out = yamlio.apply_updates(SAMPLE, {"description": "Changed."})
        assert "budget: { max_eur_per_day: 500 }" in out


class TestValidation:
    def test_valid_yaml_passes(self):
        assert yamlio.is_valid(SAMPLE) == (True, None)

    def test_invalid_yaml_reports_a_line(self):
        ok, error = yamlio.is_valid("a:\n  b: [\n")
        assert not ok and "line" in error

    def test_an_empty_document_is_valid(self):
        assert yamlio.is_valid("")[0]

    def test_load_of_an_empty_document_is_an_empty_mapping(self):
        assert yamlio.load("") == {}


class TestUnrepresentable:
    """Anything the form cannot show is surfaced rather than dropped."""

    def test_a_plain_document_is_fully_representable(self):
        assert yamlio.unrepresentable(SAMPLE) == []

    def test_merge_keys_are_detected(self):
        text = "defaults: &d\n  model: mock/echo\nagent:\n  <<: *d\n"
        assert any("merge" in f for f in yamlio.unrepresentable(text))

    def test_anchors_are_detected(self):
        text = "defaults: &base\n  model: mock/echo\n"
        assert any("anchor" in f for f in yamlio.unrepresentable(text))

    def test_multiple_documents_are_detected(self):
        text = "---\nname: a\n---\nname: b\n"
        assert any("documents" in f for f in yamlio.unrepresentable(text))

    def test_a_comment_mentioning_a_merge_is_not_a_false_positive(self):
        text = SAMPLE + "\n# we could use <<: here one day\n"
        assert yamlio.unrepresentable(text) == []


class TestDiff:
    def test_a_diff_names_the_file(self):
        d = yamlio.diff("a: 1\n", "a: 2\n", "agents/x.yaml")
        assert "a/agents/x.yaml" in d and "b/agents/x.yaml" in d

    def test_no_change_produces_an_empty_diff(self):
        assert yamlio.diff(SAMPLE, SAMPLE) == ""

    def test_has_changes_ignores_trailing_whitespace(self):
        assert not yamlio.has_changes("a: 1\n", "a: 1\n\n")
        assert yamlio.has_changes("a: 1\n", "a: 2\n")
