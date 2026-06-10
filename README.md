# rtk-pulse

Live token-usage **observability dashboard** for Claude Code — companion to
[rtk](https://github.com/) (Rust Token Killer). Zero dependencies, single-file
Python (stdlib only) + one HTML page.

```
rtk-pulse — Claude Code Token Usage
════════════════════════════════════════════════════════
Today:      in    45.3M   out   254.5K   ≈ $38.12
Cache hits: ███████████████████████░ 96.8%
rtk saved:  ██████████████░░░░░░░░░░ 58.7%  (78.2K tokens)
```

## What it does

- **Live web dashboard** (`serve`) — SSE-pushed updates every few seconds:
  today/7d/30d token totals and cost estimates, daily stacked cost chart by
  model, cost-by-model donut, per-project table, cache-efficiency meter,
  live-session feed, and an **rtk savings panel** (`rtk gain --format json`).
- **rtk-style terminal report** (`report`) — indicator bars in your terminal.
- **Usage snapshots** (`save`) — appends daily rollups to
  `~/.config/rtk-pulse/history.jsonl` (also auto-saved every 30 min while
  serving), so you keep a durable usage history beyond the 90-day index.

## Data sources

| Source | What |
|---|---|
| `~/.claude/projects/**/*.jsonl` | Claude Code transcripts — per-message `usage` (input/output/cache tokens, model) |
| `rtk gain --format json` | rtk token-savings analytics (optional — panel shows `n/a` without it) |

Parsing is **incremental**: a byte-offset index (`~/.config/rtk-pulse/index.json`)
means only appended transcript data is re-read. A cold scan of ~300 MB takes
under a second; live updates are near-free.

## Usage

```bash
python3 pulse.py serve --open      # dashboard at http://localhost:8377
python3 pulse.py report            # terminal report
python3 pulse.py save              # snapshot today's usage to history.jsonl
python3 pulse.py scan [--force]    # (re)build the index
```

Optional alias:

```bash
alias pulse='python3 ~/workspace/rtk/pulse.py'
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

## Cost model

Estimates use API list prices per MTok — Fable 5 $10/$50 · Opus 4.8/4.7/4.6
$5/$25 · older Opus $15/$75 · Sonnet $3/$15 · Haiku 4.5 $1/$5 — with cache
read at 0.1× input and cache writes at 1.25× (5m TTL) / 2× (1h TTL). These are
**estimates of equivalent API cost**, not what a subscription plan bills.

Message dedup follows the transcript format: multi-block assistant messages
repeat the same `usage` on adjacent lines, so events are deduped on
`requestId + message.id`.

## Files

```
pulse.py         CLI + collector + HTTP/SSE server (stdlib only)
dashboard.html   dark-theme dashboard (Chart.js via CDN)
```
