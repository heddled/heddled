"""Tools without writing code.

A tool used to be a directory with a Python handler, which quietly requires a
programmer for the single most common thing anyone wants to do: call an API,
send a message, look something up in a list. Most organisations do not have a
spare Python developer for that, and the ones that do should not have to spend
them on it.

So a tool may declare a `type:` instead of a `handler:`. The type is filled in
from a form, the platform builds the handler, and the engine cannot tell the
difference — same schema, same validation, same `tool.called` / `tool.result`
events, same policies. Dropping to Python stays available for anything these do
not cover; it is an escape hatch, not the entry fee.

    name: lookup_invoice
    description: Look up an invoice by number.
    input:  { invoice_number: string }
    type: http
    config:
      method: GET
      url: https://api.example.com/invoices/{invoice_number}
      headers:
        Authorization: "Bearer {{secret.billing_api_key}}"

Two kinds of placeholder, deliberately different so they cannot be confused:
  {field}            — a value from the tool's own arguments
  {{secret.name}}    — a value from Settings, never written into the file
"""

from __future__ import annotations

import json
import re
import smtplib
from email.message import EmailMessage
from typing import Any, Callable

import requests

FIELD_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
SECRET_RE = re.compile(r"\{\{\s*secret\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
HOST_RE = re.compile(r"https?://([^/?#]+)")

DEFAULT_USER_AGENT = "heddled-agent/0.1 (+https://github.com/rbarendse/heddled)"


def _host_of(url: str) -> str:
    match = HOST_RE.match(url or "")
    return match.group(1) if match else (url or "")[:60]


def guard_destination(url: str, settings: dict) -> None:
    """Refuse requests to the machine itself and to private networks.

    The realistic attack is not a malicious colleague — anyone who can write a
    tool can already write Python. It is **the model choosing the destination**:
    a tool whose URL is templated (`{target}`), pointed at content Heddled ingested
    from outside (a polled mailbox, a webhook), lets an injected instruction
    steer the request. Cloud metadata endpoints and internal admin interfaces
    are the usual targets, and they normally trust anything that can reach them.

    Set `allow_internal_http` when a tool is genuinely meant to reach a service
    on your own network.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    if settings.get("allow_internal_http"):
        return

    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        raise ToolTypeError(f"only http and https addresses are allowed, not '{parsed.scheme}'")
    host = parsed.hostname
    if not host:
        raise ToolTypeError("that address has no host")

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise ToolTypeError(f"could not find '{host}'")

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast):
            raise ToolTypeError(
                f"'{host}' is on a private or internal network, which tools may not "
                f"reach. Turn on allow_internal_http in Settings if that is "
                f"deliberate."
            )


class ToolTypeError(ValueError):
    """A misconfigured no-code tool. Worded for whoever filled in the form."""


# --------------------------------------------------------------- substitution


def fill(template: Any, args: dict, settings: dict) -> Any:
    """Resolve {field} and {{secret.name}} anywhere in a config value."""
    if isinstance(template, dict):
        return {k: fill(v, args, settings) for k, v in template.items()}
    if isinstance(template, list):
        return [fill(v, args, settings) for v in template]
    if not isinstance(template, str):
        return template

    def secret(match):
        key = match.group(1)
        value = settings.get(key)
        if value is None:
            raise ToolTypeError(
                f"this tool needs a secret called '{key}', which is not set — "
                f"add it under Settings"
            )
        return str(value)

    out = SECRET_RE.sub(secret, template)

    def field(match):
        key = match.group(1)
        if key not in args:
            raise ToolTypeError(f"no value was given for '{key}'")
        return str(args[key])

    return FIELD_RE.sub(field, out)


def _redact_secrets(value: Any, settings: dict) -> Any:
    """Never let a resolved secret reach the trace store."""
    secrets = [str(v) for v in settings.values() if isinstance(v, str) and len(v) >= 8]
    if not secrets:
        return value
    text = json.dumps(value, default=str)
    for s in secrets:
        text = text.replace(s, "«secret»")
    return json.loads(text)


# ------------------------------------------------------------------ builders


def _http(config: dict) -> Callable:
    url = config.get("url")
    if not url:
        raise ToolTypeError("an HTTP tool needs a URL")

    def handle(args: dict, ctx):
        settings = dict(getattr(ctx, "settings", {}) or {})
        method = str(config.get("method", "GET")).upper()
        resolved = fill(url, args, settings)
        headers = fill(config.get("headers") or {}, args, settings)
        # Plenty of public APIs reject an unidentified client, and "403" is a
        # baffling first experience for someone who just filled in a URL.
        if not any(k.lower() == "user-agent" for k in headers):
            headers["User-Agent"] = settings.get("http_user_agent") or DEFAULT_USER_AGENT
        timeout = float(config.get("timeout_s", 30))

        body = config.get("body")
        json_body = None
        if body:
            json_body = fill(body, args, settings)
        elif method in ("POST", "PUT", "PATCH") and config.get("send_arguments", True):
            json_body = args

        query = fill(config.get("query") or {}, args, settings)

        guard_destination(resolved, settings)
        ctx.log(f"{method} {_host_of(resolved)}")
        response = requests.request(
            method, resolved, headers=headers, params=query or None,
            json=json_body, timeout=timeout,
        )

        if response.status_code >= 400:
            # A failed call is a normal outcome the model should see and can
            # explain, not a crash.
            return {
                "ok": False,
                "status": response.status_code,
                "error": response.text[:500],
            }

        try:
            payload = response.json()
        except ValueError:
            payload = {"text": response.text[:4000]}

        path = config.get("result_path")
        if path:
            for part in str(path).split("."):
                if isinstance(payload, dict):
                    payload = payload.get(part)
                elif isinstance(payload, list) and part.isdigit():
                    payload = payload[int(part)] if int(part) < len(payload) else None
                else:
                    payload = None
                if payload is None:
                    break

        return _redact_secrets({"ok": True, "status": response.status_code,
                                "result": payload}, settings)

    return handle


def _fixed(config: dict) -> Callable:
    """Always returns the same thing. The fastest way to see an agent work
    before any real system is wired up, and a stand-in while you wait for
    someone else's API."""
    value = config.get("value", config.get("result", {}))

    def handle(args: dict, ctx):
        settings = dict(getattr(ctx, "settings", {}) or {})
        return fill(value, args, settings) if isinstance(value, (str, dict, list)) else value

    return handle


def _lookup(config: dict) -> Callable:
    """A small table kept in the tool itself — office locations, escalation
    contacts, opening hours. The things that live in a spreadsheet nobody wants
    to build an API for."""
    table = config.get("table") or {}
    if not isinstance(table, dict):
        raise ToolTypeError("a lookup tool needs a table of key → value pairs")
    key_field = config.get("key")
    default = config.get("default")

    def handle(args: dict, ctx):
        key = args.get(key_field) if key_field else next(iter(args.values()), None)
        if key is None:
            raise ToolTypeError("no lookup key was given")
        asked = str(key).strip().lower()

        for candidate, value in table.items():
            if str(candidate).strip().lower() == asked:
                return {"found": True, "key": key, "value": value}

        # People ask for "the finance team", not "finance". A table someone
        # typed into a form should tolerate that rather than answer "not found"
        # to a question a human would have understood.
        for candidate, value in table.items():
            entry = str(candidate).strip().lower()
            if entry and (entry in asked or asked in entry):
                return {"found": True, "key": key, "matched": candidate, "value": value}

        return {"found": False, "key": key,
                "value": default,
                "message": f"'{key}' is not in the list",
                "options": list(table)[:20]}

    return handle


def _template(config: dict) -> Callable:
    """Format the arguments into a sentence. Useful for turning structured data
    into something the agent can say."""
    text = config.get("text")
    if not text:
        raise ToolTypeError("a text tool needs some text")

    def handle(args: dict, ctx):
        settings = dict(getattr(ctx, "settings", {}) or {})
        return {"text": fill(text, args, settings)}

    return handle


def _webhook(config: dict) -> Callable:
    """POST somewhere — a Teams/Slack incoming webhook, Zapier, Make, or an
    internal endpoint. The most common 'do something in another system' without
    an API client."""
    url_ref = config.get("url")
    if not url_ref:
        raise ToolTypeError("a webhook tool needs a URL")

    def handle(args: dict, ctx):
        settings = dict(getattr(ctx, "settings", {}) or {})
        url = fill(url_ref, args, settings)
        guard_destination(url, settings)
        payload = fill(config["payload"], args, settings) if config.get("payload") else args
        ctx.log("sending")
        response = requests.post(
            url, json=payload, headers=fill(config.get("headers") or {}, args, settings),
            timeout=float(config.get("timeout_s", 30)),
        )
        return {"ok": response.status_code < 400, "status": response.status_code}

    return handle


def _email(config: dict) -> Callable:
    """Send mail through the SMTP server configured in Settings."""

    def handle(args: dict, ctx):
        settings = dict(getattr(ctx, "settings", {}) or {})
        host = config.get("host") or settings.get("smtp_host")
        if not host:
            raise ToolTypeError(
                "no mail server is configured — set smtp_host, smtp_user and "
                "smtp_password under Settings"
            )
        message = EmailMessage()
        message["To"] = fill(config.get("to") or args.get("to", ""), args, settings)
        message["From"] = fill(config.get("from") or settings.get("smtp_from", ""),
                               args, settings)
        message["Subject"] = fill(config.get("subject") or args.get("subject", ""),
                                  args, settings)
        message.set_content(fill(config.get("body") or args.get("body", ""), args, settings))

        port = int(config.get("port") or settings.get("smtp_port") or 587)
        ctx.log(f"sending to {message['To']}")
        with smtplib.SMTP(host, port, timeout=float(config.get("timeout_s", 30))) as server:
            if config.get("starttls", True):
                server.starttls()
            user = config.get("user") or settings.get("smtp_user")
            password = config.get("password") or settings.get("smtp_password")
            if user and password:
                server.login(user, password)
            server.send_message(message)
        return {"sent": True, "to": message["To"]}

    return handle


# --------------------------------------------------------------- the registry

BUILDERS: dict[str, Callable[[dict], Callable]] = {
    "http": _http,
    "fixed": _fixed,
    "lookup": _lookup,
    "text": _template,
    "webhook": _webhook,
    "email": _email,
}

# What the console's tool-type picker shows. Ordered by how often someone
# reaches for them, not alphabetically.
CATALOG = [
    {
        "type": "http",
        "label": "Call an API",
        "blurb": "Fetch or send data over HTTP. The most common way to reach another system.",
        "fields": [
            {"name": "method", "label": "Method", "kind": "choice",
             "choices": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "GET"},
            {"name": "url", "label": "URL", "kind": "text", "required": True,
             "placeholder": "https://api.example.com/invoices/{invoice_number}",
             "help": "Put {field_name} anywhere you want one of the tool's inputs."},
            {"name": "headers", "label": "Headers", "kind": "pairs",
             "help": "For an API key use {{secret.my_key}} and store the value in Settings."},
            {"name": "result_path", "label": "Take this part of the answer", "kind": "text",
             "placeholder": "data.invoice",
             "help": "Optional. Leave empty to give the agent the whole response."},
        ],
    },
    {
        "type": "lookup",
        "label": "Look something up in a list",
        "blurb": "A small table you fill in here — contacts, locations, opening hours.",
        "fields": [
            {"name": "key", "label": "Which input is the key", "kind": "input_field",
             "required": True},
            {"name": "table", "label": "The list", "kind": "pairs", "required": True,
             "help": "One row per entry: what to look up, and what to return."},
            {"name": "default", "label": "If not found, return", "kind": "text"},
        ],
    },
    {
        "type": "fixed",
        "label": "Always return the same thing",
        "blurb": "A stand-in while you wait for the real system. Great for a first test.",
        "fields": [
            {"name": "value", "label": "Return this", "kind": "json", "required": True,
             "placeholder": '{"status": "unpaid", "amount_eur": 249}'},
        ],
    },
    {
        "type": "text",
        "label": "Write a sentence",
        "blurb": "Turn the inputs into a piece of text for the agent to use.",
        "fields": [
            {"name": "text", "label": "Text", "kind": "textarea", "required": True,
             "placeholder": "Invoice {invoice_number} is due on {due_date}.",
             "help": "Put {field_name} anywhere you want one of the tool's inputs."},
        ],
    },
    {
        "type": "webhook",
        "label": "Notify another system",
        "blurb": "POST to a Teams or Slack webhook, Zapier, Make, or anything internal.",
        "fields": [
            {"name": "url", "label": "Webhook URL", "kind": "text", "required": True,
             "placeholder": "https://hooks.slack.com/services/…",
             "help": "Use {{secret.my_webhook}} to keep the URL out of the file."},
            {"name": "payload", "label": "What to send", "kind": "json",
             "help": "Optional. Leave empty to send the tool's inputs as-is."},
        ],
    },
    {
        "type": "email",
        "label": "Send an email",
        "blurb": "Uses the mail server from Settings.",
        "fields": [
            {"name": "to", "label": "To", "kind": "text", "placeholder": "{to}"},
            {"name": "subject", "label": "Subject", "kind": "text",
             "placeholder": "About invoice {invoice_number}"},
            {"name": "body", "label": "Message", "kind": "textarea"},
        ],
    },
]

CATALOG_BY_TYPE = {entry["type"]: entry for entry in CATALOG}


def is_no_code(raw: dict) -> bool:
    return bool(raw.get("type")) and raw.get("type") != "python"


def build_handler(raw: dict) -> Callable:
    """Turn a tool manifest into a callable. Raises ToolTypeError with a message
    aimed at whoever filled in the form, not at a developer."""
    kind = raw.get("type")
    builder = BUILDERS.get(kind)
    if builder is None:
        raise ToolTypeError(
            f"'{kind}' is not a kind of tool Heddled knows. Choose one of: "
            + ", ".join(sorted(BUILDERS))
        )
    return builder(raw.get("config") or {})


def describe(raw: dict) -> str:
    """One line for the Tools list, so a no-code tool is legible at a glance."""
    kind = raw.get("type")
    config = raw.get("config") or {}
    if kind == "http":
        return f"{str(config.get('method', 'GET')).upper()} {config.get('url', '')}"
    if kind == "lookup":
        return f"{len(config.get('table') or {})} entries"
    if kind == "fixed":
        return "always the same answer"
    if kind == "text":
        return (config.get("text") or "")[:60]
    if kind == "webhook":
        return f"POST {config.get('url', '')}"
    if kind == "email":
        return f"to {config.get('to', '—')}"
    return "Python handler"
