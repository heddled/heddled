"""Talking to Jarvis's terminal, and to the web, from Heddled's side.

Two capabilities that only make sense together with the workspace: a shell in a
container that shares `jarvis/work` with the file browser, and a reader that
fetches a page and hands back its text. Between them Jarvis can write a script,
run it, look something up, and show you the result — which is the difference
between a thing that drafts YAML and a thing that gets a job done.

Both are fenced, and the fences are different because the risks are:

**The shell** runs in `sandbox/`, a container with no Heddled source, no
database and no environment. This module only speaks to it over the compose
network; if it is not running, that is an ordinary answer rather than a crash.
Nothing here can start it, which is deliberate — turning the terminal on is
`docker compose --profile jarvis up`, a thing a person does.

**The reader** runs here, because the thing it needs is
`tooltypes.guard_destination` — the check that refuses the machine itself and
the private network. A model choosing a URL is the exact case that guard was
written for, so the reader is one of the few places it matters most. What comes
back is somebody else's text: it is labelled as such when it reaches the model,
because a page that says "ignore your instructions" is a page, not an
instruction.
"""

from __future__ import annotations

import html
import json
import os
import re
import socket
import urllib.error
import urllib.request

DEFAULT_TIMEOUT_S = 120
READ_TIMEOUT_S = 20
MAX_PAGE_CHARS = 20_000


class ShellUnavailable(RuntimeError):
    """The sandbox is not running. Said plainly, never dressed as a failure of
    the command somebody asked for."""


#: The one sentence for "there is no terminal", wherever that is discovered.
NOT_RUNNING = (
    "The terminal is not running. Start it from your Heddled folder:\n"
    "    docker compose --profile jarvis up -d --build")


def _looks_absent(exc) -> bool:
    """Whether this failure means the container is not there.

    A missing container shows up as a DNS failure — the compose network has no
    `jarvis-sandbox` to resolve — and reporting that verbatim gave somebody
    `<urlopen error [Errno -2] Name or service not known>`, which names the
    symptom and hides the cause.

    Tested by exception type, not by matching the message: the first attempt
    listed the strings resolvers produce and missed on the second machine it
    met, where the same failure reads `[Errno -5] No address associated with
    hostname`. `socket.gaierror` covers every spelling of "that name does not
    resolve". Anything else is passed through, because then the container is
    there and something else is wrong.
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (socket.gaierror, ConnectionRefusedError,
                            ConnectionError, TimeoutError)):
            return True
        exc = getattr(exc, "reason", None) or getattr(exc, "__cause__", None)
    return False


def endpoint() -> str:
    return (os.environ.get("HEDDLED_JARVIS_SANDBOX") or "").rstrip("/")


def available() -> bool:
    return bool(endpoint())


def health(timeout_s: float = 2.0) -> dict:
    """Whether the terminal is there, for the panel to say so before somebody
    types into a box that cannot answer."""
    if not endpoint():
        # "not configured" is true and useless. What somebody needs at this
        # point is the command that fixes it.
        return {"running": False, "why": NOT_RUNNING}
    try:
        with urllib.request.urlopen(f"{endpoint()}/health", timeout=timeout_s) as answer:
            body = json.loads(answer.read() or b"{}")
        return {"running": True, "free_mb": body.get("free_mb"),
                "work": body.get("work")}
    except Exception as exc:                                   # noqa: BLE001
        if _looks_absent(exc):
            return {"running": False, "why": NOT_RUNNING}
        return {"running": False,
                "why": f"the terminal is there but did not answer: {exc}"}


def run_command(command: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> dict:
    """One command in the sandbox. Returns what it printed and what it exited
    with; a command that fails is a normal outcome, not an exception."""
    command = (command or "").strip()
    if not command:
        raise ValueError("Say what to run.")
    if not endpoint():
        raise ShellUnavailable(NOT_RUNNING)

    payload = json.dumps({"command": command, "timeout_s": timeout_s}).encode()
    request = urllib.request.Request(
        f"{endpoint()}/run", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s + 15) as answer:
            return json.loads(answer.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise ShellUnavailable(f"the terminal refused that: {exc.read()[:300]!r}")
    except Exception as exc:                                   # noqa: BLE001
        if _looks_absent(exc):
            raise ShellUnavailable(NOT_RUNNING)
        raise ShellUnavailable(f"could not reach the terminal: {exc}")


# ------------------------------------------------------------------ reading


_SCRIPTS = re.compile(r"<(script|style|noscript|template)\b.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_BLANKS = re.compile(r"\n{3,}")
_LINKS = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.S | re.I)


def read_page(url: str, timeout_s: int = READ_TIMEOUT_S) -> dict:
    """Fetch a page and return its readable text.

    A reader, not a browser: no JavaScript runs, so a page that builds itself
    on the client comes back thin. That is worth saying plainly rather than
    letting the model conclude the site was empty.
    """
    from .tooltypes import ToolTypeError, guard_destination

    url = (url or "").strip()
    if not url:
        raise ValueError("Say which page to read.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # The one check that matters here. A model picking the destination is the
    # case this was written for: cloud metadata endpoints and internal admin
    # pages trust anything that can reach them.
    try:
        guard_destination(url, {})
    except ToolTypeError as exc:
        raise ValueError(str(exc))

    request = urllib.request.Request(url, headers={
        "User-Agent": "Heddled-Jarvis/1.0 (+https://heddled.com)",
        "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as answer:
            kind = (answer.headers.get_content_type() or "").lower()
            raw = answer.read(4 * 1024 * 1024)
            charset = answer.headers.get_content_charset() or "utf-8"
            final = answer.geturl()
    except urllib.error.HTTPError as exc:
        return {"url": url, "status": exc.code, "title": "",
                "text": f"The site answered {exc.code}.", "links": []}
    except Exception as exc:                                   # noqa: BLE001
        raise ValueError(f"could not read {url}: {exc}")

    body = raw.decode(charset, "replace")
    if kind not in ("text/html", "application/xhtml+xml"):
        return {"url": final, "status": 200, "title": final.rsplit("/", 1)[-1],
                "text": body[:MAX_PAGE_CHARS], "links": []}

    title = html.unescape(_TITLE.search(body).group(1).strip()) if _TITLE.search(body) else ""
    links = []
    for href, label in _LINKS.findall(body)[:60]:
        label = html.unescape(_TAGS.sub("", label)).strip()
        if label and href.startswith(("http://", "https://")):
            links.append({"href": href, "text": label[:90]})

    text = _SCRIPTS.sub(" ", body)
    text = _TAGS.sub("\n", text)
    text = html.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANKS.sub("\n\n", text).strip()
    return {"url": final, "status": 200, "title": html.unescape(title)[:200],
            "text": text[:MAX_PAGE_CHARS], "links": links}
