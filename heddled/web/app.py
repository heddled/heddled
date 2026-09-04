"""Flask app: console (server-rendered), JSON API, SSE trace stream, MCP server.

Decision 8: server-rendered Jinja with vanilla-JS islands. No SPA, no build
step, view-source honesty. Every screen is deep-linkable.
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from flask import (
    Flask,
    g,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)

from .. import (
    auth,
    users,
    authoring,
    config,
    evals,
    gitio,
    jarvis,
    otel,
    story,
    tooltypes,
    triggers,
    yamlio,
)
from ..adapters import APPROVAL_ADAPTERS, CHANNELS, channel_names
from ..events import EVENT_CLASS, EVENT_TYPES
from ..providers import OPENAI_COMPATIBLE, known_providers
from ..registry import get_registry
from ..runtime import (
    AgentNotFound,
    inbound_env as runtime_env,
    inject_operator_message,
    platform_health,
    resolve_approval,
    submit_message,
)
from ..store import Ephemeral, get_store
from .. import workspace
from ..worker import ensure_worker

HERE = Path(__file__).parent

# Plain readings of the platform's own status and origin words. The stored
# values never change — only what a person reads.
STATUS_WORDS = {
    "running": "working on it",
    "waiting-approval": "waiting for approval",
    "ended": "finished",
    "error": "stopped with a problem",
}

ORIGIN_WORDS = {
    "webchat": "someone chatting",
    "cli": "the command line",
    "webhook": "another system",
    "mcp": "another system",
    "schedule": "a schedule",
    "poll": "something arriving",
    "agent": "another agent",
    "eval": "a test run",
    "slack": "Slack",
}

# Offered in the model picker, grouped by the service that serves them. Free
# text is still accepted — this is a convenience, not an allow-list. Any
# OpenAI-compatible service works by prefix even if it isn't named here.
MODELS_BY_PROVIDER = {
    "mock": ["echo"],
    "anthropic": ["claude-opus-4-1", "claude-sonnet-4-6", "claude-haiku-4-5"],
    "openai": ["gpt-4o", "gpt-4o-mini"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "groq": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    "mistral": ["mistral-large-latest", "mistral-small-latest"],
    "together": ["meta-llama/Llama-3.3-70B-Instruct-Turbo"],
    "openrouter": ["anthropic/claude-sonnet-4", "deepseek/deepseek-chat"],
    "ollama": ["llama3.2", "qwen2.5", "mistral"],
    "vllm": [],
}

KNOWN_MODELS = [f"{p}/{m}" for p, models in MODELS_BY_PROVIDER.items() for m in models]


def model_groups(store) -> list[dict]:
    """The picker, service by service, saying which ones can actually run.

    Someone who has only pasted a DeepSeek key should see that at a glance
    rather than choosing a model that will fail on the first turn.
    """
    groups = []
    for spec in known_providers():
        key = spec["key"]
        ready = spec["local"] or bool(
            store.get_setting(f"{key}_api_key") or os.environ.get(f"{key.upper()}_API_KEY"))
        groups.append({
            "provider": key,
            "label": spec["label"],
            "ready": ready,
            "models": [f"{key}/{m}" for m in MODELS_BY_PROVIDER.get(key, [])]
                      or [f"{key}/{spec['example']}"],
        })
    # Services you can use first; the rest still selectable, just labelled.
    groups.sort(key=lambda g: (not g["ready"], g["label"].lower()))
    return groups


def model_service(store, model: str) -> dict:
    """The service behind one agent's model, and whether it can actually run.

    An agent pointed at a service with no key looks perfectly healthy until
    somebody presses Try it, so the answer belongs on the agent's own page.
    """
    provider = (model or "").split("/")[0].lower()
    match = next((g for g in model_groups(store) if g["provider"] == provider), None)
    if match:
        return {**match, "known": True}
    return {"provider": provider, "label": provider, "ready": False, "known": False,
            "models": []}


# Settings Heddled itself knows about. Anything else a person stores is their own
# secret and appears in the Secrets list instead.
KNOWN_SETTINGS = [
    ("anthropic_api_key", "Anthropic API key"),
] + [
    entry
    for key, spec in OPENAI_COMPATIBLE.items()
    # Nothing running on your own machine issues API keys, so offering a field
    # for one is a question with no answer. Set `<service>_api_key` as a secret
    # if you have put such a service behind an authenticating proxy.
    for entry in (
        [(f"{key}_api_key", f"{spec['label']} API key")] if not spec.get("local") else []
    ) + [
        (f"{key}_base_url",
         f"{spec.get('short', spec['label'])} address (blank for the usual one)"),
    ]
] + [
    ("judge_model", "Model used when a test is graded by a model"),
    ("approval_webhook_url", "Where approval requests are sent"),
    ("webhook_outbound_url", "Where replies are sent by default"),
    ("slack_webhook_url", "Slack incoming webhook"),
    ("slack_bot_token", "Slack bot token"),
    ("slack_channel", "Slack channel"),
    ("public_url", "Public address of this Heddled (used in approval links)"),
    ("default_env", "Environment for work arriving from outside: dev, staging or prod"),
    ("imap_host", "Mail server to watch, e.g. imap.gmail.com"),
    ("imap_user", "Mailbox address to sign in as"),
    ("imap_password", "Mailbox password or app password"),
    ("mcp_callers", 'Keys for other systems, e.g. {"key-abc": "copilot-studio"}'),
    ("mcp_api_key", "A single shared key for other systems"),
    ("allow_internal_http", "Let tools reach private network addresses (risky)"),
    ("otel_endpoint", "OpenTelemetry collector address"),
    ("otel_headers", 'OpenTelemetry headers, e.g. {"authorization": "Bearer …"}'),
    ("otel_service_name", "OpenTelemetry service name (default: heddled)"),
]

# How the settings page is grouped. One long undifferentiated list made it hard
# to find anything; these are the questions people actually arrive with. Every
# known setting belongs to exactly one group — a setting in none of them would
# exist but be unreachable, which test_settings_groups_cover_every_setting
# guards against.
SETTING_GROUPS = [
    ("Models",
     "A key for each service you want to use. Only the ones you fill in are "
     "available to your agents.",
     ["anthropic_api_key"]
     + [f"{k}_api_key" for k in OPENAI_COMPATIBLE if not OPENAI_COMPATIBLE[k].get("local")]
     + ["judge_model"]),
    ("Where things go", "Approvals, replies, and how Heddled refers to itself.",
     ["approval_webhook_url", "webhook_outbound_url", "slack_webhook_url",
      "slack_bot_token", "slack_channel", "public_url", "default_env"]),
    ("Other systems", "Keys for programs that drive Heddled without an account.",
     ["mcp_callers", "mcp_api_key", "allow_internal_http"]),
    ("Watching a mailbox",
     "Needed only if an agent is set to act on incoming email. The trigger names "
     "the folder; the credentials stay here rather than in the agent's file.",
     ["imap_host", "imap_user", "imap_password"]),
    ("Monitoring", "Send every turn to your own observability stack.",
     ["otel_endpoint", "otel_headers", "otel_service_name"]),
    ("Custom addresses",
     "Only needed to point a service somewhere other than its usual home — a "
     "proxy, a self-hosted model, or a compatible service Heddled does not list.",
     [f"{k}_base_url" for k in OPENAI_COMPATIBLE]),
]



# --- login throttling -------------------------------------------------------
# Without this, a password is only as good as how fast somebody can guess. Kept
# in memory on purpose: it is a speed bump, not an audit trail, and it should
# reset when the process does.
_ATTEMPTS: dict = {}
_MAX_ATTEMPTS = 8
_WINDOW_S = 60


def _attempt_key(username, ip):
    return f"{(username or '').lower()}@{ip or '-'}"


def _note_attempt(username, ip) -> None:
    now = time.time()
    key = _attempt_key(username, ip)
    recent = [t for t in _ATTEMPTS.get(key, []) if now - t < _WINDOW_S]
    recent.append(now)
    _ATTEMPTS[key] = recent
    # Don't let the dict grow without bound on a machine under attack.
    if len(_ATTEMPTS) > 5000:
        for k in [k for k, v in _ATTEMPTS.items() if not v or now - v[-1] > _WINDOW_S]:
            _ATTEMPTS.pop(k, None)


def _login_blocked(username, ip) -> bool:
    now = time.time()
    recent = [t for t in _ATTEMPTS.get(_attempt_key(username, ip), [])
              if now - t < _WINDOW_S]
    return len(recent) >= _MAX_ATTEMPTS


def _clear_attempts(username, ip) -> None:
    _ATTEMPTS.pop(_attempt_key(username, ip), None)


def create_app(start_worker: bool = True, dev: bool = False) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(HERE / "templates"),
        static_folder=str(HERE / "static"),
    )
    app.config["DEV"] = dev
    app.config["JSON_SORT_KEYS"] = False
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # Set HEDDLED_HTTPS=1 behind TLS so the cookie is never sent in the clear.
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("HEDDLED_HTTPS", "") in ("1", "true")
    config.ensure_dirs()
    app.secret_key = auth.secret_key(get_store())
    auth.install(app)

    if start_worker and not config.WEB_ONLY:
        ensure_worker()

    register_console(app)
    register_api(app)
    register_mcp(app)

    @app.context_processor
    def inject_globals():
        store = get_store()
        return {
            "health": platform_health(),
            "pending_approvals": len(store.pending_approvals()),
            "auth_status": auth.status(store),
            "dev_mode": app.config["DEV"],
            "jarvis_enabled": jarvis.enabled(store),
            "event_class": EVENT_CLASS,
            "now": time.time(),
        }

    @app.template_filter("ts")
    def _ts(value):
        if not value:
            return "—"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))

    @app.template_filter("filesize")
    def _filesize(value):
        """Bytes below a kilobyte, because "0.0 KB" reads as an empty file when
        it is a perfectly good one with 40 characters in it."""
        try:
            n = float(value)
        except (TypeError, ValueError):
            return ""
        if n < 1024:
            return f"{int(n)} bytes"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n / (1024 * 1024):.1f} MB"

    @app.template_filter("clock")
    def _clock(value):
        if not value:
            return "—"
        return time.strftime("%H:%M:%S", time.localtime(float(value)))

    @app.template_filter("clock")
    def _clock(value):
        """Wall-clock time for a message that has already happened. `ago` is
        right for a list of threads; inside a conversation you want to know it
        was 14:32, not that it was "3d" ago."""
        if not value:
            return ""
        return datetime.fromtimestamp(float(value)).strftime("%H:%M")

    @app.template_filter("ago")
    def _ago(value):
        if not value:
            return "—"
        delta = max(0, time.time() - float(value))
        for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
            if delta >= size:
                return f"{int(delta // size)}{unit} ago"
        return f"{int(delta)}s ago"

    @app.template_filter("value_words")
    def _value_words(value, field=""):
        """Readable values for the approval page. `249.0` under a field called
        `amount_eur` is a sum of money and should look like one — that page is
        where someone decides whether to let it happen."""
        name = (field or "").lower()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "eur" in name or "amount" in name or "price" in name or "cost" in name:
                symbol = "€" if "eur" in name else ""
                return f"{symbol}{value:,.2f}"
            if float(value).is_integer():
                return f"{int(value):,}"
            return f"{value:,}"
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False, default=str)
        return value

    @app.template_filter("cron_words")
    def _cron_words(value):
        return story.cron_words(value)

    @app.template_filter("tool_words")
    def _tool_words(value):
        """`*` is the platform's wildcard; nobody outside the file needs it."""
        if value in (None, "", "*"):
            return "everything it can do"
        return str(value).replace("_", " ")

    @app.template_filter("in_words")
    def _in_words(value):
        """Arguments as a person would say them, not as JSON."""
        from ..story import readable_args

        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return value
        return readable_args(value) or "no details"

    @app.template_filter("pretty")
    def _pretty(value):
        try:
            return json.dumps(value, indent=2, ensure_ascii=False, default=str)
        except Exception:
            return str(value)

    return app


