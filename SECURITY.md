# Security Policy

## Local-only posture

AI Tokens Observability is a **local-only tool**. Its security model is:

- **Loopback server only** — the HTTP server binds `127.0.0.1` exclusively.
  No port is exposed to the network; the dashboard is reachable only
  from the same machine.
- **Read-only access to local transcripts** — the scanner reads transcript
  files under `~/.claude/projects/`, `~/.codex/sessions/`, and
  `~/.gemini/tmp/` (and `$RTK_PULSE_HOME` for its own index/history).
  It never writes to those directories. Path traversal is rejected: only
  paths discovered by the scanner are accessible through the trace
  endpoint.
- **No telemetry** — no usage data, transcript content, token counts, or
  any other information leaves the machine. There are no analytics hooks,
  no callbacks, no remote logging.
- **Outbound calls** — the only network activity is:
  1. The browser fetching Chart.js from the CDN (a standard browser
     request; the server does not proxy it).
  2. An optional USD→THB exchange-rate lookup (`open.er-api.com`)
     used only when THB display is enabled; the result is cached for 12
     hours and the feature degrades gracefully if the request fails.
- **No authentication** — the server binds to loopback only; the bind
  address is hard-coded to `127.0.0.1` and is not configurable, and no
  sensitive data is served over the network, so there is no authentication
  layer. To reach the dashboard from another machine, front it with an SSH
  tunnel or an authenticating reverse proxy rather than exposing the port
  directly.

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅        |

## Reporting a vulnerability

Please use **GitHub's private vulnerability reporting** — click
**"Report a vulnerability"** under the **Security** tab of this
repository. This keeps the report private until it is triaged and, if
necessary, a fix is published.

Do not file a public issue for a security vulnerability.

This is a local-only, best-effort tool maintained by a small team.
There is no formal SLA, but reports will be reviewed in good faith
and acknowledged promptly.
