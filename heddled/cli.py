"""`heddled` — the CLI.

The dev loop is the heart of "easy" (concept §6):

    heddled dev
    heddled tool test lookup_invoice --args '{"invoice_number":"F-2231"}'
    heddled chat support "where is invoice F-2231?"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

from . import config
from .events import EVENT_CLASS


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


# ------------------------------------------------------------------ serve


def cmd_serve(args) -> int:
    from .web.app import create_app

    app = create_app(start_worker=not args.no_worker, dev=args.dev)
    url = f"http://localhost:{args.port}"
    print(f"🧵 heddled · console {url}")
    print(f"   agents  {config.AGENTS_DIR}")
    print(f"   tools   {config.TOOLS_DIR}")
    print(f"   store   {config.DB_PATH}")
    if args.dev and args.open:
        threading_open(url + (f"/agents/{args.agent}/test" if args.agent else "/"))
    app.run(host=args.host, port=args.port, threaded=True, debug=False,
            use_reloader=False)
    return 0


def threading_open(url: str) -> None:
    import threading

    def _go():
        time.sleep(1.0)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_go, daemon=True).start()


def cmd_dev(args) -> int:
    args.dev = True
    return cmd_serve(args)


def cmd_worker(args) -> int:
    from .worker import Worker

    print("🧵 heddled worker · draining the job queue and ticking pull triggers")
    w = Worker(concurrency=args.concurrency, run_triggers=not args.no_triggers,
               verbose=True).start()
    w.join()
    return 0


# ------------------------------------------------------------------- chat


def cmd_chat(args) -> int:
    from .runtime import AgentNotFound, submit_message
    from .store import get_store
    from .worker import ensure_worker

    ensure_worker()
    try:
        result = submit_message(
            args.agent, args.message, session_id=args.session, channel="cli",
            origin={"kind": "cli", "reason": "heddled chat"}, sync=True,
            timeout_s=args.timeout,
        )
    except AgentNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        _print_json(result)
        return 0 if result.get("status") == "completed" else 1

    if args.trace:
        print_trace(result["session_id"])
    print()
    if result.get("status") == "waiting-approval":
        store = get_store()
        pending = [a for a in store.pending_approvals()
                   if a["session_id"] == result["session_id"]]
        print("⏸  paused — waiting for approval")
        for a in pending:
            base = store.get_setting("public_url") or f"http://localhost:{config.DEFAULT_PORT}"
            print(f"    {a['tool']}({json.loads(a['args'])})")
            print(f"    approve: {base}/approve/{a['id']}?token={a['token']}&decision=approved")
            print(f"    deny:    {base}/approve/{a['id']}?token={a['token']}&decision=denied")
            print(f"    or:      heddled approve {a['id']} --decision approved")
    elif result.get("status") == "error":
        print(f"✗  {result.get('error')}")
    else:
        print(result.get("reply", ""))
    print(f"\n   session {result['session_id']} · "
          f"http://localhost:{config.DEFAULT_PORT}/sessions/{result['session_id']}")
    return 0


def print_trace(session_id: str) -> None:
    from .store import get_store

    for ev in get_store().events_for_session(session_id):
        stamp = time.strftime("%H:%M:%S", time.localtime(ev.ts))
        print(f"  {ev.seq:>4}  {stamp}  {ev.type:<20} {ev.summary}")


def cmd_trace(args) -> int:
    from .store import get_store

    store = get_store()
    if not store.get_session(args.session):
        print(f"unknown session {args.session}", file=sys.stderr)
        return 1
    if args.json:
        _print_json([e.to_dict() for e in store.events_for_session(args.session)])
    else:
        print_trace(args.session)
    return 0


def cmd_sessions(args) -> int:
    from .store import get_store

    rows = get_store().list_sessions(agent=args.agent, status=args.status, limit=args.limit)
    if args.json:
        _print_json([dict(r) for r in rows])
        return 0
    for r in rows:
        print(f"{r['id']}  {r['agent']:<14} {r['channel']:<9} {r['status']:<16} "
              f"{time.strftime('%m-%d %H:%M', time.localtime(r['created_at']))}  "
              f"{(r['title'] or '')[:50]}")
    return 0


# ------------------------------------------------------------------- tools


def cmd_tool_test(args) -> int:
    from .tooltest import run_tool_standalone

    try:
        result = run_tool_standalone(args.name, json.loads(args.args or "{}"))
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_json(result)
    return 0 if result.get("ok") else 1


def cmd_tool_list(args) -> int:
    from .registry import get_registry

    tools = get_registry().tools()
    if args.json:
        _print_json({n: {"description": t.description, "input": t.input_schema}
                     for n, t in tools.items()})
        return 0
    for name, t in tools.items():
        fields = ", ".join((t.input_schema.get("properties") or {}).keys())
        print(f"{name:<20} ({fields})  {t.description}")
    return 0


def cmd_agents(args) -> int:
    from .registry import get_registry

    registry = get_registry()
    agents = registry.agents()
    if args.json:
        _print_json({
            n: {"version": a.version, "model": a.model,
                "tools": list(registry.agent_tools(a).keys()),
                "channels": [c if isinstance(c, str) else list(c)[0] for c in a.channels]}
            for n, a in agents.items()
        })
        return 0
    for name, a in agents.items():
        print(f"{name:<16} {a.short_version}  {a.model:<32} "
              f"tools={len(registry.agent_tools(a))} triggers={len(a.triggers)}")
    return 0


# ------------------------------------------------------------- approvals


def cmd_approve(args) -> int:
    from .runtime import resolve_approval
    from .store import get_store
    from .worker import ensure_worker

    store = get_store()
    if not args.approval_id:
        pending = store.pending_approvals()
        if not pending:
            print("no pending approvals")
            return 0
        for a in pending:
            print(f"{a['id']}  {a['agent']}/{a['tool']}  {a['args']}  "
                  f"session {a['session_id']}")
        return 0
    ensure_worker()
    try:
        result = resolve_approval(args.approval_id, args.decision,
                                  resolver=args.by, note=args.note)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_json(result)
    if args.wait:
        from .runtime import wait_for_turn

        _print_json(wait_for_turn(result["turn_id"], timeout_s=args.timeout))
    return 0


# ----------------------------------------------------------------- evals


def cmd_eval_run(args) -> int:
    from . import evals
    from .runtime import wait_for_turn  # noqa: F401  (kept for symmetry)
    from .store import get_store
    from .worker import ensure_worker

    ensure_worker()
    store = get_store()
    try:
        run_id = evals.queue_eval_run(args.agent, against_version=args.against)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        run = store.get_eval_run(run_id)
        if run and run["status"] != "running":
            break
        time.sleep(0.2)
    run = store.get_eval_run(run_id)
    result = json.loads(run["result"] or "{}")

    if args.json:
        _print_json({"run_id": run_id, "status": run["status"], **result})
    else:
        print(f"eval run {run_id} · {run['status']} · "
              f"{run['passed']} passed / {run['failed']} failed")
        for c in result.get("cases", []):
            mark = "✓" if c.get("passed") else "✗"
            print(f"  {mark} {c.get('name')}")
            if not c.get("passed"):
                for d in (c.get("tool_diff") or {}).get("diffs", []):
                    print(f"      tool #{d['index']}: {d['kind']}")
                for a in c.get("assertions", []):
                    if not a["passed"]:
                        print(f"      assertion failed: {a['description']}")
                if c.get("error"):
                    print(f"      error: {c['error']}")
    return 0 if run["status"] == "passed" else 1


def cmd_promote(args) -> int:
    from . import evals

    try:
        gid = evals.promote_session(args.session, args.name)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"golden trace {gid} created from {args.session}")
    return 0


def cmd_deploy(args) -> int:
    from . import evals
    from .registry import get_registry
    from .store import get_store

    agent = get_registry().get_agent(args.agent)
    if not agent:
        print(f"unknown agent '{args.agent}'", file=sys.stderr)
        return 1
    if args.env not in config.ENVIRONMENTS:
        print(f"env must be one of {config.ENVIRONMENTS}", file=sys.stderr)
        return 1
    green, why = evals.is_green(agent.name, agent.version)
    if args.env == "prod" and not green and not args.force:
        print(f"refusing: promotion to prod is gated on a green eval run\n  {why}",
              file=sys.stderr)
        return 1
    get_store().promote(agent.name, args.env, agent.version, by=args.by)
    print(f"{agent.name} {agent.short_version} → {args.env}   ({why})")
    return 0


# ------------------------------------------------------------------- init


def cmd_init(args) -> int:
    from .scaffold import scaffold

    created = scaffold(Path(args.path or config.ROOT), force=args.force)
    for p in created:
        print(f"created {p}")
    if not created:
        print("nothing to do — agents/ and tools/ already have content (use --force)")
    return 0


def cmd_new(args) -> int:
    """`heddled new agent|tool|policy` — the same scaffolds the console's New
    buttons call, so the two surfaces cannot drift."""
    from .authoring import (
        AuthoringError,
        add_policy,
        new_agent,
        new_tool,
    )

    try:
        if args.object == "agent":
            written = new_agent(args.name, model=args.model, description=args.description,
                                from_agent=args.from_, commit=args.commit or None)
        elif args.object == "tool":
            written = new_tool(args.name, description=args.description,
                               input_spec=args.input, output_spec=args.output,
                               from_tool=args.from_, commit=args.commit or None)
        else:  # policy
            policy = {"tool": args.tool}
            if args.requires_approval:
                policy["requires_approval"] = True
            if args.approval_adapter:
                policy["approval_adapter"] = args.approval_adapter
            if args.max_eur_per_day:
                amount = args.max_eur_per_day
                # Write 500, not 500.0 — the file is read by humans.
                policy["budget"] = {
                    "max_eur_per_day": int(amount) if amount == int(amount) else amount
                }
            if args.redact:
                policy["redact"] = [r.strip() for r in args.redact.split(",") if r.strip()]
            if args.allow_channels:
                policy["allow_channels"] = [c.strip() for c in args.allow_channels.split(",")]
            written = add_policy(args.name, policy, commit=args.commit or None)
    except AuthoringError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for p in written.paths:
        print(f"wrote {p}")
    if not written.paths:
        print("no changes")
    if written.committed:
        print(f"committed {written.committed}")
    if args.diff and written.diff:
        print()
        print(written.diff)
    return 0


def cmd_rm(args) -> int:
    from .authoring import AuthoringError, delete_agent, delete_tool

    try:
        if args.object == "agent":
            written = delete_agent(args.name, force=args.force,
                                   commit=args.commit or None)
        else:
            written = delete_tool(args.name, force=args.force, commit=args.commit or None)
    except AuthoringError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for p in written.paths:
        print(f"removed {p}")
    for a in written.unmounted_from:
        print(f"unmounted from {a}")
    if written.committed:
        print(f"committed {written.committed}")
    return 0


def cmd_rename(args) -> int:
    from .authoring import AuthoringError, rename_agent, rename_tool
    from .store import get_store

    try:
        if args.object == "agent":
            written = rename_agent(args.name, args.new_name, commit=args.commit or None)
            if written.paths:
                # History and trigger positions follow the name, as they do in
                # the console — the two surfaces write the same bytes (§6).
                get_store().rename_agent(args.name, args.new_name)
        else:
            written = rename_tool(args.name, args.new_name, commit=args.commit or None)
    except AuthoringError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{args.name} is now {args.new_name}")
    for a in written.repointed:
        print(f"repointed {a}")
    if written.committed:
        print(f"committed {written.committed}")
    return 0


def cmd_user(args) -> int:
    """Manage people from the machine running Heddled — the way back in when the
    only administrator forgets their password."""
    from . import users
    from .store import get_store

    store = get_store()

    def _prompt(label="New password: "):
        import getpass

        first = getpass.getpass(label)
        if first != getpass.getpass("Repeat it: "):
            raise users.UserError("The two passwords differ.")
        return first

    try:
        if args.user_command == "list":
            people = users.listing(store)
            if not people:
                print("nobody yet — open the console to set up the first administrator")
                return 0
            for person in people:
                state = "" if person["active"] else "  (suspended)"
                print(f"{person['username']:<24} {person['role']:<8}{state}")
        elif args.user_command == "add":
            users.create(store, args.username, args.password or _prompt(),
                         role=args.role, created_by="cli")
            print(f"added {args.username} as {args.role}")
        elif args.user_command == "passwd":
            users.set_password(store, args.username, args.password or _prompt(), by="cli")
            print(f"password changed for {args.username}")
        elif args.user_command == "role":
            users.set_role(store, args.username, args.role, by="cli")
            print(f"{args.username} is now {args.role}")
        elif args.user_command == "suspend":
            users.set_active(store, args.username, False, by="cli")
            print(f"{args.username} can no longer sign in")
        elif args.user_command == "restore":
            users.set_active(store, args.username, True, by="cli")
            print(f"{args.username} can sign in again")
        elif args.user_command == "remove":
            users.delete(store, args.username, by="cli")
            print(f"removed {args.username}")
    except users.UserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_retention(args) -> int:
    from .store import get_store

    n = get_store().apply_retention()
    print(f"pruned full context from {n} event(s) older than "
          f"{config.KEEP_FULL_CONTEXT_DAYS} days")
    return 0


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="heddled", description="Heddled — self-hostable agent platform")
    sub = p.add_subparsers(dest="command", required=True)

    def add_serve_args(sp):
        sp.add_argument("--port", type=int, default=config.DEFAULT_PORT)
        sp.add_argument("--host", default=config.DEFAULT_HOST)
        sp.add_argument("--no-worker", action="store_true",
                        help="serve HTTP only; run `heddled worker` separately")

    sp = sub.add_parser("serve", help="run the console + API (+ worker)")
    add_serve_args(sp)
    sp.add_argument("--dev", action="store_true")
    sp.add_argument("--open", action="store_true")
    sp.add_argument("--agent")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("dev", help="dev mode: same console, opened on the Test tab")
    add_serve_args(sp)
    sp.add_argument("--agent", help="agent to open the Test tab for")
    sp.add_argument("--no-open", dest="open", action="store_false", default=True)
    sp.set_defaults(func=cmd_dev)

    sp = sub.add_parser("worker", help="run the background worker standalone")
    sp.add_argument("--concurrency", type=int, default=2)
    sp.add_argument("--no-triggers", action="store_true")
    sp.set_defaults(func=cmd_worker)

    sp = sub.add_parser("chat", help="run one scripted turn from the terminal")
    sp.add_argument("agent")
    sp.add_argument("message")
    sp.add_argument("--session")
    sp.add_argument("--trace", action="store_true", help="print the event trace")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--timeout", type=float, default=120)
    sp.set_defaults(func=cmd_chat)

    sp = sub.add_parser("trace", help="print the trace of a session")
    sp.add_argument("session")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_trace)

    sp = sub.add_parser("sessions", help="list sessions")
    sp.add_argument("--agent")
    sp.add_argument("--status")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_sessions)

    sp = sub.add_parser("agents", help="list agents")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_agents)

    tool = sub.add_parser("tool", help="tool utilities")
    tool_sub = tool.add_subparsers(dest="tool_command", required=True)
    tsp = tool_sub.add_parser("test", help="run a tool in isolation")
    tsp.add_argument("name")
    tsp.add_argument("--args", default="{}")
    tsp.set_defaults(func=cmd_tool_test)
    tsp = tool_sub.add_parser("list")
    tsp.add_argument("--json", action="store_true")
    tsp.set_defaults(func=cmd_tool_list)

    sp = sub.add_parser("approve", help="list or resolve pending approvals")
    sp.add_argument("approval_id", nargs="?")
    sp.add_argument("--decision", default="approved", choices=["approved", "denied"])
    sp.add_argument("--by", default="cli")
    sp.add_argument("--note")
    sp.add_argument("--wait", action="store_true", help="wait for the resumed turn")
    sp.add_argument("--timeout", type=float, default=60)
    sp.set_defaults(func=cmd_approve)

    ev = sub.add_parser("eval", help="golden traces and eval runs")
    ev_sub = ev.add_subparsers(dest="eval_command", required=True)
    esp = ev_sub.add_parser("run")
    esp.add_argument("agent")
    esp.add_argument("--against", help="agent version label to record the run against")
    esp.add_argument("--timeout", type=float, default=120)
    esp.add_argument("--json", action="store_true")
    esp.set_defaults(func=cmd_eval_run)
    esp = ev_sub.add_parser("promote", help="promote a recorded session to a golden trace")
    esp.add_argument("session")
    esp.add_argument("--name")
    esp.set_defaults(func=cmd_promote)

    sp = sub.add_parser("deploy", help="promote an agent version to an environment")
    sp.add_argument("agent")
    sp.add_argument("env", choices=config.ENVIRONMENTS)
    sp.add_argument("--by", default="cli")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_deploy)

    sp = sub.add_parser("init", help="scaffold an example agent and tools")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    new = sub.add_parser("new", help="scaffold an agent, tool or policy")
    new_sub = new.add_subparsers(dest="object", required=True)

    nsp = new_sub.add_parser("agent", help="create an agent definition + instructions")
    nsp.add_argument("name")
    nsp.add_argument("--model", default="mock/echo")
    nsp.add_argument("--description")
    nsp.add_argument("--from", dest="from_", metavar="AGENT",
                     help="clone an existing agent instead of starting blank")
    nsp.add_argument("--commit", action="store_true", help="commit the new files")
    nsp.add_argument("--diff", action="store_true")
    nsp.set_defaults(func=cmd_new)

    nsp = new_sub.add_parser("tool", help="create a tool directory + handler")
    nsp.add_argument("name")
    nsp.add_argument("--description")
    nsp.add_argument("--input", help="e.g. invoice_number:string,amount_eur:number")
    nsp.add_argument("--output", help="e.g. status:string,amount_eur:number")
    nsp.add_argument("--from", dest="from_", metavar="TOOL",
                     help="clone an existing tool instead of starting blank")
    nsp.add_argument("--commit", action="store_true")
    nsp.add_argument("--diff", action="store_true")
    nsp.set_defaults(func=cmd_new)

    nsp = new_sub.add_parser("policy", help="add a policy block to an agent")
    nsp.add_argument("name", help="the agent to attach the policy to")
    nsp.add_argument("--tool", required=True, help='tool name, or "*" for all')
    nsp.add_argument("--requires-approval", action="store_true")
    nsp.add_argument("--approval-adapter")
    nsp.add_argument("--max-eur-per-day", type=float)
    nsp.add_argument("--redact", help="comma-separated: iban,creditcard,email")
    nsp.add_argument("--allow-channels", help="comma-separated channel names")
    nsp.add_argument("--commit", action="store_true")
    nsp.add_argument("--diff", action="store_true")
    nsp.set_defaults(func=cmd_new)

    rm = sub.add_parser("rm", help="delete an agent or tool")
    rm_sub = rm.add_subparsers(dest="object", required=True)
    rsp = rm_sub.add_parser("agent")
    rsp.add_argument("name")
    rsp.add_argument("--force", action="store_true",
                     help="delete it and unmount it from the agents that delegate to it")
    rsp.add_argument("--commit", action="store_true")
    rsp.set_defaults(func=cmd_rm)
    rsp = rm_sub.add_parser("tool")
    rsp.add_argument("name")
    rsp.add_argument("--force", action="store_true",
                     help="delete it and unmount it from the agents that use it")
    rsp.add_argument("--commit", action="store_true")
    rsp.set_defaults(func=cmd_rm)

    mv = sub.add_parser("mv", help="rename an agent or tool, following every reference")
    mv_sub = mv.add_subparsers(dest="object", required=True)
    for kind in ("agent", "tool"):
        msp = mv_sub.add_parser(kind)
        msp.add_argument("name")
        msp.add_argument("new_name")
        msp.add_argument("--commit", action="store_true")
        msp.set_defaults(func=cmd_rename)

    usr = sub.add_parser("user", help="manage the people who can open the console")
    usr_sub = usr.add_subparsers(dest="user_command", required=True)
    usr_sub.add_parser("list").set_defaults(func=cmd_user)
    for name, needs_role in (("add", True), ("role", True)):
        u = usr_sub.add_parser(name)
        u.add_argument("username")
        u.add_argument("--role", default="member", choices=["admin", "member", "viewer"])
        if name == "add":
            u.add_argument("--password", help="omit to be prompted")
        u.set_defaults(func=cmd_user)
    u = usr_sub.add_parser("passwd")
    u.add_argument("username")
    u.add_argument("--password", help="omit to be prompted")
    u.set_defaults(func=cmd_user, role="member")
    for name in ("suspend", "restore", "remove"):
        u = usr_sub.add_parser(name)
        u.add_argument("username")
        u.set_defaults(func=cmd_user, role="member", password=None)

    sp = sub.add_parser("retention", help="apply the context retention policy now")
    sp.set_defaults(func=cmd_retention)

    return p


def main(argv=None) -> int:
    config.ensure_dirs()
    args = build_parser().parse_args(argv)
    return args.func(args)
