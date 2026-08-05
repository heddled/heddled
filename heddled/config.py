"""Paths and process-wide settings.

Everything is resolved relative to a project root so `heddled dev` in a checkout
and a container both behave the same.
"""

import os
from pathlib import Path


def find_root(start: Path = None) -> Path:
    """Locate the project root: the directory holding `agents/`.

    Resolved from the working directory, not from where the package happens to
    be installed — otherwise a `pip install heddled-agents` would scaffold agents
    into site-packages. Parent directories are searched too, so `heddled chat`
    works from anywhere inside a project, the way `git` does.

    Falls back to the working directory, which is what `heddled init` wants in an
    empty folder.
    """
    start = (start or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        if (candidate / "agents").is_dir():
            return candidate
    return start


ROOT = Path(os.environ["HEDDLED_ROOT"]).resolve() if os.environ.get("HEDDLED_ROOT") else find_root()

AGENTS_DIR = Path(os.environ.get("HEDDLED_AGENTS_DIR", ROOT / "agents"))
TOOLS_DIR = Path(os.environ.get("HEDDLED_TOOLS_DIR", ROOT / "tools"))
DATA_DIR = Path(os.environ.get("HEDDLED_DATA_DIR", ROOT / "data"))
VAR_DIR = Path(os.environ.get("HEDDLED_VAR_DIR", ROOT / "var"))

DB_PATH = Path(os.environ.get("HEDDLED_DB", DATA_DIR / "heddled.db"))

DEFAULT_PORT = int(os.environ.get("HEDDLED_PORT", "5005"))
DEFAULT_HOST = os.environ.get("HEDDLED_HOST", "0.0.0.0")

# Environments an agent version can be deployed to.
ENVIRONMENTS = ["dev", "staging", "prod"]

# Which environment work arriving from outside belongs to — a webhook, an MCP
# caller, an inbound email, a scheduled run. It decides which version of an
# agent that work runs: `dev` is the file you are editing, anything else is the
# version published there. The console's Test tab is always `dev`, and a caller
# can always say for itself by sending `env`.
#
# It ships as `dev`, which is what a single-person Heddled wants and what every
# existing install already does. Set it to `prod` once you are publishing
# deliberately, and editing an agent stops changing what your live traffic runs.
DEFAULT_ENV = os.environ.get("HEDDLED_DEFAULT_ENV", "dev")

# Retention for full context.built payloads (decision 4 in the concept doc).
KEEP_FULL_CONTEXT_DAYS = int(os.environ.get("HEDDLED_KEEP_FULL_CONTEXT_DAYS", "90"))

# Safety rail on the agent loop.
MAX_TOOL_ITERATIONS = int(os.environ.get("HEDDLED_MAX_TOOL_ITERATIONS", "12"))
# Loop protection for agents-as-tools / MCP federation (§12).
MAX_CALL_DEPTH = int(os.environ.get("HEDDLED_MAX_CALL_DEPTH", "5"))

TOOL_TIMEOUT_S = float(os.environ.get("HEDDLED_TOOL_TIMEOUT_S", "30"))

# Serve HTTP only and leave turns to a separate `heddled worker` process. Set this
# on the web container when running the split-process compose profile.
WEB_ONLY = os.environ.get("HEDDLED_WEB_ONLY", "0").lower() in ("1", "true", "yes")


def ensure_dirs() -> None:
    for d in (AGENTS_DIR, TOOLS_DIR, DATA_DIR, VAR_DIR):
        d.mkdir(parents=True, exist_ok=True)
