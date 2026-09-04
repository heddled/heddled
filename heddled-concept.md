# Heddled — Concept & Starting Point

*Working title. Sessions are threads; the event spine is the warp everything is woven on. Alternatives: Conductor, Spindle, Tracewright.*

**One-liner:** a self-hostable agent platform where building, debugging, operating, and evaluating an agent are all views on the same event stream — easy enough to ship an agent in an afternoon, powerful enough to run a fleet in production.

---

## 1. The thesis

Every agent platform today makes you choose. Low-code tools (Copilot Studio, Botpress-style builders) are approachable but become hostile the moment you need real tools, real visibility, or a human in the loop. Code frameworks (LangGraph, Agents SDKs) are powerful but give you a library, not a product — you build the console, the traces, the deployment story, and the operator experience yourself, every time.

The gap in the market is not a better builder or a better framework. It is a **platform with one spine**: a structured event stream through which every turn flows, with everything else defined as either an *adapter* (moves messages in/out — tools and channels alike) or a *consumer* (observes the stream — the trace viewer, the operator console, the eval runner). When the spine is right, the features that are bolted on elsewhere fall out naturally here.

The second half of the thesis is **progressive disclosure without rewrites**. Easy and powerful are usually a trade-off because "easy" tools make you start over when you outgrow them. Heddled has three levels — config, config + Python handler, full Python API — and moving up a level never means abandoning the level below. A YAML-defined agent and a fully programmatic agent are the same object to the platform.

## 2. Product principles

1. **The trace is the product.** Building, debugging, testing, operating, and evaluating are all interactions with traces. If a feature can't be expressed as producing or consuming the event stream, it probably doesn't belong in the core.
2. **Everything is an adapter or a consumer.** No separate mental models for tools vs. channels vs. flows vs. topics. One core, two kinds of attachments.
3. **Files first, UI second — but the UI is a real authoring surface.** Every agent, tool, and policy is a versionable file on disk. The UI reads and writes those same files — it never owns state the files don't have, and `git diff` always tells the truth about what changed. "Files first" is a statement about the *source of truth*, not a reason to make the console read-only: creating an agent, adding a tool, and changing a policy must all be possible without leaving the browser, because the alternative is an editor, a terminal, and a restart for every one-line change.
4. **Escape hatches, not cliffs.** Start in YAML. Drop to a Python handler when a tool needs logic. Drop to the Python API when the agent loop itself needs customizing. Each step is additive.
5. **Self-hosted first.** A single `docker compose up` gives you the full platform — core, console, trace store. Cloud is a deployment target, not a requirement.
6. **Humans are in the loop, not in the tool.** Approval, escalation, and takeover are core primitives — and Heddled routes them out through adapters to wherever the human already works. The console is for agent designers and admins: it is never the *required* surface for approvers, and never the surface for end users at scale. The one exception is deliberate and narrow — see §9, *Talking to an agent without operating one*. An agent nobody but its author can use is not finished, and "integrate Teams first" is too high a wall to put in front of showing a colleague what you built.

## 3. Who it's for

- **The builder** wants to define an agent, give it tools, and iterate fast with full visibility into every turn. Today they're fighting connector wizards or writing their own logging.
- **The approver** never opens Heddled. They get a card in Teams, a Slack message, or an email: what the agent wants to do, the exact arguments, and why. They approve or deny from there. Human-in-the-loop without another tab.
- **The reviewer/admin** needs to answer "what did the agent do, why, and who allowed it" — audit, budgets, permissions, and regression confidence before a new version ships.

One person can be all three (the homelab / small-team case). The platform must not assume an org chart.

## 4. Object model

| Object | What it is |
|---|---|
| **Agent** | A versioned definition: model, instructions, mounted adapters, policies. A file. |
| **Adapter** | Anything that moves messages in and/or out. Two flavors: *channels* (web chat, Teams, webhook, email, queue) and *tools* (typed capability the model can invoke). Same interface. |
| **Trigger** | What starts a turn. *Push* triggers are inbound channel adapters (webhook, MCP, inbound email). *Pull* triggers are Heddled-driven — a scheduler or poller that fires on its own and starts a session. |
| **Session** | A conversation/job with state and memory. The unit shown in the console. |
| **Turn** | One inbound message → agent work → outbound message(s). The unit of tracing. |
| **Event** | One structured record on the spine (`tool.called`, `decision.made`, …). The atom. |
| **Policy** | Declarative rules over adapters and sessions: permissions, approval gates, budgets, rate limits. |
| **Deployment** | An agent version bound to an environment (dev/staging/prod). |
| **Eval suite** | Recorded traces promoted to test cases, replayed against new agent versions. |

