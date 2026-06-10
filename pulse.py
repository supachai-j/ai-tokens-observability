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

import argparse
import csv
import io
import json
import os
import re
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


def rates_for(model):
    m = model.lower()
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
        "budget": {"limit": _budget_limit(), "month_cost": month_cost,
                   "month": current_month},
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
    return line


def _history_date(line):
    """Extract the date field from a history.jsonl line; return '' on parse error."""
    try:
        return json.loads(line).get("date", "")
    except ValueError:
        return ""


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


# ---------------------------------------------------------------- web server

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


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-tokens-observability"

    def log_message(self, *args):
        pass

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
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
                body = (SCRIPT_DIR / "dashboard.html").read_bytes()
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
        elif route == "/events":
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
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("serve", help="live web dashboard")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--open", action="store_true", help="open browser")
    p = sub.add_parser("report", help="terminal usage report")
    p.add_argument("--days", type=int, default=30)
    sub.add_parser("save", help="append usage snapshot to history.jsonl")
    p = sub.add_parser("scan", help="update the incremental index")
    p.add_argument("--force", action="store_true", help="full rebuild")
    args = ap.parse_args()

    if args.cmd == "serve":
        cmd_serve(args.port, args.open)
    elif args.cmd == "report":
        cmd_report(args.days)
    elif args.cmd == "save":
        line = save_snapshot()
        print(json.dumps(line, indent=2))
    elif args.cmd == "scan":
        t0 = time.time()
        idx, changed = refresh_index(force=getattr(args, "force", False))
        print(f"indexed {len(idx['files'])} files, {len(idx['days'])} days "
              f"({'changed' if changed else 'no change'}, {time.time() - t0:.1f}s)")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
