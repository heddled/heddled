"""Classes the templates ask for against classes the stylesheet defines.

Written after shipping two screens that referenced `table`, `stat-row`, `note`
and `side-by-side` — none of which the console stylesheet has. Nothing failed;
the pages simply rendered unstyled, which is the kind of mistake that only
shows up when somebody looks at the screen.

    python3 tools_dev/css_audit.py

Reports classes used in a template with no rule anywhere (broken styling), and
rules no template uses (dead weight). Both lists need reading rather than
obeying: a class can be added by JavaScript, and a rule can exist for a state
that only appears at runtime.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "heddled/web"
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"

# Applied from JavaScript, so they never appear in a template.
FROM_SCRIPT = {
    "is-on", "here", "on", "streaming", "done", "running", "pending", "err",
    "paused", "copied", "selected", "side-open", "turn", "bubble", "user",
    "agent", "step", "step-dot", "steps", "turn-meta", "copy-reply",
    "report-reply", "copy-btn", "copy-wrap", "carousel-btn", "carousel-dot",
    "carousel-dots", "carousel-count", "carousel-controls", "slide", "slides",
    "md-h", "chat-empty", "hidden",
    # Shown and hidden by the inline script on the agent page, by trigger kind.
    "when-schedule", "when-folder", "when-email",
    # Toggled while an eval run is in flight.
    "running-eval",
    # A hook the pair-adding script navigates from; the rows carry the styling.
    "pairs",
}

# Words this scraper lifts out of a Jinja expression that were never class
# names — `d['version']`, `m.role == 'you'`, `env-{{ s.env }}`.
NOT_CLASSES = {"version", "you", "env", "text", "if", "else", "endif"}


def classes_in_templates() -> dict[str, set[str]]:
    """class="a b {{ 'c' if x }}" — the literal names, ignoring the Jinja."""
    used: dict[str, set[str]] = {}
    for path in sorted(TEMPLATES.rglob("*.html")):
        names: set[str] = set()
        # A class attribute routinely spans lines and carries Jinja, so match
        # across newlines and take the literals out of the expressions.
        for value in re.findall(r'class="(.*?)"', path.read_text(), re.S):
            for literal in re.findall(r"'([a-zA-Z][\w -]*)'", value):
                names.update(literal.split())
            names.update(re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", value, flags=re.S).split())
        # `class="env-{{ s.env }}"` leaves a dangling prefix; the real rules are
        # env-prod and env-staging, and they are reported on the other side.
        used[path.name] = {n for n in names
                           if re.fullmatch(r"[a-zA-Z][\w-]*", n or "")
                           and not n.endswith("-")}
    return used


def classes_in_css() -> set[str]:
    text = "\n".join(p.read_text() for p in STATIC.glob("*.css"))
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return set(re.findall(r"\.([a-zA-Z][\w-]*)", text))


def classes_in_js() -> set[str]:
    text = "\n".join(p.read_text() for p in STATIC.glob("*.js"))
    names: set[str] = set()
    for pattern in (r"className\s*=\s*[`'\"]([^`'\"]+)", r"classList\.\w+\(([^)]*)\)",
                    r"querySelector(?:All)?\(\s*['\"]\.([\w-]+)"):
        for hit in re.findall(pattern, text):
            names.update(re.findall(r"[\w-]+", hit))
    return names


def main() -> int:
    defined = classes_in_css()
    from_js = classes_in_js()
    used = classes_in_templates()

    missing: dict[str, set[str]] = {}
    for template, names in used.items():
        gap = {n for n in names
               if n not in defined and n not in FROM_SCRIPT
               and n not in NOT_CLASSES}
        if gap:
            missing[template] = gap

    all_used = set().union(*used.values()) if used else set()
    unused = {c for c in defined
              if c not in all_used and c not in from_js and c not in FROM_SCRIPT}

    print(f"{len(defined)} classes defined · {len(all_used)} used in templates\n")

    if missing:
        print("USED BUT NEVER DEFINED — these render unstyled")
        for template, names in sorted(missing.items()):
            print(f"  {template}")
            for name in sorted(names):
                print(f"      .{name}")
        print()
    else:
        print("every class a template uses has a rule\n")

    if unused:
        print("DEFINED BUT UNUSED — dead unless applied at runtime")
        for name in sorted(unused):
            print(f"  .{name}")
        print()

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
