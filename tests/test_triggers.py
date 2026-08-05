"""Pull triggers — Heddled itself as the active party (concept §7).

The lifecycle wrinkle that matters: a poller is stateful and long-running. It
must not reprocess on the next tick or after a restart.
"""

from datetime import datetime

import pytest

from heddled import triggers


class TestCron:
    @pytest.mark.parametrize("expr,when,expected", [
        ("0 8 * * 1-5", datetime(2026, 8, 3, 8, 0), True),    # Monday 08:00
        ("0 8 * * 1-5", datetime(2026, 8, 7, 8, 0), True),    # Friday
        ("0 8 * * 1-5", datetime(2026, 8, 8, 8, 0), False),   # Saturday
        ("0 8 * * 1-5", datetime(2026, 8, 2, 8, 0), False),   # Sunday
        ("0 8 * * 1-5", datetime(2026, 8, 3, 9, 0), False),   # wrong hour
        ("0 8 * * 1-5", datetime(2026, 8, 3, 8, 1), False),   # wrong minute
        ("* * * * *", datetime(2026, 8, 3, 13, 37), True),
        ("*/15 * * * *", datetime(2026, 8, 3, 3, 30), True),
        ("*/15 * * * *", datetime(2026, 8, 3, 3, 31), False),
        ("30 2 1 * *", datetime(2026, 9, 1, 2, 30), True),
        ("30 2 1 * *", datetime(2026, 9, 2, 2, 30), False),
        ("0 0 * jan *", datetime(2026, 1, 5, 0, 0), True),
        ("0 0 * jan *", datetime(2026, 2, 5, 0, 0), False),
        ("0 9 * * mon", datetime(2026, 8, 3, 9, 0), True),
        ("0 9 * * sun", datetime(2026, 8, 2, 9, 0), True),
        ("0,30 * * * *", datetime(2026, 8, 3, 4, 30), True),
        ("0,30 * * * *", datetime(2026, 8, 3, 4, 15), False),
    ])
    def test_cron_matching(self, expr, when, expected):
        assert triggers.cron_matches(expr, when) is expected

    @pytest.mark.parametrize("alias,when", [
        ("@daily", datetime(2026, 8, 3, 0, 0)),
        ("@hourly", datetime(2026, 8, 3, 5, 0)),
        ("@monthly", datetime(2026, 8, 1, 0, 0)),
    ])
    def test_aliases(self, alias, when):
        assert triggers.cron_matches(alias, when)

    def test_dom_and_dow_both_restricted_means_either_may_match(self):
        """Standard cron semantics, and easy to get wrong."""
        assert triggers.cron_matches("0 0 1 * mon", datetime(2026, 9, 1, 0, 0))   # 1st
        assert triggers.cron_matches("0 0 1 * mon", datetime(2026, 9, 7, 0, 0))   # Monday
        assert not triggers.cron_matches("0 0 1 * mon", datetime(2026, 9, 8, 0, 0))

    def test_a_malformed_expression_is_rejected_loudly(self):
        with pytest.raises(ValueError):
            triggers.cron_matches("0 8 * *", datetime(2026, 8, 3, 8, 0))


class TestIntervals:
    @pytest.mark.parametrize("value,seconds", [
        ("60s", 60), ("5m", 300), ("2h", 7200), ("1d", 86400),
        ("90", 90), (30, 30), (1.5, 1.5), (" 10m ", 600),
    ])
    def test_parse_interval(self, value, seconds):
        assert triggers.parse_interval(value) == seconds

    def test_nonsense_is_rejected(self):
        with pytest.raises(ValueError):
            triggers.parse_interval("soon")


def drain_kinds(store) -> list[str]:
    """Every job kind currently on the queue. A tick may fire more than one
    trigger, so tests assert on kinds rather than on a bare count."""
    kinds = []
    while (job := store.claim_job()) is not None:
        kinds.append(job["kind"])
    return kinds


class TestScheduleFiring:
    def test_a_matching_minute_enqueues_a_schedule_job(self, store, registry):
        triggers.tick(datetime(2026, 8, 3, 8, 0))
        assert drain_kinds(store).count("schedule_trigger") == 1

    def test_the_same_minute_never_fires_twice(self, store, registry):
        when = datetime(2026, 8, 3, 8, 0)
        triggers.tick(when)
        drain_kinds(store)
        triggers.tick(when)
        assert "schedule_trigger" not in drain_kinds(store)

    def test_a_non_matching_minute_does_not_fire_the_schedule(self, store, registry):
        triggers.tick(datetime(2026, 8, 3, 9, 17))
        assert "schedule_trigger" not in drain_kinds(store)

    def test_the_minute_guard_survives_a_restart(self, store, registry, project):
        from heddled import store as store_mod

        when = datetime(2026, 8, 3, 8, 0)
        triggers.tick(when)
        drain_kinds(store)
        fresh = store_mod.Store(project / "data" / "heddled.db")
        store_mod._store = fresh
        try:
            triggers.tick(when)
            assert "schedule_trigger" not in drain_kinds(fresh)
        finally:
            store_mod._store = store


