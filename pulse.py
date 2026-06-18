#!/usr/bin/env python3
"""AI Tokens Observability — live token-usage monitoring for Claude Code, rtk-style.

Zero dependencies (Python 3.9+ stdlib only).

Commands:
  serve   Live web dashboard (SSE) at http://localhost:8377
  report  rtk-style terminal report with indicator bars
  save    Append today's usage snapshot to history.jsonl
  scan    Rebuild/update the incremental index

Data sources:
  ~/.claude/projects/**/*.jsonl   Claude Code transcripts (token usage per message)
  rtk gain --format json          rtk token-savings analytics (optional)
"""

__version__ = "0.1.0"

import argparse
import calendar
import csv
import html
import io
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.request
from urllib.parse import parse_qs, urlparse

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
GEMINI_TMP = Path.home() / ".gemini" / "tmp"
DATA_DIR = Path(os.environ.get("RTK_PULSE_HOME", Path.home() / ".config" / "rtk-pulse"))
INDEX_FILE = DATA_DIR / "index.json"
HISTORY_FILE = DATA_DIR / "history.jsonl"
BUDGET_ALERT_FILE = DATA_DIR / "budget_alert.json"
SPIKE_ALERT_FILE = DATA_DIR / "spike_alert.json"
PRICING_FILE = DATA_DIR / "pricing.json"
NODES_DIR = DATA_DIR / "nodes"
SPIKE_WINDOW_DAYS = 7      # trailing window for baseline mean
SPIKE_MIN_ACTIVE  = 3      # need ≥3 active days for a reliable baseline
MIN_FORECAST_DAY  = 3      # don't project until ≥3 days into the month
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8377
LIVE_WINDOW_MIN = 10  # a project counts as "live" if active within this many minutes
KEEP_DAYS = 90
HISTORY_KEEP_DAYS = 730  # history.jsonl is pruned to ~2 yr, outliving the 90-day index

# Claude stores projects as directory names that replace "/" with "-" and strip
# the leading "/".  On a typical macOS/Linux install the home directory itself
# becomes a prefix we want to drop so that e.g.
#   "-Users-alice-workspace-rtk" → "workspace-rtk"
# Derive the prefix at import time so it works for any username / OS.
HOME_PREFIX = str(Path.home()).replace("/", "-") + "-"

# $/MTok (input, output), list-price estimates. Matched top-down by substring.
PRICING = [
    # Anthropic (Claude Code)
    ("fable", 10.0, 50.0),
    ("opus-4-8", 5.0, 25.0),
    ("opus-4-7", 5.0, 25.0),
    ("opus-4-6", 5.0, 25.0),
    ("opus", 15.0, 75.0),       # opus 4.5 / 4.1 / 4.0 / 3
    ("sonnet", 3.0, 15.0),
    ("haiku-4-5", 1.0, 5.0),
    ("haiku-3-5", 0.8, 4.0),
    ("haiku", 0.25, 1.25),
    # OpenAI (Codex CLI)
    ("codex-mini", 1.5, 6.0),
    ("gpt-4o-mini", 0.15, 0.6),
    ("gpt-4o", 2.5, 10.0),
    ("gpt-4.1-mini", 0.4, 1.6),
    ("gpt-4.1", 2.0, 8.0),
    ("o4-mini", 1.1, 4.4),
    ("o3", 2.0, 8.0),
    # Google (Gemini CLI)
    ("gemini-3-pro", 2.0, 12.0),
    ("gemini-3-flash", 0.3, 2.5),
    ("gemini-2.5-pro", 1.25, 10.0),
    ("gemini-2.5-flash-lite", 0.1, 0.4),
    ("gemini-2.5-flash", 0.3, 2.5),
    ("gemini", 1.25, 10.0),
]


def model_source(model):
    """Which tool/vendor a model id belongs to."""
    m = model.lower()
    if m.startswith("claude") or any(s in m for s in ("opus", "sonnet", "haiku", "fable")):
        return "claude"
    if m.startswith(("gpt", "o1", "o3", "o4", "codex", "davinci")):
        return "codex"
    if m.startswith("gemini"):
        return "gemini"
    return "other"


PRICING_TTL = 5  # seconds between stat() checks (cost_usd is HOT during scans)
_pricing_mem = {"checked": 0.0, "mtime": None, "data": {}}


def _pricing_overrides():
    """User pricing overrides from PRICING_FILE → {substring(lower): (in_rate, out_rate)}.

    Cached by mtime; stat() gated to once per PRICING_TTL so a 13k-cell scan
    re-stats at most once per 5 seconds rather than per cell.
    Any error (missing file / malformed JSON / invalid entries) → {} so built-ins
    are used — this function never raises.
    """
    now = time.time()
    if now - _pricing_mem["checked"] < PRICING_TTL:
        return _pricing_mem["data"]
    _pricing_mem["checked"] = now
    try:
        st = PRICING_FILE.stat()
    except OSError:
        # File absent or inaccessible — clear stale data if mtime was set
        if _pricing_mem["mtime"] is not None:
            _pricing_mem.update(mtime=None, data={})
        return _pricing_mem["data"]
    if st.st_mtime == _pricing_mem["mtime"]:
        return _pricing_mem["data"]
    data = {}
    try:
        raw = json.loads(PRICING_FILE.read_text())
        if isinstance(raw, dict):
            for k, v in raw.items():
                # v must be a 2-element list/tuple of non-negative numbers (not bool)
                if (isinstance(v, (list, tuple)) and len(v) == 2
                        and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                for x in v)
                        and v[0] >= 0 and v[1] >= 0):
                    data[str(k).lower()] = (float(v[0]), float(v[1]))
    except (OSError, ValueError):
        data = {}
    _pricing_mem.update(mtime=st.st_mtime, data=data)
    return data


def rates_for(model):
    m = model.lower()
    # User overrides take precedence over all built-ins; longest key wins
    ov = _pricing_overrides()
    for sub in sorted(ov, key=len, reverse=True):
        if sub in m:
            return ov[sub]
    if "gpt-5" in m:  # gpt-5 family incl. dated/point releases and -codex variants
        if "nano" in m:
            return 0.05, 0.4
        if "mini" in m:
            return 0.25, 2.0
        if "pro" in m:
            return 15.0, 120.0
        return 1.25, 10.0
    for sub, i, o in PRICING:
        if sub in m:
            return i, o
    return {"codex": (1.25, 10.0), "gemini": (1.25, 10.0)}.get(
        model_source(model), (3.0, 15.0))


# cache-read price as a fraction of input price, per vendor
CACHE_READ_MULT = {"claude": 0.1, "codex": 0.1, "gemini": 0.25, "other": 0.1}


def cost_usd(model, inp, out, cc5, cc1, cr):
    ri, ro = rates_for(model)
    rm = CACHE_READ_MULT.get(model_source(model), 0.1)
    # cc5/cc1 (cache-write premium tiers) only occur for Anthropic models
    return (inp * ri + out * ro + cr * ri * rm + cc5 * ri * 1.25 + cc1 * ri * 2.0) / 1e6


# ---------------------------------------------------------------- index / scan

EMPTY = {"in": 0, "out": 0, "cc5": 0, "cc1": 0, "cr": 0, "n": 0, "cost": 0.0}

_lock = threading.RLock()  # RLock allows recursive re-entry (refresh_index force=True path)


def _empty_index():
    # days is day -> project -> model -> entry (enables any filter combination)
    return {"version": 3, "files": {}, "days": {}, "activity": {}, "recent": []}


def _load_index():
    try:
        with open(INDEX_FILE) as f:
            idx = json.load(f)
        if idx.get("version") == 3:
            return idx
    except (OSError, ValueError):
        pass
    return _empty_index()


def _save_index(idx):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(idx, f, separators=(",", ":"))
    tmp.replace(INDEX_FILE)


def _project_name(jsonl_path):
    name = jsonl_path.parent.name
    if name.startswith(HOME_PREFIX):
        return name[len(HOME_PREFIX):]
    return name


def _cwd_project(cwd):
    """Normalize a cwd path to claude-style project naming (workspace-rtk)."""
    if not cwd:
        return ""
    try:
        rel = str(Path(cwd).relative_to(Path.home()))
    except ValueError:
        rel = cwd.lstrip("/")
    return rel.replace("/", "-")


def _emit(idx, ts, project, model, inp, out, cc5, cc1, cr, session=""):
    """Record one usage event into all index aggregates."""
    if not ts or not model:
        return
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return
    day = dt.strftime("%Y-%m-%d")
    c = cost_usd(model, inp, out, cc5, cc1, cr)
    _bump(idx["days"].setdefault(day, {}).setdefault(project, {}),
          model, inp, out, cc5, cc1, cr, c)
    act = idx["activity"].setdefault(project, {"ts": "", "model": "", "session": ""})
    if ts > act["ts"]:
        act.update(ts=ts, model=model, session=session)
    # ring buffer for live monitoring (pruned to 2h / 500 events on refresh)
    idx.setdefault("recent", []).append(
        [ts, project, model, inp, out, cr + cc5 + cc1, round(c, 6)])


def _bump(bucket, key, inp, out, cc5, cc1, cr, cost):
    e = bucket.setdefault(key, dict(EMPTY))
    e["in"] += inp
    e["out"] += out
    e["cc5"] += cc5
    e["cc1"] += cc1
    e["cr"] += cr
    e["n"] += 1
    e["cost"] += cost


