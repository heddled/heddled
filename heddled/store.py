"""SQLite event + state store.

Append-only `events` table is the spine; everything else (sessions, turns, the
job queue, poller cursors, approvals, deployments, evals) is derived state that
could in principle be rebuilt from it.

WAL mode: one writer appending, many readers streaming — SQLite's comfort zone.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import zlib
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from . import config
from .events import Event, new_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL    NOT NULL,
    type           TEXT    NOT NULL,
    session_id     TEXT    NOT NULL,
    turn_id        TEXT,
    agent          TEXT,
    agent_version  TEXT,
    payload        BLOB    NOT NULL,
    encoding       TEXT    NOT NULL DEFAULT 'zlib+json'
);
CREATE INDEX IF NOT EXISTS ix_events_session ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS ix_events_turn    ON events(turn_id, seq);
CREATE INDEX IF NOT EXISTS ix_events_type_ts ON events(type, ts);

CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    agent          TEXT NOT NULL,
    agent_version  TEXT,
    env            TEXT DEFAULT 'dev',
    channel        TEXT,
    trigger_origin TEXT,
    status         TEXT NOT NULL DEFAULT 'running',
    title          TEXT,
    parent_session_id TEXT,
    call_chain     TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_updated ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_sessions_status  ON sessions(status);

CREATE TABLE IF NOT EXISTS turns (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    ended_at    REAL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_turns_session ON turns(session_id, created_at);

-- Session working state: rolling message list + memory + paused-turn snapshot.
CREATE TABLE IF NOT EXISTS session_state (
    session_id TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'queued',
    run_at     REAL NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    ended_at   REAL,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_ready ON jobs(status, run_at);

CREATE TABLE IF NOT EXISTS cursors (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    turn_id      TEXT NOT NULL,
    agent        TEXT,
    tool         TEXT NOT NULL,
    args         TEXT NOT NULL,
    reason       TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    routed_to    TEXT,
    requested_at REAL NOT NULL,
    resolved_at  REAL,
    resolver     TEXT,
    note         TEXT,
    token        TEXT
);
CREATE INDEX IF NOT EXISTS ix_approvals_status ON approvals(status, requested_at DESC);

CREATE TABLE IF NOT EXISTS deployments (
    agent       TEXT NOT NULL,
    env         TEXT NOT NULL,
    version     TEXT NOT NULL,
    promoted_at REAL NOT NULL,
    promoted_by TEXT,
    eval_run_id TEXT,
    PRIMARY KEY (agent, env)
);

-- The bytes behind a version. A deployment names a version, and without the
-- definition kept somewhere that name refers to nothing the moment the file is
-- edited: "published version 4f2a" would be a note about bytes that no longer
-- exist. Content-addressed, so recording the same version twice is free.
CREATE TABLE IF NOT EXISTS agent_versions (
    agent        TEXT NOT NULL,
    version      TEXT NOT NULL,
    definition   TEXT NOT NULL,
    instructions TEXT NOT NULL,
    first_seen   REAL NOT NULL,
    PRIMARY KEY (agent, version)
);
CREATE INDEX IF NOT EXISTS ix_agent_versions ON agent_versions(agent, first_seen DESC);

CREATE TABLE IF NOT EXISTS jarvis_runs (
    id          TEXT PRIMARY KEY,
    goal        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    budget_eur  REAL NOT NULL,
    max_steps   INTEGER NOT NULL,
    steps       INTEGER NOT NULL DEFAULT 0,
    session_id  TEXT,
    note        TEXT,
    started_by  TEXT,
    created_at  REAL NOT NULL,
    ended_at    REAL
);

CREATE TABLE IF NOT EXISTS golden_traces (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    agent       TEXT NOT NULL,
    session_id  TEXT,
    spec        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id            TEXT PRIMARY KEY,
    agent         TEXT NOT NULL,
    agent_version TEXT,
    status        TEXT NOT NULL,
    passed        INTEGER DEFAULT 0,
    failed        INTEGER DEFAULT 0,
    result        TEXT,
    started_at    REAL NOT NULL,
    ended_at      REAL
);
CREATE INDEX IF NOT EXISTS ix_eval_runs_agent ON eval_runs(agent, started_at DESC);

-- People who can open the console. Heddled previously had one shared password and
-- no notion of who did anything; an organisation needs to know that.
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member',
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL,
    created_by    TEXT,
    last_login    REAL,
    must_change   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_users_username ON users(username);

-- Who did what. Separate from the event spine: the spine records what agents
-- did, this records what people did to Heddled itself.
CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    username  TEXT,
    action    TEXT NOT NULL,
    target    TEXT,
    detail    TEXT,
    ip        TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(ts DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Spend/token ledger backing budget policies.
CREATE TABLE IF NOT EXISTS ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    day        TEXT NOT NULL,
    agent      TEXT,
    session_id TEXT,
    tool       TEXT,
    kind       TEXT NOT NULL,
    amount     REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ledger_day ON ledger(day, agent, kind);
"""


