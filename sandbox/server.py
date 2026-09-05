"""Jarvis's shell, in a container that holds nothing worth stealing.

A terminal is the one thing that unmakes every fence Jarvis has. Given a shell
where Heddled runs, `cat data/heddled.db` is every API key and password hash on
the instance, `vi agents/support.yaml` deletes the approval gate on `refund`,
and `python` walks straight past the handler sandbox. None of those fences
would be worth the words describing them.

So the shell does not run there. It runs here: a separate container with

  - **no Heddled.** Not the source, not the database, not `agents/`, not
    `tools/`. There is no path to them because they are not mounted;
  - **no environment.** No provider keys, no settings, nothing inherited;
  - **one volume**, `/work`, which is the same `jarvis/work` the file browser
    shows and the workspace tools write to. That shared directory is the whole
    point: Jarvis writes a script with a file tool, runs it here, and the
    output appears back in the panel;
  - **no published port.** Reachable only from the compose network, by the
    service name, which is how Heddled talks to it.

What this is not: a defence against someone who has already got code running
here and is trying to get out. It is a container, and a container is not a
virtual machine. It is the difference between "Jarvis can run things" and
"Jarvis can read your Anthropic key", and that is the difference worth having.

Stdlib only, on purpose — the smaller the surface here, the better.
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORK = Path(os.environ.get("SANDBOX_WORK", "/work"))
#: Container-local, deliberately not the shared volume.
HOME = os.environ.get("SANDBOX_HOME", "/home/jarvis")
PORT = int(os.environ.get("SANDBOX_PORT", "8080"))
DEFAULT_TIMEOUT_S = int(os.environ.get("SANDBOX_TIMEOUT_S", "120"))
MAX_TIMEOUT_S = 600
MAX_OUTPUT = 200_000

#: One at a time. A shell shared between the model and the person watching it
#: is easier to reason about when the order of commands is the order they were
#: sent, and it stops a runaway loop opening fifty processes at once.
_lock = threading.Lock()


def _limits() -> None:
    """Applied in the child, before the command runs."""
    for what, soft in (
        (resource.RLIMIT_NPROC, 256),
        (resource.RLIMIT_FSIZE, 512 * 1024 * 1024),
        (resource.RLIMIT_CORE, 0),
    ):
        try:
            resource.setrlimit(what, (soft, soft))
        except (ValueError, OSError):
            pass
    os.setsid()


def run(command: str, timeout_s: int) -> dict:
    WORK.mkdir(parents=True, exist_ok=True)
    # Deliberately not os.environ: even here there is no reason to hand a
    # command whatever the container happens to have been started with.
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        # Not the workspace. With HOME there, `pip install` writes a .cache
        # tree of hundreds of files into the directory the operator opens to
        # find their own CSV — through a bind mount, onto their disk. HOME is
        # inside the container, where a cache belongs.
        "HOME": HOME,
        "TERM": "dumb",
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        done = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=str(WORK), env=env, capture_output=True, text=True,
            timeout=timeout_s, preexec_fn=_limits, errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False, "exit": 124, "timed_out": True,
            "stdout": (exc.stdout or b"").decode("utf-8", "replace")[:MAX_OUTPUT]
            if isinstance(exc.stdout, bytes) else (exc.stdout or "")[:MAX_OUTPUT],
            "stderr": f"timed out after {timeout_s}s and was stopped",
        }
    except OSError as exc:
        return {"ok": False, "exit": 126, "stdout": "", "stderr": str(exc)}
    return {
        "ok": done.returncode == 0,
        "exit": done.returncode,
        "stdout": done.stdout[:MAX_OUTPUT],
        "stderr": done.stderr[:MAX_OUTPUT],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):                                         # noqa: N802
        if self.path.rstrip("/") in ("", "/health"):
            usage = shutil.disk_usage(WORK) if WORK.exists() else None
            return self._send(200, {
                "ok": True, "work": str(WORK),
                "free_mb": round(usage.free / 1024 / 1024) if usage else None,
            })
        self._send(404, {"error": "not found"})

    def do_POST(self):                                        # noqa: N802
        if self.path.rstrip("/") != "/run":
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return self._send(400, {"error": "that was not JSON"})

        command = str(body.get("command") or "").strip()
        if not command:
            return self._send(400, {"error": "no command"})
        try:
            timeout_s = min(int(body.get("timeout_s") or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S)
        except (TypeError, ValueError):
            timeout_s = DEFAULT_TIMEOUT_S

        with _lock:
            self._send(200, run(command, max(1, timeout_s)))

    def log_message(self, *args):
        """Quiet. The command and its output go back over the wire and are
        recorded by Heddled; logging them again here would put a second copy
        somewhere nobody is reading."""


def take_the_workspace_then_drop() -> None:
    """Own `/work`, then stop being root.

    `/work` is bind-mounted from the host and created by the Heddled container,
    which runs as root — so it arrives root-owned and an unprivileged process
    cannot write a single file to it. Found the honest way: the first script
    Jarvis ran could not save its own output.

    So: start as root, take the directory, and drop before serving a byte.
    Everything that matters — the HTTP server, and every command it runs — is
    unprivileged; only the one chown is not.
    """
    uid = int(os.environ.get("SANDBOX_UID", "10001"))
    gid = int(os.environ.get("SANDBOX_GID", "10001"))
    WORK.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        try:
            os.chown(WORK, uid, gid)
        except OSError:
            # A read-only or otherwise fixed mount. Say nothing here; the first
            # command that tries to write will say it far more usefully.
            pass
        os.setgroups([gid])
        os.setgid(gid)
        os.setuid(uid)
        os.environ["HOME"] = HOME


if __name__ == "__main__":
    take_the_workspace_then_drop()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
