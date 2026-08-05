"""Test fixtures.

Every test runs against a throwaway project root: its own agents/, tools/ and
SQLite store. Nothing here touches the developer's real `data/heddled.db`.

The env vars are set before `heddled` is imported anywhere, because `heddled.config`
resolves its paths at import time.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="heddled-tests-"))
os.environ.setdefault("HEDDLED_ROOT", str(_TMP_ROOT))
os.environ["HEDDLED_AGENTS_DIR"] = str(_TMP_ROOT / "agents")
os.environ["HEDDLED_TOOLS_DIR"] = str(_TMP_ROOT / "tools")
os.environ["HEDDLED_DATA_DIR"] = str(_TMP_ROOT / "data")
os.environ["HEDDLED_VAR_DIR"] = str(_TMP_ROOT / "var")
os.environ["HEDDLED_DB"] = str(_TMP_ROOT / "data" / "heddled.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# --------------------------------------------------------------- example files

AGENT_YAML = """\
# The support agent — comments here are deliberate: every authoring path is
# expected to preserve them, so this fixture exercises that on every save.
name: support
description: Invoice and billing support agent.
model: mock/echo                     # swap for anthropic when a key is set
instructions: ./support.md

adapters:
  channels: [webchat, webhook]
  tools: [lookup_invoice, refund]

triggers:
  - schedule: "0 8 * * 1-5"
    message: "Summarize overnight invoices."
  - poll: mailbox
    every: 60s
    config: { source: folder, path: ./var/mailbox }
    on_new: "Handle this incoming invoice email."

policies:
  - tool: refund
    requires_approval: true          # a human signs off on money leaving
    approval_adapter: webhook
    budget: { max_eur_per_day: 500 }
  - tool: "*"
    redact: [iban, creditcard]       # applied at the trace-store boundary

memory:
  session: auto

expose:
  mcp: true
"""

AGENT_MD = "You are an invoice support agent. Use your tools.\n"

LOOKUP_YAML = """\
name: lookup_invoice
description: Look up an invoice by number; returns status and amount.
input:  { invoice_number: string }
output: { status: string, amount_eur: number }
handler: ./handler.py
"""

LOOKUP_PY = '''\
def handle(args, ctx):
    ctx.log(f"looking up {args['invoice_number']}")
    return {"invoice_number": args["invoice_number"], "status": "unpaid",
            "amount_eur": 249.0, "iban": "NL91ABNA0417164300"}
'''

REFUND_YAML = """\
name: refund
description: Issue a refund against an invoice. Requires human approval.
input:  { invoice_number: string, amount_eur: number }
output: { refund_id: string, status: string }
handler: ./handler.py
"""

REFUND_PY = '''\
def handle(args, ctx):
    return {"refund_id": "R-TEST", "status": "issued",
            "amount_eur": args["amount_eur"]}
'''

BOOM_YAML = """\
name: boom
description: A tool that always raises, to prove failures land on the spine.
input:  { anything: "string?" }
handler: ./handler.py
"""

BOOM_PY = '''\
def handle(args, ctx):
    raise RuntimeError("handler exploded")
'''


@pytest.fixture(autouse=True)
def _reset_otel():
    """The exporter is a process-wide singleton; no test may leak one into the
    next."""
    from heddled import otel

    otel._exporter = None
    yield
    if otel._exporter is not None:
        otel._exporter.stop()
        otel._exporter = None


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """A fresh project root with one agent and three tools on disk."""
    from heddled import config

    root = tmp_path
    agents = root / "agents"
    tools = root / "tools"
    for path, content in [
        (agents / "support.yaml", AGENT_YAML),
        (agents / "support.md", AGENT_MD),
        (tools / "lookup_invoice" / "tool.yaml", LOOKUP_YAML),
        (tools / "lookup_invoice" / "handler.py", LOOKUP_PY),
        (tools / "refund" / "tool.yaml", REFUND_YAML),
        (tools / "refund" / "handler.py", REFUND_PY),
        (tools / "boom" / "tool.yaml", BOOM_YAML),
        (tools / "boom" / "handler.py", BOOM_PY),
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / "data").mkdir(exist_ok=True)
    (root / "var" / "mailbox").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "ROOT", root)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.setattr(config, "TOOLS_DIR", tools)
    monkeypatch.setattr(config, "DATA_DIR", root / "data")
    monkeypatch.setattr(config, "VAR_DIR", root / "var")
    monkeypatch.setattr(config, "DB_PATH", root / "data" / "heddled.db")
    return root


@pytest.fixture()
def store(project, monkeypatch):
    """A fresh Store, installed as the process-wide singleton."""
    from heddled import store as store_mod

    s = store_mod.Store(project / "data" / "heddled.db")
    monkeypatch.setattr(store_mod, "_store", s)
    return s


@pytest.fixture()
def registry(project, monkeypatch):
    from heddled import registry as registry_mod

    r = registry_mod.Registry(project / "agents", project / "tools")
    monkeypatch.setattr(registry_mod, "_registry", r)
    return r


@pytest.fixture()
def agent(registry):
    return registry.get_agent("support")


@pytest.fixture()
def worker(store, registry, monkeypatch):
    """A real background worker draining the real queue, stopped on teardown."""
    from heddled import worker as worker_mod

    w = worker_mod.Worker(concurrency=2, run_triggers=False)
    monkeypatch.setattr(worker_mod, "_worker", w)
    w.start()
    yield w
    w.stop()


def _app(store):
    from heddled.web.app import create_app

    app = create_app(start_worker=False)
    app.config["TESTING"] = True
    return app


# Heddled refuses state-changing requests that don't come from its own origin, so
# the test client has to identify itself the way a browser does.
BROWSER = {"HTTP_ORIGIN": "http://localhost"}


@pytest.fixture()
def admin(store):
    """The console requires somebody to have claimed it, so every test that
    drives the UI needs an account behind it."""
    from heddled import users

    return users.create(store, "tester", "a-good-test-password", role="admin",
                        created_by="tests")


@pytest.fixture()
def client(store, registry, admin, monkeypatch):
    """Signed in as an administrator — what most tests mean by "the console".
    Use `anon_client` to exercise the door itself, or `client_as` for a role."""
    app = _app(store)
    with app.test_client() as c:
        c.environ_base = {**c.environ_base, **BROWSER}
        with c.session_transaction() as sess:
            sess["uid"] = admin["id"]
        yield c


@pytest.fixture()
def mcp_key(store):
    """MCP now needs a credential once accounts exist, so tests that drive it
    have to issue one — exactly as an operator would."""
    store.set_setting("mcp_callers", {"test-key": "test-caller"})
    return {"Authorization": "Bearer test-key"}


@pytest.fixture()
def anon_client(store, registry):
    """Nobody signed in."""
    app = _app(store)
    with app.test_client() as c:
        c.environ_base = {**c.environ_base, **BROWSER}
        yield c


@pytest.fixture()
def client_as(store, registry, admin):
    """Sign in as a particular role: `client_as("viewer")`."""
    from heddled import users

    app = _app(store)

    def _make(role, username=None):
        username = username or f"{role}_person"
        user = users.get(store, username) or users.create(
            store, username, "a-good-test-password", role=role, created_by="tests")
        c = app.test_client()
        c.environ_base = {**c.environ_base, **BROWSER}
        with c.session_transaction() as sess:
            sess["uid"] = user["id"]
        return c

    return _make


@pytest.fixture()
def blank_client(store, registry):
    """A console nobody has set up yet."""
    app = _app(store)
    with app.test_client() as c:
        c.environ_base = {**c.environ_base, **BROWSER}
        yield c
