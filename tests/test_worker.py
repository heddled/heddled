"""The background worker's retry schedule.

A turn runs off a durable queue, so a transient failure must be retried
promptly and a permanent one must give up rather than spin.
"""

import time

from heddled import worker as worker_mod


class TestRetrySchedule:
    def test_the_claimed_row_reports_the_current_attempt(self, store):
        """Regression: claim_job used to return the row as it was *before* the
        attempt counter was bumped, so the first retry indexed BACKOFF_S[-1]
        and waited the longest backoff instead of the shortest."""
        store.enqueue("run_turn", {})
        assert store.claim_job()["attempts"] == 1

    def test_attempts_climb_across_claims(self, store):
        store.enqueue("run_turn", {})
        seen = []
        for _ in range(3):
            job = store.claim_job()
            seen.append(job["attempts"])
            store.execute("UPDATE jobs SET status='queued', run_at=? WHERE id=?",
                          (time.time() - 1, job["id"]))
        assert seen == [1, 2, 3]

    def test_the_first_retry_uses_the_shortest_backoff(self, store, registry, monkeypatch):
        monkeypatch.setattr(worker_mod, "BACKOFF_S", [0.05, 10, 60])
        store.enqueue("nonexistent_job_kind", {})

        w = worker_mod.Worker(concurrency=1, run_triggers=False)
        w.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                row = store.one("SELECT * FROM jobs WHERE kind='nonexistent_job_kind'")
                if row["attempts"] >= 2:
                    break
                time.sleep(0.02)
            assert row["attempts"] >= 2, "the first retry did not happen promptly"
        finally:
            w.stop()

    def test_a_permanently_failing_job_gives_up_after_max_attempts(
            self, store, registry, monkeypatch):
        monkeypatch.setattr(worker_mod, "BACKOFF_S", [0.05, 0.05, 0.05])
        store.enqueue("nonexistent_job_kind", {})

        w = worker_mod.Worker(concurrency=1, run_triggers=False)
        w.start()
        try:
            deadline = time.time() + 10
            row = None
            while time.time() < deadline:
                row = store.one("SELECT * FROM jobs WHERE kind='nonexistent_job_kind'")
                if row["status"] == "failed":
                    break
                time.sleep(0.02)
            assert row["status"] == "failed"
            assert row["attempts"] == worker_mod.MAX_ATTEMPTS
            assert "unknown job kind" in row["error"]
        finally:
            w.stop()

    def test_the_worker_heartbeats(self, store, registry):
        w = worker_mod.Worker(concurrency=1, run_triggers=False)
        w.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not store.get_cursor("worker:heartbeat"):
                time.sleep(0.02)
            assert store.get_cursor("worker:heartbeat")
        finally:
            w.stop()
