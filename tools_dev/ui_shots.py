"""Capture every console screen in both themes, so design work is done by
looking rather than by guessing."""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5001"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "shots")
SCREENS = {
    "01-agents": "/",
    "02-agent-detail": "/agents/support",
    "03-agent-new": "/agents/new",
    "04-tools": "/tools",
    "05-tool-gallery": "/tools/new",
    "06-tool-form": "/tools/new?type=lookup",
    "07-activity": "/sessions",
    "08-tests": "/evals",
    "09-publish": "/deployments",
    "10-settings": "/settings",
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for scheme in ("dark", "light"):
            ctx = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                color_scheme=scheme,
                device_scale_factor=1,
            )
            page = ctx.new_page()
            errors = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))

            for name, path in SCREENS.items():
                page.goto(BASE + path, wait_until="networkidle")
                page.screenshot(path=str(OUT / f"{name}-{scheme}.png"), full_page=True)

            # A session detail page, if one exists.
            page.goto(BASE + "/sessions", wait_until="networkidle")
            link = page.query_selector("tbody a[href^='/sessions/']")
            if link:
                page.goto(BASE + link.get_attribute("href"), wait_until="networkidle")
                page.screenshot(path=str(OUT / f"11-conversation-{scheme}.png"), full_page=True)

            if errors:
                print(f"[{scheme}] JS errors:", *dict.fromkeys(errors), sep="\n  ")
            ctx.close()
        browser.close()
    print("wrote", len(list(OUT.glob('*.png'))), "screenshots to", OUT)


if __name__ == "__main__":
    main()