def _scan_claude(path, offset, state, idx):
    """Claude Code transcript: one JSON event per appended line."""
    project = _project_name(path)
    last_key = state.get("key", "")
    with open(path, "rb") as f:
        f.seek(offset)
        for raw in f:
            offset += len(raw)
            if not raw.endswith(b"\n"):  # partial line still being written
                offset -= len(raw)
                break
            if b'"assistant"' not in raw or b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if d.get("type") != "assistant":
                continue
            m = d.get("message") or {}
            usage = m.get("usage") or {}
            model = m.get("model") or ""
            if not usage or not model or model == "<synthetic>":
                continue
            # Multi-block assistant messages repeat the same usage on adjacent
            # lines — dedupe on requestId+message.id.
            key = (d.get("requestId") or "") + "/" + (m.get("id") or "")
            if key != "/" and key == last_key:
                continue
            last_key = key
            cc = usage.get("cache_creation_input_tokens") or 0
            det = usage.get("cache_creation") or {}
            cc1 = det.get("ephemeral_1h_input_tokens") or 0
            cc5 = det.get("ephemeral_5m_input_tokens") or 0
            if cc5 + cc1 == 0:
                cc5 = cc
            _emit(idx, d.get("timestamp") or "", project, model,
                  usage.get("input_tokens") or 0, usage.get("output_tokens") or 0,
                  cc5, cc1, usage.get("cache_read_input_tokens") or 0,
                  session=d.get("sessionId") or path.stem)
    state["key"] = last_key
    return offset, state


def _scan_codex(path, offset, state, idx):
    """Codex CLI rollout: token_count events carry cumulative totals."""
    project = state.get("project", "")
    model = state.get("model", "gpt")
    tot = state.get("tot") or {}
    with open(path, "rb") as f:
        f.seek(offset)
        for raw in f:
            offset += len(raw)
            if not raw.endswith(b"\n"):
                offset -= len(raw)
                break
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            t, p = d.get("type"), d.get("payload") or {}
            if t == "session_meta":
                project = _cwd_project(p.get("cwd")) or project
            elif t == "turn_context":
                model = p.get("model") or model
                if p.get("cwd"):
                    project = _cwd_project(p["cwd"])
            elif t == "event_msg" and p.get("type") == "token_count":
                info = p.get("info") or {}
                cur = info.get("total_token_usage") or {}
                if not cur:
                    continue
                din = (cur.get("input_tokens") or 0) - (tot.get("input_tokens") or 0)
                dout = (cur.get("output_tokens") or 0) - (tot.get("output_tokens") or 0)
                dcr = (cur.get("cached_input_tokens") or 0) - (tot.get("cached_input_tokens") or 0)
                if din < 0 or dout < 0:  # counter reset: fall back to last-turn usage
                    last = info.get("last_token_usage") or {}
                    din = last.get("input_tokens") or 0
                    dout = last.get("output_tokens") or 0
                    dcr = last.get("cached_input_tokens") or 0
                tot = cur
                if din <= 0 and dout <= 0:  # duplicate emission
                    continue
                # input_tokens includes cached_input_tokens
                _emit(idx, d.get("timestamp") or "", project or "codex", model,
                      max(0, din - dcr), dout, 0, 0, max(0, dcr),
                      session=path.stem)
    state.update(project=project, model=model, tot=tot)
    return offset, state


def _gemini_event(idx, m, project, fallback_ts, session):
    tk = m.get("tokens") or {}
    model = m.get("model") or ""
    if not tk or not model:
        return
    inp = (tk.get("input") or 0) + (tk.get("tool") or 0)
    cr = tk.get("cached") or 0
    out = (tk.get("output") or 0) + (tk.get("thoughts") or 0)
    # input includes cached tokens
    _emit(idx, m.get("timestamp") or fallback_ts, project, model,
          max(0, inp - cr), out, 0, 0, cr, session=session)


def _scan_gemini_jsonl(path, offset, state, idx):
    """Gemini CLI chat (.jsonl): header line then one message per line."""
    project = path.relative_to(GEMINI_TMP).parts[0]
    with open(path, "rb") as f:
        f.seek(offset)
        for raw in f:
            offset += len(raw)
            if not raw.endswith(b"\n"):
                offset -= len(raw)
                break
            if b'"tokens"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            _gemini_event(idx, d, project, "", path.stem)
    return offset, state


def _scan_gemini_json(path, state, idx):
    """Gemini CLI chat (.json): whole-document file, messages appended over time."""
    project = path.relative_to(GEMINI_TMP).parts[0]
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return state
    msgs = doc.get("messages") or []
    n = state.get("n", 0)
    for m in msgs[n:]:
        _gemini_event(idx, m, project, doc.get("lastUpdated") or "",
                      doc.get("sessionId") or path.stem)
    state["n"] = len(msgs)
    return state


def _discover():
    """All usage files across supported tools: (path, kind)."""
    files = []
    if CLAUDE_PROJECTS.is_dir():
        for p in sorted(CLAUDE_PROJECTS.glob("*/*.jsonl")):
            files.append((p, "claude"))
    if CODEX_SESSIONS.is_dir():
        for p in sorted(CODEX_SESSIONS.rglob("rollout-*.jsonl")):
            files.append((p, "codex"))
    if GEMINI_TMP.is_dir():
        for proj in sorted(GEMINI_TMP.iterdir()):
            chats = proj / "chats"
            if not chats.is_dir():
                continue
            for p in sorted(chats.rglob("*.json")):
                files.append((p, "gemini-json"))
            for p in sorted(chats.rglob("*.jsonl")):
                files.append((p, "gemini-jsonl"))
    return files


def refresh_index(force=False):
    """Incrementally scan transcripts; returns (index, changed: bool)."""
    with _lock:
        idx = _load_index()
        if force:
            idx = _empty_index()
        changed = False
        seen = set()
        for path, kind in _discover():
            try:
                st = path.stat()
            except OSError:
                continue
            p = str(path)
            seen.add(p)
            rec = idx["files"].get(p)
            if rec and rec["size"] == st.st_size and rec["mtime"] == st.st_mtime:
                continue
            append_only = kind in ("claude", "codex", "gemini-jsonl")
            if rec and append_only and st.st_size < rec.get("offset", 0):
                # truncated file: aggregates can't be subtracted — full rebuild
                return refresh_index(force=True)
            offset = rec.get("offset", 0) if rec else 0
            state = (rec.get("state") if rec else None) or {}
            try:
                if kind == "claude":
                    offset, state = _scan_claude(path, offset, state, idx)
                elif kind == "codex":
                    offset, state = _scan_codex(path, offset, state, idx)
                elif kind == "gemini-jsonl":
                    offset, state = _scan_gemini_jsonl(path, offset, state, idx)
                else:  # gemini-json: whole-document parse, cursor is message count
                    state = _scan_gemini_json(path, state, idx)
                    offset = st.st_size
            except OSError:
                continue
            idx["files"][p] = {"size": st.st_size, "mtime": st.st_mtime,
                               "offset": offset, "kind": kind, "state": state}
            changed = True
        gone = [p for p in idx["files"] if p not in seen]
        for p in gone:
            del idx["files"][p]
        cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        for day in [d for d in idx["days"] if d < cutoff]:
            del idx["days"][day]
            changed = True
        # transcript timestamps are UTC "...Z" — lexicographic compare works
        cut2h = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        idx["recent"] = [r for r in idx.get("recent", []) if r[0] >= cut2h][-500:]
        if changed or force:
            _save_index(idx)
        return idx, changed


# ---------------------------------------------------------------- summaries

def _sum(entries):
    tot = dict(EMPTY)
    for e in entries:
        for k in tot:
            tot[k] += e[k]
    return tot


def _acc(bucket, key, e):
    t = bucket.setdefault(key, dict(EMPTY))
    for k in EMPTY:
        t[k] += e[k]


def _agg(idx, days, project=None, model=None, source=None):
    """Aggregate the day->project->model index with optional filters."""
    cutoff = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    daily, by_model, by_project = [], {}, {}
    total = dict(EMPTY)
    for day in sorted(idx["days"]):
        if day < cutoff:
            continue
        day_models = {}
        for prj, models in idx["days"][day].items():
            if project and prj != project:
                continue
            for mdl, e in models.items():
                if model and mdl != model:
                    continue
                if source and model_source(mdl) != source:
                    continue
                _acc(day_models, mdl, e)
                _acc(by_model, mdl, e)
                _acc(by_project, prj, e)
                for k in EMPTY:
                    total[k] += e[k]
        daily.append({"date": day, "models": day_models, **_sum(day_models.values())})
    return daily, by_model, by_project, total


FX_FILE = DATA_DIR / "fx.json"
FX_FALLBACK_THB = 32.0
FX_TTL = 12 * 3600
_fx_mem = {"ts": 0.0, "thb": None, "src": ""}