## 5. Architecture

```
            ┌────────────────────── consumers ──────────────────────┐
            │  Trace store   Console (build/admin)   Eval runner    │
            └───────────────────────▲───────────────────────────────┘
                                    │ subscribe
 channels ──▶ ┌─────────┐    ┌──────┴──────┐    ┌──────────────┐
 (web, Teams, │ Gateway  │──▶│  Event bus  │◀──│ Turn engine   │──▶ model
  webhook,    └─────────┘    └──────┬──────┘    │ (plan/execute)│    providers
  queue)                            │ publish    └──────┬───────┘
                                    │                   │ invoke
                                    │            ┌──────▼───────┐
                                    │            │ Tool adapters │
                                    │            └──────────────┘
                              ┌─────▼─────┐
                              │ State store│  (sessions, memory)
                              └───────────┘
```

The turn engine is deliberately boring: receive message → build context (instructions + session state + tool schemas) → call model → execute tool calls → repeat until done → emit reply. All sophistication lives in what flows through it and who watches.

**Concrete stack (v1):** Python + Flask for the core, adapters, and JSON API; SQLite (WAL mode) as the event store and state store; vanilla HTML/CSS/JS for the builder/admin console; Server-Sent Events for the live trace pane. Tool and adapter handlers are plain Python. The event bus is in-process for a single node — "append to the events table, notify subscribers" — and the contract is written so the transport can later be swapped (a background worker queue, then NATS) without touching adapters or consumers.

**Concurrency model.** An agent turn is I/O-bound and slow (waiting on model and tool calls), and — because approvals route out of Heddled and resume later — a turn must be able to outlive the HTTP request that started it. So the turn engine runs as a **background worker consuming a job queue**, not inside the request thread. In v1 the queue is a SQLite-backed table drained by a worker thread (or a separate process); Flask threaded workers handle the HTTP/SSE side. This keeps all code synchronous and readable and sidesteps async Python entirely. Formalizing the queue (RQ/Celery) or moving to multiple worker processes is a later, contract-preserving change.

**Live trace via SSE.** Consumers subscribe to the spine over a one-directional `text/event-stream` endpoint (`/sessions/<id>/stream`); the console reads it natively with `EventSource`. No WebSockets — the trace pane is server→client only, which is exactly SSE's shape.

**Canonical event set (v1):** `trigger.fired`, `message.received`, `context.built`, `model.invoked`, `model.responded`, `tool.called`, `tool.result`, `approval.requested`, `approval.resolved`, `operator.injected`, `message.sent`, `turn.completed`, `error.raised`. Every event carries `session_id`, `turn_id`, `agent_version`, and a monotonic sequence number. This contract is the platform's most important API — versioned and stable.

## 6. The building experience

An agent is one file:

```yaml
# agents/support.yaml
name: support
model: anthropic/claude-sonnet-4-6        # provider-agnostic
instructions: ./support.md
adapters:
  channels: [webchat, teams]
  tools: [lookup_invoice, create_ticket, refund]
triggers:
  - schedule: "0 8 * * 1-5"          # every weekday 08:00 — pull trigger
    message: "Summarize overnight invoices and flag anything unpaid."
  - poll: mailbox                     # pull trigger: poller adapter
    every: 60s
    on_new: "Handle this incoming invoice email."
  # push triggers (webhook, MCP, inbound email) need no entry here —
  # they arrive through mounted channel adapters
policies:
  - tool: refund
    requires_approval: true
    budget: { max_eur_per_day: 500 }
memory:
  session: auto        # summarized rolling context, on by default
```

A tool is one directory: schema + handler, testable in isolation:

```yaml
# tools/lookup_invoice/tool.yaml
name: lookup_invoice
description: Look up an invoice by number; returns status and amount.
input:  { invoice_number: string }
output: { status: string, amount_eur: number }
handler: ./handler.py
```

The dev loop is the heart of "easy":

```
heddled dev                              # runs the agent locally, hot-reloads on file change,
                                      # opens the console with a live trace pane
heddled tool test lookup_invoice --args '{"invoice_number":"F-2231"}'
heddled chat support "where is invoice F-2231?"   # scripted turn from the terminal
```

While you chat with your agent in the dev console, the trace pane shows every event in real time — the exact context sent to the model, every tool call with arguments and result, timing, tokens. Debugging is not a separate mode; it is the default view. This alone kills the biggest Copilot Studio frustration.

