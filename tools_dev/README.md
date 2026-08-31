# Developer tools

Not part of the platform — these check the console the way a person would, so
design work is verified rather than assumed.

    pip install playwright && playwright install chromium
    HEDDLED_PORT=5001 docker compose up -d          # or: heddled serve

    python tools_dev/ui_audit.py     # contrast, hit targets, overflow
    python tools_dev/md_check.py     # the chat renderer, and what escapes it
    python tools_dev/css_audit.py    # classes templates ask for vs rules that exist
    python tools_dev/ui_shots.py out # every screen, light and dark

`ui_audit.py` measures what the eye estimates badly:

* **Contrast** of every text node against its nearest painted background,
  against the WCAG AA threshold for its size and weight.
* **Hit targets** below 24px, excluding inline prose links and checkboxes that
  sit inside their own (much larger) label.
* **Horizontal overflow**, at 1440px and again at 420px.

It exits quietly when there is nothing to report. Every finding it has produced
so far was a genuine defect — including a light theme that had been broken for
four iterations because the styles referenced a token that did not exist.
