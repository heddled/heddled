"""Comment-preserving YAML round-trip for the authoring surface (§9, decision 9).

The console's forms are a *view over the file*, which only works if writing the
file back is lossless. `yaml.safe_load` throws away comments, key order and
formatting, so a form built on it would quietly reformat — and silently delete
the explanatory comments — every time somebody ticked a checkbox. That is worse
than having no form at all.

So authoring reads and writes through ruamel's round-trip loader, which keeps
comments, key order, quoting style and flow/block choices intact. The rest of
the platform keeps using `yaml.safe_load` for reading: the registry does not
care about comments, and the hot path should not pay for round-trip machinery.

The other half of honesty is `unrepresentable()`. A hand-written file may contain
things a form cannot show — anchors, merge keys, multi-document streams. Rather
than dropping them on save, the console detects them and marks that section
read-only, pointing the author at the raw tab.
"""

from __future__ import annotations

import io
from typing import Any, Optional

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

# Fields the form owns, in the order a written-from-scratch file should carry
# them. Anything not listed here is preserved wherever the author put it.
AGENT_KEY_ORDER = [
    "name", "description", "model", "instructions", "handler",
    "adapters", "triggers", "policies", "memory", "expose",
]


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # never re-wrap a long line the author chose to keep on one
    # Match the style the shipped examples use: block sequences indented under
    # their key, so a form edit does not reflow the whole file.
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load(text: str):
    """Parse for editing. Returns ruamel's CommentedMap, which behaves like a
    dict but remembers everything else about the document."""
    return _yaml().load(text or "") or {}


def dump(data) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def is_valid(text: str) -> tuple[bool, Optional[str]]:
    """Cheap syntax gate, so an invalid document is refused before the file is
    touched rather than half-written."""
    try:
        _yaml().load(text or "")
        return True, None
    except YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}: " if mark is not None else ""
        return False, f"{where}{getattr(exc, 'problem', None) or exc}"


def unrepresentable(text: str) -> list[str]:
    """Constructs the structured form cannot faithfully show.

    Detected on the raw text because ruamel resolves most of them away during
    load — by the time you have the object, a merge key looks like ordinary
    inlined keys and writing it back would erase the `<<:`.
    """
    found = []
    stripped = [line.split("#", 1)[0] for line in (text or "").splitlines()]
    body = "\n".join(stripped)

    if any(line.lstrip().startswith("<<:") for line in stripped):
        found.append("merge keys (`<<:`)")
    if any(part.strip().startswith("&") for line in stripped for part in line.split(": ")[1:]):
        found.append("anchors (`&name`)")
    if "*" in body and any("*" in line.split(":", 1)[-1] and not line.lstrip().startswith("#")
                           and any(c.isalpha() for c in line.split("*", 1)[1][:1])
                           for line in stripped):
        found.append("aliases (`*name`)")
    if len([line for line in stripped if line.strip() == "---"]) > 1:
        found.append("multiple documents")
    return found


def _restore_cosmetic(before: str, after: str) -> str:
    """Undo formatting-only rewrites.

    ruamel round-trips content faithfully but still normalises some cosmetics —
    `{ a: 1 }` comes back as `{a: 1}` — which would put lines in the diff that
    the author never edited. Wherever a line's content is unchanged once
    whitespace is ignored, the original line wins, so a one-field edit produces
    a one-line diff.
    """
    import difflib

    b = before.splitlines(keepends=True)
    a = after.splitlines(keepends=True)
    squash = lambda s: "".join(s.split())  # noqa: E731
    matcher = difflib.SequenceMatcher(None, [squash(x) for x in b], [squash(x) for x in a])

    out: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        # "equal" here means equal *ignoring whitespace* — keep how it was written.
        out.extend(b[i1:i2] if tag == "equal" else a[j1:j2])
    return "".join(out)


def apply_updates(text: str, updates: dict) -> str:
    """Write form values back into an existing document.

    Only the keys present in `updates` are touched; a key set to None is
    removed. Everything else in the file — comments, ordering, formatting, and
    keys the form knows nothing about — survives untouched.
    """
    data = load(text)
    for key, value in updates.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return _restore_cosmetic(text, dump(data))


def diff(before: str, after: str, path: str = "") -> str:
    """The change about to be written, shown before it is written."""
    import difflib

    lines = difflib.unified_diff(
        (before or "").splitlines(keepends=True),
        (after or "").splitlines(keepends=True),
        fromfile=f"a/{path}" if path else "before",
        tofile=f"b/{path}" if path else "after",
        n=3,
    )
    return "".join(lines)


def has_changes(before: str, after: str) -> bool:
    return (before or "").strip() != (after or "").strip()
