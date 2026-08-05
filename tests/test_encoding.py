"""Payload encoding (decision 4).

Context capture is worth real disk, so payloads are compressed — with zstd when
the interpreter has it and zlib otherwise. The `encoding` column records which,
so a store stays readable either way and never needs a migration.
"""

import json
import zlib

import pytest

from heddled import store as store_mod
from heddled.events import Event


class TestRoundTrip:
    def test_a_payload_survives_encoding(self):
        payload = {"a": 1, "b": ["x", "y"], "c": {"d": True}, "unicode": "€ ĳ"}
        blob = store_mod._encode(payload)
        assert store_mod._decode(blob, store_mod.ENCODING) == payload

    def test_the_encoding_label_is_one_of_the_two_supported(self):
        assert store_mod.ENCODING in ("zstd+json", "zlib+json")

    def test_compression_actually_shrinks_a_realistic_payload(self):
        payload = {"system": "You are a helpful agent. " * 200}
        assert len(store_mod._encode(payload)) < len(json.dumps(payload)) / 5


class TestBackwardCompatibility:
    """A store written before (or after) an interpreter upgrade must stay
    readable — that is the whole point of the encoding column."""

    def test_zlib_rows_are_always_readable(self):
        payload = {"written_by": "an older heddled"}
        blob = zlib.compress(json.dumps(payload).encode(), 6)
        assert store_mod._decode(blob, "zlib+json") == payload

    def test_uncompressed_json_rows_are_readable(self):
        payload = {"written_by": "something plain"}
        assert store_mod._decode(json.dumps(payload).encode(), "json") == payload

    def test_an_unlabelled_row_is_read_as_zlib(self):
        """The column default, and what every pre-existing row carries."""
        payload = {"legacy": True}
        blob = zlib.compress(json.dumps(payload).encode(), 6)
        assert store_mod._decode(blob) == payload

    @pytest.mark.skipif(store_mod._zstd is None, reason="interpreter has no zstd")
    def test_zstd_rows_are_readable_when_supported(self):
        payload = {"written_by": "a newer heddled"}
        blob = store_mod._zstd.compress(json.dumps(payload).encode(), 3)
        assert store_mod._decode(blob, "zstd+json") == payload

    @pytest.mark.skipif(store_mod._zstd is not None, reason="interpreter has zstd")
    def test_a_zstd_row_fails_loudly_on_an_interpreter_without_zstd(self):
        with pytest.raises(RuntimeError, match="zstd"):
            store_mod._decode(b"not-really-zstd", "zstd+json")


class TestStoreLabelling:
    def test_appended_events_record_their_encoding(self, store):
        store.append(Event(type="message.received", session_id="s_1", payload={"t": 1}))
        row = store.one("SELECT encoding FROM events WHERE session_id='s_1'")
        assert row["encoding"] == store_mod.ENCODING

    def test_a_row_written_as_zlib_still_reads_back(self, store):
        """Simulates a store carried over from an interpreter without zstd."""
        payload = {"text": "written the old way"}
        store.execute(
            "INSERT INTO events (ts, type, session_id, turn_id, agent, agent_version,"
            " payload, encoding) VALUES (?,?,?,?,?,?,?,?)",
            (1.0, "message.received", "s_old", "t_old", "support", "v1",
             zlib.compress(json.dumps(payload).encode(), 6), "zlib+json"),
        )
        assert store.events_for_session("s_old")[0].payload == payload

    def test_retention_relabels_the_rows_it_rewrites(self, store, monkeypatch):
        from heddled import config

        monkeypatch.setattr(config, "KEEP_FULL_CONTEXT_DAYS", 0)
        store.execute(
            "INSERT INTO events (ts, type, session_id, turn_id, agent, agent_version,"
            " payload, encoding) VALUES (?,?,?,?,?,?,?,?)",
            (1.0, "context.built", "s_old", "t_old", "support", "v1",
             zlib.compress(json.dumps({"system": "x" * 500}).encode(), 6), "zlib+json"),
        )
        assert store.apply_retention() == 1
        row = store.one("SELECT encoding FROM events WHERE session_id='s_old'")
        assert row["encoding"] == store_mod.ENCODING
        assert store.events_for_session("s_old")[0].payload["pruned"] is True