# Decision 4: context payloads are zstd-compressed. zstd is stdlib from Python
# 3.14 (`compression.zstd`) and comes from the declared `zstandard` dependency
# before that, so it is normally always available — the zlib branch below is the
# fallback for a `--no-deps` install, and stays forever as the *reader* for rows
# written by older versions. The `encoding` column records which codec produced
# each row, so a store is readable by any build and never needs a migration.
try:  # Python 3.14+
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover - depends on the interpreter
    try:
        import zstandard as _zstd_pkg

        class _zstd:  # type: ignore[no-redef]
            @staticmethod
            def compress(data, level=3):
                return _zstd_pkg.ZstdCompressor(level=level).compress(data)

            @staticmethod
            def decompress(data):
                return _zstd_pkg.ZstdDecompressor().decompress(data)
    except ImportError:
        _zstd = None

ENCODING = "zstd+json" if _zstd is not None else "zlib+json"


def _encode(payload: dict) -> bytes:
    raw = json.dumps(payload, default=str).encode("utf-8")
    if _zstd is not None:
        return _zstd.compress(raw, 3)
    return zlib.compress(raw, 6)


def _decode(blob: bytes, encoding: str = "zlib+json") -> dict:
    blob = bytes(blob)
    if encoding == "json":
        return json.loads(blob.decode("utf-8"))
    if encoding == "zstd+json":
        if _zstd is None:
            raise RuntimeError(
                "this store holds zstd-compressed events but the running "
                "interpreter has no zstd support (Python 3.14+, or `pip install zstandard`)"
            )
        return json.loads(_zstd.decompress(blob).decode("utf-8"))
    return json.loads(zlib.decompress(blob).decode("utf-8"))


@dataclass
class Ephemeral:
    """Not an event. Broadcast to live listeners, never stored, never replayed."""

    session_id: str
    kind: str
    payload: dict