# ============================================================== console


def register_console(app: Flask) -> None:
    def _version_rows(store, agent) -> list[dict]:
        """This agent's definitions over time, marked with where each one is
        running. "Published to prod" should be something you can see and put
        back, not an eight-character hash with no history behind it."""
        published = {}
        for d in store.deployments():
            if d["agent"] == agent.name:
                published.setdefault(d["version"], []).append(d["env"])
        rows = []
        for row in store.agent_versions(agent.name, limit=20):
            rows.append({
                "version": row["version"],
                "short": row["version"][:8],
                "when": row["first_seen"],
                "current": row["version"] == agent.version,
                "envs": sorted(published.get(row["version"], []),
                               key=lambda e: config.ENVIRONMENTS.index(e)
                               if e in config.ENVIRONMENTS else 99),
            })
        return rows

    def _renamed_to(store, action: str, old: str) -> Optional[str]:
        """Where a name went, according to the audit trail — so a bookmark or a
        link in someone's notes survives a rename instead of hitting a 404."""
        for row in store.query(
                "SELECT target, detail FROM audit WHERE action=? ORDER BY ts DESC LIMIT 200",
                (action,)):
            try:
                if json.loads(row["detail"] or "{}").get("from") == old:
                    return row["target"]
            except (ValueError, TypeError):
                continue
        return None

    def _rename_notice() -> dict:
        """Confirmation on the page you land on, under the new name."""
        return {"renamed_from": request.args.get("renamed_from"),
                "repointed": [a for a in (request.args.get("repointed") or "").split(",") if a]}

    def _deletion_notice() -> dict:
        """Confirmation on the list you land on after deleting something —
        including which agents were changed to let the delete happen."""
        return {"deleted": request.args.get("deleted"),
                "unmounted": [a for a in (request.args.get("unmounted") or "").split(",") if a]}

    PAGE_SIZE = 25

    def _page(items: list, per_page: int = PAGE_SIZE) -> dict:
        """One page of a list, plus what a pager needs to draw itself.

        Every list screen was written for the handful of agents you have on day
        one. The same screens have to stay usable at three hundred, which means
        not rendering three hundred rows and not querying per row.
        """
        total = len(items)
        pages = max(1, -(-total // per_page))
        try:
            page = int(request.args.get("page", 1))
        except ValueError:
            page = 1
        page = max(1, min(page, pages))
        start = (page - 1) * per_page
        args = {k: v for k, v in request.args.items() if k != "page"}
        return {"items": items[start:start + per_page], "page": page, "pages": pages,
                "total": total, "first": start + 1, "last": min(start + per_page, total),
                "per_page": per_page, "base_args": args}

    def _matches(needle: str, *fields) -> bool:
        needle = (needle or "").strip().lower()
        if not needle:
            return True
        return all(word in " ".join(f or "" for f in fields).lower()
                   for word in needle.split())

    @app.route("/")
    def home():
        store = get_store()
        registry = get_registry()
        q = request.args.get("q", "")
        # Searching by a tool's name answers "who can do this?", which is the
        # question behind most searches once there are more than a few agents.
        agents = [a for a in registry.agents().values()
                  if _matches(q, a.name, a.description, a.model,
                              " ".join(r for r in a.tool_names if isinstance(r, str)))]
        paged = _page(sorted(agents, key=lambda a: a.name))

        # One query each for the whole page, rather than four per agent.
        deployments = store.deployments_by_agent()
        recent = store.sessions_since_by_agent(time.time() - 86400)
        latest_evals = store.latest_eval_by_agent()
        rows = [
            {
                "agent": a,
                "deployments": deployments.get(a.name, {}),
                "sessions_24h": recent.get(a.name, 0),
                "latest_eval": latest_evals.get(a.name),
                "tools": list(registry.agent_tools(a).keys()),
                "gated": len([p for p in a.policies
                              if p.get("requires_approval") or p.get("approval_callers")]),
            }
            for a in paged["items"]
        ]
        return render_template("agents.html", rows=rows, environments=config.ENVIRONMENTS,
                               paging=paged, q=q, total_agents=len(registry.agents()),
                               **_deletion_notice())

    def _workspace_listing(agent):
        """What is in this agent's folder, or None if it has no workspace.

        A misconfigured workspace — pointed at agents/, say — must not take the
        whole agent page down with it; the panel says what is wrong instead.
        """
        if not getattr(agent, "workspace", None):
            return None
        try:
            root = workspace.resolve_root(agent)
        except workspace.WorkspaceError as exc:
            return {"problem": str(exc)}
        return {"root": str(root), "files": workspace.listing(root)}

    def _render_agent(name, status_code=200, **overrides):
        """Render the agent page, optionally with the text the author just
        submitted. A rejected save must never throw away their edit."""
        registry = get_registry()
        store = get_store()
        agent = registry.get_agent(name)
        if not agent:
            # A link written down before a rename still lands somewhere useful.
            moved = _renamed_to(store, "agent.renamed", name)
            if moved and registry.get_agent(moved):
                return redirect(url_for("agent_detail", name=moved, renamed_from=name)), 302
            abort(404)
        raw = overrides.pop("raw", None) or agent.raw_text()
        # Looking at an agent is enough to keep its current definition: the
        # version history should not depend on remembering to publish.
        store.record_agent_version(agent)
        return render_template(
            "agent_detail.html",
            agent=agent,
            raw=raw,
            tools=registry.agent_tools(agent),
            all_tools=registry.tools(),
            channels=channel_names(agent),
            all_channels=list(CHANNELS.keys()),
            known_models=KNOWN_MODELS,
            model_groups=model_groups(store),
            model_service=model_service(store, agent.model),
            unrepresentable=yamlio.unrepresentable(raw),
            workspace_files=_workspace_listing(agent),
            sessions=store.list_sessions(agent=name, limit=15),
            latest_eval=evals.latest_run(name),
            trigger_rows=[t for t in triggers.trigger_status() if t["agent"] == name],
            deployments={d["env"]: d for d in store.deployments() if d["agent"] == name},
            environments=config.ENVIRONMENTS,
            versions=_version_rows(store, agent),
            settings=store.all_settings(),
            public_url=(store.get_setting("public_url")
                        or os.environ.get("HEDDLED_PUBLIC_URL")
                        or request.url_root.rstrip("/")),
            schedule_choices=authoring.SCHEDULE_CHOICES,
            mounted=authoring.mounted_breakdown(agent),
            delegates=authoring.agents_delegating_to(name),
            other_agents=[a for a in registry.agents() if a != name],
            gated=[p for p in agent.policies
                   if p.get("requires_approval") or p.get("approval_callers")],
            status_words=STATUS_WORDS,
            origin_words=ORIGIN_WORDS,
            **{
                "saved": request.args.get("saved"),
                "committed": request.args.get("committed"),
                "error": request.args.get("error"),
                "restored": request.args.get("restored"),
                **_rename_notice(),
                **overrides,
            },
        ), status_code

    @app.route("/agents/<name>")
    def agent_detail(name):
        return _render_agent(name)

    @app.route("/agents/<name>/test")
    def agent_test(name):
        agent = get_registry().get_agent(name)
        if not agent:
            abort(404)
        tools = get_registry().agent_tools(agent)
        return render_template("agent_test.html", agent=agent,
                               session_id=request.args.get("session"),
                               created=request.args.get("created"),
                               model_service=model_service(get_store(), agent.model),
                               openers=story.openers(agent, tools))

    @app.route("/agents/<name>/definition", methods=["POST"])
    def save_definition(name):
        registry = get_registry()
        committed = None
        try:
            if request.form.get("instructions") is not None:
                agent = registry.get_agent(name)
                # Instructions are the most-edited part of an agent, so this is
                # the version people most often want back.
                if agent and agent.instructions != request.form["instructions"]:
                    authoring.keep_outgoing_version(name)
                registry.write_instructions(agent, request.form["instructions"])
            if request.form.get("definition") is not None:
                written = authoring.save_agent(name, request.form["definition"])
                committed = written.committed
        except authoring.AuthoringError as exc:
            # Come back with their text still in the box — a typo must not cost
            # someone the edit they just made.
            return _render_agent(name, status_code=400, raw=request.form.get("definition"),
                                 error=str(exc))
        except Exception as exc:
            return render_template("error.html", message=str(exc)), 400
        return redirect(url_for("agent_detail", name=name, saved="1", committed=committed))

    @app.route("/agents/<name>/fields", methods=["POST"])
    def save_agent_fields(name):
        """The structured form's write path: only the keys it owns are touched,
        so comments and anything the form does not cover survive (§9)."""
        form = request.form
        updates: dict = {}
        for key in ("description", "model", "handler"):
            if key in form:
                updates[key] = form.get(key).strip() or None

        if "channels" in form or "tools" in form:
            adapters = {}
            if "channels" in form:
                adapters["channels"] = form.getlist("channels")
            if "tools" in form:
                adapters["tools"] = form.getlist("tools")
            existing = dict((get_registry().get_agent(name).raw.get("adapters") or {}))
            existing.update(adapters)
            updates["adapters"] = existing

        if "memory_session" in form:
            updates["memory"] = {"session": form["memory_session"]}
        if "workspace_present" in form:
            # A checkbox writes `true`, which means work/<agent>. Somebody who
            # wants it pointed at an existing folder writes a path on the raw
            # tab, and ticking the box must not flatten that back to `true`.
            wants = form.get("workspace") == "on"
            current = get_registry().get_agent(name).raw.get("workspace")
            if not wants:
                updates["workspace"] = None
            elif not current:
                updates["workspace"] = True

        if "expose_present" in form:
            # An unticked checkbox submits nothing at all, so keying off the
            # checkbox itself meant a box could be ticked but never unticked —
            # somebody closing an endpoint got a "saved" and an endpoint that
            # was still open. The hidden marker says the section was submitted;
            # the boxes then say on or off. Existing keys are carried over so a
            # hand-written `expose:` entry the form knows nothing about survives.
            expose = dict((get_registry().get_agent(name).raw.get("expose") or {}))
            expose["mcp"] = form.get("expose_mcp") == "on"
            expose["chat"] = form.get("expose_chat") == "on"
            updates["expose"] = expose

        try:
            written = authoring.update_agent_fields(name, updates)
        except authoring.AuthoringError as exc:
            return redirect(url_for("agent_detail", name=name, error=str(exc)))
        return redirect(url_for("agent_detail", name=name,
                                saved="1" if written.paths else None,
                                committed=written.committed))

    @app.route("/agents/new")
    def new_agent_screen():
        store = get_store()
        groups = model_groups(store)
        return render_template(
            "agent_new.html",
            all_tools=get_registry().tools(),
            known_models=KNOWN_MODELS,
            model_groups=groups,
            starter=authoring.STARTER_INSTRUCTIONS,
            # "a real model is configured" is now any service with a key, not
            # just the two Heddled happened to ship with.
            has_model_key=any(g["ready"] and g["provider"] != "mock" for g in groups),
        )

    @app.route("/agents", methods=["POST"])
    def create_agent():
        form = request.form
        name = form.get("name", "").strip()
        try:
            written = authoring.new_agent(
                name,
                model=form.get("model") or "mock/echo",
                description=form.get("description") or None,
                from_agent=form.get("from_agent") or None,
                instructions=form.get("instructions") or None,
                tools=form.getlist("tools") or None,
                approval_tools=form.getlist("approval_tools") or None,
            )
        except authoring.AuthoringError as exc:
            return render_template("error.html", message=str(exc), code=400), 400
        # Straight into a chat window: the first thing anyone wants after making
        # an agent is to see whether it works.
        if form.get("from_agent") or not form.get("instructions"):
            return redirect(url_for("agent_detail", name=name, saved="1",
                                    committed=written.committed))
        return redirect(url_for("agent_test", name=name, created="1"))

    @app.route("/agents/<name>/delete", methods=["POST"])
    def delete_agent(name):
        try:
            # The page has already named who delegates to this agent and asked
            # for a confirmation, so the delete carries out the unmounting too
            # rather than bouncing back with an instruction and no button.
            written = authoring.delete_agent(name, force=True)
        except authoring.AuthoringError as exc:
            return redirect(url_for("agent_detail", name=name, error=str(exc)))
        return redirect(url_for("home", deleted=name,
                                unmounted=",".join(written.unmounted_from) or None))

    @app.route("/api/agents/<name>/versions/<version>/diff")
    def api_version_diff(name, version):
        """What changed between a stored version and the agent as it is now."""
        store = get_store()
        snapshot = store.agent_version(name, version)
        agent = get_registry().get_agent(name)
        if not snapshot or not agent:
            return jsonify({"error": "no such version"}), 404
        return jsonify({
            "definition": yamlio.diff(snapshot["definition"], agent.raw_text(),
                                      f"{name}.yaml"),
            "instructions": yamlio.diff(snapshot["instructions"], agent.instructions,
                                        f"{name}.md"),
            "same": (snapshot["definition"] == agent.raw_text()
                     and snapshot["instructions"] == agent.instructions),
        })

    @app.route("/agents/<name>/versions/<version>/restore", methods=["POST"])
    def restore_agent_version(name, version):
        """Put an earlier definition back. It becomes the current version again
        — the same bytes, so it carries the same version hash it always had."""
        store = get_store()
        snapshot = store.agent_version(name, version)
        if not snapshot:
            return redirect(url_for("agent_detail", name=name,
                                    error="that version is no longer stored"))
        registry = get_registry()
        try:
            authoring.save_agent(name, snapshot["definition"])
            agent = registry.get_agent(name)
            if agent:
                registry.write_instructions(agent, snapshot["instructions"])
        except authoring.AuthoringError as exc:
            return redirect(url_for("agent_detail", name=name, error=str(exc)))
        me = getattr(g, "user", None)
        users.record(store, me["username"] if me else None, "agent.restored",
                     target=name, detail={"version": version[:8]}, ip=request.remote_addr)
        return redirect(url_for("agent_detail", name=name, restored=version[:8]))

    @app.route("/agents/<name>/rename", methods=["POST"])
    def rename_agent(name):
        new_name = (request.form.get("new_name") or "").strip()
        try:
            written = authoring.rename_agent(name, new_name)
        except authoring.AuthoringError as exc:
            return redirect(url_for("agent_detail", name=name, error=str(exc)))
        if not written.paths:
            return redirect(url_for("agent_detail", name=name))
        moved = get_store().rename_agent(name, new_name)
        me = getattr(g, "user", None)
        users.record(get_store(), me["username"] if me else None, "agent.renamed",
                     target=new_name, detail={"from": name, "rows": moved},
                     ip=request.remote_addr)
        return redirect(url_for("agent_detail", name=new_name, renamed_from=name,
                                repointed=",".join(written.repointed) or None))

    @app.route("/agents/<name>/policies", methods=["POST"])
    def save_policy(name):
        form = request.form
        try:
            if form.get("remove"):
                authoring.remove_policy(name, form["remove"])
            else:
                policy = {"tool": form.get("tool", "").strip()}
                if form.get("requires_approval") == "on":
                    policy["requires_approval"] = True
                if form.get("approval_adapter"):
                    policy["approval_adapter"] = form["approval_adapter"]
                if form.get("max_eur_per_day"):
                    amount = float(form["max_eur_per_day"])
                    policy["budget"] = {
                        "max_eur_per_day": int(amount) if amount == int(amount) else amount}
                if form.get("redact"):
                    policy["redact"] = [r.strip() for r in form["redact"].split(",") if r.strip()]
                authoring.add_policy(name, policy)
        except (authoring.AuthoringError, ValueError) as exc:
            return redirect(url_for("agent_detail", name=name, error=str(exc)))
        return redirect(url_for("agent_detail", name=name, saved="1"))

    @app.route("/agents/<name>/mounted", methods=["POST"])
    def save_mounted(name):
        """What this agent may use: tools, other agents, MCP servers."""
        form = request.form
        agent = get_registry().get_agent(name)
        if not agent:
            abort(404)
        current = authoring.mounted_breakdown(agent)

        servers = list(current["mcp"])
        if form.get("remove_mcp"):
            servers = [s for s in servers if s.get("name") != form["remove_mcp"]]
        elif form.get("mcp_url"):
            spec = {"url": form["mcp_url"].strip(),
                    "name": (form.get("mcp_name") or "").strip() or "mcp"}
            if form.get("mcp_token"):
                spec["token"] = form["mcp_token"].strip()
            try:
                discovered = authoring.check_mcp_server(spec)
            except authoring.AuthoringError as exc:
                return redirect(url_for("agent_detail", name=name, error=str(exc)))
            servers = [s for s in servers if s.get("name") != spec["name"]] + [spec]
            request.environ["heddled_discovered"] = len(discovered)

        try:
            authoring.set_mounted(
                name,
                tools=form.getlist("tools") if "tools" in form or form.get("submitted")
                else current["tools"],
                agents=form.getlist("agents") if "agents" in form or form.get("submitted")
                else current["agents"],
                mcp_servers=servers,
            )
        except authoring.AuthoringError as exc:
            return redirect(url_for("agent_detail", name=name, error=str(exc)))

        found = request.environ.get("heddled_discovered")
        return redirect(url_for(
            "agent_detail", name=name,
            saved=f"Connected — {found} tool(s) available." if found is not None else "1"))

    @app.route("/agents/<name>/triggers", methods=["POST"])
    def save_trigger(name):
        form = request.form
        try:
            if form.get("remove") is not None:
                authoring.remove_trigger(name, int(form["remove"]))
            elif form.get("kind") == "folder":
                trigger = {
                    "poll": "mailbox",
                    "every": form.get("every") or "60s",
                    "config": {"source": "folder",
                               "path": form.get("path") or "./var/mailbox"},
                    "on_new": form.get("message", "").strip(),
                }
                authoring.add_trigger(name, trigger)
            elif form.get("kind") == "email":
                # The password is a credential: it belongs in the store, not in
                # a file that gets committed. The trigger refers to it by name.
                store = get_store()
                if form.get("imap_password"):
                    store.set_setting("imap_password", form["imap_password"])
                for field in ("imap_host", "imap_user"):
                    if form.get(field):
                        store.set_setting(field, form[field])
                trigger = {
                    "poll": "mailbox",
                    "every": form.get("every") or "5m",
                    "config": {"source": "imap",
                               "folder": form.get("mailbox_folder") or "INBOX"},
                    "on_new": form.get("message", "").strip(),
                }
                authoring.add_trigger(name, trigger)
            else:
                trigger = {
                    "schedule": authoring.cron_from_choice(
                        form.get("when", "every_day"),
                        at=form.get("at", "08:00"),
                        custom=form.get("custom", ""),
                    ),
                    "message": form.get("message", "").strip(),
                }
                authoring.add_trigger(name, trigger)
        except (authoring.AuthoringError, ValueError) as exc:
            return redirect(url_for("agent_detail", name=name, error=str(exc)))
        return redirect(url_for("agent_detail", name=name, saved="1"))

    @app.route("/api/agents/<name>/preview", methods=["POST"])
    def preview_agent(name):
        """The diff that is about to be written, before it is written (§9)."""
        body = request.get_json(silent=True) or {}
        path = authoring.agent_path(name)
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        after = body.get("definition", "")
        ok, error = yamlio.is_valid(after)
        return jsonify({
            "valid": ok,
            "error": error,
            "diff": yamlio.diff(before, after, str(path)),
            "changed": yamlio.has_changes(before, after),
            "unrepresentable": yamlio.unrepresentable(after),
        })

    # ------------------------------------------------------------------ tools

    @app.route("/tools")
    def tools_screen():
        registry = get_registry()
        store = get_store()
        q = request.args.get("q", "")
        all_tools = registry.tools()
        tools = sorted((t for t in all_tools.values()
                        if _matches(q, t.name, t.description)),
                       key=lambda t: t.name)
        paged = _page(tools)

        # Built once for the page: asking per tool walked every agent per tool.
        mounts = authoring.mount_index()
        agents = registry.agents()
        last_runs = store.last_tool_runs()
        rows = []
        for tool in paged["items"]:
            mounted = mounts.get(tool.name, [])
            gated = any(
                (agents[a].policy_for_tool(tool.name) or {}).get("requires_approval")
                for a in mounted if a in agents
            )
            rows.append({
                "tool": tool,
                "agents": mounted,
                "gated": gated,
                "last_run": last_runs.get(tool.name),
            })
        return render_template("tools.html", rows=rows, paging=paged, q=q,
                               total_tools=len(all_tools), **_deletion_notice())

    @app.route("/tools/new")
    def new_tool_screen():
        chosen = tooltypes.CATALOG_BY_TYPE.get(request.args.get("type"))
        if request.args.get("type") == "python":
            chosen = {"type": "python", "label": "Write it in Python",
                      "blurb": "One function. Heddled calls it with the values above.",
                      "fields": []}
        return render_template("tool_new.html", catalog=tooltypes.CATALOG, chosen=chosen)

    @app.route("/tools", methods=["POST"])
    def create_tool():
        form = request.form
        try:
            written = authoring.new_tool(
                form.get("name", ""),
                description=form.get("description") or None,
                input_spec=_collect_inputs(form) or form.get("input") or None,
                output_spec=form.get("output") or None,
                from_tool=form.get("from_tool") or None,
                tool_type=form.get("type") or None,
                config=_collect_config(form),
                handler=form.get("handler") or None,
            )
        except authoring.AuthoringError as exc:
            return render_template("error.html", message=str(exc), code=400), 400
        return redirect(url_for("tool_detail", name=form["name"].strip(),
                                saved=", ".join(p.name for p in written.paths)))

    def _render_tool(name, status_code=200, manifest=None, handler=None, **overrides):
        """As with agents: a rejected save re-renders with the submitted text,
        never a redirect that would discard it."""
        tool = get_registry().get_tool(name)
        if not tool or not tool.dir:
            moved = _renamed_to(get_store(), "tool.renamed", name)
            if moved and get_registry().get_tool(moved):
                return redirect(url_for("tool_detail", name=moved, renamed_from=name)), 302
            abort(404)
        manifest_path, handler_path = tool.dir / "tool.yaml", tool.dir / "handler.py"
        return render_template(
            "tool_detail.html",
            tool=tool,
            manifest=manifest if manifest is not None
            else manifest_path.read_text(encoding="utf-8"),
            handler=handler if handler is not None
            else (handler_path.read_text(encoding="utf-8") if handler_path.exists() else ""),
            agents=authoring.agents_using_tool(name),
            sample_args=json.dumps(_sample_args(tool.input_schema), indent=2),
            **{
                "saved": request.args.get("saved"),
                "committed": request.args.get("committed"),
                "error": request.args.get("error"),
                **_rename_notice(),
                **overrides,
            },
        ), status_code

    @app.route("/tools/<name>")
    def tool_detail(name):
        return _render_tool(name)

    @app.route("/tools/<name>", methods=["POST"])
    def save_tool(name):
        try:
            written = authoring.save_tool(
                name,
                manifest=request.form.get("manifest"),
                handler=request.form.get("handler"),
            )
        except authoring.AuthoringError as exc:
            return _render_tool(name, status_code=400, error=str(exc),
                                manifest=request.form.get("manifest"),
                                handler=request.form.get("handler"))
        return redirect(url_for(
            "tool_detail", name=name,
            saved=", ".join(p.name for p in written.paths) or None,
            committed=written.committed,
        ))

    @app.route("/tools/<name>/rename", methods=["POST"])
    def rename_tool(name):
        new_name = (request.form.get("new_name") or "").strip()
        try:
            written = authoring.rename_tool(name, new_name)
        except authoring.AuthoringError as exc:
            return redirect(url_for("tool_detail", name=name, error=str(exc)))
        if not written.paths:
            return redirect(url_for("tool_detail", name=name))
        me = getattr(g, "user", None)
        users.record(get_store(), me["username"] if me else None, "tool.renamed",
                     target=new_name, detail={"from": name}, ip=request.remote_addr)
        return redirect(url_for("tool_detail", name=new_name, renamed_from=name,
                                repointed=",".join(written.repointed) or None))

    @app.route("/tools/<name>/delete", methods=["POST"])
    def delete_tool(name):
        try:
            written = authoring.delete_tool(name, force=True)
        except authoring.AuthoringError as exc:
            return redirect(url_for("tool_detail", name=name, error=str(exc)))
        return redirect(url_for("tools_screen", deleted=name,
                                unmounted=",".join(written.unmounted_from) or None))

    @app.route("/sessions")
    def sessions():
        store = get_store()
        # Read one page beyond what is shown, so the pager knows whether there
        # is a next page without counting a table that only grows.
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        window = store.list_sessions(
            agent=request.args.get("agent") or None,
            status=request.args.get("status") or None,
            channel=request.args.get("channel") or None,
            origin=request.args.get("origin") or None,
            env=request.args.get("env") or None,
            limit=page * PAGE_SIZE + 1,
        )
        rows = window[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
        args = {k: v for k, v in request.args.items() if k != "page"}
        paged = {"page": page, "has_next": len(window) > page * PAGE_SIZE,
                 "first": (page - 1) * PAGE_SIZE + 1,
                 "last": (page - 1) * PAGE_SIZE + len(rows), "base_args": args}
        return render_template(
            "sessions.html",
            sessions=rows,
            status_words=STATUS_WORDS,
            origin_words=ORIGIN_WORDS,
            agents=list(get_registry().agents().keys()),
            channels=list(CHANNELS.keys()) + ["schedule", "poll", "agent", "eval"],
            filters=request.args,
            statuses=["running", "waiting-approval", "ended", "error"],
            environments=config.ENVIRONMENTS,
            paging=paged,
        )

    @app.route("/sessions/<sid>")
    def session_detail(sid):
        store = get_store()
        session = store.get_session(sid)
        if not session:
            abort(404)
        events = store.events_for_session(sid)
        approvals = store.query(
            "SELECT * FROM approvals WHERE session_id=? ORDER BY requested_at", (sid,)
        )
        children = store.query(
            "SELECT * FROM sessions WHERE parent_session_id=? ORDER BY created_at", (sid,)
        )
        compare = request.args.get("compare")
        compare_events = store.events_for_session(compare) if compare else None
        steps = story.build(events)
        return render_template(
            "session_detail.html",
            session=session,
            events=events,
            steps=steps,
            story_headline=story.headline(session, steps),
            status_words=STATUS_WORDS,
            origin_words=ORIGIN_WORDS,
            approvals=approvals,
            children=children,
            turns=store.list_turns(sid),
            trigger_origin=json.loads(session["trigger_origin"] or "{}"),
            compare=compare,
            compare_events=compare_events,
            live=session["status"] in ("running", "waiting-approval"),
        )

    @app.route("/evals")
    def evals_screen():
        store = get_store()
        goldens = store.goldens()
        # Offering to test an agent that has nothing saved for it queues a run
        # that can only report zero — a button that looks broken rather than one
        # that says why.
        counts: dict[str, int] = {}
        for g in goldens:
            counts[g["agent"]] = counts.get(g["agent"], 0) + 1
        testable = [{"name": name, "tests": counts.get(name, 0)}
                    for name in sorted(get_registry().agents())]
        testable.sort(key=lambda a: (a["tests"] == 0, a["name"]))
        return render_template(
            "evals.html",
            goldens=goldens,
            runs=store.eval_runs(limit=40),
            agents=testable,
            any_testable=any(a["tests"] for a in testable),
        )

    @app.route("/evals/runs/<rid>")
    def eval_run(rid):
        store = get_store()
        run = store.get_eval_run(rid)
        if not run:
            abort(404)
        result = json.loads(run["result"] or "{}")
        return render_template("eval_run.html", run=run, result=result)

    @app.route("/deployments")
    def deployments():
        store = get_store()
        registry = get_registry()
        q = request.args.get("q", "")
        all_agents = registry.agents()
        found = sorted((a for a in all_agents.values() if _matches(q, a.name, a.description)),
                       key=lambda a: a.name)
        paged = _page(found)

        published = store.deployments_by_agent()
        latest_evals = store.latest_eval_by_agent()
        matrix = []
        for a in paged["items"]:
            row = {"agent": a, "envs": {}}
            for env in config.ENVIRONMENTS:
                d = published.get(a.name, {}).get(env)
                row["envs"][env] = dict(d) if d else None
            run = latest_evals.get(a.name)
            green = bool(run and run["agent_version"] == a.version
                         and run["status"] == "passed")
            row["green"] = green
            row["gate_reason"] = (
                f"eval run {run['id']}: {run['passed']} passed, {run['failed']} failed"
                if run and run["agent_version"] == a.version
                else "no eval run for this version yet")
            # A content hash tells nobody anything. When it last changed does.
            try:
                row["edited"] = a.path.stat().st_mtime
            except OSError:
                row["edited"] = None
            matrix.append(row)
        return render_template("deployments.html", matrix=matrix,
                               environments=config.ENVIRONMENTS,
                               paging=paged, q=q, total_agents=len(all_agents))

    @app.route("/settings", methods=["GET", "POST"])
    def settings_screen():
        store = get_store()
        if request.method == "POST":
            # Blank never deletes a credential, so removing one needs its own
            # explicit gesture — otherwise a key can be set but never unset.
            clearing = set(request.form.getlist("clear"))
            for key in request.form:
                if key.startswith("setting_"):
                    name = key[len("setting_"):]
                    value = request.form[key]
                    if name in clearing:
                        store.execute("DELETE FROM settings WHERE key=?", (name,))
                    elif value == "":
                        # Credential fields render blank so the page never hands
                        # a secret back. Blank therefore means "leave it alone" —
                        # treating it as "delete" would wipe every API key the
                        # moment somebody pressed Save.
                        if auth.is_credential(name):
                            continue
                        store.execute("DELETE FROM settings WHERE key=?", (name,))
                    else:
                        try:
                            store.set_setting(name, json.loads(value))
                        except json.JSONDecodeError:
                            store.set_setting(name, value)
            return redirect(url_for("settings_screen"))
        return render_template(
            "settings.html",
            settings=store.all_settings(),
            known=KNOWN_SETTINGS,
            groups=SETTING_GROUPS,
            labels=dict(KNOWN_SETTINGS),
            otel=otel.status(),
            git=gitio.status(),
            auth_status=auth.status(store),
            secretish=auth.is_credential,
            safe_settings=auth.redacted_settings(store, [k for k, _ in KNOWN_SETTINGS]),
            secrets={k: auth.mask(v)
                     for k, v in auth.user_secrets(store, [k for k, _ in KNOWN_SETTINGS]).items()},
            error=request.args.get("error"),
            saved=request.args.get("saved"),
            retention_days=config.KEEP_FULL_CONTEXT_DAYS,
            default_env=config.DEFAULT_ENV,
            jarvis_on=jarvis.enabled(store),
            jarvis_model=jarvis.model(store),
            jarvis_budget=jarvis.default_budget(store),
            jarvis_max_budget=jarvis.MAX_BUDGET_EUR,
            known_models=KNOWN_MODELS,
        )

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        """First run: claim the console. Reachable only while nobody has."""
        from flask import session

        store = get_store()
        if not users.needs_setup(store):
            # Somebody already claimed this Heddled. Silently bouncing to the
            # console made a filled-in setup form look like it had worked —
            # the person then failed to sign in with credentials that were
            # never created, with nothing on screen to explain why.
            return render_template(
                "login.html", next_url="/",
                error="This Heddled has already been set up, so a new administrator "
                      "account was not created. Sign in with an existing account, "
                      "or ask whoever set it up to add you."), 409
        legacy = bool(store.get_setting(auth.SETTING))

        if request.method == "POST":
            form = request.form
            try:
                if legacy and not auth.check_password(store, form.get("existing", "")):
                    raise users.UserError("That isn't the current shared password.")
                if form.get("password") != form.get("confirm"):
                    raise users.UserError("The two passwords differ.")
                user = users.create(
                    store, form.get("username", ""), form.get("password", ""),
                    role="admin", display_name=form.get("display_name"),
                    created_by="setup",
                )
            except users.UserError as exc:
                return render_template("setup.html", error=str(exc), legacy_password=legacy,
                                       username=form.get("username")), 400
            # The shared password is superseded; leaving it would be a second
            # way in that nobody is tracking.
            store.execute("DELETE FROM settings WHERE key=?", (auth.SETTING,))
            users.record(store, user["username"], "setup.completed",
                         ip=request.remote_addr)
            session.clear()
            session["uid"] = user["id"]
            session.permanent = True
            return redirect("/")

        return render_template("setup.html", error=None, legacy_password=legacy,
                               username=None)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        from flask import session

        store = get_store()
        if users.needs_setup(store):
            return redirect(url_for("setup"))
        next_url = request.values.get("next") or "/"
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = "/"          # never bounce somebody off-site

        if request.method == "POST":
            username = request.form.get("username", "")
            if _login_blocked(username, request.remote_addr):
                return render_template(
                    "login.html", next_url=next_url,
                    error="Too many attempts. Wait a minute and try again."), 429
            user = users.authenticate(store, username, request.form.get("password", ""))
            if user:
                _clear_attempts(username, request.remote_addr)
                session.clear()
                session["uid"] = user["id"]
                session.permanent = True
                users.record(store, user["username"], "signed.in",
                             ip=request.remote_addr)
                return redirect(next_url)
            _note_attempt(username, request.remote_addr)
            users.record(store, username or "?", "signin.failed",
                         ip=request.remote_addr)
            # Deliberately not saying which half was wrong.
            return render_template("login.html", next_url=next_url,
                                   error="That username and password don't match."), 401
        return render_template("login.html", next_url=next_url, error=None)

    @app.route("/logout", methods=["POST", "GET"])
    def logout():
        from flask import session

        session.clear()
        return redirect(url_for("login"))

    # ------------------------------------------------------------- people

    @app.route("/users")
    def users_screen():
        store = get_store()
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        action = request.args.get("action") or None
        # One row beyond the page, so "older" appears only when there is more.
        window = users.audit_log(store, limit=PAGE_SIZE + 1,
                                 offset=(page - 1) * PAGE_SIZE, action=action)
        entries = window[:PAGE_SIZE]
        return render_template(
            "users.html",
            people=users.listing(store),
            roles=users.ROLES,
            role_words=users.ROLE_WORDS,
            audit=entries,
            audit_actions=users.audit_actions(store),
            audit_filter=action,
            paging={"page": page, "has_next": len(window) > PAGE_SIZE,
                    "first": (page - 1) * PAGE_SIZE + 1,
                    "last": (page - 1) * PAGE_SIZE + len(entries),
                    "base_args": {k: v for k, v in request.args.items() if k != "page"}},
            me=getattr(g, "user", None),
            error=request.args.get("error"),
            saved=request.args.get("saved"),
        )

    @app.route("/users", methods=["POST"])
    def users_change():
        store = get_store()
        form = request.form
        me = getattr(g, "user", None)
        actor = me["username"] if me else None
        try:
            if form.get("action") == "create":
                if form.get("password") != form.get("confirm"):
                    raise users.UserError("The two passwords differ.")
                users.create(store, form.get("username", ""), form.get("password", ""),
                             role=form.get("role", "member"),
                             display_name=form.get("display_name"), created_by=actor,
                             must_change=True)
                message = f"Added {form.get('username')}."
            elif form.get("action") == "role":
                users.set_role(store, form["username"], form["role"], by=actor)
                message = f"{form['username']} is now a {form['role']}."
            elif form.get("action") == "suspend":
                users.set_active(store, form["username"], False, by=actor)
                message = f"{form['username']} can no longer sign in."
            elif form.get("action") == "restore":
                users.set_active(store, form["username"], True, by=actor)
                message = f"{form['username']} can sign in again."
            elif form.get("action") == "remove":
                users.delete(store, form["username"], by=actor)
                message = f"Removed {form['username']}."
            elif form.get("action") == "passwd":
                if form.get("password") != form.get("confirm"):
                    raise users.UserError("The two passwords differ.")
                users.set_password(store, form["username"], form.get("password", ""),
                                   by=actor, must_change=True)
                message = f"New password set for {form['username']}."
            else:
                raise users.UserError("Nothing to do.")
        except users.UserError as exc:
            return redirect(url_for("users_screen", error=str(exc)))
        return redirect(url_for("users_screen", saved=message))

    @app.route("/account", methods=["GET", "POST"])
    def account():
        """Anybody can change their own password without being an admin."""
        store = get_store()
        me = getattr(g, "user", None)
        if request.method == "POST":
            form = request.form
            try:
                if not users.authenticate(store, me["username"],
                                          form.get("current", "")):
                    raise users.UserError("Your current password isn't right.")
                if form.get("password") != form.get("confirm"):
                    raise users.UserError("The two new passwords differ.")
                users.set_password(store, me["username"], form.get("password", ""),
                                   by=me["username"])
            except users.UserError as exc:
                return render_template("account.html", me=me, error=str(exc)), 400
            return render_template("account.html", me=me, error=None,
                                   saved="Password changed.")
        return render_template("account.html", me=me, error=None)

    @app.route("/settings/secrets", methods=["POST"])
    def save_secret():
        """The tool forms tell people to write {{secret.name}} and store the
        value here; until this existed there was no way to create one."""
        store = get_store()
        form = request.form
        if form.get("remove"):
            store.execute("DELETE FROM settings WHERE key=?", (form["remove"],))
            return redirect(url_for("settings_screen", saved="Secret removed."))

        name = (form.get("name") or "").strip()
        value = form.get("value") or ""
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
            return redirect(url_for(
                "settings_screen",
                error="A secret's name must be letters, digits and underscores, "
                      "starting with a letter — it is what you type inside "
                      "{{secret.…}}."))
        if not value:
            return redirect(url_for("settings_screen", error="Give the secret a value."))
        if name in auth.INTERNAL_SETTINGS:
            return redirect(url_for("settings_screen",
                                    error=f"'{name}' is used by Heddled itself."))
        store.set_setting(name, value)
        return redirect(url_for("settings_screen", saved=f"Saved. Use it as {{{{secret.{name}}}}}"))

    @app.route("/settings/commit-on-save", methods=["POST"])
    def set_commit_on_save(self=None):
        get_store().set_setting(gitio.SETTING, request.form.get("enabled") == "on")
        return redirect(url_for("settings_screen"))

    @app.route("/settings/jarvis", methods=["POST"])
    def set_jarvis():
        """Turning it on adds a tab; turning it off takes the routes away too,
        so a bookmark stops working rather than quietly still working."""
        store = get_store()
        store.set_setting(jarvis.SETTING, request.form.get("enabled") == "on")
        chosen = (request.form.get("model") or "").strip()
        if chosen:
            store.set_setting(jarvis.MODEL_SETTING, chosen)
        try:
            budget = float(request.form.get("budget") or 0)
        except ValueError:
            budget = 0
        if 0 < budget <= jarvis.MAX_BUDGET_EUR:
            store.set_setting(jarvis.BUDGET_SETTING, budget)
        return redirect(url_for("settings_screen") + "#jarvis")

    @app.route("/approve/<aid>")
    def approve_page(aid):
        store = get_store()
        approval = store.get_approval(aid)
        if not approval:
            abort(404)
        decision = request.args.get("decision")
        token = request.args.get("token")
        # The token is the approver's credential. It gates *reading* too: this
        # page shows the tool and its arguments, which routinely carry customer
        # data — an invoice number, an amount, an email address.
        if approval["token"] and not hmac.compare_digest(str(token or ""),
                                                         str(approval["token"])):
            abort(404)
        message = None
        if decision:
            try:
                result = resolve_approval(
                    aid, decision, resolver=request.args.get("by", "link"),
                    note=request.args.get("note"), token=token,
                )
                message = (
                    f"Already resolved as {result['status']}."
                    if result.get("already_resolved")
                    else f"Recorded: {result['status']}. The turn has resumed."
                )
                approval = store.get_approval(aid)
            except PermissionError:
                abort(403)
        return render_template("approve.html", approval=approval,
                               args=json.loads(approval["args"]), message=message)

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", message="Not found", code=404), 404


# ============================================================== JSON API


def register_api(app: Flask) -> None:
    @app.route("/api/health")
    def api_health():
        return jsonify({**platform_health(), "otel": otel.status()})

    @app.route("/api/agents")
    def api_agents():
        registry = get_registry()
        return jsonify(
            [
                {
                    "name": a.name,
                    "version": a.version,
                    "model": a.model,
                    "description": a.description,
                    "channels": channel_names(a),
                    "tools": list(registry.agent_tools(a).keys()),
                    "triggers": [t.raw for t in a.triggers],
                    "policies": a.policies,
                    "expose": a.expose,
                }
                for a in registry.agents().values()
            ]
        )

    @app.route("/api/agents/<name>/messages", methods=["POST"])
    def api_message(name):
        body = request.get_json(silent=True) or {}
        text = body.get("text") or request.form.get("text") or ""
        if not text.strip():
            return jsonify({"error": "text is required"}), 400
        try:
            result = submit_message(
                name,
                text,
                session_id=body.get("session_id"),
                channel=body.get("channel", "webchat"),
                env=runtime_env(body.get("env")),
                sender=body.get("sender"),
                sync=bool(body.get("sync")),
                timeout_s=float(body.get("timeout_s", 120)),
            )
        except AgentNotFound as exc:
            return jsonify({"error": str(exc)}), 404
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify(result)

    @app.route("/api/agents/<name>/webhook", methods=["POST"])
    def api_webhook(name):
        """Inbound push trigger: an external system decides when to fire."""
        body = request.get_json(silent=True) or {}
        text = body.get("text") or body.get("message") or json.dumps(body)
        try:
            result = submit_message(
                name,
                text,
                session_id=body.get("session_id"),
                channel="webhook",
                origin={"kind": "webhook", "reason": "inbound POST",
                        "remote_addr": request.remote_addr},
                env=runtime_env(body.get("env")),
                sender=body.get("sender", "webhook"),
                sync=bool(body.get("sync")),
            )
        except AgentNotFound as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify(result), 202

    @app.route("/api/sessions")
    def api_sessions():
        store = get_store()
        rows = store.list_sessions(
            agent=request.args.get("agent"),
            status=request.args.get("status"),
            channel=request.args.get("channel"),
            limit=int(request.args.get("limit", 100)),
        )
        return jsonify([dict(r) for r in rows])

    @app.route("/api/sessions/<sid>")
    def api_session(sid):
        store = get_store()
        session = store.get_session(sid)
        if not session:
            return jsonify({"error": "not found"}), 404
        return jsonify(
            {
                "session": dict(session),
                "state_keys": list(store.get_state(sid).keys()),
                "turns": [dict(t) for t in store.list_turns(sid)],
                "events": [e.to_dict() for e in store.events_for_session(sid)],
            }
        )

    @app.route("/api/sessions/<sid>/events")
    def api_events(sid):
        store = get_store()
        after = int(request.args.get("after", 0))
        return jsonify([e.to_dict() for e in store.events_for_session(sid, after_seq=after)])

    @app.route("/api/sessions/<sid>/inject", methods=["POST"])
    def api_inject(sid):
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(
                inject_operator_message(
                    sid, body.get("text", ""), operator=body.get("operator", "operator"),
                    resume=bool(body.get("resume", True)),
                )
            )
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.route("/api/approvals")
    def api_approvals():
        return jsonify([dict(a) for a in get_store().pending_approvals()])

    @app.route("/api/approvals/<aid>", methods=["POST"])
    def api_resolve_approval(aid):
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(
                resolve_approval(
                    aid,
                    body.get("decision", ""),
                    resolver=body.get("resolver", "api"),
                    note=body.get("note"),
                    token=body.get("token"),
                )
            )
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/tools")
    def api_tools():
        return jsonify(
            [
                {
                    "name": t.name,
                    "description": t.description,
                    "input": t.input_schema,
                    "output": t.output_schema,
                    "source": t.source,
                    "dir": str(t.dir) if t.dir else None,
                }
                for t in get_registry().tools().values()
            ]
        )

    @app.route("/api/tools/<name>/test", methods=["POST"])
    def api_tool_test(name):
        """Tools are testable in isolation — the console and `heddled tool test`
        share this path."""
        from ..tooltest import run_tool_standalone

        body = request.get_json(silent=True) or {}
        try:
            return jsonify(run_tool_standalone(name, body.get("args") or {}))
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.route("/api/goldens", methods=["POST"])
    def api_promote_golden():
        body = request.get_json(silent=True) or {}
        try:
            gid = evals.promote_session(body["session_id"], body.get("name"))
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"golden_id": gid})

    @app.route("/api/goldens/<gid>", methods=["DELETE"])
    def api_delete_golden(gid):
        get_store().delete_golden(gid)
        return jsonify({"deleted": gid})

    @app.route("/api/agents/<name>/evals/run", methods=["POST"])
    def api_run_evals(name):
        body = request.get_json(silent=True) or {}
        try:
            rid = evals.queue_eval_run(name, body.get("golden_ids"))
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"run_id": rid}), 202

    @app.route("/api/evals/runs")
    def api_eval_runs():
        """Enough for a page to notice that a run it is watching has finished."""
        limit = min(int(request.args.get("limit", 20)), 100)
        return jsonify({"runs": [
            {"id": r["id"], "agent": r["agent"], "status": r["status"],
             "passed": r["passed"], "failed": r["failed"], "started_at": r["started_at"]}
            for r in get_store().eval_runs(request.args.get("agent") or None, limit=limit)
        ]})

    @app.route("/api/deployments/promote", methods=["POST"])
    def api_promote():
        body = request.get_json(silent=True) or {}
        agent = get_registry().get_agent(body.get("agent", ""))
        if not agent:
            return jsonify({"error": "unknown agent"}), 404
        env = body.get("env")
        if env not in config.ENVIRONMENTS:
            return jsonify({"error": f"env must be one of {config.ENVIRONMENTS}"}), 400
        store = get_store()
        version = body.get("version") or agent.version
        # Keep the bytes before pinning them: publishing a version whose
        # definition is not stored anywhere is a label, not a deployment.
        if version == agent.version:
            store.record_agent_version(agent)
        elif not store.agent_version(agent.name, version):
            return jsonify({"error": f"no stored version {version[:8]} for {agent.name}"}), 404
        green, why = evals.is_green(agent.name, version)
        if env == "prod" and not green and not body.get("force"):
            return jsonify({"error": "promotion to prod is gated on a green eval run",
                            "detail": why}), 412
        me = getattr(g, "user", None)
        by = body.get("by") or (me["username"] if me else "api")
        store.promote(agent.name, env, version, by=by)
        return jsonify({"agent": agent.name, "env": env, "version": version, "gate": why})

    @app.route("/api/triggers")
    def api_triggers():
        return jsonify(triggers.trigger_status())

    # ------------------------------------------------------------------ SSE

    @app.route("/sessions/<sid>/stream")
    def session_stream(sid):
        """Live trace: server→client only, which is exactly SSE's shape."""
        store = get_store()
        after = int(request.args.get("after", 0))
        # A stream is opened once and can run for hours. The sign-in check
        # happened before it started, so without re-checking, suspending
        # somebody would leave them watching live conversations indefinitely.
        watcher_id = (getattr(g, "user", None) or {}).get("id")

        def generate():
            q: queue.Queue = queue.Queue(maxsize=1000)
            store.subscribe(q)
            try:
                for ev in store.events_for_session(sid, after_seq=after):
                    yield _sse(ev)
                last_ping = time.time()
                last_check = time.time()
                while True:
                    try:
                        ev = q.get(timeout=5)
                        if ev.session_id == sid:
                            yield _sse(ev)
                    except queue.Empty:
                        pass
                    now = time.time()
                    if watcher_id and now - last_check > 30:
                        last_check = now
                        if not auth.still_allowed(store, watcher_id):
                            return
                    if now - last_ping > 15:
                        last_ping = now
                        yield ": ping\n\n"
            finally:
                store.unsubscribe(q)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"},
        )

    @app.route("/api/stream")
    def global_stream():
        """Everything on the spine — what the home page's health strip and the
        sessions list listen to."""
        store = get_store()
        watcher_id = (getattr(g, "user", None) or {}).get("id")

        def generate():
            q: queue.Queue = queue.Queue(maxsize=1000)
            store.subscribe(q)
            try:
                last_check = time.time()
                while True:
                    try:
                        ev = q.get(timeout=5)
                        yield _sse(ev)
                    except queue.Empty:
                        yield ": ping\n\n"
                    if watcher_id and time.time() - last_check > 30:
                        last_check = time.time()
                        if not auth.still_allowed(store, watcher_id):
                            return
            finally:
                store.unsubscribe(q)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # =========================================================== chat surface
    #
    # A place to talk to an agent without operating one. Open to anybody who can
    # already sign in — there is no separate account type — but it renders none
    # of the console, and an agent appears here only if its file opts in with
    # `expose: { chat: true }`.

    def _chat_agents():
        """Agents that have opted in, in name order."""
        return [a for _, a in sorted(get_registry().agents().items())
                if (a.expose or {}).get("chat")]

    def _chat_agent_or_404(name):
        """404 rather than 403 for an agent that has not opted in: whether a
        given agent exists is not something this surface should confirm."""
        agent = get_registry().get_agent(name)
        if not agent or not (agent.expose or {}).get("chat"):
            abort(404)
        return agent

    @app.route("/chat")
    def chat_index():
        agents = _chat_agents()
        # One agent is the common case; a menu of one is a click for nothing.
        if len(agents) == 1:
            return redirect(url_for("chat_agent", name=agents[0].name))
        return render_template("chat_index.html", agents=agents)

    @app.route("/chat/<name>")
    def chat_agent(name):
        agent = _chat_agent_or_404(name)
        who = (getattr(g, "user", None) or {}).get("username")
        sid = request.args.get("session")
        history, last_seq = [], 0
        if sid:
            session = get_store().get_session(sid)
            # Somebody else's conversation is not yours to open, whatever your
            # role is on the console. The chat surface shows your threads only.
            if not session or not _mine(session, who, agent.name):
                abort(404)
            history, last_seq = _chat_history(sid)

        # The engine writes a title when a turn ends, so anything still running
        # — or that failed before it finished — has none. "Untitled" tells the
        # reader nothing; the question they asked identifies it perfectly well.
        # Resolved for the few that need it rather than for all thirty.
        threads, unresolved = [], 0
        for s in get_store().list_sessions(agent=agent.name, channel="chat",
                                           who=who, limit=30):
            title = s["title"]
            if not title and unresolved < 5:
                unresolved += 1
                earlier, _ = _chat_history(s["id"])
                first = next((m["text"] for m in earlier
                              if m["role"] == "you"), "")
                title = (first[:60] + "…") if len(first) > 60 else first
            threads.append({"id": s["id"], "title": title or "New conversation",
                            "updated_at": s["updated_at"]})
        # Somewhere to go if this is not the assistant they wanted. Only ones
        # that have opted in, and never the one already open.
        others = [a for a in _chat_agents() if a.name != agent.name]
        return render_template(
            "chat.html", agent=agent, session_id=sid, history=history,
            last_seq=last_seq,
            threads=threads, others=others, who=who,
            # Suggested first messages, from the agent's own tools — the same
            # helper the Test tab uses, because an empty box tells somebody who
            # did not build this agent nothing about what it can do.
            openers=story.openers(agent, get_registry().agent_tools(agent)),
        )

    def _chat_history(sid: str) -> tuple[list[dict], int]:
        """The conversation as the person had it, and the sequence number it
        ends at.

        Only the two message events: what the agent looked up and what it
        decided belong to whoever operates it, and this page is not that. The
        sequence number is what stops the live stream replaying what is already
        on the page.
        """
        out, last = [], 0
        for ev in get_store().events_for_session(sid):
            last = max(last, ev.seq or 0)
            if ev.type == "message.received":
                out.append({"role": "you", "text": ev.payload.get("text") or "",
                            "at": ev.ts})
            elif ev.type == "message.sent":
                out.append({"role": "agent", "text": ev.payload.get("text") or "",
                            "at": ev.ts})
        return out, last

    def _mine(session, who: str, agent_name: str = None) -> bool:
        if session["channel"] != "chat":
            return False
        if agent_name and session["agent"] != agent_name:
            return False
        try:
            origin = json.loads(session["trigger_origin"] or "{}")
        except ValueError:
            origin = {}
        return origin.get("who") == who

    @app.route("/chat/<name>/messages", methods=["POST"])
    def chat_send(name):
        agent = _chat_agent_or_404(name)
        who = (getattr(g, "user", None) or {}).get("username")
        body = request.get_json(silent=True) or {}
        text = (body.get("text") or "").strip()
        if not text:
            return {"error": "say something first"}, 400

        sid = body.get("session_id") or None
        if sid:
            session = get_store().get_session(sid)
            if not session or not _mine(session, who, agent.name):
                abort(404)

        # Not `sync`: the whole point of this surface is that the reply arrives
        # as it is written, over the stream opened below.
        result = submit_message(
            agent.name, text, session_id=sid, channel="chat",
            origin={"kind": "chat", "who": who}, sender=who,
            env=runtime_env(None),
        )
        return jsonify({"session_id": result["session_id"],
                        "thread_id": result["session_id"],
                        "turn_id": result.get("turn_id")})

    @app.route("/spending")
    def spending():
        """Where the money and the tokens went.

        The ledger has recorded every model call and every action with a
        declared cost since the first turn, and nothing has ever read it back:
        you could cap an agent at 500 euros a day with no way of knowing
        whether it spends five or five hundred. Budgets were being set blind.
        """
        store = get_store()
        window = int(request.args.get("days", 30))
        since = time.time() - window * 86400
        by_day = [{"day": r["day"], "total": r["total"]}
                  for r in store.spend_by_day("eur", days=window)]
        peak = max([d["total"] for d in by_day] or [0])

        # The caps that were set, so the numbers can be read against something.
        caps = []
        for agent in get_registry().agents().values():
            for policy in agent.policies or []:
                budget = policy.get("budget") or {}
                if budget.get("max_eur_per_day"):
                    caps.append({"agent": agent.name,
                                 "tool": policy.get("tool") or "*",
                                 "cap": float(budget["max_eur_per_day"]),
                                 "today": store.spend_today("eur", agent=agent.name)})
        return render_template(
            "spending.html", window=window, by_day=by_day, peak=peak,
            total=store.spend_total("eur", since),
            tokens=store.spend_total("tokens", since),
            by_agent=store.spend_by_agent("eur", since),
            by_tool=store.spend_by_tool("eur", since),
            today=store.spend_today("eur"), caps=caps,
        )

    # ============================================================ workspace
    #
    # The operator's view of the folder an agent works in. Reading is console
    # access, which viewers already have; putting files in and taking them out
    # is a write, so the read-only rule covers it with no exemption — unlike
    # chatting and approving, uploading really is changing something.

    def _agent_workspace(name):
        """The agent and its root, or 404 if it has not been given one."""
        agent = get_registry().get_agent(name)
        if not agent:
            abort(404)
        try:
            root = workspace.resolve_root(agent)
        except workspace.WorkspaceError:
            abort(404)
        if root is None:
            abort(404)
        return agent, root

    @app.route("/agents/<name>/files/view")
    def workspace_view(name):
        agent, root = _agent_workspace(name)
        given = request.args.get("path", "")
        try:
            content = workspace.read(root, given)
        except workspace.WorkspaceError as exc:
            return render_template("error.html", message=str(exc), code=400), 400
        return render_template("workspace_view.html", agent=agent, path=given,
                               content=content)

    @app.route("/agents/<name>/files/download")
    def workspace_download(name):
        """Always an attachment, never rendered.

        This serves whatever somebody put in the folder, from the console's own
        origin — the origin holding the administrator's session. Served inline,
        an uploaded .html would run as a page on that origin. So: a fixed
        content type that is not html, `attachment`, and nosniff, which between
        them leave the browser nothing to interpret.
        """
        agent, root = _agent_workspace(name)
        given = request.args.get("path", "")
        try:
            path = workspace.safe_path(root, given, must_exist=True)
        except workspace.WorkspaceError as exc:
            return render_template("error.html", message=str(exc), code=400), 400
        return Response(
            path.read_bytes(),
            mimetype="application/octet-stream",
            headers={
                "Content-Disposition":
                    f'attachment; filename="{Path(given).name}"',
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
            },
        )

    @app.route("/agents/<name>/files", methods=["POST"])
    def workspace_change(name):
        agent, root = _agent_workspace(name)
        action = request.form.get("action")
        try:
            if action == "delete":
                removed = workspace.delete(root, request.form.get("path", ""))
                note = f"removed {removed}"
            else:
                upload = request.files.get("file")
                if not upload or not upload.filename:
                    raise workspace.WorkspaceError("no file chosen")
                result = workspace.store_upload(root, upload.filename, upload.read())
                note = (f"replaced {result['path']}" if result["replaced"]
                        else f"added {result['path']}")
        except workspace.WorkspaceError as exc:
            return redirect(url_for("agent_detail", name=name, error=str(exc)))
        return redirect(url_for("agent_detail", name=name, saved=note))

    # ======================================================= approvals inbox
    #
    # A gated action pauses the turn and is routed out to wherever the approver
    # already works — that is the design and it stands. But somebody whose job
    # is signing things off had nowhere to go: a one-shot signed link handles
    # one decision, and the alternative was a console account and the whole
    # estate. This is the queue, and nothing else.

    def _approval_view(row) -> dict:
        """One waiting decision, in the words an approver needs."""
        try:
            args = json.loads(row["args"] or "{}")
        except ValueError:
            args = {}
        # A double-encoded value comes back as a string, and a screen that
        # explodes on one is worse than one that shows the row plainly.
        if not isinstance(args, dict):
            args = {"arguments": args}
        return {
            "id": row["id"],
            "agent": row["agent"],
            "tool": row["tool"],
            "what": str(row["tool"] or "").replace("_", " "),
            "args": args,
            "in_words": story.readable_args(args),
            "reason": row["reason"],
            "requested_at": row["requested_at"],
            "session_id": row["session_id"],
        }

    @app.route("/approvals")
    def approvals_inbox():
        waiting = [_approval_view(r) for r in get_store().pending_approvals()]
        return render_template("approvals.html", waiting=waiting,
                               done=request.args.get("done"),
                               problem=request.args.get("problem"))

    @app.route("/approvals/<aid>", methods=["POST"])
    def approvals_decide(aid):
        who = (getattr(g, "user", None) or {}).get("username") or "console"
        decision = (request.form.get("decision") or "").strip()
        if decision not in ("approved", "denied"):
            return redirect(url_for("approvals_inbox", problem="that is not a decision"))
        try:
            # No token: the signed-in account is the credential here, where on
            # the emailed link the token is.
            result = resolve_approval(aid, decision, resolver=who,
                                      note=request.form.get("note") or None)
        except LookupError:
            return redirect(url_for("approvals_inbox", problem="that request is gone"))
        except ValueError as exc:
            return redirect(url_for("approvals_inbox", problem=str(exc)))
        if result.get("already_resolved"):
            return redirect(url_for("approvals_inbox",
                                    problem=f"somebody already {result['status']} that"))
        return redirect(url_for("approvals_inbox", done=result["status"]))

    # --- Jarvis ------------------------------------------------------------
    #
    # A conversation with the thing that builds, and a panel of what it has
    # built beside it. Off unless somebody turns it on in Settings, and
    # admin-only either way (auth.ADMIN_WRITE_PREFIXES). Every route starts by
    # checking the setting, so turning it off closes the door rather than only
    # hiding the tab.

    def _jarvis_on():
        if not jarvis.enabled(get_store()):
            abort(404)

    def _jarvis_chat_or_404(chat_id: str):
        row = jarvis.get_chat(chat_id)
        if not row:
            abort(404)
        return row

    def _panel() -> dict:
        """Everything Jarvis has, for the panel. Not scoped to the open
        conversation: what it built last week is still what it has."""
        have = jarvis.inventory()
        promoted = set(get_registry().agents())
        for agent in have["agents"]:
            agent["promoted"] = agent["name"] in promoted
        promoted_tools = set(get_registry().tools())
        for tool in have["tools"]:
            tool["promoted"] = tool["name"] in promoted_tools
        return have

    @app.route("/jarvis")
    def jarvis_screen():
        _jarvis_on()
        who = (getattr(g, "user", None) or {}).get("username")
        chat_id = request.args.get("chat")
        chat, history, last_seq = None, [], 0
        if chat_id:
            chat = _jarvis_chat_or_404(chat_id)
            history, last_seq = _chat_history(chat["session_id"])
        threads = [{"id": c["id"], "title": c["goal"], "updated_at": c["created_at"],
                    "status": c["status"]} for c in jarvis.chats()]
        return render_template(
            "jarvis.html", chat=chat, chat_id=chat_id, threads=threads,
            history=history, last_seq=last_seq, panel=_panel(),
            session_id=(chat["session_id"] if chat else ""),
            budget=(jarvis.budget_state(chat_id) if chat_id else None),
            default_budget=jarvis.default_budget(), model=jarvis.model(),
            who=who, problem=request.args.get("problem"),
            done=request.args.get("done"))

    @app.route("/jarvis/panel")
    def jarvis_panel():
        """The panel on its own, so the page can refresh it when a turn ends
        without reloading the conversation out from under you."""
        _jarvis_on()
        chat_id = request.args.get("chat")
        return render_template(
            "_jarvis_panel.html", panel=_panel(), chat_id=chat_id,
            budget=(jarvis.budget_state(chat_id) if chat_id else None))

    @app.route("/jarvis/messages", methods=["POST"])
    def jarvis_send():
        _jarvis_on()
        who = (getattr(g, "user", None) or {}).get("username") or "console"
        body = request.get_json(silent=True) or {}
        text = (body.get("text") or "").strip()
        if not text:
            return {"error": "say something first"}, 400

        chat_id = body.get("chat_id") or None
        if chat_id:
            chat = jarvis.get_chat(chat_id)
            if not chat:
                return {"error": "that conversation is gone"}, 404
        else:
            # The first message names the conversation. Nobody wants to fill in
            # a title field before they have said what they want.
            chat_id = jarvis.start_chat(text, who)
            chat = jarvis.get_chat(chat_id)

        # The budget is the only rail on a conversation, so it is checked before
        # the turn rather than noticed after it. Refusing here is what makes
        # topping up a decision rather than a formality.
        state = jarvis.budget_state(chat_id)
        if state["spent_up"]:
            return {"error": f"This conversation has used its €{state['budget']:.2f}. "
                             "Top it up in the panel to carry on.",
                    "chat_id": chat_id, "budget": state}, 402
        if chat["steps"] >= chat["max_steps"]:
            return {"error": "This conversation has gone on long enough to be worth "
                             "starting again. Open a new one — what it built is "
                             "still there.", "chat_id": chat_id}, 409

        # Written before every turn: the memory index is part of the
        # instructions and changes as it learns.
        jarvis.write_driver()
        jarvis.record_turn(chat_id)
        result = submit_message(
            jarvis.DRIVER, text, session_id=chat["session_id"],
            channel=jarvis.CHANNEL, sender=who,
            origin={"kind": jarvis.CHANNEL, "chat": chat_id, "who": who},
        )
        return jsonify({"session_id": result["session_id"], "chat_id": chat_id,
                        "thread_id": chat_id, "turn_id": result.get("turn_id")})

    @app.route("/jarvis/stream/<sid>")
    def jarvis_stream(sid):
        """Spine events for one conversation, plus the token deltas. The same
        shape as the chat stream, over a session this screen owns."""
        _jarvis_on()
        store = get_store()
        session = store.get_session(sid)
        if not session or session["channel"] != jarvis.CHANNEL:
            abort(404)
        watcher_id = (getattr(g, "user", None) or {}).get("id")
        resume = request.headers.get("Last-Event-ID") or request.args.get("after", 0)
        try:
            after = int(resume)
        except (TypeError, ValueError):
            after = 0

        def generate():
            q: queue.Queue = queue.Queue(maxsize=1000)
            store.subscribe(q)
            try:
                for ev in store.events_for_session(sid, after_seq=after):
                    yield _sse(ev)
                last_ping = last_check = time.time()
                while True:
                    try:
                        item = q.get(timeout=5)
                        if item.session_id == sid:
                            if isinstance(item, Ephemeral):
                                yield (f"event: {item.kind}\n"
                                       f"data: {json.dumps(item.payload)}\n\n")
                            else:
                                yield _sse(item)
                    except queue.Empty:
                        pass
                    now = time.time()
                    if watcher_id and now - last_check > 30:
                        last_check = now
                        if not auth.still_allowed(store, watcher_id):
                            return
                    if now - last_ping > 15:
                        last_ping = now
                        yield ": ping\n\n"
            finally:
                store.unsubscribe(q)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"},
        )

    def _back_to(chat_id: str, **words):
        """Back to the conversation you were in. Deliberately built from the
        chat id rather than from a `back` field in the form — a redirect target
        a form can name is a redirect target an attacker can name."""
        return redirect(url_for("jarvis_screen", chat=chat_id or None, **words))

    @app.route("/jarvis/promote", methods=["POST"])
    def jarvis_promote():
        """The one door between Jarvis's tree and the operator's, and a
        signed-in administrator pressing a button is the only thing that opens
        it."""
        _jarvis_on()
        chat_id = request.form.get("chat") or ""
        try:
            path = jarvis.promote(request.form.get("kind", ""),
                                  request.form.get("name", ""))
        except ValueError as exc:
            return _back_to(chat_id, problem=str(exc))
        return _back_to(chat_id, done=f"promoted — it is now {path}")

    @app.route("/jarvis/remove", methods=["POST"])
    def jarvis_remove():
        _jarvis_on()
        kind, name = request.form.get("kind", ""), request.form.get("name", "")
        chat_id = request.form.get("chat") or ""
        try:
            jarvis.remove(kind, name)
        except ValueError as exc:
            return _back_to(chat_id, problem=str(exc))
        return _back_to(chat_id, done=f"deleted the {kind} {name}")

    @app.route("/jarvis/<chat_id>/budget", methods=["POST"])
    def jarvis_budget(chat_id):
        _jarvis_on()
        _jarvis_chat_or_404(chat_id)
        try:
            total = jarvis.top_up(chat_id, request.form.get("extra"))
        except ValueError as exc:
            return redirect(url_for("jarvis_screen", chat=chat_id, problem=str(exc)))
        return redirect(url_for("jarvis_screen", chat=chat_id,
                                done=f"this conversation can now spend €{total:.2f}"))

    @app.route("/jarvis/<chat_id>/discard", methods=["POST"])
    def jarvis_discard(chat_id):
        _jarvis_on()
        _jarvis_chat_or_404(chat_id)
        what = jarvis.discard(chat_id)
        return redirect(url_for(
            "jarvis_screen",
            done=f"discarded {len(what['agents'])} agent(s) and "
                 f"{len(what['tools'])} tool(s)"))

    @app.route("/chat/<name>/report", methods=["POST"])
    def chat_report(name):
        """"That answer was wrong" — from the person who noticed.

        The conversation becomes a golden trace, which is a test. Evals are the
        thing everybody ships and nobody writes, because writing them is a
        separate chore done later by whoever is least motivated. Here the test
        is written by the person who saw the problem, at the moment they saw
        it, as a by-product of complaining about it.
        """
        agent = _chat_agent_or_404(name)
        who = (getattr(g, "user", None) or {}).get("username")
        body = request.get_json(silent=True) or {}
        sid = body.get("session_id")
        session = get_store().get_session(sid) if sid else None
        if not session or not _mine(session, who, agent.name):
            abort(404)

        note = (body.get("note") or "").strip()
        label = f"reported by {who}"
        if session["title"]:
            label += f": {session['title'][:60]}"
        try:
            golden_id = evals.promote_session(
                sid, name=label,
                reported={"by": who, "note": note, "at": time.time()})
        except LookupError:
            abort(404)
        return jsonify({"saved": True, "golden_id": golden_id})

    @app.route("/chat/<name>/stream/<sid>")
    def chat_stream(name, sid):
        """Spine events for one conversation, plus the token deltas.

        Deltas are ephemeral broadcasts rather than events (they are not on the
        contract and are never stored), so they arrive here as their own SSE
        event name and a client that ignores them still sees every reply.
        """
        _chat_agent_or_404(name)
        store = get_store()
        who = (getattr(g, "user", None) or {}).get("username")
        session = store.get_session(sid)
        if not session or not _mine(session, who, name):
            abort(404)
        watcher_id = (getattr(g, "user", None) or {}).get("id")
        # Where to resume. The page already shows everything up to `after`, and
        # EventSource sends Last-Event-ID when it reconnects by itself — without
        # honouring one or the other, every reconnect appends the whole
        # conversation again underneath the copy already on screen.
        resume = request.headers.get("Last-Event-ID") or request.args.get("after", 0)
        try:
            after = int(resume)
        except (TypeError, ValueError):
            after = 0

        def generate():
            q: queue.Queue = queue.Queue(maxsize=1000)
            store.subscribe(q)
            try:
                for ev in store.events_for_session(sid, after_seq=after):
                    yield _sse(ev)
                last_ping = last_check = time.time()
                while True:
                    try:
                        item = q.get(timeout=5)
                        if item.session_id == sid:
                            if isinstance(item, Ephemeral):
                                yield (f"event: {item.kind}\n"
                                       f"data: {json.dumps(item.payload)}\n\n")
                            else:
                                yield _sse(item)
                    except queue.Empty:
                        pass
                    now = time.time()
                    # Same re-check as the console stream: a stream opened once
                    # would otherwise outlive a suspended account indefinitely.
                    if watcher_id and now - last_check > 30:
                        last_check = now
                        if not auth.still_allowed(store, watcher_id):
                            return
                    if now - last_ping > 15:
                        last_ping = now
                        yield ": ping\n\n"
            finally:
                store.unsubscribe(q)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"},
        )


