"""Running code that wrote itself, in a process that is not this one.

Heddled's ordinary tools are written by an operator and loaded into this
process, which is fine: somebody who can add a file to `tools/` can already do
anything the process can. Code an agent wrote for itself is a different
proposition, and running it here would hand it the event store — every provider
key, every password hash — for the asking.

So it runs as a child process:

- a fresh interpreter, with almost nothing in its environment. No API keys, no
  HEDDLED_* settings, no path back to the project;
- CPU, memory, file size and process count capped by the child on itself,
  before the handler is imported;
- the working directory is the workspace, so a bare `open("notes.txt")` lands
  where the sandbox already allows;
- killed at a deadline, whatever it thinks it is doing;
- arguments in over stdin as JSON, result out over stdout as JSON. Nothing
  shared, nothing mapped.

What this is not: an isolation boundary against a determined attacker on the
same machine. There is no namespace, no seccomp, no network block — a child
process can still open a socket. It is a strong seatbelt for code written by a
model that got the wrong idea, and the docs say exactly that rather than
implying more.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_TIMEOUT_S = 30
DEFAULT_MEMORY_MB = 512
DEFAULT_OUTPUT_BYTES = 256 * 1024


class SandboxError(RuntimeError):
    """The child failed, and the message is what the agent is told."""


#: Runs inside the child. Caps itself first, then imports the handler — the
#: order matters, or a handler could allocate before the limit exists.
RUNNER = r'''
import json, os, resource, sys, importlib.util

limit_mb = int(os.environ["SANDBOX_MEMORY_MB"])
seconds = int(os.environ["SANDBOX_CPU_S"])
out_bytes = int(os.environ["SANDBOX_OUTPUT_BYTES"])
for what, soft in (
    (resource.RLIMIT_AS, limit_mb * 1024 * 1024),
    (resource.RLIMIT_CPU, seconds),
    (resource.RLIMIT_FSIZE, out_bytes),
    (resource.RLIMIT_NOFILE, 64),
):
    try:
        hard = resource.getrlimit(what)[1]
        resource.setrlimit(what, (soft, soft if hard == resource.RLIM_INFINITY
                                  else min(soft, hard)))
    except (ValueError, OSError):
        pass
try:
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
except (ValueError, OSError):
    pass

payload = json.loads(sys.stdin.read() or "{}")
logs = []


class Ctx:
    """What a handler gets out here.

    Not the real one: there is no store to reach and no session to attach to,
    which is the point. `log` collects lines and they are put on the trace by
    the parent when the child is done.
    """

    def __init__(self):
        self.tool = payload.get("tool", "")
        self.agent = payload.get("agent", "")

    def log(self, message, **extra):
        logs.append(str(message)[:2000])

    def memory(self):
        return {}


spec = importlib.util.spec_from_file_location("sandboxed_handler", payload["handler"])
module = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(module)
    fn = getattr(module, "handle", None) or getattr(module, "main", None)
    if not callable(fn):
        raise AttributeError("the handler defines no handle(args, ctx)")
    result = fn(payload.get("args") or {}, Ctx())
except Exception as exc:
    print("\x00" + json.dumps(
        {"ok": False, "error": f"{type(exc).__name__}: {exc}", "logs": logs}))
    sys.exit(0)

def render(value):
    """A datetime or a Decimal is a reasonable thing to return and str() says
    what it is. A function is a mistake, and stringifying one hands back a
    memory address instead of telling the author."""
    if callable(value) or hasattr(value, "__dict__") and hasattr(value, "__module__"):
        raise TypeError(f"cannot return a {type(value).__name__}")
    return str(value)


try:
    body = json.dumps({"ok": True, "result": result, "logs": logs}, default=render)
except (TypeError, ValueError):
    body = json.dumps(
        {"ok": False, "logs": logs,
         "error": "the handler returned something that is not JSON"})
if len(body) > out_bytes:
    body = json.dumps({"ok": False, "logs": logs,
                       "error": "the handler returned more than the size limit"})
print("\x00" + body)
'''


def run_handler(handler_path: Path, args: dict, *, workdir: Path,
                tool: str = "", agent: str = "",
                timeout_s: int = DEFAULT_TIMEOUT_S,
                memory_mb: int = DEFAULT_MEMORY_MB) -> dict:
    """Call `handle(args, ctx)` in a child process and bring the answer back.

    Returns `{"result": …, "logs": [...]}`; raises SandboxError with something
    an agent can read if the child failed, timed out, or hit a limit.
    """
    handler_path = Path(handler_path)
    if not handler_path.is_file():
        raise SandboxError(f"there is no handler at {handler_path.name}")

    # Deliberately not os.environ: a child that inherits this one inherits
    # ANTHROPIC_API_KEY and every other secret sitting in it.
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(workdir),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "SANDBOX_MEMORY_MB": str(memory_mb),
        "SANDBOX_CPU_S": str(max(1, timeout_s)),
        "SANDBOX_OUTPUT_BYTES": str(DEFAULT_OUTPUT_BYTES),
    }
    payload = json.dumps({
        "handler": str(handler_path), "args": args or {},
        "tool": tool, "agent": agent,
    })

    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c", RUNNER],
            input=payload, capture_output=True, text=True,
            cwd=str(workdir), env=env, timeout=timeout_s + 5,
        )
    except subprocess.TimeoutExpired:
        raise SandboxError(
            f"'{tool or handler_path.stem}' was still running after "
            f"{timeout_s}s and was stopped")
    except OSError as exc:
        raise SandboxError(f"could not run '{tool or handler_path.stem}': {exc}")

    # The result is announced by a NUL so anything the handler printed on its
    # own — and handlers print — cannot be mistaken for it.
    marker = done.stdout.rfind("\x00")
    if marker == -1:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        tail = detail[-1][:300] if detail else "it produced no result"
        if done.returncode and "MemoryError" in (done.stderr or ""):
            tail = f"it ran out of memory (limit {memory_mb}MB)"
        raise SandboxError(f"'{tool or handler_path.stem}' did not finish: {tail}")

    try:
        answer = json.loads(done.stdout[marker + 1:])
    except ValueError:
        raise SandboxError(f"'{tool or handler_path.stem}' returned nothing usable")

    if not answer.get("ok"):
        raise SandboxError(answer.get("error") or "it failed without saying why")
    return {"result": answer.get("result"), "logs": answer.get("logs") or []}