**Authoring, not just editing.** The files above are the source of truth, but nobody should have to hand-write the first one. Every object Heddled owns has a scaffold behind it, reachable from both surfaces:

```
heddled new agent support --model anthropic/claude-sonnet-4-6
heddled new tool lookup_invoice --input invoice_number:string --output status:string,amount_eur:number
heddled new policy support --tool refund --requires-approval
```

Each writes the same files the console writes, and the console's **New agent** / **New tool** buttons call the same scaffolds. Copying an existing object (`heddled new agent triage --from support`) is the other half of this: most second agents are a variation on the first, and starting from a blank YAML file is a tax nobody should pay twice.

Because the registry re-reads from disk on every access, a change made in either place is live on the next turn — no restart, no deploy step, no reload button. The dev loop and the authoring loop are the same loop.

**Level 3 (Python API):** when the default loop isn't enough — custom planning, parallel tool fan-out, sub-agent orchestration — you implement the agent in Python against the same interfaces. It still emits the same events, mounts the same adapters, obeys the same policies, and appears in the same console. The platform can't tell the difference, and neither can an external caller.

## 7. Triggers — what starts a turn

Everything upstream of `message.received` is a trigger. There are two kinds, and the split matters because they have different lifecycles.

**Push triggers** are external — something else decides when to fire and calls in. A webhook POST, an MCP call from Copilot Studio, an inbound email from a provider that POSTs to you: these are just **inbound channel adapters** receiving a message. They already exist in the model; nothing new is needed. The active party is outside Heddled.

**Pull triggers** are Heddled-driven — *Heddled itself* is the active party. Two sub-types cover almost everything:

- **Schedule** — fire on cron (`0 8 * * 1-5`). The turn starts with a fixed or templated message. This is how you get "every weekday at 8, summarize overnight invoices."
- **Poll** — check a source on an interval (a mailbox, a queue, a folder, an API) and start a turn for each new item found. This is how you get "when an invoice email arrives, handle it" without that source having to know Heddled exists.

Pull triggers are what's genuinely new, and they carry a lifecycle wrinkle worth stating plainly: **a poller is stateful and long-running.** It must remember what it has already processed (a cursor: last email UID, last queue offset, last-seen timestamp) so it doesn't reprocess on the next tick or after a restart, and it must survive restarts. That's a different shape from a request-driven adapter that lives and dies inside one turn.

**Where they run.** Pull triggers live in the **same background worker** that already drains the turn queue — it's the natural home for the active, always-on party. The scheduler ticks on cron; each poller wakes on its interval, does its check, and for each new item enqueues a turn exactly as a webhook would. The cursor is persisted in SQLite alongside events and sessions, so a restart resumes where it left off. When a trigger fires it emits `trigger.fired` (carrying what fired it and why) as the first event of the session, so a scheduled or polled run is as traceable and replayable as a user-driven one — you can see in the console exactly which cron tick or which new email started a given turn.

**Reliability semantics** are a per-trigger choice: at-least-once (fire, and only advance the cursor after the turn is durably enqueued — safe default, may double-process on crash) vs. at-most-once (advance first). Idempotency is the agent's concern for now; a dedup key on `trigger.fired` is the natural hook if it becomes a problem.

This keeps the whole surface honest: **push triggers are channel adapters, pull triggers are the worker doing the calling.** No new core concept — just a named home for the active-party case the model didn't yet cover.

## 8. Operating: headless by design

Heddled is infrastructure, not a frontend. At scale, end users meet an agent where they already are — Teams, a webhook consumer, whatever channel it lives on — and approvers are followed up by an adapter rather than asked to log in. A paused turn is always followed up by an adapter.

That holds for production traffic. It does not hold for the colleague who wants to try the thing you just built, or for a small team that has no channel infrastructure to integrate with yet. For those, §9 describes a minimal chat surface: opt-in per agent, accounts issued by an admin, and no console access whatsoever. It is a way in, not a frontend to brand and ship.

