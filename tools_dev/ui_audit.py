"""Measure what the eye can only estimate: hit-target sizes, text contrast, and
whether anything overflows the viewport.

Point it at a throwaway instance, never one in use — it claims the first-run
setup if the console has not been claimed yet:

    python tools_dev/ui_audit.py http://localhost:5010

Every page in the console needs a session. Without signing in, each request
redirects to /login and the audit cheerfully measures the login screen a dozen
times and reports nothing wrong.
"""

import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5010"
USER, PASSWORD = "audit", "a-good-long-password"

PAGES = ["/", "/agents/support", "/agents/support/test", "/agents/new",
         "/tools", "/tools/lookup_invoice", "/tools/new", "/tools/new?type=lookup",
         "/tools/new?type=python",
         "/sessions", "/evals", "/deployments", "/settings", "/users", "/account",
         "/spending", "/spending?days=7",
         # The surfaces that are not the console. They share this stylesheet, so
         # they are subject to the same measurements — and a page nobody audits
         # is where the contrast quietly goes wrong.
         "/approvals", "/chat/support",
         "/agents/support/files/view?path=notes.txt"]

JS = r"""
() => {
  const srgb = c => { c /= 255; return c <= .03928 ? c/12.92 : Math.pow((c+.055)/1.055, 2.4); };
  // Chrome resolves a color-mix() background to `color(srgb 0.91 0.89 0.85)`,
  // whose channels are 0-1, not 0-255. Reading those as 0-255 made a pale tint
  // measure as near-black and reported contrast failures that were not real.
  const channels = s => {
    const n = (s.match(/-?\d*\.?\d+(e-?\d+)?/gi) || []).map(Number);
    const isColorFn = /^color\(/i.test(s.trim());
    return (isColorFn ? n.slice(0, 3).map(v => v * 255) : n.slice(0, 3));
  };
  const lum = str => {
    const [r,g,b] = channels(str);
    return .2126*srgb(r) + .7152*srgb(g) + .0722*srgb(b);
  };
  const bgOf = el => {
    for (let n = el; n; n = n.parentElement) {
      const c = getComputedStyle(n).backgroundColor;
      if (c && !/rgba\(0, 0, 0, 0\)|transparent/.test(c)) return c;
    }
    return getComputedStyle(document.body).backgroundColor;
  };
  const ratio = (a,b) => { const [x,y] = [lum(a), lum(b)].sort((m,n)=>n-m);
                           return (x + .05) / (y + .05); };

  const small = [], lowContrast = [], overflow = [];

  // Interactive things people must be able to hit.
  document.querySelectorAll('a, button, input, select, textarea, summary, .tab').forEach(el => {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return;
    // Inline links inside prose are exempt: they are text, not controls.
    const inlineLink = el.tagName === 'A' && st.display.startsWith('inline')
                       && el.closest('p, li, td, .story-detail, .meta-strip, .hint');
    if (inlineLink) return;
    // A checkbox inside its own label is not the target: the label is, and it
    // is already comfortably sized. Flagging the box produces noise, not bugs.
    if (el.type === 'checkbox' && el.closest('label')) return;
    if (r.height < 24 || r.width < 16) {
      small.push(`${el.tagName.toLowerCase()}.${el.className||''}`.slice(0,60)
                 + ` ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
  });

  // Text contrast against its nearest painted background.
  document.querySelectorAll('p, span, td, th, li, label, h1, h2, h3, a, div').forEach(el => {
    if (!el.textContent.trim() || el.children.length) return;
    const st = getComputedStyle(el);
    const size = parseFloat(st.fontSize);
    const bold = parseInt(st.fontWeight) >= 700;
    const need = (size >= 24 || (size >= 18.66 && bold)) ? 3.0 : 4.5;
    const got = ratio(st.color, bgOf(el));
    if (got < need) {
      lowContrast.push(`${el.tagName.toLowerCase()}.${el.className||''}`.slice(0,50)
                       + ` ${got.toFixed(2)}:1 (need ${need}) "${el.textContent.trim().slice(0,28)}"`);
    }
  });

  // Not window.innerWidth: under mobile emulation that reports the visual
  // viewport, which grows to match the overflow, so the test could never fail.
  const layoutWidth = document.documentElement.clientWidth;
  if (document.documentElement.scrollWidth > layoutWidth + 1) {
    overflow.push(`page scrolls horizontally: `
      + `${document.documentElement.scrollWidth}px in a ${layoutWidth}px viewport`);
    [...document.querySelectorAll('*')]
      .filter(el => el.scrollWidth > layoutWidth + 1 && el.parentElement
                    && el.parentElement.scrollWidth <= el.scrollWidth)
      .slice(0, 3)
      .forEach(el => overflow.push(`  widened by ${el.tagName.toLowerCase()}`
        + `${el.className ? '.' + String(el.className).split(' ')[0] : ''}`));
  }

  // A control a screen reader cannot name is a control nobody using one can
  // operate. A placeholder is not a name: it disappears the moment you type.
  const unnamed = [], structure = [];
  const named = el => Boolean(
    el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.title
    || (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`))
    || el.closest('label'));
  document.querySelectorAll('input:not([type=hidden]), select, textarea').forEach(el => {
    if (!named(el)) {
      unnamed.push(`${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}`
        + `${el.name ? '[name=' + el.name + ']' : ''}`
        + (el.placeholder ? ` (only a placeholder: "${el.placeholder}")` : ''));
    }
  });
  document.querySelectorAll('button, a').forEach(el => {
    if (!(el.textContent || '').trim() && !el.getAttribute('aria-label') && !el.title) {
      unnamed.push(`${el.tagName.toLowerCase()} with no text`);
    }
  });

  if (document.querySelectorAll('h1').length !== 1) {
    structure.push(`${document.querySelectorAll('h1').length} h1 elements (want exactly 1)`);
  }
  let previous = 1;
  document.querySelectorAll('h1,h2,h3,h4').forEach(h => {
    const level = +h.tagName[1];
    if (level > previous + 1) {
      structure.push(`${h.tagName} follows h${previous}: "${h.textContent.trim().slice(0, 30)}"`);
    }
    previous = level;
  });
  const ids = new Set();
  document.querySelectorAll('[id]').forEach(el => {
    if (ids.has(el.id)) structure.push(`duplicate id "${el.id}"`);
    ids.add(el.id);
  });
  document.querySelectorAll('table').forEach((t, i) => {
    if (!t.querySelector('th')) structure.push(`table ${i + 1} has no header cells`);
    else if (!t.querySelector('thead')) structure.push(`table ${i + 1} headers are not in a thead`);
  });
  document.querySelectorAll('img:not([alt])').forEach(
    img => structure.push('image with no alt text'));

  // ------------------------------------------------------ habits, not rules
  //
  // The measurements above are pass/fail. These are the practices a screen is
  // judged by even when nothing is technically broken: can you see where you
  // are, does colour carry meaning on its own, does a destructive button say
  // so, does the browser know this is a password.
  const ux = [];

  // Exactly one <main>, and a nav. Without landmarks, "skip to content" is the
  // only way past the header and screen-reader navigation has nothing to grip.
  const mains = document.querySelectorAll('main').length;
  if (mains !== 1) ux.push(`${mains} <main> landmarks (want exactly 1)`);
  if (!document.querySelector('nav, [role=navigation]')
      && !document.body.classList.contains('chat-page')
      && !document.body.classList.contains('inbox-page')) {
    ux.push('no <nav> landmark');
  }

  // Colour on its own is not information: a status people read by hue is one
  // that a colour-blind reader, or a printed page, does not carry.
  document.querySelectorAll('.badge, .status, .step-dot, .cap-fill').forEach(el => {
    const text = (el.textContent || '').trim();
    // The label routinely sits on the wrapper — a meter is one element with a
    // fill inside it — so look up, not just at the parent's text.
    const labelled = el.getAttribute('aria-label') || el.title
      || el.closest('[aria-label], [title], [role=img]')
      || (el.parentElement && (el.parentElement.textContent || '').trim());
    if (!text && !labelled) {
      ux.push(`colour is the only signal: ${el.className}`);
    }
  });

  // A new tab is a surprise worth announcing, and an unguarded one hands the
  // opened page a reference back to this one.
  document.querySelectorAll('a[target=_blank]').forEach(a => {
    const rel = (a.getAttribute('rel') || '');
    if (!rel.includes('noopener')) ux.push(`target=_blank without rel=noopener: ${a.href}`);
  });

  // Password managers and browsers need telling.
  document.querySelectorAll(
      'input[type=password], input[name=username]:not([type=hidden])').forEach(el => {
    if (!el.getAttribute('autocomplete')) {
      ux.push(`no autocomplete on ${el.type === 'password' ? 'password' : 'username'} field`);
    }
  });

  // Below about 12px, body text stops being comfortably readable.
  const tiny = new Set();
  document.querySelectorAll('p, li, td, th, label, span, div, button, a').forEach(el => {
    if (!el.childNodes.length) return;
    const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
    if (!own) return;
    const size = parseFloat(getComputedStyle(el).fontSize);
    if (size && size < 12) tiny.add(`${size}px on ${el.tagName.toLowerCase()}`
      + `${el.className ? '.' + String(el.className).split(' ')[0] : ''}`);
  });
  tiny.forEach(x => ux.push(`text under 12px: ${x}`));

  // A link is for going somewhere. A control that acts should be a button, or
  // the keyboard and the context menu both lie about what it does.
  document.querySelectorAll('a[href="#"], a[href^="javascript:"]').forEach(a => {
    ux.push(`link that goes nowhere (should be a button): "${(a.textContent||'').trim().slice(0,24)}"`);
  });

  // Where am I? A nav with nothing marked current makes every page look alike.
  // Only site navigation. A <nav> whose links all point at the current path,
  // differing by query string, is a list of things *on* this page — the chat
  // page's conversations — and having none current is right when you are on a
  // new one.
  for (const nav of document.querySelectorAll('nav')) {
    const links = [...nav.querySelectorAll('a[href]')];
    if (links.length <= 2) continue;
    const goesElsewhere = links.some(
      a => new URL(a.href, location.href).pathname !== location.pathname);
    // Only expect a current item when one of the destinations actually covers
    // where we are. On an error page nothing does, and nothing should be lit.
    const covers = links.some(a => {
      const p = new URL(a.href, location.href).pathname;
      return p !== '/' && location.pathname.startsWith(p);
    });
    if (goesElsewhere && covers && !nav.querySelector('[aria-current]')) {
      ux.push('nav marks no current page with aria-current');
    }
  }

  // Deleting without saying so.
  document.querySelectorAll('form').forEach(f => {
    const words = (f.textContent || '').toLowerCase();
    const destructive = /\b(delete|remove|revoke|suspend|wipe|discard)\b/.test(words);
    if (destructive && !f.dataset.confirm) {
      const label = [...f.querySelectorAll('button')]
        .map(b => (b.textContent || '').trim()).join('/');
      if (label) ux.push(`destructive form with no confirmation: "${label.slice(0, 30)}"`);
    }
  });

  return {small: [...new Set(small)], lowContrast: [...new Set(lowContrast)], overflow,
          unnamed: [...new Set(unnamed)], structure: [...new Set(structure)],
          ux: [...new Set(ux)]};
}
"""