class TestPollFiring:
    def test_the_first_tick_fires_the_poller(self, store, registry):
        triggers.tick(datetime(2026, 8, 3, 9, 17))
        assert drain_kinds(store).count("poll_trigger") == 1

    def test_a_second_tick_inside_the_interval_does_not_fire(self, store, registry):
        triggers.tick(datetime(2026, 8, 3, 9, 17))
        drain_kinds(store)
        triggers.tick(datetime(2026, 8, 3, 9, 17))
        assert "poll_trigger" not in drain_kinds(store)

    def test_a_trigger_error_is_recorded_rather_than_crashing_the_worker(
            self, store, registry, project):
        path = project / "agents" / "support.yaml"
        path.write_text(path.read_text().replace('"0 8 * * 1-5"', '"nonsense"'))
        triggers.tick(datetime(2026, 8, 3, 8, 0))  # must not raise
        assert store.get_cursor("trigger:error:support:schedule:0")


class TestFolderPoller:
    def _payload(self, agent):
        t = next(t for t in agent.triggers if t.kind == "poll")
        return {"agent": agent.name, "trigger": t.raw, "key": t.key}

    def test_a_new_file_starts_a_turn_and_advances_the_cursor(self, store, registry,
                                                              agent, project):
        (project / "var" / "mailbox" / "msg1.eml").write_text("Subject: Invoice F-1\n\nPlease pay.")
        triggers.run_poll_trigger(self._payload(agent))

        sessions = store.list_sessions(channel="poll")
        assert len(sessions) == 1
        cursor = store.get_cursor(f"poll:support:poll:1:cursor")
        assert cursor and any("msg1.eml" in c for c in cursor)

    def test_trigger_fired_is_the_first_event_of_the_session(self, store, registry,
                                                             agent, project):
        (project / "var" / "mailbox" / "msg1.eml").write_text("Subject: Invoice F-1\n\nPay me.")
        triggers.run_poll_trigger(self._payload(agent))
        sid = store.list_sessions(channel="poll")[0]["id"]
        first = store.events_for_session(sid)[0]
        assert first.type == "trigger.fired"
        assert first.payload["kind"] == "poll" and "msg1.eml" in first.payload["reason"]

    def test_the_same_file_is_not_reprocessed(self, store, registry, agent, project):
        (project / "var" / "mailbox" / "msg1.eml").write_text("Subject: Invoice F-1\n\nPay.")
        triggers.run_poll_trigger(self._payload(agent))
        triggers.run_poll_trigger(self._payload(agent))
        assert len(store.list_sessions(channel="poll")) == 1

    def test_a_second_file_fires_a_second_turn(self, store, registry, agent, project):
        (project / "var" / "mailbox" / "a.eml").write_text("one")
        triggers.run_poll_trigger(self._payload(agent))
        (project / "var" / "mailbox" / "b.eml").write_text("two")
        triggers.run_poll_trigger(self._payload(agent))
        assert len(store.list_sessions(channel="poll")) == 2

    def test_the_item_body_reaches_the_agent(self, store, registry, agent, project):
        (project / "var" / "mailbox" / "msg1.eml").write_text("Invoice F-4444 is overdue")
        triggers.run_poll_trigger(self._payload(agent))
        job = store.claim_job()
        assert "F-4444" in job["payload"]
        assert "Handle this incoming invoice email." in job["payload"]

    def test_an_unknown_poller_is_recorded_not_raised(self, store, registry, agent):
        triggers.run_poll_trigger({"agent": "support", "key": "poll:1",
                                   "trigger": {"poll": "nonexistent-source"}})
        assert "unknown poller" in str(store.get_cursor("trigger:error:support:poll:1"))


class TestTriggerStatus:
    def test_status_reports_what_the_agents_screen_shows(self, store, registry):
        rows = triggers.trigger_status()
        kinds = {r["kind"] for r in rows}
        assert kinds == {"schedule", "poll"}
        schedule = next(r for r in rows if r["kind"] == "schedule")
        assert schedule["cron"] == "0 8 * * 1-5"
