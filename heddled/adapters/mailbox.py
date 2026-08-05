"""Mailbox poller — the reference *pull* trigger (concept §7).

A poller is stateful and long-running: it must remember what it already
processed and survive restarts. The cursor lives in SQLite next to events and
sessions, and is only advanced after the turn is durably enqueued (at-least-once
by default).

Two sources, same shape:

    source: folder   — watch a directory for new files (works offline; this is
                       what makes the pull-trigger story demoable with no
                       credentials at all)
    source: imap     — a real mailbox, cursor = highest seen UID
"""

from __future__ import annotations

import email
import imaplib
import os
from email.header import decode_header, make_header
from pathlib import Path

from .. import config
from .base import Adapter


class MailboxPoller(Adapter):
    name = "mailbox"
    kind = "poller"

    def poll(self, cursor, cfg: dict):
        source = (cfg or {}).get("source", "folder")
        if source == "imap":
            return self._poll_imap(cursor, cfg)
        return self._poll_folder(cursor, cfg)

    # --------------------------------------------------------------- folder

    def _poll_folder(self, cursor, cfg: dict):
        path = Path(cfg.get("path") or (config.VAR_DIR / "mailbox"))
        if not path.is_absolute():
            # Relative paths in an agent file are relative to the project root,
            # not to whatever directory the worker happened to start in.
            path = (config.ROOT / path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        seen = set(cursor or [])
        items = []
        for f in sorted(path.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            key = f"{f.name}:{int(f.stat().st_mtime)}"
            if key in seen:
                continue
            try:
                body = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            items.append(
                {
                    "id": key,
                    "subject": f.name,
                    "from": "folder",
                    "body": body,
                    "text": f"Subject: {f.name}\n\n{body}",
                }
            )
            seen.add(key)
        # Keep the cursor bounded — the last 500 keys is plenty to avoid replays.
        return items, sorted(seen)[-500:]

    # ----------------------------------------------------------------- imap

    def _poll_imap(self, cursor, cfg: dict):
        # The trigger file names the mailbox; the credentials live in Settings
        # or the environment, because an agent definition is a file people
        # commit and a mailbox password is not.
        settings = self.settings or {}

        def value(key, env):
            return cfg.get(key) or settings.get(f"imap_{key}") or os.environ.get(env)

        host = value("host", "HEDDLED_IMAP_HOST")
        user = value("user", "HEDDLED_IMAP_USER")
        password = value("password", "HEDDLED_IMAP_PASSWORD")
        folder = cfg.get("folder", "INBOX")
        if not (host and user and password):
            missing = [n for n, v in (("host", host), ("user", user),
                                      ("password", password)) if not v]
            raise RuntimeError(
                "the mailbox needs " + ", ".join(missing)
                + " — set imap_" + ", imap_".join(missing) + " under Settings")

        last_uid = int(cursor or 0)
        conn = imaplib.IMAP4_SSL(host, int(cfg.get("port", 993)))
        try:
            conn.login(user, password)
            conn.select(folder)
            typ, data = conn.uid("search", None, f"UID {last_uid + 1}:*")
            if typ != "OK":
                return [], last_uid
            uids = [int(u) for u in (data[0] or b"").split() if int(u) > last_uid]
            items = []
            for uid in sorted(uids)[: int(cfg.get("max_per_tick", 20))]:
                typ, msg_data = conn.uid("fetch", str(uid), "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                subject = str(make_header(decode_header(msg.get("Subject", "(no subject)"))))
                body = _plain_body(msg)
                items.append(
                    {
                        "id": str(uid),
                        "subject": subject,
                        "from": msg.get("From", ""),
                        "body": body,
                        "text": f"From: {msg.get('From','')}\nSubject: {subject}\n\n{body}",
                    }
                )
                last_uid = max(last_uid, uid)
            return items, last_uid
        finally:
            try:
                conn.logout()
            except Exception:
                pass


def _plain_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", "replace")