def sign_in(page) -> None:
    """Claim the console on a fresh instance, or sign in to one already claimed."""
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
        sys.exit(f"could not sign in at {BASE} — audit would only measure /login")


def keyboard_sweep(page, path: str) -> list[str]:
    """Tab through the page and check each stop shows a focus ring.

    Deliberately real key presses. `element.focus()` does not put Chromium into
    the `:focus-visible` state, so a stylesheet with a perfectly good ring
    measures as having none — which is how this check first reported every link
    in the console as broken.
    """
    findings: list[str] = []
    page.keyboard.press("Tab")
    seen = set()
    for _ in range(30):
        info = page.evaluate("""() => {
            const el = document.activeElement;
            if (!el || el === document.body) return null;
            const s = getComputedStyle(el);
            return {
                tag: el.tagName.toLowerCase(),
                cls: (el.className || '').toString().split(' ')[0],
                text: (el.textContent || el.value || '').trim().slice(0, 24),
                outline: s.outlineStyle, width: s.outlineWidth,
                shadow: s.boxShadow,
                key: (el.tagName + '|' + (el.className||'') + '|'
                      + (el.textContent||'').trim().slice(0, 20)),
            };
        }""")
        if not info:
            break
        if info["key"] in seen:
            break
        seen.add(info["key"])
        ringed = (info["outline"] not in ("none", "") and info["width"] != "0px") \
            or info["shadow"] not in ("none", "")
        if not ringed:
            findings.append(f"no focus ring when tabbed to: {info['tag']}"
                            f"{'.' + info['cls'] if info['cls'] else ''}"
                            f" \"{info['text']}\"")
        page.keyboard.press("Tab")
    return findings


