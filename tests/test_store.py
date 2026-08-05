"""The spine's persistence: append-only events, session state, the job queue
that lets a turn outlive its request, and the cursors that let a poller survive
a restart."""

import time

from heddled import config
from heddled.events import Event


class TestEventAppend:
    def test_append_assigns_a_monotonic_sequence(self, store):
        seqs = [store.append(Event(type="message.received", session_id="s_1",
                                   payload={"n": i})).seq for i in range(5)]
        assert seqs == sorted(seqs) and len(set(seqs)) == 5

    def test_payload_round_trips_through_compression(self, store):
        payload = {"text": "hello", "nested": {"a": [1, 2, 3]}, "unicode": "€ ĳ"}
        store.append(Event(type="context.built", session_id="s_1", payload=payload))
        assert store.events_for_session("s_1")[0].payload == payload

    def test_events_are_scoped_to_their_session(self, store):
        store.append(Event(type="message.received", session_id="s_1", payload={}))
        store.append(Event(type="message.received", session_id="s_2", payload={}))
        assert len(store.events_for_session("s_1")) == 1

    def test_after_seq_returns_only_newer_events(self, store):
        first = store.append(Event(type="message.received", session_id="s_1", payload={}))
        store.append(Event(type="message.sent", session_id="s_1", payload={}))
        later = store.events_for_session("s_1", after_seq=first.seq)
        assert [e.type for e in later] == ["message.sent"]

    def test_events_are_addressable_per_turn(self, store):
        store.append(Event(type="message.received", session_id="s_1", turn_id="t_1", payload={}))
        store.append(Event(type="message.received", session_id="s_1", turn_id="t_2", payload={}))
        assert len(store.events_for_turn("t_1")) == 1

    def test_subscribers_are_notified_live(self, store):
        import queue

        q = queue.Queue()
        store.subscribe(q)
        try:
            store.append(Event(type="turn.completed", session_id="s_1", payload={}))
            assert q.get(timeout=2).type == "turn.completed"
        finally:
            store.unsubscribe(q)

    def test_a_slow_subscriber_does_not_break_the_append(self, store):
        import queue

        q = queue.Queue(maxsize=1)
        store.subscribe(q)
        try:
            for _ in range(5):
                store.append(Event(type="message.sent", session_id="s_1", payload={}))
            assert len(store.events_for_session("s_1")) == 5
        finally:
            store.unsubscribe(q)


class TestSessionsAndState:
    def test_create_and_read_back_a_session(self, store):
        sid = store.create_session(agent="support", agent_version="v1", channel="webchat",
                                   trigger_origin={"kind": "webchat"}, env="dev")
        row = store.get_session(sid)
        assert row["agent"] == "support" and row["status"] == "running"

    def test_state_round_trips(self, store):
        sid = store.create_session(agent="support", agent_version="v1")
        store.set_state(sid, {"messages": [{"role": "user", "content": "hi"}], "iteration": 2})
        assert store.get_state(sid)["iteration"] == 2

    def test_missing_state_is_an_empty_dict(self, store):
        assert store.get_state("s_nonexistent") == {}

    def test_sessions_filter_by_agent_and_status(self, store):
        a = store.create_session(agent="support", agent_version="v1")
        store.create_session(agent="other", agent_version="v1")
        store.update_session(a, status="ended")
        assert [r["id"] for r in store.list_sessions(agent="support", status="ended")] == [a]

    def test_child_sessions_link_to_their_parent(self, store):
        parent = store.create_session(agent="support", agent_version="v1")
        child = store.create_session(agent="billing", agent_version="v1",
                                     parent_session_id=parent)
        rows = store.query("SELECT id FROM sessions WHERE parent_session_id=?", (parent,))
        assert [r["id"] for r in rows] == [child]


