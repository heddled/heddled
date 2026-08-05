"""People, roles, and a record of what they did.

Heddled previously had one shared password. That is honest for a laptop and wrong
for an organisation: a shared secret cannot tell you who approved the refund,
cannot be revoked for one person who left, and gives a contractor the same reach
as the person who runs the place.

Three roles, chosen to match the personas the concept doc already names (§3)
rather than invented from scratch:

    admin    everything, including managing people and credentials
    member   builds and runs agents — the "builder"
    viewer   reads activity and results, changes nothing — the "reviewer"

Passwords are PBKDF2-SHA256 with a per-user salt. Not because Heddled is a bank,
but because a stolen SQLite file should not hand over everyone's password, and
people reuse passwords.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from typing import Optional

ROLES = ("admin", "member", "viewer")

ROLE_WORDS = {
    "admin": "Administrator — everything, including people and credentials",
    "member": "Member — builds agents, tools and rules, and runs them",
    "viewer": "Viewer — can look at everything, change nothing",
}

# What each role may do. Checked in one place so a new screen cannot quietly
# forget to ask.
CAN_WRITE = ("admin", "member")
CAN_MANAGE = ("admin",)

USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{2,32}$")
MIN_PASSWORD = 10


class UserError(ValueError):
    """A rejected change, worded for whoever is reading the form."""


# ------------------------------------------------------------------ hashing

ITERATIONS = 240_000


def hash_password(password: str, salt: str) -> str:
    import hashlib

    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), ITERATIONS
    ).hex()


def check_strength(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise UserError(f"Use at least {MIN_PASSWORD} characters.")
    # Length beats character classes; a long passphrase is fine. Only refuse the
    # handful that are genuinely guessed first.
    if (password or "").lower() in {
        "password12", "password123", "heddledheddled", "1234567890",
        "changeme12", "adminadmin", "qwertyuiop",
    }:
        raise UserError("That password is too easy to guess.")


def check_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise UserError(
            "A username is 2–32 characters: letters, digits, dots, dashes "
            "or underscores."
        )
    return username


# -------------------------------------------------------------------- model


def count(store) -> int:
    return store.one("SELECT COUNT(*) c FROM users")["c"]


def needs_setup(store) -> bool:
    """True until somebody has claimed the first admin account."""
    return count(store) == 0


def get(store, username: str) -> Optional[dict]:
    row = store.one("SELECT * FROM users WHERE username=?", ((username or "").strip(),))
    return dict(row) if row else None


def by_id(store, user_id: str) -> Optional[dict]:
    row = store.one("SELECT * FROM users WHERE id=?", (user_id,))
    return dict(row) if row else None


def listing(store) -> list[dict]:
    return [dict(r) for r in store.query(
        "SELECT * FROM users ORDER BY role='admin' DESC, username")]


def create(store, username: str, password: str, role: str = "member",
           display_name: str = None, created_by: str = None,
           must_change: bool = False) -> dict:
    username = check_username(username)
    check_strength(password)
    if role not in ROLES:
        raise UserError(f"'{role}' is not one of {', '.join(ROLES)}.")
    if get(store, username):
        raise UserError(f"Somebody is already called '{username}'.")

    salt = secrets.token_hex(16)
    user_id = "u_" + secrets.token_hex(8)
    store.execute(
        "INSERT INTO users (id, username, display_name, password_hash, salt, role,"
        " active, created_at, created_by, must_change) VALUES (?,?,?,?,?,?,1,?,?,?)",
        (user_id, username, (display_name or "").strip() or None,
         hash_password(password, salt), salt, role, time.time(), created_by,
         1 if must_change else 0),
    )
    record(store, created_by, "user.created", username, {"role": role})
    return by_id(store, user_id)


def authenticate(store, username: str, password: str) -> Optional[dict]:
    """Returns the user on success. Constant-ish time either way, so a wrong
    username and a wrong password are not distinguishable by timing."""
    import hmac

    user = get(store, username)
    if not user:
        # Spend the same work so a missing account cannot be detected by timing.
        hash_password(password or "", "decoy-salt-decoy-salt")
        return None
    if not user["active"]:
        return None
    if not hmac.compare_digest(user["password_hash"],
                              hash_password(password or "", user["salt"])):
        return None
    store.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), user["id"]))
    return user


def set_password(store, username: str, password: str, by: str = None,
                 must_change: bool = False) -> None:
    check_strength(password)
    user = get(store, username)
    if not user:
        raise UserError(f"There is no user called '{username}'.")
    salt = secrets.token_hex(16)
    store.execute(
        "UPDATE users SET password_hash=?, salt=?, must_change=? WHERE id=?",
        (hash_password(password, salt), salt, 1 if must_change else 0, user["id"]),
    )
    record(store, by, "user.password_changed", username)


def set_role(store, username: str, role: str, by: str = None) -> None:
    if role not in ROLES:
        raise UserError(f"'{role}' is not one of {', '.join(ROLES)}.")
    user = get(store, username)
    if not user:
        raise UserError(f"There is no user called '{username}'.")
    if user["role"] == "admin" and role != "admin" and _admin_count(store) <= 1:
        raise UserError(
            "This is the only administrator. Make somebody else an administrator "
            "first, or Heddled would have nobody who can manage it."
        )
    store.execute("UPDATE users SET role=? WHERE id=?", (role, user["id"]))
    record(store, by, "user.role_changed", username, {"role": role})


def set_active(store, username: str, active: bool, by: str = None) -> None:
    user = get(store, username)
    if not user:
        raise UserError(f"There is no user called '{username}'.")
    if not active and user["role"] == "admin" and _admin_count(store) <= 1:
        raise UserError("This is the only administrator; suspending them locks "
                        "everybody out.")
    store.execute("UPDATE users SET active=? WHERE id=?",
                  (1 if active else 0, user["id"]))
    record(store, by, "user.suspended" if not active else "user.restored", username)


def delete(store, username: str, by: str = None) -> None:
    user = get(store, username)
    if not user:
        raise UserError(f"There is no user called '{username}'.")
    if user["role"] == "admin" and _admin_count(store) <= 1:
        raise UserError("This is the only administrator; removing them locks "
                        "everybody out.")
    store.execute("DELETE FROM users WHERE id=?", (user["id"],))
    record(store, by, "user.removed", username)


def _admin_count(store) -> int:
    return store.one(
        "SELECT COUNT(*) c FROM users WHERE role='admin' AND active=1")["c"]


# ------------------------------------------------------------------- audit


def record(store, username: Optional[str], action: str, target: str = None,
           detail: dict = None, ip: str = None) -> None:
    """What people did to Heddled. The event spine records what agents did; this is
    the other half, and the two together are what an auditor asks for."""
    try:
        store.execute(
            "INSERT INTO audit (ts, username, action, target, detail, ip)"
            " VALUES (?,?,?,?,?,?)",
            (time.time(), username, action, target,
             json.dumps(detail) if detail else None, ip),
        )
    except Exception:
        # Never let bookkeeping break the action it is describing.
        pass


def audit_log(store, limit: int = 200, offset: int = 0, action: str = None) -> list[dict]:
    """Newest first, a page at a time. This table only grows — a console that
    has been running for a year should not try to render all of it."""
    sql = "SELECT * FROM audit"
    params: list = []
    if action:
        sql += " WHERE action=?"
        params.append(action)
    sql += " ORDER BY ts DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    return [dict(r) for r in store.query(sql, params)]


def audit_actions(store) -> list[str]:
    """Which kinds of change have actually happened, for the filter."""
    return [r["action"] for r in store.query(
        "SELECT DISTINCT action FROM audit ORDER BY action")]
