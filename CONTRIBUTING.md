# Contributing

Thanks for your interest in AI Tokens Observability. This guide covers
everything you need to contribute a bug fix or feature.

## Dev setup

No virtualenv, no `pip install`. Clone and run:

```bash
git clone https://github.com/supachai-j/ai-tokens-observability.git
cd ai-tokens-observability
python3 pulse.py serve --open   # dashboard at http://localhost:8377
```

Requirements: Python 3.9+ and a Unix-like OS (macOS or Linux).
The only external resources loaded at runtime are the Chart.js CDN
(loaded by the browser) and an optional USD→THB FX lookup — both
degrade gracefully offline.

## Running the test suite

```bash
python3 -m unittest discover -s . -p "test*.py"
```

The suite must stay green on every commit. Every behaviour change
ships with new or updated tests.

## Design pillars — hard constraints every PR must respect

These are non-negotiable; a PR that violates any of them will not be
merged:

1. **stdlib only** — no new runtime dependencies, ever. `pulse.py` and
   `dashboard.html` must run without any `pip install`. (Repo
   infrastructure such as GitHub Actions workflows may use normal
   pip/Actions tooling; this rule is about the shipped runtime.)
2. **Loopback-only server** — the HTTP server binds `127.0.0.1` only.
   Nothing is ever exposed to the network.
3. **Chart.js via CDN** — the only external runtime resource in the
   browser. Do not add additional CDN dependencies.
4. **HTML-escape all user-derived render data** — every value that
   originates from user data must pass through `esc()` before being
   written into HTML.
5. **`INDEX_VERSION` bump** — only when the index schema or key
   structure changes. Do not bump it for logic-only changes.
6. **Never block the SSE loop** — background scanning and all I/O must
   not stall the event-stream loop.

## Two-file core

`pulse.py` and `dashboard.html` are the shipped product. `pyproject.toml`
and `LICENSE` are packaging metadata. Keep the two core files
self-contained.

For a deep dive into the data model, scanner, server, and dashboard
architecture see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## PR etiquette

- **Small, focused changes** — one logical change per PR.
- **Tests green** — run the suite locally before opening a PR.
- **Add/update tests** — every behaviour change must be covered.
- **Update docs** — if the change affects user-visible behaviour,
  update README.md and/or ARCHITECTURE.md to match.
- **No new runtime dependencies** — see pillar 1 above.
