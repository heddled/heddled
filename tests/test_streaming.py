"""Token streaming.

The property that matters is that streaming changes *when* text arrives and
nothing else: the same events, the same stored trace, the same final text. A
turn replayed from the spine must look identical whether it streamed or not.
"""

from heddled.providers.base import ModelResponse, Provider
from heddled.providers.mock import MockProvider
from heddled.store import Ephemeral


class SilentProvider(Provider):
    """A provider from before streaming existed."""

    def complete(self, system, messages, tools=None, max_tokens=4096, temperature=None):
        return ModelResponse(text="all at once", usage={"input_tokens": 1,
                                                        "output_tokens": 2})


class TestTheFallback:
    def test_a_provider_without_streaming_still_answers(self):
        got = []
        r = SilentProvider("x").stream("", [{"role": "user", "content": "hi"}], got.append)
        assert r.text == "all at once"
        assert got == ["all at once"], "the whole reply should arrive as one delta"

    def test_and_says_so(self):
        assert SilentProvider("x").supports_streaming is False
        assert MockProvider("mock/echo").supports_streaming is True


class TestTheDeltasReassemble:
    def test_the_pieces_join_back_into_the_reply(self):
        got = []
        r = MockProvider("mock/echo").stream(
            "", [{"role": "user", "content": "hello there"}], got.append)
        assert len(got) > 1, "should arrive in pieces, not one lump"
        assert "".join(got) == r.text

    def test_nothing_is_lost_or_doubled(self):
        got = []
        r = MockProvider("mock/echo").stream(
            "", [{"role": "user", "content": "count the words in this one"}], got.append)
        assert "".join(got) == r.text
        assert r.text.count("received") == "".join(got).count("received")


class TestDeltasAreNotEvents:
    def test_a_streamed_turn_stores_no_extra_events(self, store, agent):
        """The spine is the audit log, not a transport. A thousand tokens must
        not become a thousand rows."""
        from test_engine import run_turn, types_of

        _, sid = run_turn(store, agent, "just say hello")
        kinds = types_of(store, sid)
        assert "model.delta" not in kinds
        assert kinds.count("model.responded") == 1

    def test_the_documented_sequence_is_unchanged_by_streaming(self, store, agent):
        from test_engine import run_turn, types_of

        _, sid = run_turn(store, agent, "just say hello")
        assert types_of(store, sid) == [
            "message.received", "context.built", "model.invoked",
            "model.responded", "message.sent", "turn.completed",
        ]

    def test_the_reply_is_still_recorded_in_full(self, store, agent):
        from test_engine import run_turn

        result, sid = run_turn(store, agent, "just say hello")
        sent = [e for e in store.events_for_session(sid) if e.type == "message.sent"]
        assert sent and sent[0].payload.get("text") == result.reply

    def test_broadcasts_reach_listeners_without_being_stored(self, store):
        import queue

        q: queue.Queue = queue.Queue()
        store.subscribe(q)
        try:
            store.broadcast("s_1", "model.delta", {"text": "tok"})
            item = q.get(timeout=2)
        finally:
            store.unsubscribe(q)
        assert isinstance(item, Ephemeral)
        assert item.session_id == "s_1" and item.payload["text"] == "tok"
        assert store.events_for_session("s_1") == []