def _collect_inputs(form) -> dict:
    """The repeated name/type rows from the tool wizard."""
    names = [n.strip() for n in form.getlist("input_name")]
    kinds = form.getlist("input_type")
    return {
        name: (kinds[i] if i < len(kinds) else "string")
        for i, name in enumerate(names) if name
    }


def _collect_config(form) -> dict:
    """Flatten `config__x` and `config__x__key`/`__value` pairs into a config
    mapping. JSON-shaped fields are parsed so the file holds real structure
    rather than a string containing JSON."""
    config: dict = {}
    pairs: dict[str, dict] = {}

    for key in form:
        if not key.startswith("config__"):
            continue
        rest = key[len("config__"):]
        if rest.endswith("__key") or rest.endswith("__value"):
            field, _, side = rest.rpartition("__")
            bucket = pairs.setdefault(field, {"keys": [], "values": []})
            bucket["keys" if side == "key" else "values"] = form.getlist(key)
            continue
        value = form.get(key, "").strip()
        if not value:
            continue
        if value[:1] in "{[":
            try:
                value = json.loads(value)
            except ValueError:
                pass
        config[rest] = value

    for field, bucket in pairs.items():
        mapping = {
            k.strip(): v for k, v in zip(bucket["keys"], bucket["values"]) if k.strip()
        }
        if mapping:
            config[field] = mapping
    return config