def fx_thb():
    """USD->THB rate: env override > fresh cache > live API > stale cache > fallback."""
    env = os.environ.get("RTK_PULSE_THB")
    if env:
        try:
            return float(env), "env"
        except ValueError:
            pass
    if _fx_mem["thb"] and time.time() - _fx_mem["ts"] < 600:
        return _fx_mem["thb"], _fx_mem["src"]
    disk = None
    try:
        with open(FX_FILE) as f:
            disk = json.load(f)
        if time.time() - disk.get("ts", 0) < FX_TTL:
            _fx_mem.update(ts=time.time(), thb=disk["thb"], src="cached")
            return disk["thb"], "cached"
    except (OSError, ValueError, KeyError):
        disk = None
    try:
        with urllib.request.urlopen("https://open.er-api.com/v6/latest/USD",
                                    timeout=5) as r:
            rate = float(json.load(r)["rates"]["THB"])
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(FX_FILE, "w") as f:
            json.dump({"ts": time.time(), "thb": rate}, f)
        _fx_mem.update(ts=time.time(), thb=rate, src="live")
        return rate, "live"
    except Exception:
        if disk and disk.get("thb"):
            _fx_mem.update(ts=time.time(), thb=disk["thb"], src="stale")
            return disk["thb"], "stale"
        _fx_mem.update(ts=time.time(), thb=FX_FALLBACK_THB, src="fallback")
        return FX_FALLBACK_THB, "fallback"


_rtk_mem = {"ts": 0.0, "data": None}
RTK_TTL = 60  # seconds between rtk subprocess calls