def reveal(page) -> None:
    """Open the panels that are shut by default, so they get measured too."""
    for selector in ("details.rename summary", "details.advanced summary"):
        found = page.locator(selector)
        if found.count():
            found.first.click()
            page.wait_for_timeout(120)


def sweep(page, label: str) -> None:
    print(f"\n=================== {label} ===================")
    clean = True
    for path in PAGES:
        # `load`, not `networkidle`: a screen with a live SSE stream never goes
        # idle, so waiting for that timed out on the one page that has one
        # rather than measuring it. The settle below is what `networkidle` was
        # really being used for.
        page.goto(BASE + path, wait_until="load")
        page.wait_for_timeout(400)
        reveal(page)
        r = page.evaluate(JS)
        r["ux"] += keyboard_sweep(page, path)
        kinds = (("tiny target", r["small"]), ("contrast", r["lowContrast"]),
                 ("overflow", r["overflow"]), ("unnamed", r["unnamed"]),
                 ("structure", r["structure"]), ("ux", r["ux"]))
        if any(items for _, items in kinds):
            clean = False
            print(f"\n{path}")
            for kind, items in kinds:
                for i in items[:6]:
                    print(f"  {kind:12} {i}")
    if clean:
        print("  no findings")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for scheme in ("dark", "light"):
            ctx = browser.new_context(viewport={"width": 1440, "height": 1000},
                                      color_scheme=scheme)
            page = ctx.new_page()
            sign_in(page)
            sweep(page, scheme)
            ctx.close()

        # A phone, with the touch traits that change the rules: `pointer:
        # coarse` raises the minimum tap target, and 390px is the width most
        # phones actually report.
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844}, device_scale_factor=3,
            is_mobile=True, has_touch=True,
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1")
        page = ctx.new_page()
        sign_in(page)
        sweep(page, "phone · 390px")
        ctx.close()
        browser.close()


if __name__ == "__main__":
    main()