class TestJobQueue:
    """A turn must be able to outlive the HTTP request that started it, so the
    queue is durable and claims are exclusive."""

    def test_enqueued_job_is_claimable(self, store):
        store.enqueue("run_turn", {"agent": "support"})
        job = store.claim_job()
        assert job["kind"] == "run_turn"

    def test_a_job_is_claimed_exactly_once(self, store):
        store.enqueue("run_turn", {"n": 1})
        assert store.claim_job() is not None
        assert store.claim_job() is None

    def test_claiming_an_empty_queue_returns_none(self, store):
        assert store.claim_job() is None

    def test_finished_jobs_leave_the_queue(self, store):
        store.enqueue("run_turn", {})
        job = store.claim_job()
        store.finish_job(job["id"], "done")
        assert store.health()["queue_depth"] == 0

    def test_concurrent_claims_never_hand_out_the_same_job(self, store):
        import threading

        for i in range(20):
            store.enqueue("run_turn", {"n": i})
        claimed, lock = [], threading.Lock()

        def drain():
            while True:
                job = store.claim_job()
                if job is None:
                    return
                with lock:
                    claimed.append(job["id"])

        threads = [threading.Thread(target=drain) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(claimed) == 20 and len(set(claimed)) == 20


class TestCursors:
    """A poller is stateful and long-running: the cursor must survive a
    restart (concept §7)."""

    def test_cursor_round_trips_json(self, store):
        store.set_cursor("poll:support:0:cursor", ["msg1.eml:123"])
        assert store.get_cursor("poll:support:0:cursor") == ["msg1.eml:123"]

    def test_missing_cursor_returns_the_default(self, store):
        assert store.get_cursor("nope", "fallback") == "fallback"

    def test_cursor_survives_a_new_store_on_the_same_file(self, store, project):
        from heddled.store import Store

        store.set_cursor("poll:support:0:cursor", 42)
        assert Store(project / "data" / "heddled.db").get_cursor("poll:support:0:cursor") == 42


class TestRenamingAnAgent:
    """A rename is the same agent under a new name, so what it did comes with
    it — otherwise the new page looks like the history was lost."""

    def test_sessions_and_events_follow_the_name(self, store):
        sid = store.create_session(agent="support", agent_version="v1")
        store.append(Event(type="turn.started", session_id=sid, agent="support", payload={}))
        store.rename_agent("support", "billing")
        assert [r["id"] for r in store.list_sessions(agent="billing")] == [sid]
        assert store.list_sessions(agent="support") == []
        assert store.one("SELECT agent FROM events WHERE session_id=?", (sid,))["agent"] \
            == "billing"

    def test_a_poller_keeps_its_place(self, store):
        """Left behind, the cursor resets and the poller re-reads everything it
        has already seen — a rename would replay the whole mailbox."""
        store.set_cursor("poll:support:0:cursor", ["msg1.eml:123"])
        store.set_cursor("schedule:support:1", "2026-08-03T08:00")
        store.rename_agent("support", "billing")
        assert store.get_cursor("poll:billing:0:cursor") == ["msg1.eml:123"]
        assert store.get_cursor("schedule:billing:1") == "2026-08-03T08:00"
        assert store.get_cursor("poll:support:0:cursor") is None

    def test_another_agents_rows_are_left_alone(self, store):
        mine = store.create_session(agent="support", agent_version="v1")
        theirs = store.create_session(agent="office_helper", agent_version="v1")
        store.set_cursor("poll:office_helper:0:cursor", "untouched")
        store.rename_agent("support", "billing")
        assert [r["id"] for r in store.list_sessions(agent="office_helper")] == [theirs]
        assert store.get_cursor("poll:office_helper:0:cursor") == "untouched"
        assert store.get_session(mine)["agent"] == "billing"

    def test_it_reports_what_it_moved(self, store):
        store.create_session(agent="support", agent_version="v1")
        moved = store.rename_agent("support", "billing")
        assert moved["sessions"] == 1
        assert "deployments" not in moved      # nothing there to move


class TestApprovals:
    def test_pending_approval_is_listed_then_resolved(self, store):
        sid = store.create_session(agent="support", agent_version="v1")
        aid = store.create_approval(session_id=sid, turn_id="t_1", agent="support",
                                    tool="refund", args={"amount_eur": 10},
                                    reason="policy", routed_to="webhook", token="tok_1")
        assert [a["id"] for a in store.pending_approvals()] == [aid]
        store.resolve_approval(aid, "approved", "ralph", None)
        assert store.pending_approvals() == []
        assert store.get_approval(aid)["status"] == "approved"


class TestLedgerAndHealth:
    def test_spend_accumulates_per_agent(self, store):
        store.record_spend("eur", 1.5, agent="support", session_id="s_1")
        store.record_spend("eur", 2.0, agent="support", session_id="s_1")
        assert store.spend_today("eur", agent="support") == 3.5

    def test_spend_is_filterable_by_session(self, store):
        store.record_spend("tokens", 100, agent="support", session_id="s_1")
        store.record_spend("tokens", 500, agent="support", session_id="s_2")
        assert store.spend_today("tokens", session_id="s_1") == 100

    def test_health_reports_what_the_admin_strip_shows(self, store):
        h = store.health()
        assert {"queue_depth", "errors_last_hour", "sessions_running",
                "waiting_approval", "events_total"} <= set(h)

    def test_errors_last_hour_counts_error_events(self, store):
        store.append(Event(type="error.raised", session_id="s_1",
                           payload={"kind": "tool_failed"}))
        assert store.health()["errors_last_hour"] == 1


class TestToolCallCounting:
    """Backs the rate-limit policy."""

    def _call(self, store, tool, ts):
        ev = Event(type="tool.called", session_id="s_1", agent="support",
                   payload={"tool": tool}, ts=ts)
        store.append(ev)

    def test_counts_only_the_named_tool(self, store):
        now = time.time()
        for _ in range(3):
            self._call(store, "refund", now)
        self._call(store, "lookup_invoice", now)
        assert store.count_tool_calls("support", "refund", now - 60) == 3

    def test_calls_outside_the_window_are_excluded(self, store):
        now = time.time()
        self._call(store, "refund", now - 3600)
        self._call(store, "refund", now)
        assert store.count_tool_calls("support", "refund", now - 60) == 1

    def test_other_agents_are_excluded(self, store):
        now = time.time()
        store.append(Event(type="tool.called", session_id="s_1", agent="other",
                           payload={"tool": "refund"}, ts=now))
        assert store.count_tool_calls("support", "refund", now - 60) == 0


class TestRetention:
    """Decision 4: store everything, compress, and prune full context on a
    retention knob."""

    def test_old_context_payloads_are_pruned_but_the_event_remains(self, store, monkeypatch):
        monkeypatch.setattr(config, "KEEP_FULL_CONTEXT_DAYS", 1)
        old = time.time() - 86400 * 5
        store.append(Event(type="context.built", session_id="s_1",
                           payload={"system": "x" * 500, "messages": [1, 2, 3]}, ts=old))
        assert store.apply_retention() == 1
        ev = store.events_for_session("s_1")[0]
        assert ev.type == "context.built"
        assert "pruned_reason" in ev.payload

    def test_recent_context_is_kept(self, store, monkeypatch):
        monkeypatch.setattr(config, "KEEP_FULL_CONTEXT_DAYS", 90)
        store.append(Event(type="context.built", session_id="s_1",
                           payload={"system": "keep me"}))
        assert store.apply_retention() == 0
        assert store.events_for_session("s_1")[0].payload["system"] == "keep me"