def rtk_gain():
    """Run `rtk gain` and return the summary dict; cached for RTK_TTL seconds."""
    if _rtk_mem["ts"] and time.time() - _rtk_mem["ts"] < RTK_TTL:
        return _rtk_mem["data"]
    data = None
    try:
        out = subprocess.run(["rtk", "gain", "--format", "json"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            data = json.loads(out.stdout).get("summary")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    _rtk_mem.update(ts=time.time(), data=data)
    return data


def _budget_limit():
    """Read RTK_PULSE_BUDGET env var → float USD/month, or None if unset/invalid."""
    try:
        v = os.environ.get("RTK_PULSE_BUDGET")
        return float(v) if v else None
    except (ValueError, TypeError):
        return None


def _budget_thresholds():
    """Read RTK_PULSE_BUDGET_ALERT → sorted ascending list of positive floats.

    Default is [80.0, 100.0].  Invalid / blank tokens are silently skipped.
    If the var is unset or yields no valid values the default is returned.
    """
    raw = os.environ.get("RTK_PULSE_BUDGET_ALERT", "80,100")
    result = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            v = float(tok)
            if v > 0:
                result.append(v)
        except ValueError:
            pass
    return sorted(result) if result else [80.0, 100.0]


def _build_forecast(month_cost, limit, now):
    """Linear month-end projection from month-to-date spend.

    Returns projected end-of-month cost (None until MIN_FORECAST_DAY), and —
    when a budget limit is set — projected % of budget and the day-of-month
    the limit is projected to be crossed at the current daily rate.
    """
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    day = now.day
    out = {"projected": None, "projected_pct": None, "will_exceed": False,
           "exceed_day": None, "day_of_month": day, "days_in_month": days_in_month}
    if day < MIN_FORECAST_DAY:
        return out  # too early to project reliably
    daily_rate = month_cost / day  # treats today as a full day → mildly conservative
    out["projected"] = round(daily_rate * days_in_month, 4)
    if limit:
        out["projected_pct"] = round(out["projected"] / limit * 100, 2)
        if out["projected"] > limit and daily_rate > 0:
            day_cross = limit / daily_rate
            out["will_exceed"] = True
            out["exceed_day"] = min(days_in_month, max(day, math.ceil(day_cross)))
    return out


def _build_budget(month_cost, current_month, now):
    """Build the budget sub-object for build_summary."""
    limit = _budget_limit()
    thresholds = _budget_thresholds()
    if limit:
        pct = round(month_cost / limit * 100, 2)
        # highest threshold that has been crossed (≤ pct), or None
        crossed = None
        for t in thresholds:
            if t <= pct:
                crossed = t
    else:
        pct = None
        crossed = None
    return {
        "limit": limit,
        "month_cost": month_cost,
        "month": current_month,
        "pct": pct,
        "thresholds": thresholds,
        "crossed": crossed,
        "forecast": _build_forecast(month_cost, limit, now),
    }


def _spike_config():
    """Read RTK_PULSE_SPIKE and RTK_PULSE_SPIKE_MIN env vars.

    Returns (multiple, floor, enabled).
    multiple > 0 enables the alert; RTK_PULSE_SPIKE=0 (or negative) disables it.
    floor is the minimum today_cost (USD) required to fire.
    """
    raw = os.environ.get("RTK_PULSE_SPIKE", "3")
    try:
        mult = float(raw)
    except (ValueError, TypeError):
        mult = 3.0
    enabled = mult > 0
    try:
        floor = float(os.environ.get("RTK_PULSE_SPIKE_MIN", "5"))
    except (ValueError, TypeError):
        floor = 5.0
    if floor < 0:
        floor = 5.0
    return mult, floor, enabled


def _build_spike(idx, now, today_cost, project, model, source):
    """Compute the spike sub-object for build_summary.

    Baseline = mean daily cost over the SPIKE_WINDOW_DAYS calendar days BEFORE
    today (today excluded because it is partial), counting only days with cost > 0
    so idle/weekend $0 days do not deflate the mean and manufacture false positives.
    """
    mult, floor, enabled = _spike_config()
    today_str = now.strftime("%Y-%m-%d")
    costs = []
    for i in range(1, SPIKE_WINDOW_DAYS + 1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        day_cost = 0.0
        for prj, models in idx["days"].get(day, {}).items():
            if project and prj != project:
                continue
            for mdl, e in models.items():
                if model and mdl != model:
                    continue
                if source and model_source(mdl) != source:
                    continue
                day_cost += e["cost"]
        if day_cost > 0:
            costs.append(day_cost)
    n_active = len(costs)
    baseline = sum(costs) / n_active if n_active else 0.0
    ratio = round(today_cost / baseline, 2) if baseline > 0 else None
    triggered = bool(
        enabled
        and n_active >= SPIKE_MIN_ACTIVE
        and baseline > 0
        and today_cost >= floor
        and today_cost >= mult * baseline
    )
    # Attribution: top contributing project for today (skipped when project filter active)
    top_project, top_project_cost = None, 0.0
    if not project:
        proj_today = {}
        for prj, models in idx["days"].get(today_str, {}).items():
            for mdl, e in models.items():
                if model and mdl != model:
                    continue
                if source and model_source(mdl) != source:
                    continue
                proj_today[prj] = proj_today.get(prj, 0.0) + e["cost"]
        if proj_today:
            top_project, top_project_cost = max(proj_today.items(), key=lambda kv: kv[1])
    top_project_share = (
        round(top_project_cost / today_cost, 4)
        if top_project and today_cost > 0
        else None
    )
    return {
        "today_cost": round(today_cost, 4),
        "baseline": round(baseline, 4),
        "ratio": ratio,
        "multiple": mult,
        "floor": floor,
        "window_days": SPIKE_WINDOW_DAYS,
        "active_days": n_active,
        "enabled": enabled,
        "triggered": triggered,
        "date": today_str,
        "top_project": top_project,
        "top_project_cost": round(top_project_cost, 4),
        "top_project_share": top_project_share,
    }


def build_summary(idx, project=None, model=None, days=30, source=None):
    now = datetime.now().astimezone()
    today = now.strftime("%Y-%m-%d")
    daily, by_model, by_project, total = _agg(idx, days, project, model, source)
    today_models = {}
    for prj, models in idx["days"].get(today, {}).items():
        if project and prj != project:
            continue
        for mdl, e in models.items():
            if model and mdl != model:
                continue
            if source and model_source(mdl) != source:
                continue
            _acc(today_models, mdl, e)
    today_tot = _sum(today_models.values())
    live, cutoff = [], (now - timedelta(minutes=LIVE_WINDOW_MIN))
    for prj, act in idx["activity"].items():
        if project and prj != project:
            continue
        if model and act.get("model") != model:
            continue
        if source and model_source(act.get("model") or "") != source:
            continue
        try:
            ts = datetime.fromisoformat(act["ts"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts >= cutoff:
            live.append({"project": prj, "ts": act["ts"], "model": act["model"]})
    live.sort(key=lambda x: x["ts"], reverse=True)
    cache_denom = total["in"] + total["cr"] + total["cc5"] + total["cc1"]
    # live monitoring: activity feed + per-minute throughput over the last 60 min
    cut60 = (datetime.now(timezone.utc) - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%S")
    ev = sorted((r for r in idx.get("recent", [])
                 if r[0] >= cut60
                 and (not project or r[1] == project)
                 and (not model or r[2] == model)
                 and (not source or model_source(r[2]) == source)),
                key=lambda r: r[0], reverse=True)
    feed = [{"ts": r[0], "project": r[1], "model": r[2], "out": r[4], "cost": r[6]}
            for r in ev[:30]]
    buckets = {}
    for r in ev:
        try:
            dt = datetime.fromisoformat(r[0].replace("Z", "+00:00")).astimezone()
        except ValueError:
            continue
        b = buckets.setdefault(dt.strftime("%H:%M"), {"out": 0, "cost": 0.0, "n": 0})
        b["out"] += r[4]
        b["cost"] += r[6]
        b["n"] += 1
    start = now - timedelta(minutes=29)
    minutely = []
    for i in range(30):
        label = (start + timedelta(minutes=i)).strftime("%H:%M")
        minutely.append({"t": label, **buckets.get(label, {"out": 0, "cost": 0.0, "n": 0})})
    all_projects = sorted({p for d in idx["days"].values() for p in d})
    all_models = sorted({m for d in idx["days"].values()
                         for ms in d.values() for m in ms})
    all_sources = sorted({model_source(m) for m in all_models})
    # Monthly spend — independent of the days window; same project/model/source filters
    current_month = now.strftime("%Y-%m")
    month_cost = 0.0
    for day, day_data in idx["days"].items():
        if day[:7] != current_month:
            continue
        for prj, mdls in day_data.items():
            if project and prj != project:
                continue
            for mdl, e in mdls.items():
                if model and mdl != model:
                    continue
                if source and model_source(mdl) != source:
                    continue
                month_cost += e["cost"]
    by_tool = {}
    for mdl, e in by_model.items():
        _acc(by_tool, model_source(mdl), e)
    by_tool = dict(sorted(by_tool.items(), key=lambda kv: kv[1]["cost"], reverse=True))
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "filter": {"project": project or "", "model": model or "",
                   "days": days, "source": source or ""},
        "today": {**today_tot, "models": today_models},
        "window": total,
        "daily": daily,
        "by_model": by_model,
        "by_tool": by_tool,
        "by_project": dict(sorted(by_project.items(),
                                  key=lambda kv: kv[1]["cost"], reverse=True)[:20]),
        "cache_hit_rate": (total["cr"] / cache_denom) if cache_denom else 0.0,
        "live_sessions": live[:10],
        "feed": feed,
        "minutely": minutely,
        "projects": all_projects,
        "models": all_models,
        "sources": all_sources,
        "fx": dict(zip(("thb", "src"), fx_thb())),
        "rtk": rtk_gain(),
        "budget": _build_budget(month_cost, current_month, now),
        "spike": _build_spike(idx, now, today_tot["cost"], project, model, source),
    }


# ---------------------------------------------------------------- tracing

try:
    TRACE_MAX_STEPS = max(50, int(os.environ.get("RTK_PULSE_TRACE_MAX", 600)))
except (ValueError, TypeError):
    TRACE_MAX_STEPS = 600


def _ex(text, n=220):
    text = str(text or "").strip().replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")


def _usage_step(ts, model, inp, out, cr, cc=0):
    return {"ts": ts, "kind": "usage", "model": model,
            "in": inp, "out": out, "cache": cr + cc,
            "cost": round(cost_usd(model, inp, out, cc, 0, cr), 6)}


def _trace_claude(path):
    steps, last_usage_key = [], ""
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t, ts = d.get("type"), d.get("timestamp") or ""
            if t == "user":
                c = (d.get("message") or {}).get("content")
                if isinstance(c, str):
                    steps.append({"ts": ts, "kind": "prompt", "text": _ex(c)})
                elif isinstance(c, list):
                    for b in c:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text":
                            steps.append({"ts": ts, "kind": "prompt",
                                          "text": _ex(b.get("text"))})
                        elif b.get("type") == "tool_result":
                            content = b.get("content")
                            if isinstance(content, list):
                                content = " ".join(x.get("text", "") for x in content
                                                   if isinstance(x, dict))
                            steps.append({"ts": ts, "kind": "tool_result",
                                          "text": _ex(content),
                                          "error": bool(b.get("is_error"))})
            elif t == "assistant":
                m = d.get("message") or {}
                model = m.get("model") or ""
                for b in m.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        steps.append({"ts": ts, "kind": "assistant",
                                      "model": model, "text": _ex(b.get("text"))})
                    elif bt == "thinking":
                        steps.append({"ts": ts, "kind": "thinking",
                                      "model": model, "text": _ex(b.get("thinking"))})
                    elif bt == "tool_use":
                        name = b.get("name") or ""
                        steps.append({"ts": ts,
                                      "kind": "mcp" if name.startswith("mcp__") else "tool",
                                      "name": name,
                                      "text": _ex(json.dumps(b.get("input") or {}))})
                usage = m.get("usage") or {}
                key = (d.get("requestId") or "") + "/" + (m.get("id") or "")
                if usage and model and model != "<synthetic>" and key != last_usage_key:
                    last_usage_key = key
                    steps.append(_usage_step(
                        ts, model,
                        usage.get("input_tokens") or 0,
                        usage.get("output_tokens") or 0,
                        usage.get("cache_read_input_tokens") or 0,
                        usage.get("cache_creation_input_tokens") or 0))
    return steps


def _trace_codex(path):
    steps, model, prev = [], "", {}
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t, p = d.get("type"), d.get("payload") or {}
            ts = d.get("timestamp") or ""
            if t == "turn_context":
                model = p.get("model") or model
            elif t == "response_item":
                pt = p.get("type")
                if pt == "message":
                    c = p.get("content")
                    if isinstance(c, list):
                        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
                    kind = "prompt" if p.get("role") == "user" else "assistant"
                    steps.append({"ts": ts, "kind": kind, "model": model, "text": _ex(c)})
                elif pt in ("function_call", "custom_tool_call", "local_shell_call"):
                    name = p.get("name") or pt
                    steps.append({"ts": ts,
                                  "kind": "mcp" if "mcp" in name.lower() else "tool",
                                  "name": name,
                                  "text": _ex(p.get("arguments") or p.get("input") or "")})
                elif pt in ("function_call_output", "custom_tool_call_output"):
                    out = p.get("output")
                    if isinstance(out, dict):
                        out = out.get("content") or json.dumps(out)
                    steps.append({"ts": ts, "kind": "tool_result", "text": _ex(out)})
                elif pt == "reasoning":
                    s = p.get("summary")
                    if isinstance(s, list):
                        s = " ".join(x.get("text", "") if isinstance(x, dict) else str(x)
                                     for x in s)
                    if s:
                        steps.append({"ts": ts, "kind": "thinking",
                                      "model": model, "text": _ex(s)})
            elif t == "event_msg" and p.get("type") == "token_count":
                info = p.get("info") or {}
                last, cur = info.get("last_token_usage") or {}, info.get("total_token_usage") or {}
                if not last or cur == prev:
                    continue
                prev = cur
                inp = last.get("input_tokens") or 0
                cr = last.get("cached_input_tokens") or 0
                steps.append(_usage_step(ts, model, max(0, inp - cr),
                                         last.get("output_tokens") or 0, cr))
    return steps


def _trace_gemini(path):
    steps = []
    if path.suffix == ".jsonl":
        msgs = []
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    msgs.append(json.loads(line))
                except ValueError:
                    pass
    else:
        try:
            with open(path) as f:
                msgs = (json.load(f) or {}).get("messages") or []
        except (OSError, ValueError):
            return steps
    for m in msgs:
        if not isinstance(m, dict):
            continue
        ts = m.get("timestamp") or ""
        if m.get("type") == "user":
            steps.append({"ts": ts, "kind": "prompt", "text": _ex(m.get("content"))})
            continue
        model = m.get("model") or ""
        th = m.get("thoughts")
        if th:
            if isinstance(th, list):
                th = " ".join((x.get("subject") or x.get("description") or "")
                              if isinstance(x, dict) else str(x) for x in th)
            steps.append({"ts": ts, "kind": "thinking", "model": model, "text": _ex(th)})
        for tc in m.get("toolCalls") or []:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name") or "tool"
            steps.append({"ts": ts,
                          "kind": "mcp" if "mcp" in name.lower() else "tool",
                          "name": name,
                          "text": _ex(json.dumps(tc.get("args") or tc.get("arguments") or {})),
                          "error": tc.get("status") == "error"})
        if m.get("content"):
            steps.append({"ts": ts, "kind": "assistant", "model": model,
                          "text": _ex(m.get("content"))})
        tk = m.get("tokens") or {}
        if tk and model:
            inp = (tk.get("input") or 0) + (tk.get("tool") or 0)
            cr = tk.get("cached") or 0
            steps.append(_usage_step(ts, model, max(0, inp - cr),
                                     (tk.get("output") or 0) + (tk.get("thoughts") or 0), cr))
    return steps


_codex_project_cache = {}  # path_str → project name; first line is immutable


def _codex_project(path_str):
    """Resolve a codex session's project from its first line (session_meta cwd).
    Cached by path — the first line is immutable once written.
    OSError/ValueError are NOT cached so transient failures are retried.
    """
    if path_str in _codex_project_cache:
        return _codex_project_cache[path_str]
    try:
        with open(path_str, errors="replace") as f:
            first = json.loads(f.readline())
    except (OSError, ValueError):
        return ""  # don't cache — retry on next call
    prj = _cwd_project((first.get("payload") or {}).get("cwd") or "")
    _codex_project_cache[path_str] = prj  # cache success, incl. legit ""
    return prj


def list_sessions(project=None, source=None, limit=25):
    """Recent sessions across all tools, newest first."""
    out = []
    for path, kind in _discover():
        src = "gemini" if kind.startswith("gemini") else kind
        if source and src != source:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_size < 300:
            continue
        if kind == "claude":
            prj = _project_name(path)
        elif kind == "gemini-json" or kind == "gemini-jsonl":
            prj = path.relative_to(GEMINI_TMP).parts[0]
        else:
            prj = ""
        out.append({"path": str(path), "kind": kind, "source": src,
                    "project": prj, "mtime": st.st_mtime, "size": st.st_size})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    res = []
    for s in out:
        if s["kind"] == "codex" and not s["project"]:
            s["project"] = _codex_project(s["path"])
        if project and s["project"] != project:
            continue
        res.append(s)
        if len(res) >= limit:
            break
    return res


def build_trace(path_str):
    kinds = {str(p): k for p, k in _discover()}
    kind = kinds.get(path_str)
    if not kind:  # unknown/unsafe path — only discovered files are traceable
        return {"error": "unknown session"}
    path = Path(path_str)
    if kind == "claude":
        steps = _trace_claude(path)
    elif kind == "codex":
        steps = _trace_codex(path)
    else:
        steps = _trace_gemini(path)
    truncated = len(steps) > TRACE_MAX_STEPS
    if truncated:
        steps = steps[-TRACE_MAX_STEPS:]
    summary = {"steps": len(steps),
               "prompts": sum(1 for s in steps if s["kind"] == "prompt"),
               "tools": sum(1 for s in steps if s["kind"] == "tool"),
               "mcp": sum(1 for s in steps if s["kind"] == "mcp"),
               "in": sum(s.get("in", 0) for s in steps),
               "out": sum(s.get("out", 0) for s in steps),
               "cache": sum(s.get("cache", 0) for s in steps),
               "cost": round(sum(s.get("cost", 0) for s in steps), 4)}
    return {"path": path_str, "kind": kind, "steps": steps,
            "summary": summary, "truncated": truncated}


def save_snapshot(summary=None):
    if summary is None:
        idx, _ = refresh_index()
        summary = build_summary(idx)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    line = {
        "ts": summary["generated_at"],
        "date": summary["generated_at"][:10],
        "today": {k: v for k, v in summary["today"].items() if k != "models"},
        "last30_cost": summary["window"]["cost"],
        "cache_hit_rate": summary["cache_hit_rate"],
        "rtk_saved": (summary["rtk"] or {}).get("total_saved"),
    }
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(line, separators=(",", ":")) + "\n")
    # Prune history to HISTORY_KEEP_DAYS (~2yr) so the file doesn't grow unbounded.
    cutoff = (datetime.now() - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
    try:
        with open(HISTORY_FILE) as f:
            entries = f.readlines()
        pruned = [e for e in entries if _history_date(e) >= cutoff]
        if len(pruned) < len(entries):
            tmp = HISTORY_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                f.writelines(pruned)
            tmp.replace(HISTORY_FILE)
    except OSError:
        pass
    try:
        notify_budget(summary)
    except Exception:
        pass
    try:
        notify_spike(summary)
    except Exception:
        pass
    return line


def _history_date(line):
    """Extract the date field from a history.jsonl line; return '' on parse error."""
    try:
        return json.loads(line).get("date", "")
    except ValueError:
        return ""


def _native_notify(msg):
    """Fire a native OS notification with the given message string.

    Uses osascript on macOS; notify-send on Linux (when available).
    All exceptions are silently swallowed — callers must not depend on success.
    """
    try:
        if sys.platform == "darwin":
            # Escape for an AppleScript string literal so user-derived content
            # (e.g. project names) cannot break or inject into the script.
            safe = msg.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{safe}" with title "AI Tokens Observability"'],
                timeout=5, capture_output=True)
        elif shutil.which("notify-send"):
            # notify-send receives argv, no shell involved — already safe.
            subprocess.run(
                ["notify-send", "AI Tokens Observability", msg],
                timeout=5, capture_output=True)
    except (OSError, subprocess.TimeoutExpired):
        pass


def notify_budget(summary):
    """Fire a native OS notification when a new budget threshold is crossed.

    State is persisted in BUDGET_ALERT_FILE so each threshold fires at most once
    per calendar month.  A new month or a higher crossing re-triggers the alert.
    Safe to call from any thread — all exceptions are swallowed.
    """
    b = (summary or {}).get("budget") or {}
    crossed = b.get("crossed")
    if crossed is None:
        return
    month = b.get("month") or ""
    limit = b.get("limit") or 0
    pct = b.get("pct") or 0
    # Read state
    try:
        with open(BUDGET_ALERT_FILE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    alerted = state.get("alerted", 0) if state.get("month") == month else 0
    if crossed <= alerted:
        return
    # Persist new state before firing (so a crash in notify doesn't re-fire)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(BUDGET_ALERT_FILE, "w") as f:
            json.dump({"month": month, "alerted": crossed}, f)
    except OSError:
        pass
    msg = f"Month-to-date {pct:.1f}% of ${limit:.2f} budget ({month})"
    _native_notify(msg)


def notify_spike(summary):
    """Fire a native OS notification when today's cost is an anomalous spike.

    Fires at most once per calendar day — state persisted in SPIKE_ALERT_FILE.
    Safe to call from any thread — all exceptions are swallowed.
    """
    sp = (summary or {}).get("spike") or {}
    if not sp.get("triggered"):
        return
    date = sp.get("date") or ""
    try:
        with open(SPIKE_ALERT_FILE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    if state.get("date") == date and state.get("alerted"):
        return
    # Persist state before firing so a notify crash cannot re-fire
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(SPIKE_ALERT_FILE, "w") as f:
            json.dump({"date": date, "alerted": True}, f)
    except OSError:
        pass
    msg = (f"Today ${sp.get('today_cost', 0):.2f} is {sp.get('ratio') or 0:.1f}x "
           f"the {sp.get('window_days', 7)}-day average (${sp.get('baseline', 0):.2f})")
    tp = sp.get("top_project")
    if tp:
        disp = tp if len(tp) <= 40 else "…" + tp[-39:]
        disp = disp.replace("\n", " ").replace("\r", " ")
        msg += f" — top: {disp} (${sp.get('top_project_cost', 0):.2f})"
    _native_notify(msg)


def read_history(max_days=None):
    """Parse history.jsonl; dedupe to one record per calendar date (last wins);
    return ascending list. Tolerates malformed lines and missing/empty file.

    Per-day dict keys: date, cost, out, in (total incl. cache), n,
    cache_hit_rate, last30_cost, rtk_saved.
    """
    if max_days is None:
        max_days = HISTORY_KEEP_DAYS
    cutoff = (datetime.now() - timedelta(days=max_days - 1)).strftime("%Y-%m-%d")
    by_date = {}
    try:
        with open(HISTORY_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                date = rec.get("date") or (rec.get("ts") or "")[:10]
                if not date or date < cutoff:
                    continue
                today = rec.get("today") or {}
                by_date[date] = {
                    "date": date,
                    "cost": today.get("cost") or 0.0,
                    "out": today.get("out") or 0,
                    "in": ((today.get("in") or 0) + (today.get("cr") or 0) +
                           (today.get("cc5") or 0) + (today.get("cc1") or 0)),
                    "n": today.get("n") or 0,
                    "cache_hit_rate": rec.get("cache_hit_rate") or 0.0,
                    "last30_cost": rec.get("last30_cost") or 0.0,
                    "rtk_saved": rec.get("rtk_saved"),
                }
    except OSError:
        return []
    return sorted(by_date.values(), key=lambda x: x["date"])


_HISTORY_CSV_FIELDS = ["date", "cost", "out", "in", "n",
                       "cache_hit_rate", "last30_cost", "rtk_saved"]


def history_csv():
    """Return history.jsonl rows as CSV text (header + one row per day, ascending)."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_HISTORY_CSV_FIELDS, extrasaction="ignore")
    w.writeheader()
    w.writerows(read_history())
    return buf.getvalue()


# ---------------------------------------------------------------- fleet / multi-machine

def _node_name():
    """Canonical node identifier: RTK_PULSE_NODE env var, then hostname, then 'node'."""
    return (os.environ.get("RTK_PULSE_NODE") or socket.gethostname() or "node").strip()


def _node_slug(name):
    """Filesystem-safe slug from a node name.

    Replaces any character outside [A-Za-z0-9._-] with '-', strips leading
    dots (avoids hidden files / '..' components), and falls back to 'node'
    if the result is empty.  Critically, '/' and path separators are always
    replaced, so the slug can never escape NODES_DIR when joined with it.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", name).lstrip(".")
    return slug or "node"


def build_node_snapshot(idx, days=KEEP_DAYS):
    """Build a portable per-node snapshot from the in-memory index.

    Projects are COLLAPSED (summed) into day→model entries so that project
    names — the sensitive dimension — are never included in an exported file
    intended for a shared nodes/ directory.

    Returns a dict suitable for JSON serialisation and later merging via
    build_fleet().
    """
    now = datetime.now().astimezone()
    cutoff = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    days_out = {}
    for day, day_data in idx["days"].items():
        if day < cutoff:
            continue
        day_models = {}
        for _prj, models in day_data.items():
            for mdl, e in models.items():
                _acc(day_models, mdl, e)
        if day_models:
            days_out[day] = day_models
    return {
        "schema": 1,
        "node": _node_name(),
        "generated_at": now.isoformat(timespec="seconds"),
        "days": days_out,
    }


def cmd_export(out=None):
    """Build a node snapshot and write it to NODES_DIR/<slug>.json (default)
    or the path supplied via --out.  Prints the written path.
    """
    idx, _ = refresh_index()
    snap = build_node_snapshot(idx)
    if out is None:
        NODES_DIR.mkdir(parents=True, exist_ok=True)
        dest = NODES_DIR / (_node_slug(snap["node"]) + ".json")
    else:
        dest = Path(out)
        dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, separators=(",", ":")))
    tmp.replace(dest)
    print(str(dest))


def read_nodes():
    """Glob NODES_DIR/*.json and return a list of valid node snapshot dicts.

    Tolerates: missing directory, malformed JSON, non-dict payloads, missing
    required keys.  Accepts any file with a string 'node' key and a dict
    'days' key (does NOT require schema==1 for forward-compat).  Never raises.
    """
    result = []
    try:
        files = list(NODES_DIR.glob("*.json"))
    except OSError:
        return result
    for f in files:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if not isinstance(data.get("node"), str) or not data["node"]:
            continue
        if not isinstance(data.get("days"), dict):
            continue
        result.append(data)
    return result


def build_fleet(idx, days=30):
    """Merge remote node snapshots with the live-local node into a fleet summary.

    Steps:
    1. read_nodes() → dedupe by node name (newer generated_at wins).
    2. Overlay live-local (build_node_snapshot from in-memory idx); always
       supersedes any same-named file — local data is never stale.
    3. Compute per-node stats within the clamped day window.
    4. Sum fleet totals; build fleet_daily: sorted list of
       {date, nodes:{name: cost}} for the stacked daily chart.

    Returns a JSON-serialisable dict.  Does NOT touch build_summary or the
    SSE loop.
    """
    now = datetime.now().astimezone()
    today_str = now.strftime("%Y-%m-%d")
    cutoff = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")

    # --- 1. dedupe remote files by node name (newer generated_at wins)
    nodes_by_name = {}
    for nd in read_nodes():
        name = nd["node"]
        existing = nodes_by_name.get(name)
        if existing is None:
            nodes_by_name[name] = nd
        else:
            if (nd.get("generated_at") or "") > (existing.get("generated_at") or ""):
                nodes_by_name[name] = nd

    # --- 2. overlay live-local (always wins)
    local_name = _node_name()
    local_snap = build_node_snapshot(idx, days)
    local_snap["local"] = True
    nodes_by_name[local_name] = local_snap

    # --- 3. per-node stats within window
    node_stats = []
    for name, nd in nodes_by_name.items():
        nd_days = nd.get("days") or {}
        today_by_model = {}
        window_by_model = {}
        last_active = ""
        for day, models in nd_days.items():
            if day < cutoff:
                continue
            if not isinstance(models, dict):
                continue
            for mdl, e in models.items():
                if not isinstance(e, dict):
                    continue
                _acc(window_by_model, mdl, e)
                if day == today_str:
                    _acc(today_by_model, mdl, e)
                if day > last_active and (e.get("out") or e.get("n")):
                    last_active = day

        today_tot = _sum(today_by_model.values()) if today_by_model else dict(EMPTY)
        window_tot = _sum(window_by_model.values()) if window_by_model else dict(EMPTY)

        cache_denom = (window_tot["in"] + window_tot["cr"] +
                       window_tot["cc5"] + window_tot["cc1"])
        cache_hit_rate = (window_tot["cr"] / cache_denom) if cache_denom else 0.0

        top_model = ""
        if window_by_model:
            top_model = max(window_by_model.items(), key=lambda kv: kv[1]["out"])[0]

        stale_hours = None
        if nd.get("local"):
            stale_hours = 0.0
        else:
            ga = nd.get("generated_at")
            if ga:
                try:
                    ga_dt = datetime.fromisoformat(ga)
                    if ga_dt.tzinfo is None:
                        ga_dt = ga_dt.replace(tzinfo=now.tzinfo)
                    stale_hours = max(0.0, (now - ga_dt).total_seconds() / 3600)
                except ValueError:
                    pass

        node_stats.append({
            "node": name,
            "local": bool(nd.get("local")),
            "generated_at": nd.get("generated_at"),
            "stale_hours": stale_hours,
            "today": today_tot,
            "window": window_tot,
            "top_model": top_model,
            "cache_hit_rate": cache_hit_rate,
            "last_active": last_active,
        })

    # local first, then by window cost descending
    node_stats.sort(key=lambda n: (-int(n["local"]), -n["window"]["cost"]))

    # --- 4. fleet totals + daily cost breakdown per node
    fleet_today = _sum(n["today"] for n in node_stats) if node_stats else dict(EMPTY)
    fleet_window = _sum(n["window"] for n in node_stats) if node_stats else dict(EMPTY)

    # fleet_daily: [{date, nodes:{name: cost}}, …] sorted by date — feeds the stacked chart
    daily_by_date: dict = {}
    for name, nd in nodes_by_name.items():
        nd_days = nd.get("days") or {}
        for day, models in nd_days.items():
            if day < cutoff:
                continue
            if not isinstance(models, dict):
                continue
            day_cost = sum(
                e.get("cost", 0) for e in models.values() if isinstance(e, dict)
            )
            if day not in daily_by_date:
                daily_by_date[day] = {}
            daily_by_date[day][name] = round(
                daily_by_date[day].get(name, 0) + day_cost, 8
            )
    fleet_daily = [
        {"date": d, "nodes": daily_by_date[d]}
        for d in sorted(daily_by_date)
    ]

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "days": days,
        "local_node": local_name,
        "nodes": node_stats,
        "fleet": {"today": fleet_today, "window": fleet_window},
        "fleet_daily": fleet_daily,
    }


# ---------------------------------------------------------------- terminal report

def fmt_tok(n):
    if n >= 1e9:
        return f"{n / 1e9:.1f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}K"
    return str(int(n))


def bar(frac, width=24):
    full = max(0, min(width, round(frac * width)))
    return "█" * full + "░" * (width - full)


def cmd_report(days):
    idx, _ = refresh_index()
    s = build_summary(idx, days=days)
    t, today = s["window"], s["today"]
    print("AI Tokens Observability — Claude Code Token Usage")
    print("═" * 64)
    print(f"Today:      in {fmt_tok(today['in'] + today['cr'] + today['cc5'] + today['cc1']):>8}"
          f"   out {fmt_tok(today['out']):>8}   ≈ ${today['cost']:.2f}   ({today['n']} msgs)")
    print(f"Last {days:>2}d:   in {fmt_tok(t['in'] + t['cr'] + t['cc5'] + t['cc1']):>8}"
          f"   out {fmt_tok(t['out']):>8}   ≈ ${t['cost']:.2f}   ({t['n']} msgs)")
    print(f"Cache hits: {bar(s['cache_hit_rate'])} {s['cache_hit_rate'] * 100:.1f}%")
    if s["rtk"]:
        r = s["rtk"]
        pct = r.get("avg_savings_pct") or 0
        print(f"rtk saved:  {bar(pct / 100)} {pct:.1f}%  ({fmt_tok(r.get('total_saved') or 0)} tokens)")
    print()
    print(f"By Model (last {days}d)")
    print("─" * 64)
    by_model = sorted(s["by_model"].items(), key=lambda kv: kv[1]["cost"], reverse=True)
    max_cost = max((e["cost"] for _, e in by_model), default=1) or 1
    for mdl, e in by_model:
        print(f"  {mdl:<28} ${e['cost']:>8.2f}  {bar(e['cost'] / max_cost, 16)}  "
              f"out {fmt_tok(e['out'])}")
    print()
    print(f"By Project (top 10, last {days}d)")
    print("─" * 64)
    projects = list(s["by_project"].items())[:10]
    max_cost = max((e["cost"] for _, e in projects), default=1) or 1
    for prj, e in projects:
        name = prj if len(prj) <= 36 else "…" + prj[-35:]
        print(f"  {name:<38} ${e['cost']:>7.2f}  {bar(e['cost'] / max_cost, 12)}")
    print()
    days_rows = s["daily"][-14:]
    if days_rows:
        print("Daily cost (last 14d)")
        print("─" * 64)
        max_c = max((d["cost"] for d in days_rows), default=1) or 1
        for d in days_rows:
            print(f"  {d['date']}  ${d['cost']:>7.2f}  {bar(d['cost'] / max_c, 28)}")
    if s["live_sessions"]:
        print()
        print("Live now: " + ", ".join(x["project"] for x in s["live_sessions"]))


# ---------------------------------------------------------------- weekly digest

_DIGEST_TOOL_NAMES = {
    "claude": "Claude Code",
    "codex": "Codex CLI",
    "gemini": "Gemini CLI",
    "other": "Other",
}


def build_digest(idx, days=7):
    """Week-over-week digest: current period vs prior same-length period.

    Uses _agg twice — once for `days` and once for `2*days`.  The prior
    period total is the difference (both windows share the same right edge
    so the subtraction is exact for all additive EMPTY fields).
    """
    now = datetime.now().astimezone()
    today = now.strftime("%Y-%m-%d")
    start = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")

    cur_daily, cur_by_model, cur_by_project, cur_total = _agg(idx, days)
    _, _, _, two_total = _agg(idx, 2 * days)
    prev_total = {k: two_total[k] - cur_total[k] for k in EMPTY}

    delta_cost_pct = None
    if prev_total["cost"] > 0:
        delta_cost_pct = round(
            (cur_total["cost"] - prev_total["cost"]) / prev_total["cost"] * 100, 1)

    in_total = cur_total["in"] + cur_total["cr"] + cur_total["cc5"] + cur_total["cc1"]
    cache_denom = in_total
    cache_hit_rate = (cur_total["cr"] / cache_denom) if cache_denom else 0.0

    by_tool = {}
    for mdl, e in cur_by_model.items():
        _acc(by_tool, model_source(mdl), e)
    by_tool = dict(sorted(by_tool.items(), key=lambda kv: kv[1]["cost"], reverse=True))

    by_day = [{"date": d["date"], "cost": d["cost"], "out": d["out"], "n": d["n"]}
              for d in cur_daily]

    busiest_day = (max(cur_daily, key=lambda d: d["cost"]) if cur_daily else None)
    if busiest_day:
        busiest_day = {"date": busiest_day["date"], "cost": busiest_day["cost"]}

    top_projects = sorted(cur_by_project.items(),
                          key=lambda kv: kv[1]["cost"], reverse=True)[:5]
    top_projects = [{"project": p, "cost": e["cost"], "out": e["out"]}
                    for p, e in top_projects]

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "period": {"start": start, "end": today, "days": days},
        "totals": {"cost": cur_total["cost"], "out": cur_total["out"],
                   "in_total": in_total, "n": cur_total["n"]},
        "prev": {"cost": prev_total["cost"], "out": prev_total["out"],
                 "n": prev_total["n"]},
        "delta_cost_pct": delta_cost_pct,
        "cache_hit_rate": cache_hit_rate,
        "by_tool": by_tool,
        "by_day": by_day,
        "busiest_day": busiest_day,
        "top_projects": top_projects,
        "rtk": rtk_gain(),
    }


def digest_html(d):
    """Render a build_digest() dict as a self-contained HTML string.

    Hard constraints:
    - No JavaScript (<script>), no remote resources (CDN/http/https).
    - Inline CSS only; inline style="" for dynamic widths/colors.
    - All user-derived strings (project names, tool labels) go through html.escape().
    """
    esc = html.escape  # shorthand

    p = d.get("period") or {}
    t = d.get("totals") or {}
    pv = d.get("prev") or {}
    days = p.get("days", 7)
    start = p.get("start", "")
    end = p.get("end", "")

    # WoW delta
    delta = d.get("delta_cost_pct")
    if delta is not None:
        if delta >= 0:
            wow_html = (f'<span style="color:#e5484d">&#9650;{delta:.1f}%</span>'
                        f' vs prior {days}d &asymp;${pv.get("cost", 0):.2f}')
        else:
            wow_html = (f'<span style="color:#30a46c">&#9660;{abs(delta):.1f}%</span>'
                        f' vs prior {days}d &asymp;${pv.get("cost", 0):.2f}')
    else:
        wow_html = (f'<span style="color:#888">n/a</span>'
                    f' vs prior {days}d &asymp;${pv.get("cost", 0):.2f}')

    # Cache bar
    cache_rate = d.get("cache_hit_rate") or 0.0
    cache_pct = min(100.0, cache_rate * 100)
    cache_bar = (
        f'<div style="display:inline-block;background:#e5e7eb;border-radius:4px;'
        f'height:8px;width:120px;vertical-align:middle">'
        f'<div style="background:#3b82f6;height:8px;border-radius:4px;'
        f'width:{cache_pct:.0f}%"></div></div>')

    # By Tool table rows
    by_tool = d.get("by_tool") or {}
    max_tool_cost = max((e["cost"] for e in by_tool.values()), default=1) or 1
    tool_rows = []
    for tool, e in by_tool.items():
        label = esc(_DIGEST_TOOL_NAMES.get(tool, tool))
        pct = e["cost"] / max_tool_cost * 100
        bar = (f'<div style="background:#e5e7eb;border-radius:4px;height:8px;width:120px">'
               f'<div style="background:#3b82f6;height:8px;border-radius:4px;'
               f'width:{pct:.0f}%"></div></div>')
        tool_rows.append(
            f'<tr><td>{label}</td>'
            f'<td style="text-align:right">${e["cost"]:.2f}</td>'
            f'<td style="text-align:right">{fmt_tok(e["out"])}</td>'
            f'<td style="padding-left:8px">{bar}</td></tr>')
    tool_table = ("\n".join(tool_rows)
                  if tool_rows else '<tr><td colspan="4" style="color:#888">—</td></tr>')

    # Busiest day
    bd = d.get("busiest_day")
    busiest_html = (f'<p><strong>Busiest day:</strong> {esc(bd["date"])} &asymp;${bd["cost"]:.2f}</p>'
                    if bd else "")

    # Top projects table rows
    top_projects = d.get("top_projects") or []
    max_proj_cost = max((proj["cost"] for proj in top_projects), default=1) or 1
    proj_rows = []
    for proj in top_projects:
        name = proj["project"]
        display = name if len(name) <= 40 else "…" + name[-39:]
        pct = proj["cost"] / max_proj_cost * 100
        bar = (f'<div style="background:#e5e7eb;border-radius:4px;height:8px;width:120px">'
               f'<div style="background:#3b82f6;height:8px;border-radius:4px;'
               f'width:{pct:.0f}%"></div></div>')
        proj_rows.append(
            f'<tr><td>{esc(display)}</td>'
            f'<td style="text-align:right">${proj["cost"]:.2f}</td>'
            f'<td style="text-align:right">{fmt_tok(proj["out"])}</td>'
            f'<td style="padding-left:8px">{bar}</td></tr>')
    proj_table = ("\n".join(proj_rows)
                  if proj_rows else '<tr><td colspan="4" style="color:#888">—</td></tr>')

    # Daily cost list
    by_day = d.get("by_day") or []
    max_day_cost = max((row["cost"] for row in by_day), default=1) or 1
    day_rows = []
    for row in by_day:
        pct = row["cost"] / max_day_cost * 100
        bar = (f'<div style="display:inline-block;background:#e5e7eb;border-radius:4px;'
               f'height:8px;width:100px;vertical-align:middle">'
               f'<div style="background:#3b82f6;height:8px;border-radius:4px;'
               f'width:{pct:.0f}%"></div></div>')
        day_rows.append(
            f'<li style="margin:4px 0">'
            f'{esc(row["date"])} &nbsp; ${row["cost"]:.2f} &nbsp; {bar}</li>')
    day_list = "\n".join(day_rows) if day_rows else '<li style="color:#888">—</li>'

    # rtk savings
    rtk = d.get("rtk")
    rtk_html = ""
    if rtk:
        avg = rtk.get("avg_savings_pct") or 0
        saved = rtk.get("total_saved") or 0
        rtk_html = f'<p><strong>rtk savings:</strong> {avg:.1f}% avg &middot; {fmt_tok(saved)} tokens</p>'

    # Inline CSS (system font, light theme, no external refs)
    css = """
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                   Helvetica, Arial, sans-serif;
      font-size: 14px; line-height: 1.5; color: #111; background: #fff;
      max-width: 640px; margin: 32px auto; padding: 0 16px;
    }
    h1 { font-size: 20px; margin-bottom: 4px; }
    h2 { font-size: 15px; margin: 24px 0 8px; border-bottom: 1px solid #e5e7eb;
         padding-bottom: 4px; color: #333; }
    p { margin: 6px 0; }
    table { width: 100%; border-collapse: collapse; margin-top: 4px; }
    th { text-align: left; font-weight: 600; color: #555; font-size: 12px;
         padding: 4px 6px; border-bottom: 1px solid #e5e7eb; }
    td { padding: 5px 6px; border-bottom: 1px solid #f3f4f6; }
    ul { list-style: none; padding: 0; }
    .period { color: #555; font-size: 13px; margin-bottom: 16px; }
    .stat { margin: 4px 0; }
    footer { margin-top: 32px; font-size: 11px; color: #888; border-top: 1px solid #e5e7eb;
             padding-top: 8px; }
    """

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weekly Digest &mdash; {esc(start)} &rarr; {esc(end)}</title>
<style>{css}</style>
</head><body>
<h1>AI Tokens &mdash; Weekly Digest</h1>
<p class="period">{esc(start)} &rarr; {esc(end)} ({days}d)</p>

<h2>Totals</h2>
<p class="stat"><strong>Cost:</strong> ${t.get("cost", 0):.2f} &nbsp; {wow_html}</p>
<p class="stat"><strong>Input:</strong> {fmt_tok(t.get("in_total", 0))} &nbsp;
  <strong>Output:</strong> {fmt_tok(t.get("out", 0))} &nbsp;
  <strong>Messages:</strong> {t.get("n", 0)}</p>

<h2>Cache Hit Rate</h2>
<p>{cache_pct:.1f}% &nbsp; {cache_bar}</p>

{rtk_html}

<h2>By Tool</h2>
<table>
  <thead><tr>
    <th>Tool</th><th style="text-align:right">Cost (USD)</th>
    <th style="text-align:right">Output tokens</th><th></th>
  </tr></thead>
  <tbody>{tool_table}</tbody>
</table>

{busiest_html}

<h2>Top Projects</h2>
<table>
  <thead><tr>
    <th>Project</th><th style="text-align:right">Cost (USD)</th>
    <th style="text-align:right">Output tokens</th><th></th>
  </tr></thead>
  <tbody>{proj_table}</tbody>
</table>

<h2>Daily Cost</h2>
<ul>{day_list}</ul>

<footer>Generated {esc(d.get("generated_at", ""))} &middot; AI Tokens Observability</footer>
</body></html>"""


def cmd_digest(days, fmt):
    idx, _ = refresh_index()
    d = build_digest(idx, days=days)
    if fmt == "json":
        print(json.dumps(d, indent=2, default=str))
        return
    if fmt == "html":
        print(digest_html(d))
        return
    p = d["period"]
    t = d["totals"]
    pv = d["prev"]
    print(f"Weekly Digest — {p['start']} → {p['end']} ({p['days']}d)")
    print("═" * 64)
    print(f"Total:      in {fmt_tok(t['in_total']):>8}   out {fmt_tok(t['out']):>8}"
          f"   ≈ ${t['cost']:.2f}   ({t['n']} msgs)")
    if d["delta_cost_pct"] is not None:
        arrow = "▲" if d["delta_cost_pct"] >= 0 else "▼"
        wow = f"{arrow}{abs(d['delta_cost_pct']):.1f}%"
    else:
        wow = "n/a"
    print(f"vs prior {days:>2}d: ≈${pv['cost']:.2f}   WoW: {wow}")
    print(f"Cache hits: {bar(d['cache_hit_rate'])} {d['cache_hit_rate'] * 100:.1f}%")
    if d["rtk"]:
        r = d["rtk"]
        pct = r.get("avg_savings_pct") or 0
        print(f"rtk saved:  {bar(pct / 100)} {pct:.1f}%  "
              f"({fmt_tok(r.get('total_saved') or 0)} tokens)")
    if d["by_tool"]:
        print()
        print(f"By Tool (last {days}d)")
        print("─" * 64)
        max_cost = max((e["cost"] for e in d["by_tool"].values()), default=1) or 1
        for tool, e in d["by_tool"].items():
            label = _DIGEST_TOOL_NAMES.get(tool, tool)
            print(f"  {label:<20} ${e['cost']:>8.2f}  {bar(e['cost'] / max_cost, 16)}"
                  f"  out {fmt_tok(e['out'])}")
    if d["busiest_day"]:
        bd = d["busiest_day"]
        print()
        print(f"Busiest day: {bd['date']}  ≈${bd['cost']:.2f}")
    if d["top_projects"]:
        print()
        print(f"Top projects (last {days}d)")
        print("─" * 64)
        max_cost = max((proj["cost"] for proj in d["top_projects"]), default=1) or 1
        for proj in d["top_projects"]:
            name = proj["project"]
            if len(name) > 36:
                name = "…" + name[-35:]
            print(f"  {name:<38} ${proj['cost']:>7.2f}  {bar(proj['cost'] / max_cost, 12)}")


# ---------------------------------------------------------------- web server


def _dashboard_path():
    """Resolve dashboard.html for both clone/dev and pip/pipx installed setups.

    Search order:
    1. SCRIPT_DIR/dashboard.html  — git-clone / `python3 pulse.py serve` (current behaviour)
    2. sys.prefix/share/rtk-pulse/dashboard.html — pip/pipx wheel install (data-files)
    Returns candidate 1 unconditionally as fallback so the existing
    OSError → "dashboard.html not found" path in do_GET still triggers.
    """
    dev = SCRIPT_DIR / "dashboard.html"
    if dev.exists():
        return dev
    installed = Path(sys.prefix) / "share" / "rtk-pulse" / "dashboard.html"
    if installed.exists():
        return installed
    return dev  # triggers OSError in do_GET if neither exists


def _fs_state():
    """Cheap change detector across all tools: (file count, total size, max mtime)."""
    n = total = newest = 0
    for path, _ in _discover():
        try:
            st = path.stat()
        except OSError:
            continue
        n += 1
        total += st.st_size
        newest = max(newest, st.st_mtime)
    return (n, total, newest)


# Security: only serve requests whose Host header is a loopback name. This
# defends against DNS-rebinding, where a malicious web page rebinds its own
# domain to 127.0.0.1 to read the dashboard's sensitive transcript data through
# the victim's browser. Browsers keep the ORIGINAL hostname in Host, so a
# rebind of evil.com -> 127.0.0.1 still arrives as "Host: evil.com" — rejected.
ALLOWED_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

# Cap concurrent SSE (/events) streams: each holds a server thread in an
# infinite loop, so an unbounded number would exhaust ThreadingHTTPServer.
MAX_SSE_CONNECTIONS = 8
_sse_lock = threading.Lock()
_sse_active = 0

# Content-Security-Policy for the dashboard document. The page is a single file
# with inline <script>/<style> (hence 'unsafe-inline') and loads Chart.js from
# jsdelivr; everything else is same-origin. Blocks arbitrary external origins
# and framing (clickjacking) as defense-in-depth around the esc() output guard.
_CSP = ("default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'")


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-tokens-observability"

    def log_message(self, *args):
        pass

    def _host_ok(self):
        """True only if the Host header names a loopback address (anti-DNS-rebind)."""
        host = self.headers.get("Host", "")
        if not host:
            return False
        # Strip the port; handle bracketed IPv6 literals like [::1]:8377.
        if host.startswith("["):
            hostname = host[:host.index("]") + 1] if "]" in host else host
        else:
            hostname = host.rsplit(":", 1)[0] if ":" in host else host
        return hostname in ALLOWED_HOSTNAMES

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", _CSP)
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            self._send(403, "text/plain", b"forbidden: invalid Host header")
            return
        parsed = urlparse(self.path)
        route = parsed.path
        q = parse_qs(parsed.query)
        project = (q.get("project") or [""])[0] or None
        model = (q.get("model") or [""])[0] or None
        source = (q.get("source") or [""])[0] or None
        try:
            days = max(1, min(KEEP_DAYS, int((q.get("days") or ["30"])[0])))
        except ValueError:
            days = 30
        if route in ("/", "/index.html"):
            try:
                body = _dashboard_path().read_bytes()
            except OSError:
                body = b"dashboard.html not found"
            self._send(200, "text/html; charset=utf-8", body)
        elif route == "/api/summary":
            idx, _ = refresh_index()
            body = json.dumps(build_summary(idx, project, model, days, source)).encode()
            self._send(200, "application/json", body)
        elif route == "/api/sessions":
            body = json.dumps(list_sessions(project, source)).encode()
            self._send(200, "application/json", body)
        elif route == "/api/trace":
            path_str = (q.get("path") or [""])[0]
            body = json.dumps(build_trace(path_str)).encode()
            self._send(200, "application/json", body)
        elif route == "/api/history":
            body = json.dumps(read_history()).encode()
            self._send(200, "application/json", body)
        elif route == "/api/history.csv":
            body = history_csv().encode()
            self._send(200, "text/csv; charset=utf-8", body,
                       extra={"Content-Disposition": 'attachment; filename="rtk-pulse-history.csv"'})
        elif route == "/api/fleet":
            idx, _ = refresh_index()
            body = json.dumps(build_fleet(idx, days)).encode()
            self._send(200, "application/json", body)
        elif route == "/events":
            global _sse_active
            with _sse_lock:
                if _sse_active >= MAX_SSE_CONNECTIONS:
                    self._send(503, "text/plain", b"too many live connections")
                    return
                _sse_active += 1
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                last_state = None
                last_beat = 0.0
                try:
                    while True:
                        state = _fs_state()
                        if state != last_state:
                            last_state = state
                            idx, _ = refresh_index()
                            payload = json.dumps(build_summary(idx, project, model, days, source))
                            self.wfile.write(f"data: {payload}\n\n".encode())
                            self.wfile.flush()
                        elif time.time() - last_beat > 15:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            last_beat = time.time()
                        time.sleep(3)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            finally:
                with _sse_lock:
                    _sse_active -= 1
        else:
            self._send(404, "text/plain", b"not found")


def _snapshot_loop(interval_min=30):
    while True:
        time.sleep(interval_min * 60)
        try:
            save_snapshot()
        except Exception as e:  # broad catch: loop must survive any bad cycle
            print(f"[rtk-pulse] snapshot error (skipping): {e}", file=sys.stderr)


def cmd_serve(port, open_browser):
    print("Indexing transcripts (first run may take a moment)...")
    refresh_index()
    save_snapshot()
    threading.Thread(target=_snapshot_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"AI Tokens Observability dashboard → {url}")
    if open_browser:
        subprocess.Popen(["open", url] if sys.platform == "darwin" else ["xdg-open", url])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(prog="ai-tokens-observability", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("serve", help="live web dashboard")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--open", action="store_true", help="open browser")
    p = sub.add_parser("report", help="terminal usage report")
    p.add_argument("--days", type=int, default=30)
    p = sub.add_parser("digest", help="week-over-week digest (WoW deltas + by-tool)")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--format", choices=["text", "json", "html"], default="text", dest="fmt")
    sub.add_parser("save", help="append usage snapshot to history.jsonl")
    p = sub.add_parser("scan", help="update the incremental index")
    p.add_argument("--force", action="store_true", help="full rebuild")
    p = sub.add_parser("export", help="export a per-node snapshot to nodes/<slug>.json")
    p.add_argument("--out", metavar="PATH", default=None,
                   help="write to PATH instead of the default nodes/ location")
    args = ap.parse_args()

    if args.cmd == "serve":
        cmd_serve(args.port, args.open)
    elif args.cmd == "report":
        cmd_report(args.days)
    elif args.cmd == "digest":
        cmd_digest(args.days, args.fmt)
    elif args.cmd == "save":
        line = save_snapshot()
        print(json.dumps(line, indent=2))
    elif args.cmd == "scan":
        t0 = time.time()
        idx, changed = refresh_index(force=getattr(args, "force", False))
        print(f"indexed {len(idx['files'])} files, {len(idx['days'])} days "
              f"({'changed' if changed else 'no change'}, {time.time() - t0:.1f}s)")
    elif args.cmd == "export":
        cmd_export(getattr(args, "out", None))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
