#!/usr/bin/env python3
"""Zero-dependency stdlib tests for pulse.py.

Run: python3 -m unittest test_pulse
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Make pulse importable from the same directory.
sys.path.insert(0, str(Path(__file__).parent))
import pulse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for obj in lines:
            f.write(json.dumps(obj) + "\n")


def _claude_msg(ts, model, inp, out, cr=0, cc=0, req_id="r1", msg_id="m1"):
    return {
        "type": "assistant",
        "timestamp": ts,
        "requestId": req_id,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cc,
            },
        },
    }


# ---------------------------------------------------------------------------
# 1. Cost / rates / model_source
# ---------------------------------------------------------------------------

class TestModelSource(unittest.TestCase):
    def test_claude_variants(self):
        for name in ("claude-3-5-sonnet", "claude-sonnet-4-5", "fable", "opus", "haiku"):
            self.assertEqual(pulse.model_source(name), "claude", name)

    def test_codex_variants(self):
        for name in ("gpt-4o", "gpt-4o-mini", "codex-mini", "o4-mini", "o3"):
            self.assertEqual(pulse.model_source(name), "codex", name)

    def test_gemini_variants(self):
        for name in ("gemini-2.5-pro", "gemini-2.5-flash", "gemini-3-flash"):
            self.assertEqual(pulse.model_source(name), "gemini", name)

    def test_other(self):
        self.assertEqual(pulse.model_source("llama-3"), "other")


class TestRatesFor(unittest.TestCase):
    def test_sonnet_cheaper_than_opus(self):
        si, so = pulse.rates_for("claude-sonnet-4-5")
        oi, oo = pulse.rates_for("claude-opus-4-6")
        self.assertLess(si, oi)
        self.assertLess(so, oo)

    def test_haiku_4_5_beats_haiku_plain(self):
        # haiku-4-5 should match "haiku-4-5" entry (1.0/5.0) not generic haiku (0.25/1.25)
        i, o = pulse.rates_for("claude-haiku-4-5")
        self.assertEqual(i, 1.0)
        self.assertEqual(o, 5.0)

    def test_haiku_3_5(self):
        i, o = pulse.rates_for("claude-haiku-3-5")
        self.assertEqual(i, 0.8)
        self.assertEqual(o, 4.0)

    def test_haiku_plain(self):
        i, o = pulse.rates_for("claude-haiku")
        self.assertEqual(i, 0.25)
        self.assertEqual(o, 1.25)

    def test_gpt4o_vs_gpt4o_mini_ordering(self):
        # gpt-4o-mini must not match gpt-4o first (substring ordering matters)
        i_mini, _ = pulse.rates_for("gpt-4o-mini")
        i_full, _ = pulse.rates_for("gpt-4o")
        self.assertLess(i_mini, i_full)

    def test_gpt5_nano(self):
        i, o = pulse.rates_for("gpt-5-nano")
        self.assertEqual(i, 0.05)

    def test_gpt5_mini(self):
        i, o = pulse.rates_for("gpt-5-mini")
        self.assertEqual(i, 0.25)

    def test_gpt5_default(self):
        i, o = pulse.rates_for("gpt-5")
        self.assertEqual(i, 1.25)

    def test_fable(self):
        i, o = pulse.rates_for("fable")
        self.assertEqual(i, 10.0)
        self.assertEqual(o, 50.0)

    def test_opus_4_variants_match_before_generic_opus(self):
        # opus-4-8 / opus-4-7 / opus-4-6 should hit their specific entries (5.0/25.0)
        for name in ("claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6"):
            i, o = pulse.rates_for(name)
            self.assertEqual(i, 5.0, f"{name} input rate")
            self.assertEqual(o, 25.0, f"{name} output rate")


class TestCostUSD(unittest.TestCase):
    def test_zero_usage(self):
        self.assertEqual(pulse.cost_usd("claude-sonnet-4-5", 0, 0, 0, 0, 0), 0.0)

    def test_output_only(self):
        # 1M output tokens at $15/MTok → $15
        c = pulse.cost_usd("claude-opus", 0, 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(c, 75.0, places=2)

    def test_cache_read_is_cheaper_than_input(self):
        # 1M input vs 1M cache-read for same model
        c_in = pulse.cost_usd("claude-sonnet-4-5", 1_000_000, 0, 0, 0, 0)
        c_cr = pulse.cost_usd("claude-sonnet-4-5", 0, 0, 0, 0, 1_000_000)
        self.assertLess(c_cr, c_in)

    def test_cc5_premium(self):
        # cc5 (5-min cache write) is 1.25× input rate
        model = "claude-sonnet-4-5"
        ri, _ = pulse.rates_for(model)
        expected = 1_000_000 * ri * 1.25 / 1e6
        c = pulse.cost_usd(model, 0, 0, 1_000_000, 0, 0)
        self.assertAlmostEqual(c, expected, places=6)

    def test_cc1_premium(self):
        # cc1 (1-hr cache write) is 2.0× input rate
        model = "claude-sonnet-4-5"
        ri, _ = pulse.rates_for(model)
        expected = 1_000_000 * ri * 2.0 / 1e6
        c = pulse.cost_usd(model, 0, 0, 0, 1_000_000, 0)
        self.assertAlmostEqual(c, expected, places=6)


# ---------------------------------------------------------------------------
# 2. Claude multi-block dedup
# ---------------------------------------------------------------------------

class TestClaudeDedup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tp = Path(self.tmp.name)
        self.prj_dir = tp / "projects" / "proj1"
        self.prj_dir.mkdir(parents=True)
        self.session = self.prj_dir / "sess.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _scan(self):
        idx = pulse._empty_index()
        pulse._scan_claude(self.session, 0, {}, idx)
        return idx

    def test_duplicate_requestid_counted_once(self):
        """Two lines with identical requestId+message.id → one usage event."""
        msg = _claude_msg("2024-01-01T10:00:00Z", "claude-sonnet-4-5", 100, 50,
                          req_id="req-1", msg_id="msg-1")
        _write_jsonl(self.session, [msg, msg])  # exact duplicate
        idx = self._scan()
        day = idx["days"].get("2024-01-01", {})
        prj = list(day.values())[0] if day else {}
        model_entry = list(prj.values())[0] if prj else {}
        self.assertEqual(model_entry.get("n", 0), 1, "duplicate should be deduped to 1")
        self.assertEqual(model_entry.get("out", 0), 50)

    def test_different_requestid_counted_separately(self):
        """Two lines with different requestIds → two usage events."""
        msg1 = _claude_msg("2024-01-01T10:00:00Z", "claude-sonnet-4-5", 100, 50,
                           req_id="req-1", msg_id="msg-1")
        msg2 = _claude_msg("2024-01-01T10:01:00Z", "claude-sonnet-4-5", 200, 80,
                           req_id="req-2", msg_id="msg-2")
        _write_jsonl(self.session, [msg1, msg2])
        idx = self._scan()
        day = idx["days"].get("2024-01-01", {})
        prj = list(day.values())[0] if day else {}
        model_entry = list(prj.values())[0] if prj else {}
        self.assertEqual(model_entry.get("n", 0), 2)
        self.assertEqual(model_entry.get("out", 0), 130)


# ---------------------------------------------------------------------------
# 3. Codex cumulative→delta + counter-reset
# ---------------------------------------------------------------------------

class TestCodexDelta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tp = Path(self.tmp.name)
        self.sessions_dir = tp / "codex" / "sessions"
        self.sessions_dir.mkdir(parents=True)
        self.session = self.sessions_dir / "rollout-abc.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _codex_token_event(self, ts, total_inp, total_out, total_cr=0,
                           last_inp=None, last_out=None, last_cr=None):
        last = {}
        if last_inp is not None:
            last = {"input_tokens": last_inp, "output_tokens": last_out or 0,
                    "cached_input_tokens": last_cr or 0}
        return {
            "type": "event_msg",
            "timestamp": ts,
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": total_inp,
                        "output_tokens": total_out,
                        "cached_input_tokens": total_cr,
                    },
                    "last_token_usage": last,
                },
            },
        }

    def _scan(self):
        idx = pulse._empty_index()
        pulse._scan_codex(self.session, 0, {}, idx)
        return idx

    def test_cumulative_delta(self):
        """Two token events → delta (not cumulative) is recorded."""
        e1 = self._codex_token_event("2024-01-01T10:00:00Z", 1000, 500)
        e2 = self._codex_token_event("2024-01-01T10:01:00Z", 1200, 600)
        _write_jsonl(self.session, [e1, e2])
        idx = self._scan()
        day = idx["days"].get("2024-01-01", {})
        prj = list(day.values())[0] if day else {}
        totals = {}
        for model_entry in prj.values():
            for k in ("in", "out", "n"):
                totals[k] = totals.get(k, 0) + model_entry.get(k, 0)
        self.assertEqual(totals.get("n", 0), 2)
        # First event: 1000 in, 500 out; second delta: 200 in, 100 out
        self.assertEqual(totals.get("out", 0), 600)

    def test_counter_reset_fallback(self):
        """Counter reset (new total < old total) falls back to last_token_usage."""
        e1 = self._codex_token_event("2024-01-01T10:00:00Z", 1000, 500)
        # Counter resets: new total is smaller than old, fallback to last_token_usage
        e2 = self._codex_token_event("2024-01-01T10:01:00Z", 50, 30,
                                     last_inp=50, last_out=30)
        _write_jsonl(self.session, [e1, e2])
        idx = self._scan()
        day = idx["days"].get("2024-01-01", {})
        prj = list(day.values())[0] if day else {}
        totals = {}
        for model_entry in prj.values():
            for k in ("out",):
                totals[k] = totals.get(k, 0) + model_entry.get(k, 0)
        # First event: 500 out; reset event uses last_out=30
        self.assertEqual(totals.get("out", 0), 530)


# ---------------------------------------------------------------------------
# 4. _agg filtering
# ---------------------------------------------------------------------------

class TestAgg(unittest.TestCase):
    def _make_idx(self):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        idx = pulse._empty_index()
        # today: projA with sonnet + haiku; projB with sonnet
        idx["days"][today] = {
            "projA": {
                "claude-sonnet-4-5": {"in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 0, "n": 1, "cost": 0.001},
                "claude-haiku": {"in": 200, "out": 80, "cc5": 0, "cc1": 0, "cr": 0, "n": 1, "cost": 0.0002},
            },
            "projB": {
                "claude-sonnet-4-5": {"in": 300, "out": 120, "cc5": 0, "cc1": 0, "cr": 0, "n": 2, "cost": 0.003},
            },
        }
        return idx

    def test_no_filter_totals_all(self):
        idx = self._make_idx()
        _, _, _, total = pulse._agg(idx, 90)
        self.assertEqual(total["n"], 4)
        self.assertEqual(total["out"], 250)

    def test_project_filter(self):
        idx = self._make_idx()
        _, _, by_project, total = pulse._agg(idx, 90, project="projA")
        self.assertEqual(total["n"], 2)
        self.assertNotIn("projB", by_project)

    def test_model_filter(self):
        idx = self._make_idx()
        _, _, _, total = pulse._agg(idx, 90, model="claude-sonnet-4-5")
        self.assertEqual(total["n"], 3)   # projA sonnet + projB sonnet
        self.assertEqual(total["out"], 170)

    def test_source_filter_gemini_absent(self):
        idx = self._make_idx()
        _, _, _, total = pulse._agg(idx, 90, source="gemini")
        self.assertEqual(total["n"], 0)

    def test_source_filter_claude(self):
        idx = self._make_idx()
        _, _, _, total = pulse._agg(idx, 90, source="claude")
        self.assertEqual(total["n"], 4)


# ---------------------------------------------------------------------------
# 5. build_trace rejects unknown paths
# ---------------------------------------------------------------------------

class TestBuildTrace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tp = Path(self.tmp.name)
        self.prj_dir = tp / "projects" / "proj1"
        self.prj_dir.mkdir(parents=True)
        self.session = self.prj_dir / "sess.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _patch_dirs(self):
        return patch.multiple(
            "pulse",
            CLAUDE_PROJECTS=self.prj_dir.parent,
            CODEX_SESSIONS=Path(self.tmp.name) / "codex",
            GEMINI_TMP=Path(self.tmp.name) / "gemini",
        )

    def test_unknown_path_returns_error(self):
        with self._patch_dirs():
            result = pulse.build_trace("/etc/passwd")
        self.assertIn("error", result)
        self.assertEqual(result["error"], "unknown session")

    def test_known_path_parses(self):
        msg = _claude_msg("2024-01-01T10:00:00Z", "claude-sonnet-4-5", 100, 50)
        _write_jsonl(self.session, [msg])
        with self._patch_dirs():
            result = pulse.build_trace(str(self.session))
        self.assertNotIn("error", result)
        self.assertIn("steps", result)


# ---------------------------------------------------------------------------
# 6. Regression: refresh_index survives truncated append-only file (RLock fix)
# ---------------------------------------------------------------------------

class TestRLockNoDeadlock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tp = Path(self.tmp.name)
        self.prj_dir = tp / "projects" / "myproj"
        self.prj_dir.mkdir(parents=True)
        self.session = self.prj_dir / "sess.jsonl"
        self.data_dir = tp / "data"

    def tearDown(self):
        self.tmp.cleanup()

    def test_truncated_file_does_not_deadlock(self):
        """
        Write a transcript, scan it, then shrink the file.
        refresh_index should detect the truncation, call itself with force=True,
        and return without deadlocking. We run it in a thread with a timeout.
        """
        msg = _claude_msg("2024-01-01T10:00:00Z", "claude-sonnet-4-5", 100, 50)
        _write_jsonl(self.session, [msg])

        with patch.multiple(
            "pulse",
            CLAUDE_PROJECTS=self.prj_dir.parent,
            CODEX_SESSIONS=Path(self.tmp.name) / "codex",
            GEMINI_TMP=Path(self.tmp.name) / "gemini",
            DATA_DIR=self.data_dir,
            INDEX_FILE=self.data_dir / "index.json",
        ):
            # First scan: record the file with its full size.
            pulse.refresh_index()

            # Truncate the file below the recorded offset.
            self.session.write_text("")

            result = [None]
            exc = [None]

            def run():
                try:
                    result[0] = pulse.refresh_index()
                except Exception as e:
                    exc[0] = e

            t = threading.Thread(target=run, daemon=True)
            t.start()
            t.join(timeout=5)  # 5 s is generous; deadlock hangs forever

            if t.is_alive():
                self.fail("refresh_index deadlocked on truncated file (RLock fix regression)")
            if exc[0]:
                raise exc[0]
            self.assertIsNotNone(result[0], "refresh_index should return (idx, changed)")


if __name__ == "__main__":
    unittest.main()