- **Approval routing.** A tool flagged `requires_approval` pauses the turn and emits `approval.requested`. An *approval adapter* delivers it wherever the approver already works — a Teams/Slack card, an email with signed approve/deny links, a webhook into an existing ticketing flow — carrying the proposed action, exact arguments, and context. The answer returns inbound as `approval.resolved` and the turn resumes. The generic webhook approval adapter is the reference implementation; everything else is a nicer skin over the same two events.
- **The console is for builders and admins only.** Session list, trace drill-in, replay, deployments, policy management — mission control, not a contact-center UI. It carries an approval view purely as a fallback consumer of the same events. Nobody who is not designing or administering agents should ever reach *the console* — which is a statement about the console, not about the platform having no door for anyone else.
- **Takeover is a primitive, not a product.** `operator.injected` stays in the event contract (it's just another inbound adapter). The console surfaces it for designers diagnosing a stuck session; teams that want human handoff for end users build that surface in *their* frontend on top of the primitive.
- **Replay** — step through any completed turn event by event. Because the exact model context is captured in `context.built`, you can re-run a turn against a modified agent version and diff the behavior.

## 9. Console: how the GUI is organized

The console serves exactly two personas — builder and reviewer/admin — and its design follows from principles already decided: the trace is the product, files-first, vanilla stack. Ground rules:

- **One component to rule them: the trace view.** An event timeline plus a detail pane, built once and reused in four contexts: the dev harness live pane, a running session's live view, historical replay, and eval diffs. If the trace view is good, the whole console is good.
- **Server-rendered pages, vanilla-JS islands.** Flask/Jinja renders full pages; JavaScript is progressive enhancement (the SSE feed, JSON viewers, keyboard navigation). No SPA framework, no build step — view-source honesty, matching the stack decision.
- **Everything deep-linkable.** `/sessions/<id>#e-1234` addresses one event in one session. For an infrastructure tool, links pasted into tickets and Teams threads *are* the distribution mechanism; any state worth looking at gets a URL.
- **Inspection by default, authoring when you want it.** Most of the console is read-only, because most of the time you are looking at what happened rather than changing what will happen. But the write paths that exist are *complete*: create, edit, clone, and delete an agent, a tool, or a policy; edit settings; resolve a fallback approval; promote a deployment or golden trace. Anything a YAML file can express, the console can author. Every edit screen is a view over the file on disk with a **raw file** toggle, and saving writes that file. `git diff` stays the truth.
- **Density over decoration.** Tables, monospace payloads, timestamps. No vanity dashboard: the home page is the agents list plus a thin health strip (worker alive, queue depth, errors last hour) — the three numbers an admin actually checks.

Six top-level screens:

| Screen | Purpose | Key elements |
|---|---|---|
| **Agents** (home) | definitions | List with version + deploy state per environment, and a **New agent** button (blank or cloned from an existing one). Detail: a structured editor for model, instructions, adapters, triggers and policies, with a raw-YAML toggle; recent sessions; latest eval result. A **Test** tab opens the dev-harness chat beside a live trace pane. |
| **Sessions** | the operational heart | Filterable list (agent, channel, trigger origin, status: running / waiting-approval / ended / error). Drill-in is the trace view — live over SSE for running sessions, replay with step controls for ended ones. |
| **Tools** | the capability library | Every tool in the registry, which agents mount it, and when it was last exercised. Detail has three tabs: **Schema** (the input/output contract), **Handler** (the Python, editable), and **Test** — sample arguments in, real result and `ctx.log` output back, the same path as `heddled tool test`. **New tool** scaffolds the directory, manifest and handler. |
| **Evals** | regression | Golden traces, runs per agent version; a result opens the trace view in diff mode. |
| **Deployments** | promote | Agent-version × environment matrix; promotion gated on a green eval run. |
| **Settings** | admin | Model providers and keys, MCP caller credentials, retention knobs, OTel export, global policies, and the git-commit-on-save toggle. |

Tools get a screen of their own rather than living under the agent that mounts them, because the registry is global: one tool is typically mounted by several agents, and editing it from inside one of them hides that fact at exactly the moment it matters. The tool detail page names every agent affected by a change before you save it.

Approvals deliberately get **no top-level screen** — they are out-of-Heddled (§8). The fallback surface is the `waiting-approval` filter on Sessions, with a badge count in the nav. If that badge is nonzero for long, the fix is a better approval adapter, not a better inbox.

**Dev mode is the same app.** `heddled dev` serves this exact console locally with the agent opened on its Test tab. There is no separate dev UI to build or maintain, and dev/staging/prod showing identical screens is itself a debugging feature — what you saw locally is what you'll see in production.

**The form and the file are the same object.** Every authoring screen renders two views of one YAML document, and the tab you are on is a display preference, not a mode:

- The **form** knows the schema. Model is a dropdown of configured providers; channels and tools are pickers over what actually exists; policies, triggers and budgets are rows you add and remove. It cannot express something the schema disallows, which is most of the value — the common mistakes (a tool that isn't mounted, a cron field that can't fire, a policy naming a tool that was renamed) are unreachable rather than diagnosed.
- The **raw file** is the whole YAML, always one click away, always editable. Anything the form does not yet cover is reachable here, so the form never becomes a ceiling.

Switching tabs round-trips through the file: the form is parsed from it and serialised back to it, preserving comments and key order. If a hand-written file contains something the form cannot represent, the form says so plainly and stays read-only for that section rather than silently dropping it on save. A form that quietly rewrites your file is worse than no form at all.

Saving always shows the diff that is about to be written, and validation runs before the file is touched — an invalid definition is rejected with the line and a suggestion, never half-written. Deleting an agent or tool warns about what depends on it first.

**Git is optional, and off by default.** A `commit_on_save` setting makes each console write a commit with a generated message (`support: require approval for refund`), which turns the file history into the change log and makes "who changed this and when" a `git log` rather than a feature. It ships off, because taking over someone's working tree without asking is not convenience — it is a surprise. Heddled writes files; whether those files become commits is the operator's call.

**Trace view anatomy** (specified once, since everything reuses it): left, the event timeline — color-coded by event type, auto-appending over SSE when live, scrubbable in replay; right, the detail pane for the selected event — pretty-printed payload, the full `context.built` when applicable, tool arguments and result, duration, tokens; top, the turn header — session, agent version, and trigger origin (which cron tick, which email, which MCP caller started this). Keyboard: `j`/`k` to step events, `Enter` to expand. In diff mode, two timelines render side by side, aligned by event sequence, highlighting from the first divergent tool call onward.

### Talking to an agent without operating one

The console is mission control. But an agent only its author can talk to is not
finished, and "wire up Teams first" is too high a wall to put in front of showing
a colleague what you built. So there is a second surface, and it is deliberately
small.

**Opt in per agent, in the file.** `expose: { chat: true }` alongside the existing
`expose: { mcp: true }` — so turning it on is a diff somebody can review, and no
upgrade ever opens a door on an install that did not ask for one. Off by default.

**No new kind of account.** Chat is open to anyone who can already sign in;
`viewer` is the lowest role and is enough. A fourth "guest" role was considered
and rejected: a second identity system is a second set of guards to get right,
and every parallel privilege model eventually grows a hole where the two meet.
One account type, one guard, one place to revoke.

The consequence is worth stating plainly rather than discovering: **an account is
an account.** A viewer added so they can chat can also read the console —
every agent, every conversation, every trace. That is the existing trust model,
unchanged, and it is the right trade for a small team where accounts go to people
you already trust. If you need somebody to reach one agent and nothing else, the
answer is the one it has always been: route them through an adapter — Slack,
Teams, a webhook — and do not give them an account at all.

**Chat is its own channel.** The Test tab stays `webchat`; the chat surface is a
separate channel, `chat`. This matters because `allow_channels` and
`deny_channels` are security controls, and an operator trying something in the
console is not the same context as somebody typing into a chat box. Two channel
names lets a policy say so: `refund` usable from the console, never over `chat`.
One name would make that inexpressible.

**A pause is shown, not hidden.** When a turn stops for approval the person is
told it is waiting for someone, and told again when it resolves. Silence would be
the easy implementation and the wrong one — the pause is the product working, and
it is the one thing this surface can show that a chat box bolted onto a model
cannot.

**Tokens stream.** Waiting in silence for eight seconds and then being handed a
finished paragraph reads as broken; the same words arriving as they are generated
read as thinking. Providers grow an optional `stream()` that yields text deltas,
and the default implementation falls back to `complete()` so a provider without
streaming still works — it simply arrives all at once.

Deltas are **not events**. The spine stays thirteen event types, and
`model.responded` is still emitted once with the finished text — putting every
token on the audit log would multiply the event store by three orders of
magnitude to record something no one will ever query. Instead deltas ride the
existing subscriber fan-out as ephemeral broadcasts: same SSE connection, never
persisted, and a reader that misses them still reconstructs the whole
conversation from the events alone.

What this is not: a frontend to brand, theme, embed, or ship to customers. No
logo upload, no CSS hooks, no widget snippet. Teams who want a customer-facing
chat build it on the API, and that answer does not change.

### A folder an agent may work in

Some work is file work: a folder of exports to summarise, a report to write out
for somebody to collect. An agent can be given a workspace — one directory —
and three tools that reach it and nothing else.

**Opted into per agent**, in the file: `workspace: true` for one of its own at
`work/<name>`, or a path to somewhere that already exists. Off by default, so no
install gains one on upgrade.

**Where it points is the operator's choice. What it can reach is not.** None of
the confinement is expressible in an agent file, and that asymmetry is the
design. A path is resolved before it is used and must land inside the root, so a
symlink out is refused rather than followed; `..` and absolute paths are refused
on sight as well.

**`agents/`, `tools/`, `data/` and the project root are refused
unconditionally**, whatever the workspace says. This is the one rule that
carries weight: agents are *files*, and an agent that can rewrite
`agents/support.yaml` can delete the approval gate that constrains it. No policy
fixes that, because the policy is the file — so it is closed before any policy
is consulted.

**Three tools, not one with a mode.** `list_files`, `read_file`, `write_file`.
Policy has to be able to tell them apart: reading wants to be ungated so the
agent gets on with the job, while writing wants `requires_approval` and no
availability on the chat channel. A single tool with an `operation` argument
would make that distinction inexpressible — the same reasoning that gives the
chat surface its own channel name.

**Built into the platform, not left to a handler.** If every operator wrote
their own `read_file` they would each write their own path check, and one of
them would get it wrong. Written once, tested once — most of
`tests/test_workspace.py` is attempts to escape — and every agent with a
workspace inherits it.

**Documents, because "write a report" means a .docx.** A model emits text, so
an assistant writes Markdown and the extension decides what is made: `.docx`,
`.xlsx` and `.pptx` become real files, with headings as headings and numbers as
numbers. Reading works the other way — Word and Excel are unzipped and their
text pulled out.

None of that adds a dependency. All three formats are zip archives of XML, and
`zipfile` with the stdlib parser makes them; python-docx and python-pptx would
have pulled lxml, and python-pptx Pillow as well, which is a poor trade for a
platform whose five third-party packages each carry a comment justifying
themselves. The tests hand what is produced to those libraries anyway, as a
dev-only check — a package that unzips and parses is not necessarily one Word
will open.

PDF is the exception and stays optional, because extracting text from one
genuinely needs a library. What can be read is computed from what is installed
rather than declared, so a file list never marks something readable that the
read would refuse. No delete — overwriting is recoverable and deleting is not.

What this is **not** is a sandbox. Handlers run in this process, so a Python
tool written by hand can still read whatever the process can; this makes the
*built-in* file tools safe and does not pretend to isolate the platform from
itself. Nor is there a shell, or network access, or any way for one agent to
reach another's workspace.

**Managed from the agent's own page.** The workspace is listed there with what
is in it, and an operator can view a text file, download any file, add one, or
delete one. Deleting is a person's decision on a screen with a confirmation and
deliberately not a tool a model can reach for: overwriting is recoverable and
deleting is not.

Downloads are always `attachment`, with a content type that is not html and
`nosniff`. This route serves whatever somebody put in the folder, from the
origin that holds the administrator's session — served inline, an uploaded
`.html` would run as a page on that origin. Uploaded names go through the same
path check as everything else, because a filename from a browser is a string
somebody else chose.

Reading the panel is console access, which viewers already have. Adding and
removing are writes, and the read-only rule covers them with no exemption —
unlike chatting and approving, putting a file in really is changing something.

One consequence to hold on to: an agent that reads untrusted files and also
holds a consequential tool is a combination worth gating. Typed tools have been
the ceiling on prompt injection — an email cannot make an agent do something it
has no tool for — and a workspace does not remove that ceiling, but it does put
attacker-controlled text in front of whatever else the agent can already do.

## 10. Trust layer

Policies are declarative and attach to agents, tools, or environments: per-tool allow/deny per channel, approval gates, spend and token budgets per session/day, rate limits, and PII redaction rules applied at the trace-store boundary (operate on data, store the redacted form). Every approval, takeover, and policy denial is on the spine, so the audit log is not a feature — it's a query.

## 11. Evals: closing the loop

The trace store makes regression testing nearly free. Any recorded session can be promoted to a **golden trace**. An eval run replays the recorded inbound messages against a candidate agent version with tools in mock mode (recorded results played back) and reports: did the agent call the same tools with equivalent arguments, and does the final answer pass assertions (exact, contains, or LLM-judged)? `heddled eval run --against v14` becomes the gate before promoting a deployment — you change the prompt on Tuesday and know by Tuesday what broke.

## 12. Multi-agent — inside and outside the walls

**Inside:** an agent is mountable as a tool on another agent. That's the whole composition model: delegation is a `tool.called` event whose handler is another agent's turn engine, sub-sessions link to parent sessions, and the trace view renders the tree. No new orchestration language — the spine already expresses it.

**Outside:** the same idea crosses the platform boundary in both directions via MCP.

- **Consume (MCP client):** a tool adapter can wrap any third-party MCP server, so its tools mount onto an agent like native ones.
- **Expose (MCP server):** every agent can publish *itself* as an MCP server:

```yaml
# agents/support.yaml (addition)
expose:
  mcp: true            # serves ask_support (+ session continuation) at /mcp/support
```

An external orchestrator — Copilot Studio, Claude, an IDE, another Heddled node — sees one typed tool carrying the agent's name, description, and input schema. The call lands as a channel adapter and becomes `message.received`; the full spine applies.

Why this matters:

1. **Adoption without replacement.** Copilot Studio and most major orchestrators speak MCP. A Heddled agent can be dropped into an existing Copilot Studio setup as a tool — Heddled becomes the engine behind someone else's frontend. You don't have to win a migration argument to be used.
2. **Governance travels with the agent.** External calls still hit policies, budgets, approval gates, and the trace store. Even when a foreign orchestrator drives your agent, *your* rules and *your* audit log apply. No other platform offers this.
3. **Federation for free.** Heddled→Heddled across nodes is just MCP expose + MCP consume; no bespoke clustering protocol.

Design considerations:

- **Sessions over a stateless surface:** the exposed tool accepts an optional `session_id` and returns one, so stateful callers get multi-turn continuity and stateless callers just omit it.
- **Caller identity:** API keys/OAuth per external caller; policies can key on caller (e.g. Copilot Studio may call `lookup_invoice`-backed answers but anything touching `refund` still pauses for approval).
- **Approval gates vs. synchronous calls:** a turn paused for human approval can outlive a caller's tool timeout. Needs an async pattern — progress notifications while paused, or an immediate `pending` result plus a poll/continuation tool.
- **Loop protection:** propagate a call-chain in event metadata (`via: [copilot-a, heddled/support]`); enforce depth limits and cycle detection so A→B→A dies fast and visibly in the trace.

## 13. Non-goals (v1)

No visual flow/topic builder — instructions + tools + policies over canned dialog trees. Note the distinction from §9: structured *forms* over an agent's own fields are in, because they are a typed view of a file you could have written by hand; a *canvas* on which conversation flow is wired together is out, because it invents a second language for something the model already does. No connector marketplace (adapters are code; a community repo can come later). No built-in vector database (memory/RAG is a tool adapter interface, bring your own). No fine-tuning. No multi-tenant SaaS billing.

## 14. MVP plan

**Phase 0 — the spine (prove the thesis).**
Turn engine, in-process event bus, the canonical event contract. Tool artifact format + handler runtime. Two channel adapters: REST webhook and a web-chat *dev harness* (test surface only — never a product frontend). `heddled dev` with the live trace pane. Anthropic + OpenAI-compatible providers.
*Done when:* you define an agent + two tools in files, chat with it locally, and watch every event live.

**Phase 1 — operate.**
Persistent trace store (SQLite), session list, historical replay, approval gates with the webhook approval adapter, takeover primitive. Pull triggers in the background worker: cron scheduler + one poller (mailbox), with persisted cursors and `trigger.fired` on the spine. `docker compose up` self-host story.
*Done when:* an agent with a `requires_approval` tool runs on the webhook channel and a human approves the action through the approval adapter — without ever opening the console; and a cron-scheduled agent fires on its own and its run is fully traceable.

**Phase 2 — trust and iterate.**
Policies (budgets, rate limits, redaction), environments + `heddled deploy`, golden traces + eval runner, one "real" channel adapter (Teams or Slack).
*Done when:* you can change an agent, run evals against recorded traffic, and promote to prod with a diff you trust.

**Phase 2.5 — authoring.**
Scaffolds (`heddled new agent|tool|policy`, and `--from` to clone) shared by CLI and console. Structured editors for agents and tools with a raw-file toggle, schema validation and a diff preview before write. The Tools screen: registry list, schema/handler editing, and an isolated test panel. Optional commit-on-save.
*Done when:* someone who has never seen a Heddled YAML file can create an agent, give it a new tool, test that tool in isolation, and gate it behind an approval policy — entirely from the browser — and the resulting `git diff` is a file a hand-author would have written.

**Phase 3 — compose.** Agents-as-tools internally, MCP in both directions (client tool adapter + `expose: mcp` per agent), sub-session trace trees, NATS bus option for multi-node.
*Done when:* a Copilot Studio agent calls a Heddled agent as an MCP tool, the call shows up in the trace store, and a `requires_approval` action from that external call pauses for an operator.

## 15. Decisions

Resolved 2026-08-01:

1. **Runtime: Python + Flask.** One language for core, tool/adapter handlers, and the programmatic agent API. Chosen for build velocity in a familiar stack over the theoretical headroom of a compiled runtime; per-turn overhead only matters at Phase 3 multi-node scale, which is far off and a good problem to have.
2. **Event & state store: SQLite** in WAL mode, append-only events table. One writer appending, several readers streaming — exactly SQLite's comfort zone. The event contract is the API; the transport stays boring. Postgres becomes a drop-in only when multi-node genuinely demands it.
3. **Concurrency & live trace.** Turns run in a **background worker off a SQLite-backed job queue** so they survive their originating request (required by the out-of-Heddled approval flow). Flask threaded workers serve the JSON API and the SSE trace stream; turns are I/O-bound so threads suffice and all code stays synchronous — no asyncio in v1. The live trace pane is **Server-Sent Events** (server→client only); no WebSockets.
4. **Context capture: store everything, compress, defer cleverness.** The full `context.built` payload is persisted per model call, zstd-compressed, in SQLite — replay and evals depend on it and are worth the disk. A retention knob (`keep_full_context: 90d`) ships in v1; content-addressed dedup of prompt segments is added only when storage measurably hurts. Correctness first.
5. **Approvals are out-of-Heddled by design.** A paused turn is always followed up through an adapter: `approval.requested` routes out to where the human already works, `approval.resolved` comes back in (see §8). Heddled never needs to be opened to approve anything.
6. **License: fully open source.** Apache-2.0 preferred over MIT for the patent grant, which matters for an adapter ecosystem; final call at repo creation.
7. **MCP depth:** both directions, tools-first; resources/prompts/elicitation deferred; long-running turns over MCP handled with pending + continuation (per §12). OpenTelemetry export for the spine as a consumer.
8. **Console GUI:** server-rendered Flask/Jinja with vanilla-JS islands — no SPA, no build step. One reusable trace-view component across dev harness, live sessions, replay, and eval diff. Six top-level screens (Agents, Sessions, Tools, Evals, Deployments, Settings); everything deep-linkable; approvals surface only as a Sessions filter, never a top-level screen (see §9).

Resolved 2026-08-02:

9. **Authoring is a first-class console capability.** "Files first" governs where truth lives, not how much the UI may do, and a console you cannot create an agent from fails the "ship an agent in an afternoon" promise for anyone who does not already have the YAML memorised. So: structured forms over the agent and tool schemas, a raw-file toggle beside every one of them, scaffolds (`heddled new …` and the matching buttons) shared between CLI and UI, and a Tools screen so shared capabilities are edited where their blast radius is visible. Round-tripping preserves comments and key order, and anything the form cannot represent is surfaced rather than silently dropped. Committing on save is offered and defaults to off.

Resolved 2026-09-04:

10. **Jarvis is an admitted drift, and is fenced as one.** Everything above says an agent does what a person decided it may do; Jarvis writes its own tools, writes its own agents, runs them, and continues until it says it is finished. That contradicts §2 and it is worth saying so plainly rather than reframing it. It ships because the question "what would this goal actually take?" is worth being able to ask, and because a single-operator Heddled is the one place where asking it is nobody else's risk.

    What makes it defensible is not a warning banner. It is that the three properties the rest of the platform earns are kept by construction rather than by instruction: it writes into a **separate tree** read through a second registry, so "it cannot edit your policies" is a fact about which directories exist and not a rule it is asked to follow; it may **invoke** the operator's agents but never write to them, on its own channel (`jarvis`) so a policy can refuse it by name, with every approval gate and budget on those agents still applying; and nothing it makes reaches the operator's estate until a person **promotes** it one thing at a time, which is the approval gate applied to creation instead of to action. Python it writes runs in a child process with no keys, no store and no path back in — and keeps doing so after promotion, because promoting means somebody wanted it, not that a person wrote it.

    A **conversation** is the unit rather than an agent: you answer every turn, which is why there is no step cap to set, and the budget is the rail. It keeps notes as Markdown files whose one-line summaries ride into every conversation and whose bodies are read on demand. And it cannot write a trigger — a schedule means running with nobody there, so that stays a person's decision, made on the agent's own page after promotion. Nothing here is on by default, and the screen is admin-only whether or not it is.

Nothing remaining blocks Phase 0.

---

*Next step: build Phase 0 as a walking skeleton — one agent, one tool, live trace pane. Everything in this document should be testable against that skeleton within a few weeks of evenings.*