class Store:
    """Thread-safe-enough SQLite wrapper: one connection per thread."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._subscribers: list = []
        self._sub_lock = threading.Lock()
        self._init_schema()

    # ---------------------------------------------------------------- plumbing

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    def _init_schema(self) -> None:
        with self._write_lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._write_lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchone()

    # ------------------------------------------------------------------- spine

    def append(self, event: Event) -> Event:
        """Append to the spine and notify subscribers. The only write path for events."""
        with self._write_lock:
            cur = self.conn.execute(
                "INSERT INTO events (ts, type, session_id, turn_id, agent, agent_version,"
                " payload, encoding) VALUES (?,?,?,?,?,?,?,?)",
                (
                    event.ts,
                    event.type,
                    event.session_id,
                    event.turn_id,
                    event.agent,
                    event.agent_version,
                    _encode(event.payload or {}),
                    ENCODING,
                ),
            )
            self.conn.commit()
            event.seq = cur.lastrowid
            event.id = cur.lastrowid
        self._notify(event)
        return event

    def _row_to_event(self, r: sqlite3.Row) -> Event:
        return Event(
            type=r["type"],
            session_id=r["session_id"],
            turn_id=r["turn_id"],
            agent=r["agent"],
            agent_version=r["agent_version"],
            payload=_decode(r["payload"], r["encoding"]),
            ts=r["ts"],
            seq=r["seq"],
            id=r["seq"],
        )

    def events_for_session(self, session_id: str, after_seq: int = 0) -> list[Event]:
        rows = self.query(
            "SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq",
            (session_id, after_seq),
        )
        return [self._row_to_event(r) for r in rows]

    def events_for_turn(self, turn_id: str) -> list[Event]:
        rows = self.query("SELECT * FROM events WHERE turn_id=? ORDER BY seq", (turn_id,))
        return [self._row_to_event(r) for r in rows]

    def recent_events(self, limit: int = 100, type: Optional[str] = None) -> list[Event]:
        if type:
            rows = self.query(
                "SELECT * FROM events WHERE type=? ORDER BY seq DESC LIMIT ?", (type, limit)
            )
        else:
            rows = self.query("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,))
        return [self._row_to_event(r) for r in rows]

    def count_tool_calls(self, agent: str, tool: str, since: float) -> int:
        """How many times this agent called this tool since `since`.

        The tool name lives inside the compressed payload, so SQL narrows on the
        indexed (type, ts) columns and only the rows in the window are decoded —
        bounded by the rate-limit window rather than by an arbitrary event tail.
        """
        rows = self.query(
            "SELECT payload, encoding FROM events "
            "WHERE type='tool.called' AND agent=? AND ts>?",
            (agent, since),
        )
        return sum(
            1 for r in rows
            if (_decode(r["payload"], r["encoding"]) or {}).get("tool") == tool
        )

    def last_tool_run(self, tool: str) -> Optional[float]:
        """When this tool was last called, for the Tools screen. Scans newest
        first and stops at the first hit, so a busy store stays cheap."""
        for row in self.query(
            "SELECT ts, payload, encoding FROM events WHERE type='tool.called'"
            " ORDER BY seq DESC LIMIT 500"
        ):
            if (_decode(row["payload"], row["encoding"]) or {}).get("tool") == tool:
                return row["ts"]
        return None

    # --------------------------------------------------------------- pub / sub

    def subscribe(self, queue) -> None:
        with self._sub_lock:
            self._subscribers.append(queue)

    def unsubscribe(self, queue) -> None:
        with self._sub_lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    def broadcast(self, session_id: str, kind: str, payload: dict) -> None:
        """Send something to live listeners without writing it down.

        Token deltas need the fan-out that events already have, but they must
        not become events: a paragraph is a thousand of them, and the event
        store is the audit log, not a transport. So they go on the same
        subscriber queues wrapped in a marker the SSE layer recognises, and
        nothing is persisted. A listener that misses every delta still
        reconstructs the conversation from `model.responded` alone.
        """
        self._notify(Ephemeral(session_id=session_id, kind=kind, payload=payload))

    def _notify(self, event) -> None:
        with self._sub_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except Exception:
                pass

    # ---------------------------------------------------------------- sessions

    def create_session(
        self,
        agent: str,
        agent_version: str = None,
        channel: str = None,
        trigger_origin: dict = None,
        env: str = "dev",
        title: str = None,
        parent_session_id: str = None,
        call_chain: list = None,
        session_id: str = None,
    ) -> str:
        sid = session_id or new_id("s")
        now = time.time()
        self.execute(
            "INSERT INTO sessions (id, agent, agent_version, env, channel, trigger_origin, status,"
            " title, parent_session_id, call_chain, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,'running',?,?,?,?,?)",
            (
                sid,
                agent,
                agent_version,
                env,
                channel,
                json.dumps(trigger_origin or {}),
                title,
                parent_session_id,
                json.dumps(call_chain or []),
                now,
                now,
            ),
        )
        return sid

    def get_session(self, session_id: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM sessions WHERE id=?", (session_id,))

    def update_session(self, session_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.execute(
            f"UPDATE sessions SET {sets} WHERE id=?", (*fields.values(), session_id)
        )

    def list_sessions(
        self,
        agent: str = None,
        status: str = None,
        channel: str = None,
        origin: str = None,
        who: str = None,
        env: str = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sessions WHERE 1=1"
        params: list = []
        if agent:
            sql += " AND agent=?"
            params.append(agent)
        if status:
            sql += " AND status=?"
            params.append(status)
        if channel:
            sql += " AND channel=?"
            params.append(channel)
        if env:
            sql += " AND env=?"
            params.append(env)
        if origin:
            sql += " AND trigger_origin LIKE ?"
            params.append(f'%"kind": "{origin}"%')
        if who is not None:
            # Whose conversation this is, recorded on the origin when a chat
            # session starts. Scoping the chat surface to your own threads is a
            # filter here rather than a join: the origin already travels with
            # the session and is what the trace shows.
            sql += " AND trigger_origin LIKE ?"
            params.append(f'%"who": "{who}"%')
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, params)

    # ------------------------------------------------------------------- turns

    def create_turn(self, turn_id: str, session_id: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO turns (id, session_id, status, created_at) VALUES (?,?,?,?)",
            (turn_id, session_id, "running", time.time()),
        )

    def end_turn(self, turn_id: str, status: str, error: str = None) -> None:
        self.execute(
            "UPDATE turns SET status=?, ended_at=?, error=? WHERE id=?",
            (status, time.time(), error, turn_id),
        )

    def set_turn_status(self, turn_id: str, status: str) -> None:
        self.execute("UPDATE turns SET status=? WHERE id=?", (status, turn_id))

    def list_turns(self, session_id: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM turns WHERE session_id=? ORDER BY created_at", (session_id,)
        )

    # --------------------------------------------------------- session state

    def get_state(self, session_id: str) -> dict:
        r = self.one("SELECT state FROM session_state WHERE session_id=?", (session_id,))
        return json.loads(r["state"]) if r else {}

    def set_state(self, session_id: str, state: dict) -> None:
        self.execute(
            "INSERT INTO session_state (session_id, state, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
            (session_id, json.dumps(state, default=str), time.time()),
        )

    # --------------------------------------------------------------- job queue

    def enqueue(self, kind: str, payload: dict, delay_s: float = 0) -> int:
        now = time.time()
        cur = self.execute(
            "INSERT INTO jobs (kind, payload, run_at, created_at) VALUES (?,?,?,?)",
            (kind, json.dumps(payload, default=str), now + delay_s, now),
        )
        return cur.lastrowid

    def claim_job(self) -> Optional[sqlite3.Row]:
        """Atomically take the next ready job. Single-worker-safe, and safe for
        several workers thanks to the status guard in the UPDATE."""
        with self._write_lock:
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE status='queued' AND run_at<=? ORDER BY run_at, id LIMIT 1",
                (time.time(),),
            ).fetchone()
            if not row:
                return None
            cur = self.conn.execute(
                "UPDATE jobs SET status='running', started_at=?, attempts=attempts+1"
                " WHERE id=? AND status='queued'",
                (time.time(), row["id"]),
            )
            self.conn.commit()
            if cur.rowcount == 0:
                return None
            # Re-read so the caller sees the incremented attempt count: the
            # worker's backoff schedule is indexed by it.
            return self.conn.execute(
                "SELECT * FROM jobs WHERE id=?", (row["id"],)
            ).fetchone()

    def finish_job(self, job_id: int, status: str = "done", error: str = None) -> None:
        self.execute(
            "UPDATE jobs SET status=?, ended_at=?, error=? WHERE id=?",
            (status, time.time(), error, job_id),
        )

    def queue_depth(self) -> int:
        r = self.one("SELECT COUNT(*) c FROM jobs WHERE status='queued'")
        return r["c"] if r else 0

    # ----------------------------------------------------------------- cursors

    def get_cursor(self, key: str, default=None):
        r = self.one("SELECT value FROM cursors WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_cursor(self, key: str, value) -> None:
        self.execute(
            "INSERT INTO cursors (key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, default=str), time.time()),
        )

    # ------------------------------------------------------------------ rename

    #: Everything keyed by an agent's name. A rename is the same agent under a
    #: new name, so its history follows it — the alternative is a page that
    #: looks like the conversations were lost.
    AGENT_KEYED = ("events", "sessions", "approvals", "deployments",
                   "golden_traces", "eval_runs", "ledger", "agent_versions")

    def rename_agent(self, old: str, new: str) -> dict:
        """Carry an agent's history and its trigger positions to its new name."""
        moved = {}
        for table in self.AGENT_KEYED:
            cur = self.execute(f"UPDATE {table} SET agent=? WHERE agent=?", (new, old))
            if cur.rowcount:
                moved[table] = cur.rowcount
        # Trigger cursors carry the agent name in their key. Left behind, a
        # poller forgets where it was and re-reads everything it already saw.
        prefixes = (f"schedule:{old}:", f"poll:{old}:", f"trigger:error:{old}:")
        for row in self.query("SELECT key FROM cursors"):
            for prefix in prefixes:
                if row["key"].startswith(prefix):
                    self.execute("UPDATE cursors SET key=? WHERE key=?",
                                 (prefix.replace(f":{old}:", f":{new}:", 1)
                                  + row["key"][len(prefix):], row["key"]))
                    moved["cursors"] = moved.get("cursors", 0) + 1
                    break
        return moved

    # --------------------------------------------------------------- approvals

    def create_approval(self, **f) -> str:
        aid = f.get("id") or new_id("a")
        self.execute(
            "INSERT INTO approvals (id, session_id, turn_id, agent, tool, args, reason,"
            " routed_to, requested_at, token) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                aid,
                f["session_id"],
                f["turn_id"],
                f.get("agent"),
                f["tool"],
                json.dumps(f.get("args") or {}),
                f.get("reason"),
                f.get("routed_to"),
                time.time(),
                f.get("token"),
            ),
        )
        return aid

    def get_approval(self, approval_id: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM approvals WHERE id=?", (approval_id,))

    def resolve_approval(self, approval_id: str, decision: str, resolver: str, note: str = None) -> bool:
        cur = self.execute(
            "UPDATE approvals SET status=?, resolved_at=?, resolver=?, note=?"
            " WHERE id=? AND status='pending'",
            (decision, time.time(), resolver, note, approval_id),
        )
        return cur.rowcount > 0

    def pending_approvals(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM approvals WHERE status='pending' ORDER BY requested_at DESC"
        )

    # ------------------------------------------------------------------ ledger

    def record_spend(self, kind: str, amount: float, agent: str = None,
                     session_id: str = None, tool: str = None) -> None:
        self.execute(
            "INSERT INTO ledger (ts, day, agent, session_id, tool, kind, amount) VALUES (?,?,?,?,?,?,?)",
            (time.time(), time.strftime("%Y-%m-%d"), agent, session_id, tool, kind, amount),
        )

    def spend_by_day(self, kind: str = "eur", days: int = 30) -> list[sqlite3.Row]:
        """What was spent per day, oldest first. The ledger has carried this
        since the first turn and nothing has ever read it back — every budget
        on the platform has been set blind."""
        return self.query(
            "SELECT day, COALESCE(SUM(amount),0) total FROM ledger"
            " WHERE kind=? GROUP BY day ORDER BY day DESC LIMIT ?",
            (kind, days),
        )[::-1]

    def spend_by_agent(self, kind: str = "eur", since: float = 0) -> list[sqlite3.Row]:
        return self.query(
            "SELECT agent, COALESCE(SUM(amount),0) total, COUNT(*) entries"
            " FROM ledger WHERE kind=? AND ts>=? AND agent IS NOT NULL"
            " GROUP BY agent ORDER BY total DESC",
            (kind, since),
        )

    def spend_by_tool(self, kind: str = "eur", since: float = 0) -> list[sqlite3.Row]:
        return self.query(
            "SELECT tool, COALESCE(SUM(amount),0) total, COUNT(*) entries"
            " FROM ledger WHERE kind=? AND ts>=? AND tool IS NOT NULL"
            " GROUP BY tool ORDER BY total DESC",
            (kind, since),
        )

    def spend_total(self, kind: str = "eur", since: float = 0) -> float:
        return float(self.one(
            "SELECT COALESCE(SUM(amount),0) t FROM ledger WHERE kind=? AND ts>=?",
            (kind, since))["t"])

    def spend_today(self, kind: str, agent: str = None, session_id: str = None) -> float:
        sql = "SELECT COALESCE(SUM(amount),0) t FROM ledger WHERE day=? AND kind=?"
        params: list = [time.strftime("%Y-%m-%d"), kind]
        if agent:
            sql += " AND agent=?"
            params.append(agent)
        if session_id:
            sql += " AND session_id=?"
            params.append(session_id)
        return self.one(sql, params)["t"]

    # ---------------------------------------------------------------- settings

    def get_setting(self, key: str, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_setting(self, key: str, value) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def all_settings(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self.query("SELECT * FROM settings")}

    # ------------------------------------------------------------- deployments

    def deployments(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM deployments ORDER BY agent, env")

    def deployment(self, agent: str, env: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM deployments WHERE agent=? AND env=?", (agent, env))

    # ------------------------------------------------------- list-screen rollups
    #
    # One query for the whole page instead of one per row. A console with three
    # agents does not notice the difference; one with three hundred does — the
    # Agents screen was issuing four queries per agent and taking seconds.

    def sessions_since_by_agent(self, since: float) -> dict[str, int]:
        return {r["agent"]: r["c"] for r in self.query(
            "SELECT agent, COUNT(*) c FROM sessions WHERE created_at>? GROUP BY agent",
            (since,))}

    def latest_eval_by_agent(self) -> dict[str, sqlite3.Row]:
        """The newest run per agent, in one pass over the index."""
        out: dict[str, sqlite3.Row] = {}
        for row in self.query("SELECT * FROM eval_runs ORDER BY started_at DESC"):
            out.setdefault(row["agent"], row)
        return out

    def deployments_by_agent(self) -> dict[str, dict[str, sqlite3.Row]]:
        out: dict[str, dict[str, sqlite3.Row]] = {}
        for d in self.deployments():
            out.setdefault(d["agent"], {})[d["env"]] = d
        return out

    def last_tool_runs(self, limit: int = 2000) -> dict[str, float]:
        """When each tool last ran. The per-tool version decoded up to 500
        payloads *per tool*, so a console with 300 tools decoded 150,000."""
        seen: dict[str, float] = {}
        for row in self.query(
            "SELECT ts, payload, encoding FROM events WHERE type='tool.called'"
            " ORDER BY seq DESC LIMIT ?", (limit,)
        ):
            name = (_decode(row["payload"], row["encoding"]) or {}).get("tool")
            if name and name not in seen:
                seen[name] = row["ts"]
        return seen

    # ---------------------------------------------------------- agent versions

    def record_agent_version(self, agent) -> None:
        """Keep the bytes behind a version, so it can be published, compared and
        restored later. Cheap and idempotent: the version *is* their hash."""
        self.execute(
            "INSERT INTO agent_versions (agent, version, definition, instructions,"
            " first_seen) VALUES (?,?,?,?,?) ON CONFLICT(agent, version) DO NOTHING",
            (agent.name, agent.version, agent.raw_text(), agent.instructions,
             time.time()),
        )

    def agent_version(self, agent: str, version: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM agent_versions WHERE agent=? AND version=?",
                        (agent, version))

    def agent_versions(self, agent: str, limit: int = 25) -> list[sqlite3.Row]:
        return self.query(
            "SELECT agent, version, first_seen, length(definition) AS size"
            " FROM agent_versions WHERE agent=? ORDER BY first_seen DESC LIMIT ?",
            (agent, limit))

    def promote(self, agent: str, env: str, version: str, by: str = "console",
                eval_run_id: str = None) -> None:
        self.execute(
            "INSERT INTO deployments (agent, env, version, promoted_at, promoted_by, eval_run_id)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(agent, env) DO UPDATE SET"
            " version=excluded.version, promoted_at=excluded.promoted_at,"
            " promoted_by=excluded.promoted_by, eval_run_id=excluded.eval_run_id",
            (agent, env, version, time.time(), by, eval_run_id),
        )

    # ------------------------------------------------------------------- evals

    def add_golden(self, name: str, agent: str, session_id: str, spec: dict) -> str:
        gid = new_id("g")
        self.execute(
            "INSERT INTO golden_traces (id, name, agent, session_id, spec, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (gid, name, agent, session_id, json.dumps(spec, default=str), time.time()),
        )
        return gid

    def goldens(self, agent: str = None) -> list[sqlite3.Row]:
        if agent:
            return self.query(
                "SELECT * FROM golden_traces WHERE agent=? ORDER BY created_at DESC", (agent,)
            )
        return self.query("SELECT * FROM golden_traces ORDER BY created_at DESC")

    def get_golden(self, gid: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM golden_traces WHERE id=?", (gid,))

    def delete_golden(self, gid: str) -> None:
        self.execute("DELETE FROM golden_traces WHERE id=?", (gid,))

    def create_eval_run(self, agent: str, agent_version: str) -> str:
        rid = new_id("e")
        self.execute(
            "INSERT INTO eval_runs (id, agent, agent_version, status, started_at) VALUES (?,?,?,?,?)",
            (rid, agent, agent_version, "running", time.time()),
        )
        return rid

    def finish_eval_run(self, rid: str, status: str, passed: int, failed: int, result: dict) -> None:
        self.execute(
            "UPDATE eval_runs SET status=?, passed=?, failed=?, result=?, ended_at=? WHERE id=?",
            (status, passed, failed, json.dumps(result, default=str), time.time(), rid),
        )

    def eval_runs(self, agent: str = None, limit: int = 50) -> list[sqlite3.Row]:
        if agent:
            return self.query(
                "SELECT * FROM eval_runs WHERE agent=? ORDER BY started_at DESC LIMIT ?",
                (agent, limit),
            )
        return self.query("SELECT * FROM eval_runs ORDER BY started_at DESC LIMIT ?", (limit,))

    def get_eval_run(self, rid: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM eval_runs WHERE id=?", (rid,))

    # ------------------------------------------------------------------ health

    def health(self) -> dict:
        hour_ago = time.time() - 3600
        errs = self.one(
            "SELECT COUNT(*) c FROM events WHERE type='error.raised' AND ts>?", (hour_ago,)
        )["c"]
        return {
            "queue_depth": self.queue_depth(),
            "errors_last_hour": errs,
            "sessions_running": self.one(
                "SELECT COUNT(*) c FROM sessions WHERE status='running'"
            )["c"],
            "waiting_approval": self.one(
                "SELECT COUNT(*) c FROM sessions WHERE status='waiting-approval'"
            )["c"],
            "events_total": self.one("SELECT COUNT(*) c FROM events")["c"],
        }

    # --------------------------------------------------------------- retention

    def apply_retention(self) -> int:
        """Drop full context.built payloads older than the retention window,
        keeping a stub so the trace still shows the event happened."""
        cutoff = time.time() - config.KEEP_FULL_CONTEXT_DAYS * 86400
        rows = self.query(
            "SELECT seq, payload, encoding FROM events WHERE type='context.built' AND ts<?",
            (cutoff,),
        )
        n = 0
        for r in rows:
            p = _decode(r["payload"], r["encoding"])
            # Retention is idempotent: an already-pruned stub is left alone.
            # (Filtering on stored length instead would miss large contexts that
            # happen to compress well — which real prompts usually do.)
            if p.get("pruned"):
                continue
            stub = {
                "message_count": p.get("message_count"),
                "tool_count": p.get("tool_count"),
                "pruned": True,
                "pruned_reason": f"retention {config.KEEP_FULL_CONTEXT_DAYS}d",
            }
            # Re-label as well as re-encode: the row may have been written by an
            # interpreter with a different codec available.
            self.execute("UPDATE events SET payload=?, encoding=? WHERE seq=?",
                         (_encode(stub), ENCODING, r["seq"]))
            n += 1
        return n


_store: Optional[Store] = None
_store_lock = threading.Lock()


def get_store() -> Store:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = Store()
    return _store
