"""Optional commit-on-save (decision 9).

When `commit_on_save` is on, every console write becomes a commit and the file
history *is* the change log — "who changed this and when" turns into `git log`
rather than a feature Heddled has to build.

It ships **off**. Taking over someone's working tree without being asked is a
surprise, not a convenience: plenty of people want to stage a set of edits and
commit them as one change, and Heddled has no business deciding otherwise.

Failures here are never fatal. A commit that does not happen leaves the file
written, which is the part that actually matters — `git diff` still tells the
truth, it just tells it about the working tree instead of the history.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable, Optional

from . import config

SETTING = "commit_on_save"
TIMEOUT_S = 15


def _run(args: list[str], cwd: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args, cwd=str(cwd), capture_output=True, text=True, timeout=TIMEOUT_S
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def repo_root(start: Path = None) -> Optional[Path]:
    root = Path(start or config.ROOT)
    code, out = _run(["git", "rev-parse", "--show-toplevel"], root)
    return Path(out.strip()) if code == 0 and out.strip() else None


def is_enabled() -> bool:
    from .store import get_store

    try:
        return bool(get_store().get_setting(SETTING, False))
    except Exception:
        return False


def status() -> dict:
    """What the Settings screen shows about this."""
    root = repo_root()
    return {
        "enabled": is_enabled(),
        "repo": str(root) if root else None,
        "available": root is not None,
    }


def maybe_commit(paths: Iterable[Path], message: str,
                 enabled: bool = None) -> Optional[str]:
    """Commit the given files if the setting says so.

    `enabled` overrides the stored setting, which is what lets a CLI flag ask
    for a commit on a one-off basis. Returns the short sha, or None when nothing
    was committed — including every failure case.
    """
    if enabled is None:
        enabled = is_enabled()
    if not enabled:
        return None

    paths = [Path(p) for p in paths]
    if not paths:
        return None
    root = repo_root()
    if root is None:
        return None

    relative = []
    for p in paths:
        try:
            relative.append(str(p.resolve().relative_to(root)))
        except ValueError:
            continue  # outside the repo — not ours to commit
    if not relative:
        return None

    code, out = _run(["git", "add", "--", *relative], root)
    if code != 0:
        return None

    # Nothing staged means nothing changed; a no-op save should not make an
    # empty commit.
    code, _ = _run(["git", "diff", "--cached", "--quiet", "--", *relative], root)
    if code == 0:
        return None

    code, out = _run(
        ["git", "commit", "-m", f"heddled: {message}", "--only", "--", *relative], root
    )
    if code != 0:
        return None

    code, sha = _run(["git", "rev-parse", "--short", "HEAD"], root)
    return sha.strip() if code == 0 else None