def _sample_args(schema: dict) -> dict:
    """Prefill the test panel from the tool's own schema, so Run works on the
    first click instead of after you have looked up the field names."""
    examples = {"string": "", "number": 0, "integer": 0, "boolean": False,
                "array": [], "object": {}}
    return {
        field: examples.get(spec.get("type", "string"), "")
        for field, spec in (schema.get("properties") or {}).items()
    }


def _sse(ev) -> str:
    data = ev.to_dict()
    data["summary"] = ev.summary
    data["css"] = EVENT_CLASS.get(ev.type, "")
    return f"id: {ev.seq}\nevent: {ev.type}\ndata: {json.dumps(data, default=str)}\n\n"


# ============================================================== MCP server


def register_mcp(app: Flask) -> None:
    """Expose an agent as an MCP server (§12): one typed tool carrying the
    agent's name, description and input schema. The call lands as a channel
    adapter and becomes `message.received`; the full spine applies."""

    @app.route("/mcp/<name>", methods=["POST", "GET"])
    def mcp_endpoint(name):
        registry = get_registry()
        agent = registry.get_agent(name)
        if not agent or not (agent.expose or {}).get("mcp"):
            return jsonify({"error": f"agent '{name}' does not expose MCP"}), 404

        store = get_store()
        # The console requires a sign-in; an unauthenticated RPC endpoint that
        # can *run* an agent would simply be the way around it. Once anybody has
        # an account, MCP needs a credential too.
        if _mcp_is_open(store) and not users.needs_setup(store):
            return jsonify({
                "error": "this Heddled has accounts, so callers need a credential — "
                         "add one under Settings (mcp_callers) and send it as "
                         "'Authorization: Bearer <key>'"
            }), 401
        caller, authorized = _authenticate_mcp_caller(store, request)
        if not authorized:
            return jsonify({"error": "unauthorized"}), 401

        if request.method == "GET":
            return jsonify(_mcp_descriptor(agent, request.host_url))

        body = request.get_json(silent=True) or {}
        method = body.get("method")
        rpc_id = body.get("id")
        params = body.get("params") or {}

        def ok(result):
            return jsonify({"jsonrpc": "2.0", "id": rpc_id, "result": result})

        if method == "initialize":
            return ok(
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": f"heddled/{agent.name}", "version": agent.short_version},
                }
            )
        if method in ("notifications/initialized", "ping"):
            return ok({})
        if method == "tools/list":
            return ok({"tools": _mcp_tools(agent)})
        if method == "tools/call":
            tool_name = params.get("name")
            args = params.get("arguments") or {}
            if tool_name not in (f"ask_{agent.name}", f"continue_{agent.name}"):
                return ok({"isError": True,
                           "content": [{"type": "text", "text": f"unknown tool {tool_name}"}]})
            chain = [c for c in (request.headers.get("X-Heddled-Chain", "").split(",")) if c]
            result = submit_message(
                agent.name,
                args.get("message", ""),
                session_id=args.get("session_id"),
                channel="mcp",
                origin={"kind": "mcp", "reason": f"tools/call by {caller}",
                        "caller": caller, "via": chain + [caller]},
                sender=caller,
                caller=caller,
                call_chain=chain + [caller],
                env=runtime_env(args.get("env")),
                sync=True,
                timeout_s=float(args.get("timeout_s", 60)),
            )
            # A turn paused for approval can outlive a caller's tool timeout, so
            # return `pending` plus a continuation handle instead of blocking (§12).
            if result.get("status") in ("waiting-approval", "timeout"):
                return ok(
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "status": "pending",
                                        "reason": "awaiting human approval"
                                        if result["status"] == "waiting-approval"
                                        else "still running",
                                        "session_id": result["session_id"],
                                        "continue_with": f"continue_{agent.name}",
                                    }
                                ),
                            }
                        ]
                    }
                )
            return ok(
                {
                    "content": [{"type": "text", "text": result.get("reply", "")}],
                    "structuredContent": {
                        "reply": result.get("reply", ""),
                        "session_id": result["session_id"],
                        "status": result.get("status"),
                    },
                }
            )
        return jsonify(
            {"jsonrpc": "2.0", "id": rpc_id,
             "error": {"code": -32601, "message": f"method '{method}' not found"}}
        )


