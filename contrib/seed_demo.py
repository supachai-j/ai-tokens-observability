#!/usr/bin/env python3
"""Seed a self-contained demo data directory for screenshots and CI previews.

Creates obviously-fake data under a temporary (or specified) directory.
No real user data is touched.

Usage:
    python3 contrib/seed_demo.py               # print RTK_PULSE_HOME= and then serve
    python3 contrib/seed_demo.py --out /tmp/demo-data
    RTK_PULSE_HOME=$(python3 contrib/seed_demo.py --out /tmp/demo --quiet) \\
        python3 pulse.py serve --open

All project names, models, and costs are synthetic.
"""

import argparse
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Fake data parameters — obviously fictional
# ---------------------------------------------------------------------------

FAKE_PROJECTS = [
    "acme-billing-api",
    "robot-haiku-generator",
    "infinite-todo-app",
    "hyperdrive-scheduler",
    "quantum-standup-bot",
]

FAKE_MODELS = [
    "claude-opus-4-8",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
]

FAKE_NODES = [
    ("macbook-pro",    True,   2.4),   # (name, is_local, daily_cost_usd)
    ("ci-runner-01",   False,  0.8),
    ("ci-runner-02",   False,  0.4),
]

# Rough per-model cost fractions (input_per_MTok, output_per_MTok)
_MODEL_RATES = {
    "claude-opus-4-8":  (5.0, 25.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0,  5.0),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry(out_tokens, model, cache_frac=0.93, msgs=1):
    """Build a minimal index entry dict."""
    in_tok = int(out_tokens * 0.4)
    cr_tok = int(in_tok * cache_frac)
    cc5_tok = int(in_tok * 0.03)
    raw_in = in_tok - cr_tok - cc5_tok
    ir, outr = _MODEL_RATES.get(model, (3.0, 15.0))
    cost = (raw_in * ir + cc5_tok * ir * 1.25 + cr_tok * ir * 0.1
            + out_tokens * outr) / 1_000_000
    return {"in": raw_in, "out": out_tokens, "cc5": cc5_tok, "cc1": 0,
            "cr": cr_tok, "n": msgs, "cost": round(cost, 6)}


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _add(a, b):
    return {k: a.get(k, 0) + b.get(k, 0) for k in ("in", "out", "cc5", "cc1", "cr", "n", "cost")}


# ---------------------------------------------------------------------------
# Build fake index
# ---------------------------------------------------------------------------

def build_fake_index(num_days=35):
    """Return a v3 index dict with multi-project, multi-model fake data."""
    now = datetime.now(tz=timezone.utc)
    days = {}

    import random
    rng = random.Random(42)  # deterministic seed

    for d in range(num_days, -1, -1):
        day_str = (now - timedelta(days=d)).strftime("%Y-%m-%d")
        # weight toward recent days
        daily_scale = max(0.3, 1.0 - d * 0.008) * rng.uniform(0.7, 1.3)
        day_data = {}
        for proj in FAKE_PROJECTS:
            proj_scale = rng.uniform(0.5, 2.5)
            day_data[proj] = {}
            for mdl in FAKE_MODELS:
                out_tok = int(rng.randint(8_000, 60_000) * daily_scale * proj_scale)
                if out_tok < 200:
                    continue
                msgs = rng.randint(3, 40)
                day_data[proj][mdl] = _entry(out_tok, mdl, msgs=msgs)
        if day_data:
            days[day_str] = day_data

    return {
        "version": 3,
        "files": {},          # scanner hasn't run — that's fine for demo
        "days": days,
        "activity": {},
        "recent": [],
    }


# ---------------------------------------------------------------------------
# Build fake history.jsonl
# ---------------------------------------------------------------------------

def build_fake_history(num_days=90):
    """Return list of snapshot dicts for history.jsonl."""
    now = datetime.now(tz=timezone.utc)
    import random
    rng = random.Random(7)
    lines = []
    for d in range(num_days, 0, -1):
        day_str = (now - timedelta(days=d)).strftime("%Y-%m-%d")
        scale = max(0.4, 1.0 - d * 0.004) * rng.uniform(0.6, 1.4)
        out = int(rng.randint(100_000, 500_000) * scale)
        entry = _entry(out, "claude-sonnet-4-5", msgs=rng.randint(30, 200))
        lines.append({
            "date": day_str,
            "saved_at": _iso(now - timedelta(days=d)),
            **entry,
        })
    return lines


# ---------------------------------------------------------------------------
# Build fake node snapshots (for fleet panel)
# ---------------------------------------------------------------------------

def build_fake_node_snapshot(node_name, daily_cost_usd, num_days=35):
    """Return a node snapshot dict (schema v1)."""
    now = datetime.now(tz=timezone.utc)
    import random
    rng = random.Random(sum(ord(c) for c in node_name))
    snap_days = {}
    for d in range(num_days, -1, -1):
        day_str = (now - timedelta(days=d)).strftime("%Y-%m-%d")
        day_data = {}
        for mdl in FAKE_MODELS:
            fraction = rng.uniform(0.1, 0.6)
            target_cost = daily_cost_usd * fraction * rng.uniform(0.5, 1.5)
            _, outr = _MODEL_RATES[mdl]
            out_tok = max(0, int(target_cost / outr * 1_000_000))
            if out_tok < 100:
                continue
            day_data[mdl] = _entry(out_tok, mdl, msgs=rng.randint(2, 20))
        if day_data:
            snap_days[day_str] = day_data
    return {
        "schema": 1,
        "node": node_name,
        "generated_at": _iso(now - timedelta(minutes=rng.randint(5, 120))),
        "days": snap_days,
    }


# ---------------------------------------------------------------------------
# Write everything to the output directory
# ---------------------------------------------------------------------------

def seed(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    nodes_dir = out_dir / "nodes"
    nodes_dir.mkdir(exist_ok=True)

    # index.json
    idx = build_fake_index()
    tmp = out_dir / "index.json.tmp"
    tmp.write_text(json.dumps(idx), encoding="utf-8")
    tmp.rename(out_dir / "index.json")

    # history.jsonl
    lines = build_fake_history()
    hist_path = out_dir / "history.jsonl"
    hist_path.write_text(
        "\n".join(json.dumps(l) for l in lines) + "\n",
        encoding="utf-8",
    )

    # node snapshots (remote nodes only — local is recomputed live)
    for name, is_local, daily_cost in FAKE_NODES:
        if is_local:
            continue  # live-local never pre-seeded
        snap = build_fake_node_snapshot(name, daily_cost)
        snap_path = nodes_dir / f"{name}.json"
        tmp = nodes_dir / f"{name}.json.tmp"
        tmp.write_text(json.dumps(snap), encoding="utf-8")
        tmp.rename(snap_path)

    return out_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", metavar="DIR", default=None,
                    help="output directory (default: new tempdir)")
    ap.add_argument("--quiet", action="store_true",
                    help="print only the data dir path (for shell substitution)")
    args = ap.parse_args()

    if args.out:
        out_dir = Path(args.out)
    else:
        out_dir = Path(tempfile.mkdtemp(prefix="rtk-pulse-demo-"))

    seed(out_dir)

    if args.quiet:
        print(out_dir)
    else:
        print(f"Demo data seeded to: {out_dir}")
        print()
        print("Serve the demo dashboard:")
        print(f"  RTK_PULSE_HOME={out_dir} python3 pulse.py serve --open")
        print()
        print("Or one-liner:")
        print("  RTK_PULSE_HOME=$(python3 contrib/seed_demo.py --quiet) \\")
        print("      python3 pulse.py serve --open")
        print()
        print("Fake projects:", ", ".join(FAKE_PROJECTS))
        print("Fake nodes:   ", ", ".join(n for n, _, _ in FAKE_NODES if not _))


if __name__ == "__main__":
    main()
