<h1>
  <img src="heddled/web/static/brand/mark-small.svg" alt="" height="28" align="top">
  Heddled
</h1>

**Everything an AI agent needs around it.** A model on its own is not a system. Heddled is the rest of it: what an agent is allowed to do, who signs off before it does anything serious, what it costs, and a record of every step it took — on your own machine, with any model.

[![CI](https://github.com/heddled/heddled/actions/workflows/ci.yml/badge.svg)](https://github.com/heddled/heddled/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

[heddled.com](https://heddled.com) · [Documentation](https://heddled.com/docs.html)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/console-dark.webp">
  <img src="docs/console-light.webp" alt="The console showing one conversation step by step: the question, the tool call, what it returned, and the reply.">
</picture>

## Install

```bash
curl -fsSL https://heddled.com/install.sh | sh      # macOS, Linux
irm https://heddled.com/install.ps1 | iex          # Windows, PowerShell
```

Checks for Docker, fetches Heddled, and starts it on `http://localhost:5005`. From a checkout, `docker compose up` does the same.

There is a built-in stand-in model, so you can build an assistant, watch it work and approve something without an API key.

## What it gives you

- **A pause you control.** Mark an action and the run stops until a person decides — enforced by the platform, not asked of the model. It resumes from that exact point, days later if need be.
- **Limits that hold.** Spending caps per day and per conversation, rate limits, and sensitive values kept out of the record.
- **A record of everything.** What was asked, what it looked up, what came back, what it decided — readable months later, not a log to grep.
- **It starts itself.** On a schedule, when a file lands, or when email arrives.
- **It reaches your systems.** HTTP, lookups, email, webhooks and MCP servers, mostly without writing code.
- **It works with real files.** A folder of its own, where it reads Word and Excel documents and writes `.docx`, `.xlsx` and `.pptx` — not a `.txt` somebody has to reformat.
- **It stays yours.** One container on your hardware, any model provider or your own, and every definition a plain file you can read.

## Documentation

- [heddled.com/docs](https://heddled.com/docs.html) — building and running one, from first assistant to backups
- [`docs/architecture.md`](docs/architecture.md) — the event contract, the object model, the CLI and configuration reference
- [`heddled-concept.md`](heddled-concept.md) — the design document the code is answerable to

## Contributing

```bash
git clone https://github.com/heddled/heddled && cd heddled
pip install -e ".[dev]"
pytest
```

CI runs the suite on Python 3.10 through 3.14, builds the image, and drives a full turn through a running container.

Two things worth knowing before opening a pull request. **[`heddled-concept.md`](heddled-concept.md) states the principles the code answers to** — contradicting it is fine, but argue with it in an issue first. And **behaviour is tested through the surface that has it**: console behaviour goes through the Flask test client rather than the functions underneath, so a passing suite means the screen works. `tools_dev/` goes further and measures contrast, hit targets and layout in a real browser.

## Non-goals

No visual flow builder. No connector marketplace. No built-in vector database. No fine-tuning. No multi-tenant SaaS billing.

## License

Apache-2.0. See [LICENSE](LICENSE).