def _mcp_is_open(store) -> bool:
    """Whether any credential has been issued for the MCP surface at all."""
    return not (store.get_setting("mcp_callers") or store.get_setting("mcp_api_key"))


def _authenticate_mcp_caller(store, request) -> tuple[str, bool]:
    """Resolve the external caller's identity from its credential (§12).

    Two settings, in order of precedence:

        mcp_callers  {"key-abc": "copilot-studio", "key-def": "claude"}
        mcp_api_key  "one-shared-key"      (single-caller shorthand)

    With neither configured the endpoint is open and the caller is whatever it
    claims in `X-Heddled-Caller` — fine for a homelab, and the reason policies key
    on caller only when you have actually issued keys.

    Returns (caller_name, authorized).
    """
    presented = (request.headers.get("Authorization", "").replace("Bearer ", "").strip()
                 or request.args.get("api_key") or "")
    claimed = request.headers.get("X-Heddled-Caller", "mcp")

    callers = store.get_setting("mcp_callers") or {}
    if callers:
        if not isinstance(callers, dict):
            return claimed, False
        name = callers.get(presented)
        # The key names the caller — a caller cannot rename itself via a header.
        return (name, True) if name else (claimed, False)

    shared = store.get_setting("mcp_api_key")
    if shared:
        return claimed, presented == shared

    return claimed, True


def _mcp_tools(agent) -> list[dict]:
    return [
        {
            "name": f"ask_{agent.name}",
            "description": agent.description or f"Ask the '{agent.name}' Heddled agent.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "What to ask the agent."},
                    "session_id": {
                        "type": "string",
                        "description": "Optional: continue an existing session for multi-turn context.",
                    },
                },
                "required": ["message"],
            },
        },
        {
            "name": f"continue_{agent.name}",
            "description": "Continue a session that returned status=pending (e.g. it paused for approval).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["session_id"],
            },
        },
    ]


def _mcp_descriptor(agent, host_url: str) -> dict:
    return {
        "name": f"heddled/{agent.name}",
        "version": agent.short_version,
        "transport": "streamable-http",
        "endpoint": f"{host_url.rstrip('/')}/mcp/{agent.name}",
        "tools": _mcp_tools(agent),
    }
