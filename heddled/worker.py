"""The background worker.

Two responsibilities, one process (concept §5, §7):
  1. drain the SQLite-backed job queue — this is where turns actually run, so a
     turn survives the HTTP request that started it and can outlive an approval;
  2. be the active party for pull triggers — tick the cron scheduler and wake
     pollers on their interval.

Threads, not asyncio: turns are I/O-bound, and keeping everything synchronous
keeps handler code readable for the people writing tools.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from typing import Optional

from . import triggers
from .store import get_store

MAX_ATTEMPTS = 3
BACKOFF_S = [2, 10, 60]


class Worker:
    def __init__(self, concurrency: int = 2, run_triggers: bool = True, verbose: bool = False):
        self.concurrency = max(1, concurrency)
        self.run_triggers = run_triggers
        self.verbose = verbose
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------- lifecycle

    def start(self) -> "Worker":
        # The OTel exporter is a consumer of the spine, so it attaches wherever
        # the spine lives. A no-op unless a collector endpoint is configured.
        try:
            from . import otel

            otel.configure(get_store())
        except Exception:
            traceback.print_exc()
        for i in range(self.concurrency):
            t = threading.Thread(target=self._drain_loop, name=f"heddled-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        if self.run_triggers:
            t = threading.Thread(target=self._trigger_loop, name="heddled-triggers", daemon=True)
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self._heartbeat_loop, name="heddled-heartbeat", daemon=True)
        t.start()
        self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()

    def join(self) -> None:
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.stop()

    # ------------------------------------------------------------ queue loop

    def _drain_loop(self) -> None:
        from . import runtime

        store = get_store()
        idle = 0.02
        while not self._stop.is_set():
            job = None
            try:
                job = store.claim_job()
            except Exception:
                traceback.print_exc()
            if job is None:
                time.sleep(min(idle, 0.5))
                idle = min(idle * 1.5, 0.5)
                continue
            idle = 0.02
            if self.verbose:
                print(f"[worker] job {job['id']} {job['kind']}")
            try:
                runtime.execute_job(job)
                store.finish_job(job["id"], "done")
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
                if job["attempts"] < MAX_ATTEMPTS:
                    # attempts is 1-based on the row we just claimed, so the
                    # first failure waits BACKOFF_S[0].
                    delay = BACKOFF_S[min(job["attempts"] - 1, len(BACKOFF_S) - 1)]
                    store.execute(
                        "UPDATE jobs SET status='queued', run_at=?, error=? WHERE id=?",
                        (time.time() + delay, err, job["id"]),
                    )
                else:
                    store.finish_job(job["id"], "failed", err)

    # ---------------------------------------------------------- trigger loop

    def _trigger_loop(self) -> None:
        while not self._stop.is_set():
            try:
                triggers.tick()
            except Exception:
                traceback.print_exc()
            self._stop.wait(1.0)

    def _heartbeat_loop(self) -> None:
        store = get_store()
        while not self._stop.is_set():
            try:
                store.set_cursor("worker:heartbeat", time.time())
            except Exception:
                pass
            self._stop.wait(5.0)


_worker: Optional[Worker] = None
_lock = threading.Lock()


def ensure_worker(concurrency: int = 2, run_triggers: bool = True) -> Worker:
    """Start the in-process worker if it isn't running. `heddled serve` uses this
    so a single `docker compose up` (or a single command) is the whole platform;
    `heddled worker` runs it standalone when you want to split the processes."""
    global _worker
    if _worker is None:
        with _lock:
            if _worker is None:
                _worker = Worker(concurrency=concurrency, run_triggers=run_triggers).start()
    return _worker
