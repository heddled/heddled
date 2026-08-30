"""The chat page's markdown renderer, and whether anything can escape it.

The renderer turns model output into HTML, and model output is not trusted — a
prompt-injected reply can contain whatever an attacker likes. So the question
is not "does the string look safe" (escaped text contains the word `onerror`
and is perfectly inert) but "what does the browser build from it".

Every case below is rendered into a real DOM, and the assertions are about the
resulting nodes: no scripts, no event-handler attributes, no javascript: or
data: links, and no payload that actually fires.

    python3 tools_dev/md_check.py
"""

import pathlib
import sys

from playwright.sync_api import sync_playwright

RENDERER = pathlib.Path(__file__).resolve().parents[1] / "heddled/web/static/chat.js"
PAGE_HALF = "// ------------------------------------------------------------------- page"

# (name, markdown, a fragment that must appear in the rendered HTML)
RENDERS = [
    ("bold", "Invoice **F-2231** is unpaid.", "<strong>F-2231</strong>"),
    ("italic", "That is *probably* fine.", "<em>probably</em>"),
    ("code span", "Use `lookup_invoice`.", "<code>lookup_invoice</code>"),
    ("bullets", "- one\n- two", "<ul><li>one</li><li>two</li></ul>"),
    ("numbered", "1. first\n2. second", "<ol><li>first</li>"),
    ("heading", "### What I'd suggest", 'class="md-h"'),
    ("link", "See [docs](https://heddled.com/x).", 'href="https://heddled.com/x"'),
    ("code block", "```\nx = 1\n```", "<pre><code>x = 1</code></pre>"),
    ("quote", "> mind this", "<blockquote>"),
    ("no emphasis in code", "`a **b** c`", "<code>a **b** c</code>"),
]

# Things a reply might contain if somebody were trying.
ATTACKS = [
    ("script tag", "<script>window.PWNED=1</script>"),
    ("img onerror", '<img src=x onerror="window.PWNED=1">'),
    ("svg onload", "<svg onload=window.PWNED=1>"),
    ("iframe", '<iframe src="javascript:window.PWNED=1"></iframe>'),
    ("javascript link", "[click](javascript:window.PWNED=1)"),
    ("data link", "[click](data:text/html,<script>window.PWNED=1</script>)"),
    ("attribute break", '**a" onmouseover="window.PWNED=1**'),
    ("html inside code", "`<img src=x onerror=window.PWNED=1>`"),
    ("nested", "- <script>window.PWNED=1</script>\n- ok"),
]

INSPECT = """(html) => {
    const host = document.createElement('div');
    host.innerHTML = html;
    document.body.append(host);
    const nodes = [...host.querySelectorAll('*')];
    const handlers = [];
    for (const el of nodes) {
        for (const attr of el.attributes) {
            if (/^on/i.test(attr.name)) handlers.push(el.tagName + '/' + attr.name);
        }
    }
    const links = [...host.querySelectorAll('a')].map(a => a.getAttribute('href') || '');
    return {
        tags: [...new Set(nodes.map(n => n.tagName.toLowerCase()))],
        handlers,
        links,
        fired: Boolean(window.PWNED),
    };
}"""

FORBIDDEN_TAGS = {"script", "iframe", "object", "embed", "img", "svg", "link", "style"}


def main() -> int:
    source = RENDERER.read_text().split(PAGE_HALF)[0]
    bad: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto("about:blank")
        page.add_script_tag(content=source)

        print("renders:")
        for name, src, want in RENDERS:
            got = page.evaluate("s => renderMd(s)", src)
            ok = want in got
            print(f"  {'ok  ' if ok else 'FAIL'} {name:20} {got[:60]}")
            if not ok:
                bad.append(f"{name}: expected {want!r} in {got!r}")

        print("\nattacks:")
        for name, src in ATTACKS:
            page.evaluate("() => { window.PWNED = false; }")
            html = page.evaluate("s => renderMd(s)", src)
            result = page.evaluate(INSPECT, html)

            problems = []
            leaked = FORBIDDEN_TAGS.intersection(result["tags"])
            if leaked:
                problems.append(f"built {sorted(leaked)}")
            if result["handlers"]:
                problems.append(f"event handlers {result['handlers']}")
            for href in result["links"]:
                if not href.startswith(("http://", "https://")):
                    problems.append(f"link to {href!r}")
            if result["fired"]:
                problems.append("payload executed")

            print(f"  {'FAIL' if problems else 'safe'} {name:20} "
                  f"{'; '.join(problems) or 'inert: ' + html[:44]}")
            if problems:
                bad.append(f"{name}: {'; '.join(problems)}")

        browser.close()

    print()
    if bad:
        print("PROBLEMS")
        for line in bad:
            print(" -", line)
        return 1
    print("markdown renders correctly, and nothing escapes it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
