"""A folder an agent may work in, and nothing else.

An agent that can touch files is an agent that can touch *these* files. The
location is the operator's choice; the confinement is not, and none of it is
reachable from an agent file. That asymmetry is the whole design:

- The workspace is opted into per agent (`workspace:` in its file), so no
  existing install gains one on upgrade.
- Where it points is configurable. What it may reach is not.
- The platform's own directories are refused unconditionally — agents/, tools/,
  data/, var/, and the project root itself. Agents are *files*, and an agent
  that can rewrite `agents/support.yaml` can delete the approval gate that
  constrains it. No policy fixes that, because the policy is the file. So it is
  closed here, before any policy is consulted.

Confinement is a check, not a jail. Handlers run in this process, so a Python
tool somebody writes by hand can still read anything the process can. This
module is what makes the *built-in* file tools safe, and it is deliberately the
only thing that hands out paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import config, documents

#: Read and write caps. A model that asks for a 200MB file has misunderstood
#: the task, and the answer should be a clear refusal rather than an outage.
MAX_READ_BYTES = 512 * 1024
MAX_WRITE_BYTES = 512 * 1024
MAX_LIST = 500


class WorkspaceError(ValueError):
    """Refused. The message is shown to the agent, so it says what to do."""


def _sensitive() -> list[Path]:
    """Directories a workspace may not be, sit inside, or contain.

    `data/` holds every password hash and provider key; `agents/` and `tools/`
    are the definitions and the policies. None of them is a place an agent has
    business writing, whatever its file says.
    """
    return [Path(p).resolve() for p in (
        config.AGENTS_DIR, config.TOOLS_DIR, config.DATA_DIR, config.VAR_DIR,
    )]


def resolve_root(agent) -> Optional[Path]:
    """Where this agent may work, or None if it has not been given a workspace.

    `workspace: true` means "somewhere of your own" and lands in `work/<name>`.
    A string points wherever the operator says — an existing export folder, a
    shared drop — and is still subject to every rule below.
    """
    declared = getattr(agent, "workspace", None)
    if not declared:
        return None

    if declared is True:
        path = Path(config.ROOT) / "work" / agent.name
    else:
        path = Path(str(declared))
        if not path.is_absolute():
            path = Path(config.ROOT) / path

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceError(f"cannot use {path} as a workspace: {exc}")
    root = path.resolve()

    # Being *inside* the project is normal — `work/<agent>` is the default.
    # Being the project, or containing it, is not: either would put agents/ and
    # data/ within reach.
    project = Path(config.ROOT).resolve()
    if root == project or root in project.parents:
        raise WorkspaceError(
            f"'{declared}' is the project itself, which holds the definitions "
            "and the record. Point the workspace somewhere of its own — "
            "`workspace: true` uses work/<agent>."
        )
    for sensitive in _sensitive():
        if root == sensitive or sensitive in root.parents or root in sensitive.parents:
            raise WorkspaceError(
                f"'{declared}' overlaps {sensitive.name}/, which holds the "
                "definitions and the record. Point the workspace somewhere of "
                "its own — `workspace: true` uses work/<agent>."
            )
    return root


def safe_path(root: Path, given: str, *, must_exist: bool = False) -> Path:
    """One path inside the workspace, or a refusal.

    Rejected before resolving as well as after: an absolute path or a `..` is
    refused on sight, and whatever the string resolves to must still land
    inside the root. That second check is what catches a symlink pointing out —
    `resolve()` follows it, and the result is then outside and refused.
    """
    text = str(given or "").strip()
    if not text:
        raise WorkspaceError("no file named")
    candidate = Path(text)
    if candidate.is_absolute():
        raise WorkspaceError(f"'{given}' is an absolute path; use a name inside the workspace")
    if any(part == ".." for part in candidate.parts):
        raise WorkspaceError(f"'{given}' points outside the workspace")

    target = (root / candidate).resolve()
    if target != root and root not in target.parents:
        raise WorkspaceError(f"'{given}' points outside the workspace")
    if must_exist and not target.is_file():
        raise WorkspaceError(f"there is no file called '{given}'")
    return target


def looks_like_text(path: Path) -> bool:
    """Whether the agent could open this at all — as text or as a document.

    The console lists and downloads anything. Marking what the agent can
    actually read closes a loop that would otherwise be discovered by an
    operator dropping in a file and wondering why it says it cannot read it.
    """
    if path.suffix.lower() in documents.readable_suffixes():
        return True
    try:
        with path.open("rb") as fh:
            return b"\0" not in fh.read(8000)
    except OSError:
        return False


def listing(root: Path) -> list[dict]:
    """Every file in the workspace, nearest the top first.

    Directories are walked but not listed as entries: what an agent needs is
    the set of files it could read, and a tree adds a shape it has to reason
    about for nothing.
    """
    out: list[dict] = []
    for path in sorted(root.rglob("*")):
        if len(out) >= MAX_LIST:
            break
        if not path.is_file() or path.is_symlink():
            continue
        try:
            resolved = path.resolve()
            if root not in resolved.parents:
                continue          # a symlink out, or a race; not ours to show
            stat = path.stat()
        except OSError:
            continue
        out.append({
            "path": str(path.relative_to(root)),
            "bytes": stat.st_size,
            "modified": stat.st_mtime,
            "readable": looks_like_text(path),
        })
    return out


def read(root: Path, given: str) -> str:
    path = safe_path(root, given, must_exist=True)
    size = path.stat().st_size
    if size > MAX_READ_BYTES:
        raise WorkspaceError(
            f"'{given}' is {size // 1024}KB, over the {MAX_READ_BYTES // 1024}KB limit")

    # A document is not text, but the text is in there. Extracted rather than
    # refused: the agent asked to read a file and does not need to know which
    # kind of zip archive it turned out to be.
    if path.suffix.lower() in documents.readable_suffixes():
        try:
            return documents.extract(path)
        except documents.DocumentError as exc:
            raise WorkspaceError(str(exc))
        except Exception as exc:
            raise WorkspaceError(f"'{given}' could not be opened: {exc}")

    data = path.read_bytes()
    if b"\0" in data[:8000]:
        raise WorkspaceError(
            f"'{given}' is not something this can read. It handles text, CSV, "
            "JSON and Markdown, and Word, Excel and PDF files.")
    return data.decode("utf-8", errors="replace")


def write(root: Path, given: str, content) -> dict:
    # A model cannot emit a .docx, so it writes markdown and this makes the
    # file. Chosen by extension: an assistant asked for a report gets one,
    # without a second tool to know about.
    suffix = Path(str(given or "")).suffix.lower()
    if suffix in documents.WRITABLE:
        try:
            encoded = documents.build(suffix, content)
        except documents.DocumentError as exc:
            raise WorkspaceError(str(exc))
        if len(encoded) > MAX_WRITE_BYTES:
            raise WorkspaceError(
                f"that would be {len(encoded) // 1024}KB, over the "
                f"{MAX_WRITE_BYTES // 1024}KB limit for one file")
        path = safe_path(root, given)
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.is_file()
        path.write_bytes(encoded)
        return {"path": str(path.relative_to(root)), "bytes": len(encoded),
                "replaced": existed}

    text = "" if content is None else str(content)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        raise WorkspaceError(
            f"that is {len(encoded) // 1024}KB, over the "
            f"{MAX_WRITE_BYTES // 1024}KB limit for one file")
    path = safe_path(root, given)
    if path.is_dir():
        raise WorkspaceError(f"'{given}' is a folder")
    existed = path.is_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return {"path": str(path.relative_to(root)), "bytes": len(encoded),
            "replaced": existed}


def delete(root: Path, given: str) -> str:
    """Remove one file. Deliberately not something an agent can do.

    Overwriting is recoverable and deleting is not, so this is a person's
    decision made on a screen with a confirmation — not a tool a model can
    reach for. Empty folders left behind are tidied, because a workspace full
    of empty directories is noise nobody asked for.
    """
    path = safe_path(root, given, must_exist=True)
    path.unlink()
    parent = path.parent
    while parent != root and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent
    return given


def store_upload(root: Path, filename: str, data: bytes) -> dict:
    """Put a file somebody chose into the workspace.

    The name comes from a browser and is therefore a string an attacker could
    have chosen — `../../agents/support.yaml` included — so it goes through the
    same check as everything else rather than a bespoke one.
    """
    name = Path(str(filename or "")).name          # drop any directory part
    if not name or name in (".", ".."):
        raise WorkspaceError("that file has no usable name")
    if len(data) > MAX_WRITE_BYTES:
        raise WorkspaceError(
            f"that is {len(data) // 1024}KB, over the "
            f"{MAX_WRITE_BYTES // 1024}KB limit for one file")
    path = safe_path(root, name)
    existed = path.is_file()
    path.write_bytes(data)
    return {"path": name, "bytes": len(data), "replaced": existed}
