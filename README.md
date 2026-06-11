# AI Tokens Observability

Live token-usage **observability dashboard** for AI coding tools — **Claude
Code, OpenAI Codex CLI, and Gemini CLI** — companion to
[rtk](https://github.com/) (Rust Token Killer). Zero dependencies, single-file
Python (stdlib only) + one HTML page.

```
AI Tokens Observability — Claude Code Token Usage
════════════════════════════════════════════════════════
Today:      in    45.3M   out   254.5K   ≈ $38.12
Cache hits: ███████████████████████░ 96.8%
rtk saved:  ██████████████░░░░░░░░░░ 58.7%  (78.2K tokens)
```

## What it does

- **Live web dashboard** (`serve`) — SSE-pushed updates every few seconds:
  today/windowed token totals and cost estimates, daily stacked cost chart by
  model, cost-by-model donut, per-project table, cache-efficiency meter,
  live throughput + activity feed, an **rtk savings panel**
  (`rtk gain --format json`), and a **monthly budget indicator** (set
  `RTK_PULSE_BUDGET` to show month-to-date spend vs. limit with a
  color-coded progress meter).
- **Filters** — by tool (Claude Code / Codex / Gemini), project, model, and
  time window (today–90d); light/dark theme; USD/THB currency (live FX rate,
  cached 12h, `RTK_PULSE_THB` override); live monitoring can be toggled
  on/off.
- **Long-term trend** — a daily cost line chart spanning up to 2 years of
  saved snapshots, always global (all tools, all projects), currency-aware;
  downloadable as CSV (`⬇ CSV` link in the panel header).
- **Tracing** — pick any recent session and drill into its full timeline:
  prompts, assistant output, thinking, tool calls, MCP calls (badged
  separately), tool results (errors flagged), and per-API-call token
  usage + cost.
- **rtk-style terminal report** (`report`) — indicator bars in your terminal.
- **Usage snapshots** (`save`) — appends daily rollups to
  `~/.config/rtk-pulse/history.jsonl` (also auto-saved every 30 min while
  serving), so you keep a durable usage history beyond the 90-day index.

## Data sources

| Source | Tool | What |
|---|---|---|
| `~/.claude/projects/**/*.jsonl` | Claude Code | per-message `usage` (input/output/cache tokens, model) |
| `~/.codex/sessions/**/rollout-*.jsonl` | Codex CLI | `token_count` events (cumulative totals, deduped via deltas) |
| `~/.gemini/tmp/*/chats/**` | Gemini CLI | per-message `tokens` (input/output/cached/thoughts) |
| `rtk gain --format json` | rtk | token-savings analytics (optional — panel shows `n/a` without it) |

Tools that aren't installed are simply skipped. A **tool filter** on the
dashboard slices everything by Claude Code / Codex CLI / Gemini CLI.

Parsing is **incremental**: a byte-offset index (`~/.config/rtk-pulse/index.json`)
means only appended transcript data is re-read. A cold scan of ~300 MB takes
under a second; live updates are near-free.

> _Add dashboard screenshots here before publishing._

## Installation

**Requirements**

- Python **3.9+** (stdlib only — no `pip install` needed)
- Claude Code installed locally (transcripts under `~/.claude/projects/`)
- macOS or Linux
- Optional: [rtk](https://github.com/) on `PATH` for the savings panel
  (without it the panel just shows `n/a`)
- Internet access only for the Chart.js CDN and the USD→THB rate
  (both degrade gracefully offline)

**Install via git clone (zero setup)**

```bash
git clone https://github.com/supachai-j/ai-tokens-observability.git
cd ai-tokens-observability
python3 pulse.py scan        # build the index (first run, <1s per ~300MB)
python3 pulse.py serve --open
```

That's it — no virtualenv, no dependencies. The dashboard is at
<http://localhost:8377> (change with `--port`). It binds to `127.0.0.1`
only, so nothing is exposed to the network.

**Install via pipx (console script)**

```bash
pipx install git+https://github.com/supachai-j/ai-tokens-observability
rtk-pulse serve --open
```

This installs the `rtk-pulse` command globally. `rtk-pulse` is a drop-in
alias for `python3 pulse.py`; all subcommands and env vars work identically.
`pyproject.toml` and `LICENSE` are packaging metadata — the two-file
stdlib core (`pulse.py` + `dashboard.html`) is unchanged.

**Optional setup**

```bash
# shell alias
alias pulse='python3 ~/workspace/rtk/pulse.py'

# pin a custom USD->THB rate (skips the live FX lookup)
export RTK_PULSE_THB=33.0

# relocate the data dir (index, history, fx cache); default ~/.config/rtk-pulse
export RTK_PULSE_HOME=~/somewhere/else

# monthly spend limit in USD — enables the budget card + color-coded meter
# also shows a projected month-end spend on the card; flags the estimated day
# your limit will be exceeded if you stay at the current daily rate
export RTK_PULSE_BUDGET=20.0

# budget alert thresholds as % of limit (default: 80,100)
# triggers a dashboard banner + native OS notification (once per threshold per month)
export RTK_PULSE_BUDGET_ALERT=80,100

# cost-spike alert: warn when today's spend >= N× the trailing 7-day average
# (only over active days; gated by a $ floor). Default 3x / $5. Set 0 to disable.
# The alert message names the top contributing project so you know where to look.
export RTK_PULSE_SPIKE=3
export RTK_PULSE_SPIKE_MIN=5

# max trace steps shown in the session drilldown (default 600, min 50)
export RTK_PULSE_TRACE_MAX=600
```

**Custom pricing overrides (`pricing.json`)**

Drop a `pricing.json` file in `~/.config/rtk-pulse/` (or `$RTK_PULSE_HOME`) to
override or extend the built-in cost table — useful for negotiated enterprise
rates or new models that aren't listed yet.

Format: a JSON object whose keys are **model-name substrings** and values are
`[input_per_MTok, output_per_MTok]` in USD:

```json
{
  "opus-4-8": [3.5, 17.5],
  "my-new-model": [1.0, 4.0]
}
```

Matching rules:
- Keys are compared **case-insensitively** as substrings of the model name.
- When multiple keys match, the **longest key wins** (most-specific first).
- Overrides beat all built-in rates and the gpt-5 special-case block.

> **Important:** Costs are computed at **scan time** and stored in the index.
> New sessions pick up the override automatically. To recompute costs for
> existing history, run `pulse.py scan --force` after editing `pricing.json`.

**Uninstall**
- clone: delete the clone directory and `~/.config/rtk-pulse/`
- pipx: `pipx uninstall ai-tokens-observability` then delete `~/.config/rtk-pulse/`

## Usage

```bash
python3 pulse.py serve --open      # dashboard at http://localhost:8377
python3 pulse.py report [--days N] # terminal report
python3 pulse.py digest [--days N] [--format text|json|html]  # WoW weekly digest; HTML for email
python3 pulse.py save              # snapshot today's usage to history.jsonl
python3 pulse.py scan [--force]    # (re)build the index
```

### Auto-snapshot via Claude Code hook (optional)

Add to `~/.claude/settings.json` to save a snapshot at the end of every session:

```json
{
  "hooks": {
    "SessionEnd": [
      { "hooks": [ { "type": "command",
        "command": "python3 ~/workspace/rtk/pulse.py save >/dev/null 2>&1" } ] }
    ]
  }
}
```

### Run as a background service (macOS launchd)

To have the dashboard start automatically at login, install a LaunchAgent.
Copy the sample from `contrib/com.rtk-pulse.serve.plist` or create it manually:

```bash
# 1. Create the plist (adjust path to match your install)
cat > ~/Library/LaunchAgents/com.rtk-pulse.serve.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>         <string>com.rtk-pulse.serve</string>
  <key>ProgramArguments</key>
  <array>
    <!-- pipx install:  /Users/YOU/.local/bin/rtk-pulse serve -->
    <!-- git clone:     /Users/YOU/workspace/rtk/pulse.py serve -->
    <string>/Users/YOU/.local/bin/rtk-pulse</string>
    <string>serve</string>
  </array>
  <key>RunAtLoad</key>  <true/>
  <key>KeepAlive</key>  <true/>
  <key>StandardOutPath</key> <string>/tmp/rtk-pulse.log</string>
  <key>StandardErrorPath</key> <string>/tmp/rtk-pulse.err</string>
</dict>
</plist>
EOF

# 2. Load it (starts immediately)
launchctl load -w ~/Library/LaunchAgents/com.rtk-pulse.serve.plist

# 3. To stop / unload:
launchctl unload -w ~/Library/LaunchAgents/com.rtk-pulse.serve.plist
```

**Automate snapshots and weekly digest via cron / launchd**

Combine the [SessionEnd hook](#auto-snapshot-via-claude-code-hook-optional)
above (snapshot after every session) with a weekly cron for the digest:

```cron
# Weekly digest every Monday at 09:00 (JSON → pipe to mail/Slack/etc.)
0 9 * * 1  rtk-pulse digest --format json >> ~/rtk-pulse-digest.jsonl

# HTML version — self-contained, inline-CSS, no JS/CDN — safe to email or open in any browser
0 9 * * 1  rtk-pulse digest --format html > ~/rtk-pulse-digest.html
```

The `--format html` artifact is completely self-contained (inline CSS, no JavaScript,
no remote resources) so it opens correctly in any browser or email client.

## Security & privacy

The server binds to `127.0.0.1` only — no data is exposed beyond your machine.
All state is stored in `~/.config/rtk-pulse/` (or `RTK_PULSE_HOME`). The only
outbound calls are the Chart.js CDN fetch by your browser and an optional
USD→THB exchange-rate lookup — both degrade gracefully offline, and neither
sends usage data anywhere. Session-trace file access is restricted to paths
discovered by the scanner; arbitrary path traversal is rejected.
See [Architecture — Security model](docs/ARCHITECTURE.md#security-model) for details.

## Cost model

Estimates use API list prices per MTok — e.g. Fable 5 $10/$50 · Opus
4.8/4.7/4.6 $5/$25 · Sonnet $3/$15 · Haiku 4.5 $1/$5 · GPT-5 family
$1.25/$10 ($0.25/$2 mini) · Gemini 3 Pro $2/$12 · Gemini 3 Flash $0.30/$2.50.
Cache reads cost 0.1× input (0.25× for Gemini); Anthropic cache writes cost
1.25× (5m TTL) / 2× (1h TTL). These are **estimates of equivalent API cost**,
not what a subscription plan bills.

Message dedup follows the transcript format: multi-block assistant messages
repeat the same `usage` on adjacent lines, so events are deduped on
`requestId + message.id`.

## Files

```
pulse.py                 CLI + collector + HTTP/SSE server (stdlib only)
dashboard.html           light/dark dashboard (Chart.js via CDN)
pyproject.toml           packaging metadata (pipx/pip install)
LICENSE                  MIT
contrib/
  com.rtk-pulse.serve.plist  sample macOS LaunchAgent
docs/ARCHITECTURE.md     design & architecture overview
```

## Documentation

- [Architecture overview](docs/ARCHITECTURE.md) — components, data flow,
  index design, cost model, API, and design decisions.

## License

MIT — see [LICENSE](LICENSE).
