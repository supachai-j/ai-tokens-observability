# Changelog

All notable changes to **AI Tokens Observability** are documented here.

## [0.1.0] — 2026-06-11

First public release. Feature-complete for single-machine and multi-machine
(fleet) token observability across Claude Code, Codex CLI, and Gemini CLI.

### Added

#### Core
- **Live web dashboard** (`serve`) — SSE-pushed updates, light/dark theme,
  USD/THB currency toggle, project/model/tool/time-window filters,
  live-monitoring on/off toggle
- **Terminal report** (`report`) — rtk-style indicator bars for today + window
- **Usage snapshots** (`save`) — append daily rollups to `history.jsonl`;
  auto-saved every 30 min while serving
- **Incremental index** (`scan`) — byte-offset tracking so only new transcript
  bytes are re-read; cold scan of ~300 MB in < 1 s
- **Multi-machine fleet view** (`export`) — per-node snapshot export; fleet
  panel merges remote nodes with live-local overlay; stacked daily chart
- **Weekly digest** (`digest`) — week-over-week deltas by model/tool; text,
  JSON, and self-contained HTML formats
- **Session tracing** — drill into any recent session: prompts, tool calls,
  MCP calls (badged), tool results, per-API-call token + cost breakdown

#### Data sources
- Claude Code (`~/.claude/projects/**/*.jsonl`) — per-message `usage` tokens
- Codex CLI (`~/.codex/sessions/**/rollout-*.jsonl`) — cumulative token events
- Gemini CLI (`~/.gemini/tmp/*/chats/**`) — per-message token counts
- `rtk gain --format json` — optional token-savings panel (shows `n/a` without rtk)

#### Observability features
- **Monthly budget indicator** — `RTK_PULSE_BUDGET`; color-coded meter;
  month-end linear projection with estimated overage day
- **Budget alerts** — threshold banners + native macOS notifications
  (`RTK_PULSE_BUDGET_ALERT`); dismissable per-month, per-threshold
- **Cost-spike alert** — trailing-7d anomaly detection; names top contributing
  project (`RTK_PULSE_SPIKE` / `RTK_PULSE_SPIKE_MIN`)
- **Long-term trend panel** — up to 2-year daily-cost line chart from
  `history.jsonl`; CSV download link
- **rtk savings panel** — token-savings meter + percentage + command count
- **Cache-hit meter** — Anthropic prompt-cache efficiency across the window
- **Per-tool breakdown** — cost/tokens split by Claude Code / Codex / Gemini
- **Custom pricing overrides** — `pricing.json` for negotiated rates or new
  models; longest-key-wins matching, case-insensitive substring

#### Dashboard UX (C19 polish)
- Spacing scale (`--s1`…`--s6`) and font-size scale (`--fs-xs`…`--fs-hero`)
- Tabular-nums on all numeric cells (`font-variant-numeric`)
- Semantic budget-card alert accent (amber/red left-border when threshold crossed)
- Rounded bar chart segments (`borderRadius: 4`) across all bar charts
- Fleet stacked chart wired to `fleet_daily`; "this machine" badge uses
  `.b-local` CSS class; inline styles eliminated from JS-generated HTML

#### Quality
- 270 stdlib-only unit tests (zero dependencies)
- Static `TestDashboardIdContract` — 46 JS-required element ids verified
  against the HTML at test time
- `TestVersionSync` — `pulse.__version__` and `pyproject.toml` kept in sync
- Path-traversal guard on node slugs (`_node_slug`)
- All file writes are atomic (tmp + rename)
- Server binds to `127.0.0.1` only

#### Packaging
- `pyproject.toml` — installable via `pipx install git+…`; `rtk-pulse` console
  script; Python 3.9–3.13
- macOS LaunchAgent sample (`contrib/com.rtk-pulse.serve.plist`)
- `docs/ARCHITECTURE.md` — full design, data-flow, security model, fleet view

### Technical

- Index schema v3: `{version, files, days, activity, recent}`
- Entry shape: `{in, out, cc5, cc1, cr, n, cost}`
- Node snapshot schema v1: `{schema, node, generated_at, days:{day:{model:entry}}}`
  (projects collapsed for privacy)
- Zero runtime dependencies — Python 3.9+ stdlib only
- Chart.js 4.4.9 via CDN (dashboard only; degrades offline)
- USD→THB FX cached 12 h; `RTK_PULSE_THB` pin available

[0.1.0]: https://github.com/supachai-j/ai-tokens-observability/releases/tag/v0.1.0
