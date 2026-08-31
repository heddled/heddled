"""How consistent the forms and boxes actually are, measured rather than judged.

"Looks sloppy" is usually something specific: inputs 30px tall on one screen
and 38px on another, six different corner radii, four greys doing the job of
one, labels that are 13px here and 14px there. None of that fails a test and
all of it reads as carelessness.

This collects the computed values across every screen and reports where one
role has more than one answer.

    python3 tools_dev/form_audit.py [base-url]
"""

import collections
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5010"
USER, PASSWORD = "audit", "a-good-long-password"

PAGES = ["/", "/agents/support", "/agents/new", "/agents/support/test",
         "/tools", "/tools/lookup_invoice", "/tools/new", "/tools/new?type=lookup",
         "/tools/new?type=http", "/tools/new?type=python",
         "/sessions", "/evals", "/deployments", "/settings", "/users", "/account",
         "/spending", "/approvals", "/chat/support"]

COLLECT = """
() => {
  const px = v => Math.round(parseFloat(v) * 10) / 10;
  const out = {};
  const note = (group, value, el) => {
    (out[group] = out[group] || []).push({
      value: String(value),
      where: el.tagName.toLowerCase()
             + (el.className ? '.' + String(el.className).trim().split(/\\s+/)[0] : ''),
    });
  };

  // Text fields, drop-downs and text areas should feel like one family.
  document.querySelectorAll('input:not([type=hidden]):not([type=checkbox])'
    + ':not([type=radio]), select, textarea').forEach(el => {
    if (!el.offsetParent) return;
    const s = getComputedStyle(el);
    // A textarea's height is its content, not a consistency question. Its
    // padding, radius and font still are.
    if (el.tagName !== 'TEXTAREA') {
      note('field height', px(el.getBoundingClientRect().height) + 'px', el);
    }
    note('field radius', s.borderRadius, el);
    note('field border', s.borderColor + ' ' + s.borderWidth, el);
    note('field padding', s.padding, el);
    note('field font', px(s.fontSize) + 'px', el);
  });

  document.querySelectorAll('button, .btn').forEach(el => {
    if (!el.offsetParent) return;
    const s = getComputedStyle(el);
    const tiny = el.classList.contains('tiny');
    note(tiny ? 'small button height' : 'button height',
         px(el.getBoundingClientRect().height) + 'px', el);
    note('button radius', s.borderRadius, el);
    note('button font', px(s.fontSize) + 'px', el);
  });

  // The boxes things sit in.
  document.querySelectorAll('.card, .ask, .stat, .note, .banner, .pick-card,'
    + ' .thread, .bubble').forEach(el => {
    if (!el.offsetParent) return;
    const s = getComputedStyle(el);
    note('box radius', s.borderRadius, el);
    note('box border', s.borderColor + ' ' + s.borderWidth, el);
    note('box padding', s.padding, el);
  });

  document.querySelectorAll('label').forEach(el => {
    if (!el.offsetParent) return;
    const s = getComputedStyle(el);
    note('label font', px(s.fontSize) + 'px ' + s.fontWeight, el);
  });

  return out;
}
"""


def sign_in(page) -> None:
    page.goto(BASE + "/setup", wait_until="load")
    if page.locator("#display_name").count():
        page.fill("#display_name", "Audit")
        page.fill("#username", USER)
        page.fill("#password", PASSWORD)
        page.fill("#confirm", PASSWORD)
    else:
        page.goto(BASE + "/login", wait_until="load")
        page.fill("#username", USER)
        page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_load_state("load")
    if "/login" in page.url or "/setup" in page.url:
        sys.exit(f"could not sign in at {BASE}")


def main() -> int:
    groups: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    examples: dict[tuple[str, str], set[str]] = collections.defaultdict(set)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        sign_in(page)
        for path in PAGES:
            page.goto(BASE + path, wait_until="load")
            page.wait_for_timeout(120)
            for group, entries in page.evaluate(COLLECT).items():
                for e in entries:
                    groups[group][e["value"]] += 1
                    if len(examples[(group, e["value"])]) < 3:
                        examples[(group, e["value"])].add(f"{path} {e['where']}")
        browser.close()

    drift = 0
    for group in sorted(groups):
        counts = groups[group]
        if len(counts) <= 1:
            only = next(iter(counts))
            print(f"\n{group}: one value everywhere — {only}")
            continue
        drift += 1
        print(f"\n{group}: {len(counts)} different values")
        for value, n in counts.most_common():
            seen = sorted(examples[(group, value)])[:2]
            print(f"  {n:4}×  {value:34} {seen[0] if seen else ''}")
            for s in seen[1:]:
                print(f"        {'':34} {s}")

    print(f"\n{drift} of {len(groups)} things have more than one answer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
