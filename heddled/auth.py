"""Console access control.

Heddled binds 0.0.0.0 by default so it is reachable from other machines, and until
now anything that could reach it could do anything: create agents, read every
recorded conversation — which contain whatever customers said — and approve
payments. On a laptop that is fine. On an office network it is not, and the
concept doc's own trust layer (§10) is about what agents may do, not about who
may drive them.

So: an optional console password. It is **off by default**, because a self-hosted
tool that demands credentials before it will start is a tool people abandon
during evaluation, and because `docker compose up` on a laptop genuinely does not
need it. But the console says so, loudly, whenever it is both unset and reachable
from outside this machine.

Deliberately simple: one shared password, a signed session cookie, no user
accounts. Heddled has no notion of people, and inventing half a user system would
be worse than an honest shared secret. Put a real identity provider in front of
it when you need one — that is the normal shape for self-hosted software.

Two paths stay open when a password is set, because they are used by people and
programs that will never have it:

  /approve/<id>?token=…   an approver followed a link from Slack or email; the
                          per-approval token is already their credential
  /mcp/<agent>            external callers authenticate with their own API key
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from functools import wraps

from flask import g, redirect, render_template, request, session, url_for

SETTING = "console_password"
SALT_SETTING = "console_password_salt"

# Paths that never require the console password.
OPEN_PREFIXES = ("/approve/", "/mcp/", "/static/")
OPEN_EXACT = ("/login", "/logout", "/setup", "/api/health")


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 240_000
    ).hex()


def set_password(store, password: str) -> None:
    """Store a password (or clear it with an empty string)."""
    if not password:
        store.execute("DELETE FROM settings WHERE key=?", (SETTING,))
        return
    salt = secrets.token_hex(16)
    store.set_setting(SALT_SETTING, salt)
    store.set_setting(SETTING, hash_password(password, salt))


def check_password(store, password: str) -> bool:
    stored = store.get_setting(SETTING)
    salt = store.get_setting(SALT_SETTING)
    if not stored or not salt:
        return False
    return hmac.compare_digest(stored, hash_password(password or "", salt))


def is_enabled(store) -> bool:
    return bool(store.get_setting(SETTING))


def secret_key(store) -> str:
    """A stable key for signing the session cookie, kept with the rest of the
    state so restarts do not log everybody out."""
    key = store.get_setting("console_secret_key")
    if not key:
        key = secrets.token_hex(32)
        store.set_setting("console_secret_key", key)
    return key


def exposed_beyond_localhost() -> bool:
    """Whether this process is listening on more than the loopback interface.
    Used only to decide how loudly to warn."""
    host = os.environ.get("HEDDLED_HOST", "0.0.0.0")
    return host not in ("127.0.0.1", "localhost", "::1")


def status(store) -> dict:
    """What Settings and the warning banner need to know.

    Accounts replaced the shared password, so protection now means "somebody has
    an account", not "the old console_password setting exists". Reading the dead
    setting made the console shout "this has no password" at every signed-in
    user forever — an alarm that is always on teaches people to ignore alarms.
    """
    from . import users

    protected = users.count(store) > 0 or is_enabled(store)
    return {
        "enabled": protected,
        "exposed": exposed_beyond_localhost(),
        # Only worth shouting about if it is genuinely reachable and unclaimed.
        "at_risk": exposed_beyond_localhost() and not protected,
    }


# Settings Heddled owns. Everything else a person has stored is theirs — an API
# key or a webhook URL a tool refers to as {{secret.name}} — and belongs in the
# Secrets list rather than being invisible.
INTERNAL_SETTINGS = {
    SETTING, SALT_SETTING, "console_secret_key", "commit_on_save",
}


def user_secrets(store, known_keys) -> dict:
    """Named values a person added themselves, for the Secrets list."""
    reserved = INTERNAL_SETTINGS | set(known_keys)
    return {
        key: value for key, value in sorted(store.all_settings().items())
        if key not in reserved
    }


def mask(value) -> str:
    """Show enough to recognise a value, never enough to use it."""
    text = str(value)
    if len(text) <= 8:
        return "••••••••"
    return f"{text[:3]}••••••••{text[-2:]}"



# --- cross-site request forgery -------------------------------------------
# Without this, a page on another site can make your browser POST to Heddled using
# your session — creating agents, approving refunds — because the cookie rides
# along automatically.
#
# Checked by origin rather than by a hidden token in every form: it is one place
# instead of thirty, it cannot be forgotten on a new form, and combined with the
# SameSite=Lax cookie it is the same defence Django falls back to. A cross-site
# POST cannot set Origin to Heddled's own host.
SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")

# Flask's session cookie. Its presence is what makes a request forgeable.
SESSION_COOKIE = "session"


def _same_origin(req) -> bool:
    from urllib.parse import urlparse

    stated = req.headers.get("Origin") or req.headers.get("Referer")
    if not stated:
        # No Origin and no Referer on a state-changing request is not something
        # a browser form does; refuse rather than guess.
        return False
    return urlparse(stated).netloc == urlparse(req.host_url).netloc


def check_origin(req):
    """None when the request is fine, or a (body, status) refusal.

    The attack only exists when the browser attaches a credential by itself.
    A program posting to `/mcp/…` or an inbound webhook carries no session
    cookie, cannot be made to carry one by another site, and therefore cannot be
    the victim — so requiring an Origin header from them just breaks every
    machine-to-machine caller. Only cookie-bearing requests are checked.
    """
    if req.method in SAFE_METHODS:
        return None
    if SESSION_COOKIE not in req.cookies:
        return None
    if _same_origin(req):
        return None
    if req.path.startswith("/api/"):
        return {"error": "cross-site request refused"}, 403
    return ("This request came from another site, so Heddled refused it.", 403)


def current_user(store):
    """The signed-in person, or None. Re-read per request so a suspended account
    or a role change takes effect immediately rather than at next sign-in."""
    from . import users

    user_id = session.get("uid")
    if not user_id:
        return None
    user = users.by_id(store, user_id)
    if not user or not user["active"]:
        session.clear()
        return None
    return user


def install(app) -> None:
    """Every console and API route requires a signed-in user, once anybody
    exists. Before that, the only reachable page is first-run setup."""
    from . import users
    from .store import get_store

    @app.before_request
    def _require_user():
        path = request.path
        store = get_store()

        refused = check_origin(request)
        if refused is not None:
            return refused

        if path.startswith(OPEN_PREFIXES) or path in OPEN_EXACT:
            return None

        # Nobody has claimed the console yet: force setup and allow nothing else.
        if users.needs_setup(store):
            if path == "/setup":
                return None
            if path.startswith("/api/"):
                return {"error": "Heddled has not been set up yet"}, 503
            return redirect(url_for("setup"))

        if path == "/setup":
            return redirect("/")

        user = current_user(store)
        if user is None:
            # Programs authenticate with a username:token pair rather than a form.
            user = _from_bearer(store, request)
        if user is None:
            if path.startswith("/api/"):
                return {"error": "sign in first"}, 401
            return redirect(url_for("login", next=path))

        g.user = user

        # People and credentials are admin-only to *read* as well as to change:
        # the nav hides them, and a page you cannot see in the menu should not
        # be reachable by typing its address.
        if _needs_admin(path) and (user.get("is_integration")
                                   or user["role"] not in users.CAN_MANAGE):
            users.record(store, user["username"], "denied.admin", path,
                         ip=request.remote_addr)
            if path.startswith("/api/"):
                return {"error": "administrators only"}, 403
            return render_template("error.html", code=403,
                                   message="Only an administrator can see that."), 403

        # A read-only account must not be able to change anything, whichever
        # screen or endpoint it reaches. Talking to an agent is the one POST
        # that is *using* Heddled rather than changing it: a viewer who can open
        # the chat page but cannot send a message has a text box that does
        # nothing, which is why the chat surface exists at all.
        #
        # Deliberately one exact shape of path, not a prefix — this is a hole in
        # the read-only guarantee and it should be exactly the size of the chat
        # box. Note what a viewer can still cause through it: whatever actions
        # that agent is allowed, bounded by its own policies and budgets. An
        # agent that can send email can send email on a viewer's say-so.
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            using_not_changing = (
                (path.startswith("/chat/") and path.endswith("/messages"))
                # Deciding an approval is using Heddled too. Note what the
                # alternative would be: requiring `member` to sign something
                # off gives an approver the run of the agent files as well,
                # which is more power, not less. The decision is recorded
                # against their name either way — and anybody holding the
                # emailed link can already decide it without an account at all.
                or _is_approval_decision(path)
            )
            if user["role"] not in users.CAN_WRITE and not using_not_changing:
                users.record(store, user["username"], "denied.write", path,
                             ip=request.remote_addr)
                if path.startswith("/api/"):
                    return {"error": "your account can look, but not change"}, 403
                return render_template("error.html", code=403,
                                       message="Your account can look at things, "
                                               "but not change them."), 403
        return None


# Paths only an administrator may change: people, credentials, and anything
# that reaches outside Heddled.
# `/jarvis` is here to *read* as well as to change: a screen that starts an
# autonomous loop, spends money and writes agents is not something a member
# account should be able to open, let alone press.
ADMIN_WRITE_PREFIXES = ("/users", "/settings", "/jarvis")


def _is_approval_decision(path: str) -> bool:
    """`/approvals/<id>` and nothing else — not the listing, not a prefix."""
    parts = [p for p in path.split("/") if p]
    return len(parts) == 2 and parts[0] == "approvals"


def _needs_admin(path: str) -> bool:
    return path.startswith(ADMIN_WRITE_PREFIXES)


def _from_bearer(store, req):
    """Two kinds of non-browser credential.

    `Bearer <username>:<password>` — a person's account, for scripts and CI.

    `Bearer <integration-key>` — a key issued under Settings for another
    *system*. Push triggers arrive this way: a webhook from a ticketing tool is
    not a person and should not need an account, but it should still have a
    credential that can be revoked on its own.
    """
    from . import users

    presented = req.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not presented:
        return None

    if ":" in presented:
        username, _, password = presented.partition(":")
        return users.authenticate(store, username, password)

    callers = store.get_setting("mcp_callers") or {}
    if isinstance(callers, dict) and presented in callers:
        # A system, not a person: it may drive agents but never manage Heddled.
        return {"id": None, "username": callers[presented], "role": "member",
                "active": 1, "is_integration": True}
    shared = store.get_setting("mcp_api_key")
    if shared and hmac.compare_digest(str(presented), str(shared)):
        return {"id": None, "username": "integration", "role": "member",
                "active": 1, "is_integration": True}
    return None


# Setting names whose values must never be echoed back to a browser.
_CREDENTIAL_HINTS = ("key", "token", "password", "secret", "webhook_url")


def is_credential(name: str) -> bool:
    """Whether a setting holds something that should stay write-only."""
    lowered = (name or "").lower()
    return any(hint in lowered for hint in _CREDENTIAL_HINTS)


def redacted_settings(store, known_keys) -> dict:
    """Every setting, with credentials masked.

    The settings page used to dump this verbatim — which showed every API key,
    every webhook URL, and the console password hash to anyone who reached the
    page. A debugging view is worth keeping; handing back the secrets is not.
    """
    out = {}
    for key, value in sorted(store.all_settings().items()):
        if key in INTERNAL_SETTINGS or is_credential(key) or key not in set(known_keys):
            out[key] = mask(value) if value not in (None, "") else value
        else:
            out[key] = value
    return out


def still_allowed(store, user_id: str) -> bool:
    """Whether a long-running connection may keep going.

    Server-Sent Event streams outlive the request that opened them — often by
    hours. Without this, suspending somebody would leave their open stream
    delivering live conversations until they chose to close the tab.
    """
    from . import users

    user = users.by_id(store, user_id)
    return bool(user and user["active"])
