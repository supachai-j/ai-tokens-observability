#!/usr/bin/env python3
"""Zero-dependency stdlib tests for pulse.py.

Run: python3 -m unittest test_pulse
"""
import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
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
# 0. _project_name — HOME_PREFIX generalization
# ---------------------------------------------------------------------------

class TestProjectName(unittest.TestCase):
    def _fake_path(self, dir_name):
        """Return a fake Path whose .parent.name == dir_name."""
        return Path("/fake") / dir_name / "sess.jsonl"

    def test_home_prefix_stripped(self):
        """Directory names starting with HOME_PREFIX lose that prefix."""
        # Claude Code encodes /Users/alice/workspace/rtk as -Users-alice-workspace-rtk
        # (each "/" including the leading one becomes "-").
        home_slug = str(Path.home()).replace("/", "-")  # e.g. "-Users-alice"
        dir_name = home_slug + "-workspace-rtk"         # e.g. "-Users-alice-workspace-rtk"
        result = pulse._project_name(self._fake_path(dir_name))
        self.assertEqual(result, "workspace-rtk")

    def test_non_home_path_unchanged(self):
        """Directory names that don't start with HOME_PREFIX pass through."""
        result = pulse._project_name(self._fake_path("-private-tmp-m3deal"))
        self.assertEqual(result, "-private-tmp-m3deal")

    def test_no_over_strip_short_prefix(self):
        """A name shorter than HOME_PREFIX is returned as-is."""
        result = pulse._project_name(self._fake_path("myproject"))
        self.assertEqual(result, "myproject")

    def test_home_prefix_constant_derived_from_home(self):
        """HOME_PREFIX ends with '-' and equals the home dir slug + '-'."""
        self.assertTrue(pulse.HOME_PREFIX.endswith("-"))
        expected = str(Path.home()).replace("/", "-") + "-"
        self.assertEqual(pulse.HOME_PREFIX, expected)


# ---------------------------------------------------------------------------
# 0c. _load_index — version bump triggers full rebuild (no stale key bleed)
# ---------------------------------------------------------------------------

class TestLoadIndexVersionBump(unittest.TestCase):
    def test_old_version_returns_empty_index(self):
        """A v2 index on disk must be discarded so stale project keys are rebuilt."""
        with tempfile.TemporaryDirectory() as d:
            idx_path = Path(d) / "index.json"
            # Write a plausible v2 index with stale unstripped keys.
            stale = {"version": 2, "files": {}, "days": {"2026-01-01": {
                "-Users-tumz-workspace-rtk": {"old-model": {"in": 1, "out": 1, "n": 1}}
            }}, "activity": {}, "recent": []}
            idx_path.write_text(json.dumps(stale))
            with patch("pulse.INDEX_FILE", idx_path):
                loaded = pulse._load_index()
            self.assertEqual(loaded["version"], 3,
                             "_load_index should return a fresh v3 index for a v2 file")
            self.assertEqual(loaded["days"], {},
                             "stale days from v2 index must not bleed into the fresh index")

    def test_current_version_loads_as_is(self):
        """A v3 index on disk is loaded without rebuild."""
        with tempfile.TemporaryDirectory() as d:
            idx_path = Path(d) / "index.json"
            v3 = {"version": 3, "files": {}, "days": {"2026-01-01": {"proj": {}}},
                  "activity": {}, "recent": []}
            idx_path.write_text(json.dumps(v3))
            with patch("pulse.INDEX_FILE", idx_path):
                loaded = pulse._load_index()
            self.assertEqual(loaded["version"], 3)
            self.assertIn("2026-01-01", loaded["days"])


# ---------------------------------------------------------------------------
# 0b. rtk_gain() cache — None-result path doesn't re-spawn every SSE refresh
# ---------------------------------------------------------------------------

class TestRtkGainCache(unittest.TestCase):
    def setUp(self):
        # Reset module-level memo before each test
        pulse._rtk_mem["ts"] = 0.0
        pulse._rtk_mem["data"] = None

    def tearDown(self):
        pulse._rtk_mem["ts"] = 0.0
        pulse._rtk_mem["data"] = None

    def test_second_call_within_ttl_skips_subprocess(self):
        """After one call, a second call within RTK_TTL must not invoke subprocess."""
        with patch("pulse.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stdout": ""})()
            pulse.rtk_gain()   # first call → subprocess
            pulse.rtk_gain()   # second call → should hit cache
            self.assertEqual(mock_run.call_count, 1,
                             "subprocess.run called more than once within TTL")

    def test_none_result_is_cached(self):
        """When rtk is absent (returncode != 0), None is returned but cached."""
        with patch("pulse.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stdout": ""})()
            r1 = pulse.rtk_gain()
            r2 = pulse.rtk_gain()
            self.assertIsNone(r1)
            self.assertIsNone(r2)
            self.assertEqual(mock_run.call_count, 1)

    def test_result_returned_after_ttl(self):
        """A call after TTL expires re-invokes subprocess."""
        with patch("pulse.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stdout": ""})()
            pulse.rtk_gain()
            pulse._rtk_mem["ts"] -= pulse.RTK_TTL + 1  # age the cache
            pulse.rtk_gain()
            self.assertEqual(mock_run.call_count, 2)


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
# 1b. TestPricingOverrides — _pricing_overrides() + rates_for() precedence
# ---------------------------------------------------------------------------

class TestPricingOverrides(unittest.TestCase):
    """Test custom pricing override via pricing.json.

    IMPORTANT: _pricing_overrides() caches globally in _pricing_mem.
    Every setUp/tearDown resets the cache AND patches PRICING_FILE to a
    temp path so overrides don't bleed between tests or into TestRatesFor.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._fake_pricing_file = Path(self.tmp.name) / "pricing.json"
        self._patch = patch("pulse.PRICING_FILE", self._fake_pricing_file)
        self._patch.start()
        # Reset the module-level cache so previous tests' state never bleeds
        pulse._pricing_mem.update(checked=0.0, mtime=None, data={})

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()
        # Reset cache so subsequent test classes see built-in rates
        pulse._pricing_mem.update(checked=0.0, mtime=None, data={})

    def _write(self, obj):
        self._fake_pricing_file.write_text(json.dumps(obj))

    # --- core behaviour ---

    def test_no_file_returns_empty(self):
        """No pricing.json → _pricing_overrides() == {} (built-ins used)."""
        result = pulse._pricing_overrides()
        self.assertEqual(result, {})

    def test_no_file_rates_for_uses_builtin(self):
        """No pricing.json → rates_for unchanged (regression: no file = no change)."""
        i, o = pulse.rates_for("claude-opus-4-8")
        self.assertEqual(i, 5.0)
        self.assertEqual(o, 25.0)

    def test_valid_override_beats_builtin(self):
        """Override for opus-4-8 wins over the built-in 5.0/25.0."""
        self._write({"opus-4-8": [4.0, 20.0]})
        i, o = pulse.rates_for("claude-opus-4-8")
        self.assertEqual(i, 4.0)
        self.assertEqual(o, 20.0)

    def test_new_model_matched(self):
        """A model not in PRICING is matched via override substring."""
        self._write({"my-model": [2.0, 8.0]})
        i, o = pulse.rates_for("my-model-v1")
        self.assertEqual(i, 2.0)
        self.assertEqual(o, 8.0)

    def test_override_beats_gpt5_special_case(self):
        """Override key matching gpt-5 wins before the gpt-5 special-case block."""
        self._write({"gpt-5": [9.9, 9.9]})
        i, o = pulse.rates_for("gpt-5-codex")
        self.assertEqual(i, 9.9)
        self.assertEqual(o, 9.9)

    def test_longest_key_wins(self):
        """When multiple override keys match, the longest one is used."""
        self._write({"gpt": [1.0, 1.0], "gpt-4o": [2.0, 2.0]})
        i, o = pulse.rates_for("gpt-4o-mini")
        self.assertEqual(i, 2.0)
        self.assertEqual(o, 2.0)

    def test_cost_usd_reflects_override(self):
        """cost_usd uses the override rate (flows through rates_for)."""
        self._write({"sonnet": [10.0, 10.0]})
        c = pulse.cost_usd("claude-sonnet-4-5", 1_000_000, 0, 0, 0, 0)
        self.assertAlmostEqual(c, 10.0, places=6)

    # --- error/malformed handling ---

    def test_malformed_json_returns_empty(self):
        """Malformed JSON → _pricing_overrides() == {} (no crash)."""
        self._fake_pricing_file.write_text("{bad json")
        result = pulse._pricing_overrides()
        self.assertEqual(result, {})

    def test_invalid_entries_skipped(self):
        """Only well-formed entries survive; malformed ones are silently dropped."""
        self._write({
            "a": [1],           # wrong length
            "b": ["x", "y"],   # non-numeric
            "c": [-1, 2],      # negative
            "d": [1, 2, 3],    # too long
            "e": True,         # bool, not list
            "good": [1.0, 2.0],
        })
        result = pulse._pricing_overrides()
        self.assertEqual(set(result.keys()), {"good"})
        self.assertEqual(result["good"], (1.0, 2.0))

    def test_bool_values_rejected(self):
        """JSON true/false are bool (subclass of int) and must NOT be accepted as rates."""
        self._write({"bad": [True, False]})
        result = pulse._pricing_overrides()
        self.assertNotIn("bad", result)

    # --- normalisation ---

    def test_keys_lowercased(self):
        """Keys in pricing.json are lower-cased so matching is case-insensitive."""
        self._write({"OPUS-4-8": [4.0, 20.0]})
        i, o = pulse.rates_for("claude-opus-4-8")
        self.assertEqual(i, 4.0)
        self.assertEqual(o, 20.0)

    # --- cache behaviour ---

    def test_mtime_change_reloads(self):
        """Writing a new file (different mtime) causes the cache to reload."""
        self._write({"sonnet": [1.0, 1.0]})
        pulse._pricing_mem.update(checked=0.0, mtime=None, data={})  # force re-stat
        r1 = pulse._pricing_overrides()
        self.assertIn("sonnet", r1)
        self.assertEqual(r1["sonnet"], (1.0, 1.0))

        # Overwrite file with new content and force a re-stat by resetting checked
        import os as _os
        self._write({"haiku": [0.5, 2.5]})
        # Bump mtime by at least 1s to guarantee a distinct mtime
        old_mtime = self._fake_pricing_file.stat().st_mtime
        _os.utime(self._fake_pricing_file, (old_mtime + 2, old_mtime + 2))
        pulse._pricing_mem["checked"] = 0.0  # expire TTL gate

        r2 = pulse._pricing_overrides()
        self.assertNotIn("sonnet", r2)
        self.assertIn("haiku", r2)
        self.assertEqual(r2["haiku"], (0.5, 2.5))


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


# ---------------------------------------------------------------------------
# 7. Gemini scan — token math (_scan_gemini_jsonl + _scan_gemini_json)
# ---------------------------------------------------------------------------

class TestGeminiScanJsonl(unittest.TestCase):
    """_scan_gemini_jsonl: inp=input+tool, out=output+thoughts, cr=cached,
    net_in = max(0, inp - cr) because input already includes cached."""

    def _setup(self, tmp):
        tp = Path(tmp)
        proj_dir = tp / "proj1" / "chats"
        proj_dir.mkdir(parents=True)
        return proj_dir

    def _gemini_msg(self, model, inp, out, tool=0, thoughts=0, cached=0,
                    ts="2026-01-01T10:00:00Z"):
        return {"timestamp": ts, "model": model,
                "tokens": {"input": inp, "output": out, "tool": tool,
                           "thoughts": thoughts, "cached": cached}}

    def _scan(self, gemini_tmp, path):
        idx = pulse._empty_index()
        with patch("pulse.GEMINI_TMP", gemini_tmp):
            pulse._scan_gemini_jsonl(path, 0, {}, idx)
        return idx

    def test_basic_token_math(self):
        """input+tool → inp, output+thoughts → out, cached → cr."""
        with tempfile.TemporaryDirectory() as d:
            gdir = self._setup(d)
            path = gdir / "chat.jsonl"
            msg = self._gemini_msg("gemini-2.5-pro", inp=400, out=100,
                                   tool=50, thoughts=20, cached=0)
            _write_jsonl(path, [msg])
            idx = self._scan(Path(d), path)
            day = list(idx["days"].values())[0]
            entry = list(list(day.values())[0].values())[0]
            self.assertEqual(entry["in"], 450)   # inp+tool = 400+50
            self.assertEqual(entry["out"], 120)  # out+thoughts = 100+20

    def test_cached_subtracted_from_net_input(self):
        """Net input = max(0, input+tool - cached); cached → cr field."""
        with tempfile.TemporaryDirectory() as d:
            gdir = self._setup(d)
            path = gdir / "chat.jsonl"
            msg = self._gemini_msg("gemini-2.5-pro", inp=400, out=100, cached=100)
            _write_jsonl(path, [msg])
            idx = self._scan(Path(d), path)
            day = list(idx["days"].values())[0]
            entry = list(list(day.values())[0].values())[0]
            self.assertEqual(entry["in"], 300)  # 400 - 100
            self.assertEqual(entry["cr"], 100)

    def test_cached_exceeds_input_clamps_to_zero(self):
        """max(0, inp - cr) never goes negative."""
        with tempfile.TemporaryDirectory() as d:
            gdir = self._setup(d)
            path = gdir / "chat.jsonl"
            msg = self._gemini_msg("gemini-2.5-pro", inp=50, out=30, cached=100)
            _write_jsonl(path, [msg])
            idx = self._scan(Path(d), path)
            day = list(idx["days"].values())[0]
            entry = list(list(day.values())[0].values())[0]
            self.assertEqual(entry["in"], 0)
            self.assertEqual(entry["cr"], 100)

    def test_lines_without_tokens_skipped(self):
        """Lines missing the 'tokens' key are silently ignored."""
        with tempfile.TemporaryDirectory() as d:
            gdir = self._setup(d)
            path = gdir / "chat.jsonl"
            no_tokens = {"timestamp": "2026-01-01T10:00:00Z", "type": "user",
                         "content": "hello"}
            with_tokens = self._gemini_msg("gemini-2.5-pro", inp=200, out=80)
            _write_jsonl(path, [no_tokens, with_tokens])
            idx = self._scan(Path(d), path)
            total_n = sum(
                e["n"]
                for day in idx["days"].values()
                for proj in day.values()
                for e in proj.values()
            )
            self.assertEqual(total_n, 1)


class TestGeminiScanJson(unittest.TestCase):
    """_scan_gemini_json: whole-document .json, cursor tracks message count."""

    def _setup(self, tmp):
        tp = Path(tmp)
        proj_dir = tp / "proj2" / "chats"
        proj_dir.mkdir(parents=True)
        return proj_dir

    def _scan(self, gemini_tmp, path, state=None):
        idx = pulse._empty_index()
        with patch("pulse.GEMINI_TMP", gemini_tmp):
            new_state = pulse._scan_gemini_json(path, state or {}, idx)
        return idx, new_state

    def test_basic_token_math(self):
        """Same token math as jsonl variant."""
        with tempfile.TemporaryDirectory() as d:
            gdir = self._setup(d)
            path = gdir / "chat.json"
            doc = {"sessionId": "s1", "messages": [
                {"timestamp": "2026-01-01T10:00:00Z", "model": "gemini-2.5-flash",
                 "tokens": {"input": 300, "output": 90, "tool": 30,
                            "thoughts": 10, "cached": 0}},
            ]}
            path.write_text(json.dumps(doc))
            idx, _ = self._scan(Path(d), path)
            day = list(idx["days"].values())[0]
            entry = list(list(day.values())[0].values())[0]
            self.assertEqual(entry["in"], 330)  # 300+30
            self.assertEqual(entry["out"], 100)  # 90+10

    def test_cursor_prevents_recount(self):
        """Second scan with cursor n=1 skips the first message."""
        with tempfile.TemporaryDirectory() as d:
            gdir = self._setup(d)
            path = gdir / "chat.json"
            msg = {"timestamp": "2026-01-01T10:00:00Z", "model": "gemini-2.5-flash",
                   "tokens": {"input": 100, "output": 50, "cached": 0}}
            doc = {"messages": [msg, msg]}
            path.write_text(json.dumps(doc))
            # Scan from cursor n=1: only second message should be processed
            idx, state = self._scan(Path(d), path, {"n": 1})
            total_n = sum(
                e["n"]
                for day in idx["days"].values()
                for proj in day.values()
                for e in proj.values()
            )
            self.assertEqual(total_n, 1)
            self.assertEqual(state["n"], 2)

    def test_fallback_ts_used_when_message_has_no_timestamp(self):
        """lastUpdated is used as fallback timestamp when message has none."""
        with tempfile.TemporaryDirectory() as d:
            gdir = self._setup(d)
            path = gdir / "chat.json"
            doc = {"lastUpdated": "2026-01-02T08:00:00Z", "messages": [
                {"model": "gemini-2.5-flash",
                 "tokens": {"input": 100, "output": 40, "cached": 0}},
            ]}
            path.write_text(json.dumps(doc))
            idx, _ = self._scan(Path(d), path)
            # Should land in the 2026-01-02 day bucket (from lastUpdated)
            self.assertIn("2026-01-02", idx["days"])


# ---------------------------------------------------------------------------
# 8. build_summary end-to-end
# ---------------------------------------------------------------------------

class TestBuildSummary(unittest.TestCase):
    def _make_idx(self):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        idx = pulse._empty_index()
        idx["days"][today] = {
            "projA": {
                "claude-sonnet-4-5": {
                    "in": 1000, "out": 500, "cc5": 0, "cc1": 0,
                    "cr": 200, "n": 3, "cost": 0.01,
                },
            },
            "projB": {
                "claude-opus-4-6": {
                    "in": 2000, "out": 800, "cc5": 100, "cc1": 0,
                    "cr": 0, "n": 2, "cost": 0.05,
                },
            },
        }
        # activity for projA (recent enough to be "live")
        from datetime import timezone
        idx["activity"]["projA"] = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model": "claude-sonnet-4-5",
            "session": "s1",
        }
        return idx

    def test_today_totals_match_index(self):
        idx = self._make_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx, days=1)
        self.assertEqual(s["today"]["n"], 5)   # 3 + 2
        self.assertEqual(s["today"]["out"], 1300)  # 500 + 800

    def test_window_aggregates_all_data(self):
        idx = self._make_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx, days=90)
        self.assertEqual(s["window"]["n"], 5)
        self.assertIn("projA", s["by_project"])
        self.assertIn("projB", s["by_project"])

    def test_cache_hit_rate_computed(self):
        idx = self._make_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx, days=90)
        # cache_denom = in(1000+2000) + cr(200) + cc5(100) + cc1(0) = 3300
        # cache_hit_rate = cr(200) / 3300
        expected = 200 / 3300
        self.assertAlmostEqual(s["cache_hit_rate"], expected, places=5)

    def test_by_model_keys_present(self):
        idx = self._make_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx, days=90)
        self.assertIn("claude-sonnet-4-5", s["by_model"])
        self.assertIn("claude-opus-4-6", s["by_model"])

    def test_live_sessions_includes_recent_activity(self):
        idx = self._make_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx, days=90)
        projects = [x["project"] for x in s["live_sessions"]]
        self.assertIn("projA", projects)

    def test_project_filter_isolates_projA(self):
        idx = self._make_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx, days=90, project="projA")
        self.assertNotIn("projB", s["by_project"])
        self.assertEqual(s["window"]["n"], 3)

    def test_sources_list_populated(self):
        idx = self._make_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx, days=90)
        self.assertIn("claude", s["sources"])


# ---------------------------------------------------------------------------
# by_tool aggregation
# ---------------------------------------------------------------------------

class TestBuildSummaryByTool(unittest.TestCase):
    """by_tool groups model entries by model_source, summing n/out/cost."""

    def _make_mixed_idx(self):
        """Index with two claude models and one codex model."""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        idx = pulse._empty_index()
        idx["days"][today] = {
            "projA": {
                "claude-sonnet-4-5": {
                    "in": 1000, "out": 500, "cc5": 0, "cc1": 0,
                    "cr": 0, "n": 3, "cost": 0.01,
                },
                "claude-opus-4-6": {
                    "in": 2000, "out": 800, "cc5": 0, "cc1": 0,
                    "cr": 0, "n": 2, "cost": 0.05,
                },
                "gpt-4o": {
                    "in": 500, "out": 200, "cc5": 0, "cc1": 0,
                    "cr": 0, "n": 1, "cost": 0.002,
                },
            },
        }
        return idx

    def test_claude_models_collapsed_into_one_entry(self):
        """claude-sonnet + claude-opus → single 'claude' by_tool entry."""
        idx = self._make_mixed_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx, days=90)
        self.assertIn("claude", s["by_tool"])
        self.assertIn("codex", s["by_tool"])
        # n and out should be summed across both claude models
        self.assertEqual(s["by_tool"]["claude"]["n"], 5)   # 3 + 2
        self.assertEqual(s["by_tool"]["claude"]["out"], 1300)  # 500 + 800
        self.assertAlmostEqual(s["by_tool"]["claude"]["cost"], 0.06, places=5)

    def test_codex_tool_entry_separate(self):
        """gpt-4o maps to 'codex' tool bucket via model_source."""
        idx = self._make_mixed_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx, days=90)
        self.assertEqual(s["by_tool"]["codex"]["n"], 1)
        self.assertEqual(s["by_tool"]["codex"]["out"], 200)

    def test_source_filter_leaves_only_claude_tool(self):
        """source='claude' filter → by_tool has only 'claude' key."""
        idx = self._make_mixed_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx, days=90, source="claude")
        self.assertIn("claude", s["by_tool"])
        self.assertNotIn("codex", s["by_tool"])

    def test_by_tool_sorted_by_cost_desc(self):
        """by_tool entries are ordered highest cost first."""
        idx = self._make_mixed_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx, days=90)
        costs = [e["cost"] for e in s["by_tool"].values()]
        self.assertEqual(costs, sorted(costs, reverse=True))


# ---------------------------------------------------------------------------
# 9. save_snapshot — history.jsonl pruning
# ---------------------------------------------------------------------------

class TestSaveSnapshotPruning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_history(self, dates):
        hist = self.data_dir / "history.jsonl"
        with open(hist, "w") as f:
            for d in dates:
                f.write(json.dumps({"ts": d + "T00:00:00", "date": d,
                                    "today": {}, "last30_cost": 0.0,
                                    "cache_hit_rate": 0.0,
                                    "rtk_saved": None}) + "\n")
        return hist

    def test_old_entries_pruned(self):
        from datetime import datetime, timedelta
        today = datetime.now()
        old = (today - timedelta(days=pulse.HISTORY_KEEP_DAYS + 10)).strftime("%Y-%m-%d")
        recent = (today - timedelta(days=5)).strftime("%Y-%m-%d")
        # An entry older than KEEP_DAYS (90d) but within HISTORY_KEEP_DAYS (730d)
        # must be RETAINED — history is long-term and outlives the index window.
        mid = (today - timedelta(days=pulse.KEEP_DAYS + 10)).strftime("%Y-%m-%d")
        hist = self._write_history([old, mid, recent])

        idx = pulse._empty_index()
        idx["days"][today.strftime("%Y-%m-%d")] = {
            "p": {"claude-sonnet-4-5": {"in": 10, "out": 5, "cc5": 0, "cc1": 0,
                                        "cr": 0, "n": 1, "cost": 0.0001}}
        }
        with patch("pulse.DATA_DIR", self.data_dir), \
             patch("pulse.HISTORY_FILE", hist), \
             patch("pulse.INDEX_FILE", self.data_dir / "index.json"), \
             patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            pulse.save_snapshot(pulse.build_summary(idx))

        lines = [json.loads(l) for l in hist.read_text().splitlines() if l.strip()]
        dates = [l["date"] for l in lines]
        self.assertNotIn(old, dates,
                         "entry older than HISTORY_KEEP_DAYS should be pruned")
        self.assertIn(mid, dates,
                      "entry older than KEEP_DAYS but within HISTORY_KEEP_DAYS must be retained")
        self.assertIn(recent, dates, "recent entry should be kept")

    def test_empty_history_safe(self):
        """save_snapshot handles missing history file gracefully."""
        hist = self.data_dir / "history.jsonl"
        idx = pulse._empty_index()
        with patch("pulse.DATA_DIR", self.data_dir), \
             patch("pulse.HISTORY_FILE", hist), \
             patch("pulse.INDEX_FILE", self.data_dir / "index.json"), \
             patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            pulse.save_snapshot(pulse.build_summary(idx))
        self.assertTrue(hist.exists())


# ---------------------------------------------------------------------------
# 10. Budget math in build_summary
# ---------------------------------------------------------------------------

class TestBuildSummaryBudget(unittest.TestCase):
    def _make_idx_with_months(self):
        """Index with data in two calendar months."""
        from datetime import datetime
        today = datetime.now()
        this_month = today.strftime("%Y-%m-%d")
        # Construct a date in the previous month
        first_of_month = today.replace(day=1)
        import time as _time
        prev_ts = _time.mktime(first_of_month.timetuple()) - 86400
        from datetime import datetime as dt2
        prev_day = dt2.fromtimestamp(prev_ts).strftime("%Y-%m-%d")

        idx = pulse._empty_index()
        idx["days"][this_month] = {
            "projA": {"claude-sonnet-4-5": {
                "in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 0,
                "n": 1, "cost": 5.0}},
        }
        idx["days"][prev_day] = {
            "projA": {"claude-sonnet-4-5": {
                "in": 200, "out": 80, "cc5": 0, "cc1": 0, "cr": 0,
                "n": 2, "cost": 10.0}},
        }
        return idx

    def test_month_cost_only_current_month(self):
        """month_cost sums only the current YYYY-MM, not previous months."""
        idx = self._make_idx_with_months()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx, days=90)
        self.assertAlmostEqual(s["budget"]["month_cost"], 5.0, places=4)

    def test_month_cost_respects_project_filter(self):
        """month_cost applies the same project filter as the window."""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        idx = pulse._empty_index()
        idx["days"][today] = {
            "projA": {"claude-sonnet-4-5": {
                "in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 0,
                "n": 1, "cost": 3.0}},
            "projB": {"claude-opus-4-6": {
                "in": 200, "out": 80, "cc5": 0, "cc1": 0, "cr": 0,
                "n": 1, "cost": 7.0}},
        }
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx, days=90, project="projA")
        self.assertAlmostEqual(s["budget"]["month_cost"], 3.0, places=4)

    def test_budget_limit_from_env(self):
        """budget.limit reflects RTK_PULSE_BUDGET env var."""
        idx = pulse._empty_index()
        with patch.dict("os.environ", {"RTK_PULSE_BUDGET": "500"}), \
             patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx)
        self.assertEqual(s["budget"]["limit"], 500.0)

    def test_budget_limit_none_when_unset(self):
        """budget.limit is None when RTK_PULSE_BUDGET is not set."""
        idx = pulse._empty_index()
        env = {k: v for k, v in __import__("os").environ.items()
               if k != "RTK_PULSE_BUDGET"}
        with patch.dict("os.environ", env, clear=True), \
             patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx)
        self.assertIsNone(s["budget"]["limit"])

    def test_budget_limit_none_on_invalid_env(self):
        """budget.limit is None (not an exception) when env value is invalid."""
        idx = pulse._empty_index()
        with patch.dict("os.environ", {"RTK_PULSE_BUDGET": "not-a-number"}), \
             patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx)
        self.assertIsNone(s["budget"]["limit"])

    def test_budget_month_field(self):
        """budget.month matches the current YYYY-MM."""
        from datetime import datetime
        expected = datetime.now().strftime("%Y-%m")
        idx = pulse._empty_index()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx)
        self.assertEqual(s["budget"]["month"], expected)


# ---------------------------------------------------------------------------
# 11. cmd_report smoke test
# ---------------------------------------------------------------------------

class TestCmdReport(unittest.TestCase):
    def _make_idx(self):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        idx = pulse._empty_index()
        idx["days"][today] = {
            "workspace-rtk": {
                "claude-sonnet-4-5": {
                    "in": 5000, "out": 2000, "cc5": 0, "cc1": 0,
                    "cr": 1000, "n": 10, "cost": 0.12},
            },
            "workspace-test": {
                "claude-haiku-4-5": {
                    "in": 1000, "out": 400, "cc5": 0, "cc1": 0,
                    "cr": 0, "n": 3, "cost": 0.01},
            },
        }
        return idx

    def test_cmd_report_runs_and_contains_sections(self):
        """cmd_report should complete without raising and include standard sections."""
        idx = self._make_idx()
        buf = io.StringIO()
        with patch("pulse.refresh_index", return_value=(idx, False)), \
             patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")), \
             contextlib.redirect_stdout(buf):
            pulse.cmd_report(days=7)
        out = buf.getvalue()
        self.assertIn("By Model", out)
        self.assertIn("By Project", out)
        self.assertIn("claude-sonnet-4-5", out)
        self.assertIn("workspace-rtk", out)

    def test_cmd_report_today_line_present(self):
        """cmd_report always prints a Today: summary line."""
        idx = self._make_idx()
        buf = io.StringIO()
        with patch("pulse.refresh_index", return_value=(idx, False)), \
             patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")), \
             contextlib.redirect_stdout(buf):
            pulse.cmd_report(days=30)
        self.assertIn("Today:", buf.getvalue())


# ---------------------------------------------------------------------------
# TestListSessions
# ---------------------------------------------------------------------------

class TestListSessions(unittest.TestCase):
    """list_sessions ordering, source filter, size floor, and codex cwd inference."""

    def _fake_discover(self, tmp):
        """Build fake (path, kind) pairs that _discover would return."""
        claude_dir = tmp / "claude"
        codex_dir = tmp / "codex"
        claude_dir.mkdir()
        codex_dir.mkdir()

        # Claude session — project derived from path; must be >= 300 bytes
        claude_file = claude_dir / "session.jsonl"
        line = '{"requestId":"r1"}'
        claude_file.write_text((line + " " * max(0, 301 - len(line))) + "\n")

        # Codex session — project derived from cwd in first line; must be >= 300 bytes
        codex_file = codex_dir / "rollout-abc.jsonl"
        cwd = str(Path.home() / "workspace" / "myproject")
        codex_line = json.dumps({"payload": {"cwd": cwd}})
        codex_file.write_text(
            (codex_line + " " * max(0, 301 - len(codex_line))) + "\n"
        )

        # Tiny file below the 300-byte floor — should be skipped
        small_file = claude_dir / "small.jsonl"
        small_file.write_text('{"requestId":"r2"}\n')  # < 300 bytes

        return claude_file, codex_file, small_file

    def test_newest_first_ordering(self):
        """Sessions are returned newest-first by mtime."""
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            claude_file, codex_file, _ = self._fake_discover(tmp)

            import os
            # Make claude_file older, codex_file newer
            os.utime(claude_file, (1_000_000, 1_000_000))
            os.utime(codex_file, (2_000_000, 2_000_000))

            def fake_discover():
                return [
                    (claude_file, "claude"),
                    (codex_file, "codex"),
                ]

            with patch.multiple(
                pulse,
                _discover=fake_discover,
                _project_name=lambda p: "myproject",
            ):
                sessions = pulse.list_sessions()

            self.assertGreaterEqual(len(sessions), 2)
            mtimes = [s["mtime"] for s in sessions]
            self.assertEqual(mtimes, sorted(mtimes, reverse=True))

    def test_source_filter(self):
        """source='codex' returns only codex entries."""
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            claude_file, codex_file, _ = self._fake_discover(tmp)

            def fake_discover():
                return [
                    (claude_file, "claude"),
                    (codex_file, "codex"),
                ]

            with patch.multiple(
                pulse,
                _discover=fake_discover,
                _project_name=lambda p: "myproject",
            ):
                sessions = pulse.list_sessions(source="codex")

            self.assertTrue(all(s["source"] == "codex" for s in sessions))
            paths = [s["path"] for s in sessions]
            self.assertIn(str(codex_file), paths)
            self.assertNotIn(str(claude_file), paths)

    def test_small_file_skipped(self):
        """Files smaller than 300 bytes are excluded."""
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            _, _, small_file = self._fake_discover(tmp)

            def fake_discover():
                return [(small_file, "claude")]

            with patch.multiple(
                pulse,
                _discover=fake_discover,
                _project_name=lambda p: "myproject",
            ):
                sessions = pulse.list_sessions()

            paths = [s["path"] for s in sessions]
            self.assertNotIn(str(small_file), paths)

    def test_codex_cwd_project_inference(self):
        """Codex sessions derive project from cwd in the first JSON line."""
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            codex_dir = tmp / "codex"
            codex_dir.mkdir()
            codex_file = codex_dir / "rollout-xyz.jsonl"

            cwd = str(Path.home() / "workspace" / "myproject")
            # Write enough bytes to exceed the 300-byte floor
            line = json.dumps({"payload": {"cwd": cwd}})
            padding = " " * max(0, 301 - len(line))
            codex_file.write_text(line + padding + "\n")

            def fake_discover():
                return [(codex_file, "codex")]

            with patch.multiple(pulse, _discover=fake_discover):
                sessions = pulse.list_sessions()

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["project"], "workspace-myproject")


# ---------------------------------------------------------------------------
# TestFxThbEnvOverride
# ---------------------------------------------------------------------------

class TestFxThbEnvOverride(unittest.TestCase):
    """RTK_PULSE_THB env var returns (float, 'env') without any network call."""

    def test_env_rate_returned(self):
        """RTK_PULSE_THB=35.5 → fx_thb() returns (35.5, 'env')."""
        with patch.dict("os.environ", {"RTK_PULSE_THB": "35.5"}):
            # Clear in-process memo so env check fires first
            orig = dict(pulse._fx_mem)
            pulse._fx_mem.update(ts=0.0, thb=None, src=None)
            try:
                rate, src = pulse.fx_thb()
            finally:
                pulse._fx_mem.update(**orig)
        self.assertAlmostEqual(rate, 35.5)
        self.assertEqual(src, "env")

    def test_env_skips_network(self):
        """RTK_PULSE_THB set → urlopen is never called."""
        with patch.dict("os.environ", {"RTK_PULSE_THB": "40.0"}), \
             patch("pulse.urllib.request.urlopen") as mock_open:
            orig = dict(pulse._fx_mem)
            pulse._fx_mem.update(ts=0.0, thb=None, src=None)
            try:
                rate, src = pulse.fx_thb()
            finally:
                pulse._fx_mem.update(**orig)
        mock_open.assert_not_called()
        self.assertEqual(src, "env")

    def test_invalid_env_falls_through(self):
        """RTK_PULSE_THB=notanumber → falls through to cache/live/fallback."""
        with patch.dict("os.environ", {"RTK_PULSE_THB": "notanumber"}), \
             patch("pulse.urllib.request.urlopen", side_effect=OSError("offline")):
            # Seed memo with a known value to check fall-through path
            orig = dict(pulse._fx_mem)
            pulse._fx_mem.update(ts=time.time(), thb=31.0, src="cached")
            try:
                rate, src = pulse.fx_thb()
            finally:
                pulse._fx_mem.update(**orig)
        # Should return the memo value (not env, not network)
        self.assertAlmostEqual(rate, 31.0)
        self.assertNotEqual(src, "env")


# ---------------------------------------------------------------------------
# TestCodexProjectCache — _codex_project() cache behaviour
# ---------------------------------------------------------------------------

class TestCodexProjectCache(unittest.TestCase):
    """_codex_project: path-keyed cache; parse errors not cached; '' cached."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        pulse._codex_project_cache.clear()

    def tearDown(self):
        pulse._codex_project_cache.clear()
        self.tmp.cleanup()

    def _write_first_line(self, path, cwd=None):
        payload = {} if cwd is None else {"cwd": cwd}
        path.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n")

    def test_resolves_project_from_cwd(self):
        """First-line cwd is normalized to a project name and returned."""
        p = Path(self.tmp.name) / "rollout-abc.jsonl"
        self._write_first_line(p, cwd=str(Path.home() / "workspace" / "myproject"))
        result = pulse._codex_project(str(p))
        self.assertEqual(result, "workspace-myproject")

    def test_cache_hit_avoids_reread(self):
        """After first resolve, overwriting the file does not change the result."""
        p = Path(self.tmp.name) / "rollout-orig.jsonl"
        self._write_first_line(p, cwd=str(Path.home() / "workspace" / "original"))
        first = pulse._codex_project(str(p))
        # Overwrite with a different cwd — cache must shield us from the re-read.
        self._write_first_line(p, cwd=str(Path.home() / "workspace" / "changed"))
        second = pulse._codex_project(str(p))
        self.assertEqual(first, "workspace-original")
        self.assertEqual(second, "workspace-original",
                         "cache hit should return original value, not re-read file")

    def test_missing_path_not_cached(self):
        """OSError on open → '' returned but cache stays empty (will retry)."""
        missing = str(Path(self.tmp.name) / "no-such-file.jsonl")
        result = pulse._codex_project(missing)
        self.assertEqual(result, "")
        self.assertNotIn(missing, pulse._codex_project_cache,
                         "failed resolution must NOT be cached so it retries next call")

    def test_no_cwd_cached_as_empty_string(self):
        """First line with no cwd → '' is cached (legitimate resolved value)."""
        p = Path(self.tmp.name) / "rollout-nocwd.jsonl"
        self._write_first_line(p, cwd=None)
        result = pulse._codex_project(str(p))
        self.assertEqual(result, "")
        self.assertIn(str(p), pulse._codex_project_cache,
                      "successful resolve of '' must be cached to avoid repeated opens")


# ---------------------------------------------------------------------------
# TestHttpRoutes — live ThreadingHTTPServer on port 0, hermetic patches
# ---------------------------------------------------------------------------

class TestHttpRoutes(unittest.TestCase):
    """Boot a real ThreadingHTTPServer on an OS-assigned port; hit every route."""

    @staticmethod
    def _reldate(days_ago):
        from datetime import datetime as _dt, timedelta as _td
        return (_dt.now() - _td(days=days_ago)).strftime("%Y-%m-%d")

    def setUp(self):
        # Seed history rows using relative dates so they always fall within the
        # 730-day default window of read_history().
        d1, d2 = self._reldate(2), self._reldate(1)
        hist_rows = [
            {"date": d1, "ts": d1 + "T12:00:00",
             "today": {"cost": 2.22, "out": 100, "in": 50, "cr": 0, "cc5": 0, "cc1": 0, "n": 1},
             "cache_hit_rate": 0.2, "last30_cost": 5.0, "rtk_saved": None},
            {"date": d2, "ts": d2 + "T12:00:00",
             "today": {"cost": 3.33, "out": 200, "in": 80, "cr": 0, "cc5": 0, "cc1": 0, "n": 2},
             "cache_hit_rate": 0.3, "last30_cost": 6.0, "rtk_saved": None},
        ]
        self.tmp = tempfile.TemporaryDirectory()
        data_dir = Path(self.tmp.name)
        hist_file = data_dir / "history.jsonl"
        hist_file.write_text(
            "\n".join(json.dumps(r) for r in hist_rows) + "\n"
        )
        # Patch at module level so the server thread sees them.
        self._patches = [
            patch("pulse._discover", return_value=[]),
            patch("pulse.fx_thb", return_value=(32.0, "test")),
            patch("pulse.rtk_gain", return_value=None),
            patch("pulse.DATA_DIR", data_dir),
            patch("pulse.INDEX_FILE", data_dir / "index.json"),
            patch("pulse.HISTORY_FILE", hist_file),
        ]
        for p in self._patches:
            p.start()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), pulse.Handler)
        self.port = self.srv.server_address[1]
        self._thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}")

    def _json(self, path):
        with self._get(path) as r:
            return json.loads(r.read()), r

    def test_root_serves_dashboard(self):
        """GET / → 200, text/html, body contains chart-history canvas."""
        with self._get("/") as r:
            self.assertEqual(r.status, 200)
            self.assertIn("text/html", r.headers.get("Content-Type", ""))
            body = r.read().decode()
        self.assertIn('id="chart-history"', body)

    def test_api_summary_returns_json(self):
        """/api/summary → 200, application/json, required keys present."""
        data, resp = self._json("/api/summary")
        self.assertEqual(resp.status, 200)
        self.assertIn("application/json", resp.headers.get("Content-Type", ""))
        for key in ("by_tool", "budget", "daily"):
            self.assertIn(key, data, f"key '{key}' missing from /api/summary")

    def test_api_summary_days_clamp(self):
        """/api/summary?days=9999 → filter.days clamped to KEEP_DAYS (90)."""
        data, _ = self._json(f"/api/summary?days=9999")
        self.assertEqual(data["filter"]["days"], pulse.KEEP_DAYS)

    def test_api_summary_days_invalid(self):
        """/api/summary?days=abc → filter.days defaults to 30."""
        data, _ = self._json("/api/summary?days=abc")
        self.assertEqual(data["filter"]["days"], 30)

    def test_api_sessions_returns_list(self):
        """/api/sessions → 200, JSON list (empty because _discover is patched to [])."""
        data, resp = self._json("/api/sessions")
        self.assertEqual(resp.status, 200)
        self.assertIsInstance(data, list)

    def test_api_history_returns_seeded_rows(self):
        """/api/history → 200, JSON list of seeded history rows."""
        data, resp = self._json("/api/history")
        self.assertEqual(resp.status, 200)
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 1)

    def test_api_history_csv_format(self):
        """/api/history.csv → 200, text/csv, correct header, Content-Disposition."""
        with self._get("/api/history.csv") as r:
            self.assertEqual(r.status, 200)
            ctype = r.headers.get("Content-Type", "")
            self.assertIn("text/csv", ctype)
            disp = r.headers.get("Content-Disposition", "")
            self.assertIn("attachment", disp)
            first_line = r.read().decode().splitlines()[0]
        self.assertEqual(first_line, "date,cost,out,in,n,cache_hit_rate,last30_cost,rtk_saved")

    def test_api_trace_unknown_path_returns_error(self):
        """/api/trace?path=bogus → 200 JSON with 'error' key."""
        data, resp = self._json("/api/trace?path=bogus")
        self.assertEqual(resp.status, 200)
        self.assertIn("error", data)

    def test_unknown_route_returns_404(self):
        """GET /nope → 404."""
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_api_trace_absolute_path_returns_error_json(self):
        """/api/trace?path=/etc/passwd → 200 with JSON error (no file read, no traversal)."""
        data, resp = self._json("/api/trace?path=/etc/passwd")
        self.assertEqual(resp.status, 200)
        self.assertIn("application/json", resp.headers.get("Content-Type", ""))
        self.assertEqual(data.get("error"), "unknown session",
                         "absolute path outside discover set must return 'unknown session'")

    def test_api_trace_dotdot_traversal_returns_error_json(self):
        """/api/trace?path=../../etc/passwd → 200 with JSON error (traversal blocked)."""
        data, resp = self._json("/api/trace?path=../../etc/passwd")
        self.assertEqual(resp.status, 200)
        self.assertEqual(data.get("error"), "unknown session",
                         "path-traversal attempt must return 'unknown session'")

    def test_api_summary_days_negative_clamps_to_one(self):
        """/api/summary?days=-5 → filter.days clamped to 1."""
        data, _ = self._json("/api/summary?days=-5")
        self.assertEqual(data["filter"]["days"], 1)


# ---------------------------------------------------------------------------
# TestReadHistory
# ---------------------------------------------------------------------------

class TestReadHistory(unittest.TestCase):
    """read_history: dedupe per date, ascending, tolerates bad lines, max_days."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hist_file = Path(self.tmp.name) / "history.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _reldate(days_ago=0):
        from datetime import datetime as _dt, timedelta as _td
        return (_dt.now() - _td(days=days_ago)).strftime("%Y-%m-%d")

    def _line(self, date, cost=1.0, out=100, n=1, cache_hit_rate=0.2,
              last30_cost=5.0, rtk_saved=None, ts=None):
        return {
            "ts": ts or (date + "T12:00:00"),
            "date": date,
            "today": {"cost": cost, "out": out, "in": 50,
                      "cr": 10, "cc5": 0, "cc1": 0, "n": n},
            "last30_cost": last30_cost,
            "cache_hit_rate": cache_hit_rate,
            "rtk_saved": rtk_saved,
        }

    def _write(self, lines):
        with open(self.hist_file, "w") as f:
            for obj in lines:
                f.write(json.dumps(obj) + "\n")

    def test_empty_file_returns_empty(self):
        self.hist_file.write_text("")
        with patch("pulse.HISTORY_FILE", self.hist_file):
            self.assertEqual(pulse.read_history(), [])

    def test_missing_file_returns_empty(self):
        with patch("pulse.HISTORY_FILE", self.hist_file):
            self.assertEqual(pulse.read_history(), [])

    def test_ascending_order(self):
        d0, d1, d2 = self._reldate(0), self._reldate(1), self._reldate(2)
        # Write in non-ascending order; read_history must return ascending.
        self._write([self._line(d0), self._line(d2), self._line(d1)])
        with patch("pulse.HISTORY_FILE", self.hist_file):
            result = pulse.read_history()
        dates = [r["date"] for r in result]
        self.assertEqual(dates, sorted(dates))

    def test_dedupe_last_wins(self):
        """Two snapshots for the same date — the last line's values win."""
        d = self._reldate(0)
        self._write([self._line(d, cost=1.0), self._line(d, cost=9.99)])
        with patch("pulse.HISTORY_FILE", self.hist_file):
            result = pulse.read_history()
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["cost"], 9.99)

    def test_malformed_line_skipped(self):
        """Lines that are not valid JSON are silently skipped."""
        d = self._reldate(0)
        with open(self.hist_file, "w") as f:
            f.write("not valid json\n")
            f.write(json.dumps(self._line(d)) + "\n")
        with patch("pulse.HISTORY_FILE", self.hist_file):
            result = pulse.read_history()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["date"], d)

    def test_ts_fallback_when_no_date_field(self):
        """When 'date' is absent, ts[:10] is used as the date."""
        d = self._reldate(3)
        obj = {
            "ts": d + "T10:00:00",
            "today": {"cost": 2.0, "out": 50, "in": 20,
                      "cr": 5, "cc5": 0, "cc1": 0, "n": 1},
            "last30_cost": 3.0,
            "cache_hit_rate": 0.1,
            "rtk_saved": None,
        }
        self.hist_file.write_text(json.dumps(obj) + "\n")
        with patch("pulse.HISTORY_FILE", self.hist_file):
            result = pulse.read_history()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["date"], d)

    def test_max_days_truncation(self):
        """Only dates within the last max_days calendar days are returned."""
        from datetime import datetime as _dt, timedelta as _td
        today = _dt.now()
        lines = [(today - _td(days=14 - i)).strftime("%Y-%m-%d") for i in range(15)]
        self._write([self._line(d) for d in lines])
        with patch("pulse.HISTORY_FILE", self.hist_file):
            result = pulse.read_history(max_days=5)
        self.assertLessEqual(len(result), 5)
        if result:
            cutoff = (today - _td(days=4)).strftime("%Y-%m-%d")
            self.assertGreaterEqual(result[0]["date"], cutoff)

    def test_in_field_sums_all_input_types(self):
        """'in' field = in + cr + cc5 + cc1 from the today sub-object."""
        d = self._reldate(1)
        obj = {
            "ts": d + "T08:00:00",
            "date": d,
            "today": {"cost": 1.0, "out": 100, "in": 200,
                      "cr": 50, "cc5": 30, "cc1": 20, "n": 1},
            "last30_cost": 5.0,
            "cache_hit_rate": 0.3,
            "rtk_saved": None,
        }
        self.hist_file.write_text(json.dumps(obj) + "\n")
        with patch("pulse.HISTORY_FILE", self.hist_file):
            result = pulse.read_history()
        self.assertEqual(result[0]["in"], 300)  # 200 + 50 + 30 + 20


# ---------------------------------------------------------------------------
# TestBudgetThresholds — _budget_thresholds() parsing
# ---------------------------------------------------------------------------

class TestBudgetThresholds(unittest.TestCase):
    def test_default_when_unset(self):
        """Unset env → default [80.0, 100.0]."""
        env = {k: v for k, v in __import__("os").environ.items()
               if k != "RTK_PULSE_BUDGET_ALERT"}
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(pulse._budget_thresholds(), [80.0, 100.0])

    def test_custom_thresholds(self):
        """'50,75,90' → [50.0, 75.0, 90.0] sorted ascending."""
        with patch.dict("os.environ", {"RTK_PULSE_BUDGET_ALERT": "50,75,90"}):
            self.assertEqual(pulse._budget_thresholds(), [50.0, 75.0, 90.0])

    def test_invalid_tokens_skipped(self):
        """Invalid tokens ('bad', negative) are ignored; valid ones kept."""
        with patch.dict("os.environ", {"RTK_PULSE_BUDGET_ALERT": "50,bad,,-5,90"}):
            self.assertEqual(pulse._budget_thresholds(), [50.0, 90.0])

    def test_all_invalid_falls_back_to_default(self):
        """All-invalid input yields the default [80.0, 100.0]."""
        with patch.dict("os.environ", {"RTK_PULSE_BUDGET_ALERT": "bad,,,"}):
            self.assertEqual(pulse._budget_thresholds(), [80.0, 100.0])

    def test_empty_string_falls_back_to_default(self):
        """Empty string env value yields the default."""
        with patch.dict("os.environ", {"RTK_PULSE_BUDGET_ALERT": ""}):
            self.assertEqual(pulse._budget_thresholds(), [80.0, 100.0])

    def test_unsorted_input_returns_sorted(self):
        """Thresholds are returned in ascending order regardless of input order."""
        with patch.dict("os.environ", {"RTK_PULSE_BUDGET_ALERT": "100,50,80"}):
            self.assertEqual(pulse._budget_thresholds(), [50.0, 80.0, 100.0])


# ---------------------------------------------------------------------------
# TestBuildSummaryBudgetAlert — pct / crossed fields in build_summary
# ---------------------------------------------------------------------------

class TestBuildSummaryBudgetAlert(unittest.TestCase):
    """build_summary budget dict includes pct, thresholds, crossed."""

    def _make_idx(self, cost):
        """Index with `cost` USD in the current month."""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        idx = pulse._empty_index()
        idx["days"][today] = {
            "projA": {"claude-sonnet-4-5": {
                "in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 0,
                "n": 1, "cost": cost}},
        }
        return idx

    def _summary(self, cost, limit_str, alert_str="80,100"):
        idx = self._make_idx(cost)
        env = {"RTK_PULSE_BUDGET": limit_str,
               "RTK_PULSE_BUDGET_ALERT": alert_str}
        with patch.dict("os.environ", env), \
             patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            return pulse.build_summary(idx, days=90)

    def test_pct_below_all_thresholds(self):
        """50% spend → pct ~50, crossed=None."""
        s = self._summary(50.0, "100")
        b = s["budget"]
        self.assertAlmostEqual(b["pct"], 50.0, places=1)
        self.assertIsNone(b["crossed"])

    def test_pct_above_first_threshold(self):
        """85% spend → crossed=80."""
        s = self._summary(85.0, "100")
        b = s["budget"]
        self.assertAlmostEqual(b["pct"], 85.0, places=1)
        self.assertEqual(b["crossed"], 80.0)

    def test_pct_above_second_threshold(self):
        """103% spend → crossed=100."""
        s = self._summary(103.0, "100")
        b = s["budget"]
        self.assertAlmostEqual(b["pct"], 103.0, places=1)
        self.assertEqual(b["crossed"], 100.0)

    def test_no_limit_pct_and_crossed_are_none(self):
        """No budget set → pct=None, crossed=None."""
        idx = self._make_idx(50.0)
        env = {k: v for k, v in __import__("os").environ.items()
               if k not in ("RTK_PULSE_BUDGET", "RTK_PULSE_BUDGET_ALERT")}
        with patch.dict("os.environ", env, clear=True), \
             patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx, days=90)
        self.assertIsNone(s["budget"]["pct"])
        self.assertIsNone(s["budget"]["crossed"])

    def test_thresholds_present_in_budget(self):
        """budget dict always contains a 'thresholds' list."""
        s = self._summary(50.0, "100", alert_str="60,90")
        self.assertEqual(s["budget"]["thresholds"], [60.0, 90.0])


# ---------------------------------------------------------------------------
# TestNotifyBudget — notify_budget() fire-once-per-threshold logic
# ---------------------------------------------------------------------------

class TestNotifyBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.alert_file = self.data_dir / "budget_alert.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _summary(self, crossed, month="2026-06", pct=85.0, limit=100.0):
        return {
            "budget": {
                "crossed": crossed,
                "month": month,
                "pct": pct,
                "limit": limit,
            }
        }

    def _notify(self, summary):
        with patch("pulse.DATA_DIR", self.data_dir), \
             patch("pulse.BUDGET_ALERT_FILE", self.alert_file):
            pulse.notify_budget(summary)

    def test_no_crossed_is_noop(self):
        """crossed=None → subprocess never called."""
        with patch("pulse.subprocess.run") as mock_run:
            self._notify(self._summary(crossed=None))
            mock_run.assert_not_called()

    def test_fires_on_first_cross(self):
        """First time a threshold is crossed → notification fires."""
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "darwin"):
            self._notify(self._summary(crossed=80.0))
            mock_run.assert_called_once()

    def test_does_not_refire_same_threshold(self):
        """Same threshold for same month → fires only once."""
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "darwin"):
            self._notify(self._summary(crossed=80.0))
            self._notify(self._summary(crossed=80.0))
            self.assertEqual(mock_run.call_count, 1)

    def test_refires_on_higher_threshold(self):
        """After alerted=80, crossing 100 fires again."""
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "darwin"):
            self._notify(self._summary(crossed=80.0))
            self._notify(self._summary(crossed=100.0, pct=103.0))
            self.assertEqual(mock_run.call_count, 2)

    def test_resets_and_refires_on_month_change(self):
        """New month → alerted resets; same threshold fires again."""
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "darwin"):
            self._notify(self._summary(crossed=80.0, month="2026-05"))
            self._notify(self._summary(crossed=80.0, month="2026-06"))
            self.assertEqual(mock_run.call_count, 2)

    def test_state_persisted_to_file(self):
        """After firing, budget_alert.json reflects the new alerted level."""
        with patch("pulse.subprocess.run"), \
             patch("pulse.sys.platform", "darwin"):
            self._notify(self._summary(crossed=80.0, month="2026-06"))
        state = json.loads(self.alert_file.read_text())
        self.assertEqual(state["month"], "2026-06")
        self.assertEqual(state["alerted"], 80.0)

    def test_subprocess_error_swallowed(self):
        """OSError in subprocess does not propagate."""
        with patch("pulse.subprocess.run", side_effect=OSError("no osascript")), \
             patch("pulse.sys.platform", "darwin"):
            # Should not raise
            self._notify(self._summary(crossed=80.0))

    def test_linux_uses_notify_send(self):
        """On Linux with notify-send available, notify-send is invoked."""
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "linux"), \
             patch("pulse.shutil.which", return_value="/usr/bin/notify-send"):
            self._notify(self._summary(crossed=80.0))
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[0], "notify-send")

    def test_linux_no_notify_send_skips(self):
        """On Linux without notify-send, no subprocess is spawned."""
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "linux"), \
             patch("pulse.shutil.which", return_value=None):
            self._notify(self._summary(crossed=80.0))
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# TestBuildDigest — build_digest() aggregation correctness
# ---------------------------------------------------------------------------

class TestBuildDigest(unittest.TestCase):
    """build_digest with a fabricated 14-day index (relative dates so _agg
    cutoff doesn't discard them) covering 3 tools and 2 projects."""

    def _reldate(self, days_ago):
        from datetime import datetime as _dt, timedelta as _td
        return (_dt.now() - _td(days=days_ago)).strftime("%Y-%m-%d")

    def _make_idx(self):
        """
        Current 7d (days 0-6): projA claude cost=6.0, projB gpt-4o cost=2.0,
                                projC gemini cost=1.0  → total cur=9.0
        Prior 7d (days 7-13): projA claude cost=4.0
                                → total prior=4.0
        delta_cost_pct = (9-4)/4*100 = 125.0%

        Claude entry has cr=60 (cache reads), in=300 → cache_hit_rate = 60/(300+60)
        """
        idx = pulse._empty_index()

        # ---- current period: days 0-6 ----
        # day 0: projA (claude) + projB (gpt-4o)
        d0 = self._reldate(0)
        idx["days"][d0] = {
            "projA": {"claude-sonnet-4-5": {
                "in": 200, "out": 100, "cc5": 0, "cc1": 0, "cr": 40, "n": 3, "cost": 4.0}},
            "projB": {"gpt-4o": {
                "in": 80, "out": 30, "cc5": 0, "cc1": 0, "cr": 0, "n": 1, "cost": 2.0}},
        }
        # day 3: projA (claude) + projC (gemini)
        d3 = self._reldate(3)
        idx["days"][d3] = {
            "projA": {"claude-sonnet-4-5": {
                "in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 20, "n": 2, "cost": 2.0}},
            "projC": {"gemini-2.5-pro": {
                "in": 50, "out": 20, "cc5": 0, "cc1": 0, "cr": 0, "n": 1, "cost": 1.0}},
        }

        # ---- prior period: days 7-13 ----
        d8 = self._reldate(8)
        idx["days"][d8] = {
            "projA": {"claude-sonnet-4-5": {
                "in": 150, "out": 70, "cc5": 0, "cc1": 0, "cr": 10, "n": 2, "cost": 4.0}},
        }
        return idx

    def _digest(self, idx=None, days=7):
        if idx is None:
            idx = self._make_idx()
        with patch("pulse.rtk_gain", return_value=None):
            return pulse.build_digest(idx, days=days)

    # -- period bounds --
    def test_period_start_end(self):
        d = self._digest()
        from datetime import datetime as _dt, timedelta as _td
        expected_start = (_dt.now() - _td(days=6)).strftime("%Y-%m-%d")
        expected_end = _dt.now().strftime("%Y-%m-%d")
        self.assertEqual(d["period"]["start"], expected_start)
        self.assertEqual(d["period"]["end"], expected_end)
        self.assertEqual(d["period"]["days"], 7)

    # -- totals (cur period: 6+2+1=9.0 cost) --
    def test_totals_cost(self):
        d = self._digest()
        self.assertAlmostEqual(d["totals"]["cost"], 9.0, places=4)

    def test_totals_messages(self):
        d = self._digest()
        self.assertEqual(d["totals"]["n"], 7)  # 3+1+2+1 = 7

    def test_totals_in_total_includes_cache(self):
        """in_total = in + cr + cc5 + cc1 across cur period."""
        d = self._digest()
        # projA: in=200+100=300, cr=40+20=60; projB: in=80; projC: in=50
        # in_total = 300+80+50+60 = 490
        self.assertEqual(d["totals"]["in_total"], 490)

    # -- prev totals (prior 7d: cost=4.0) --
    def test_prev_cost(self):
        d = self._digest()
        self.assertAlmostEqual(d["prev"]["cost"], 4.0, places=4)

    def test_prev_messages(self):
        d = self._digest()
        self.assertEqual(d["prev"]["n"], 2)

    # -- delta --
    def test_delta_cost_pct(self):
        """(9-4)/4*100 = 125.0% increase."""
        d = self._digest()
        self.assertAlmostEqual(d["delta_cost_pct"], 125.0, places=1)

    def test_delta_none_when_prev_zero(self):
        """No prior data → prev cost == 0 → delta_cost_pct is None."""
        idx = pulse._empty_index()
        d0 = self._reldate(0)
        idx["days"][d0] = {"projA": {"claude-sonnet-4-5": {
            "in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 0, "n": 1, "cost": 1.0}}}
        d = self._digest(idx=idx)
        self.assertIsNone(d["delta_cost_pct"])

    # -- cache hit rate --
    def test_cache_hit_rate(self):
        """cr=60, in_total=490 → rate = 60/490."""
        d = self._digest()
        self.assertAlmostEqual(d["cache_hit_rate"], 60 / 490, places=5)

    # -- by_tool --
    def test_by_tool_has_claude_codex_gemini(self):
        d = self._digest()
        self.assertIn("claude", d["by_tool"])
        self.assertIn("codex", d["by_tool"])
        self.assertIn("gemini", d["by_tool"])

    def test_by_tool_sorted_by_cost_desc(self):
        d = self._digest()
        costs = [e["cost"] for e in d["by_tool"].values()]
        self.assertEqual(costs, sorted(costs, reverse=True))

    def test_by_tool_claude_cost(self):
        d = self._digest()
        self.assertAlmostEqual(d["by_tool"]["claude"]["cost"], 6.0, places=4)

    # -- top_projects --
    def test_top_projects_ordered_by_cost(self):
        d = self._digest()
        costs = [p["cost"] for p in d["top_projects"]]
        self.assertEqual(costs, sorted(costs, reverse=True))

    def test_top_projects_capped_at_5(self):
        """Even with more projects, only top 5 are returned."""
        idx = pulse._empty_index()
        d0 = self._reldate(0)
        idx["days"][d0] = {
            f"proj{i}": {"claude-sonnet-4-5": {
                "in": 10, "out": 5, "cc5": 0, "cc1": 0, "cr": 0,
                "n": 1, "cost": float(i)}}
            for i in range(1, 8)
        }
        d = self._digest(idx=idx)
        self.assertLessEqual(len(d["top_projects"]), 5)

    # -- busiest_day --
    def test_busiest_day_is_highest_cost_day(self):
        d = self._digest()
        # day0 has cost 4.0+2.0=6.0, day3 has 2.0+1.0=3.0 → busiest is day0
        self.assertIsNotNone(d["busiest_day"])
        self.assertEqual(d["busiest_day"]["date"], self._reldate(0))

    def test_busiest_day_none_on_empty_index(self):
        d = self._digest(idx=pulse._empty_index())
        self.assertIsNone(d["busiest_day"])

    # -- empty index safety --
    def test_empty_index_no_crash(self):
        """build_digest on an empty index returns zero totals, no exceptions."""
        d = self._digest(idx=pulse._empty_index())
        self.assertEqual(d["totals"]["cost"], 0.0)
        self.assertEqual(d["totals"]["n"], 0)
        self.assertIsNone(d["delta_cost_pct"])
        self.assertEqual(d["by_tool"], {})
        self.assertEqual(d["top_projects"], [])


# ---------------------------------------------------------------------------
# TestCmdDigest — cmd_digest text + JSON output
# ---------------------------------------------------------------------------

class TestCmdDigest(unittest.TestCase):

    def _make_idx(self):
        from datetime import datetime as _dt, timedelta as _td
        today = _dt.now().strftime("%Y-%m-%d")
        idx = pulse._empty_index()
        idx["days"][today] = {
            "projA": {
                "claude-sonnet-4-5": {
                    "in": 500, "out": 200, "cc5": 0, "cc1": 0, "cr": 100,
                    "n": 5, "cost": 3.0},
            },
            "projB": {
                "gpt-4o": {
                    "in": 100, "out": 40, "cc5": 0, "cc1": 0, "cr": 0,
                    "n": 2, "cost": 1.0},
            },
        }
        return idx

    def _run_digest(self, days=7, fmt="text"):
        idx = self._make_idx()
        buf = io.StringIO()
        with patch("pulse.refresh_index", return_value=(idx, False)), \
             patch("pulse.rtk_gain", return_value=None), \
             contextlib.redirect_stdout(buf):
            pulse.cmd_digest(days, fmt)
        return buf.getvalue()

    def test_text_contains_header(self):
        """Text output must start with 'Weekly Digest'."""
        out = self._run_digest()
        self.assertIn("Weekly Digest", out)

    def test_text_contains_tool_label(self):
        """At least one friendly tool name appears in the by-tool block."""
        out = self._run_digest()
        # Claude Code or Codex CLI must appear
        self.assertTrue(
            any(label in out for label in ("Claude Code", "Codex CLI", "Gemini CLI")),
            f"No tool label found in output:\n{out}")

    def test_json_round_trips(self):
        """JSON format produces valid JSON with required top-level keys."""
        out = self._run_digest(fmt="json")
        d = json.loads(out)
        for key in ("period", "totals", "prev", "by_tool", "top_projects"):
            self.assertIn(key, d, f"key '{key}' missing from JSON digest")

    def test_json_period_has_expected_keys(self):
        out = self._run_digest(fmt="json")
        d = json.loads(out)
        self.assertIn("start", d["period"])
        self.assertIn("end", d["period"])
        self.assertIn("days", d["period"])


# ---------------------------------------------------------------------------
# TestDashboardPath — _dashboard_path() resolves the dev-tree copy
# ---------------------------------------------------------------------------

class TestDashboardPath(unittest.TestCase):
    def test_returns_existing_file_in_dev_tree(self):
        """In the dev/clone tree SCRIPT_DIR/dashboard.html exists and is returned."""
        path = pulse._dashboard_path()
        self.assertTrue(path.exists(),
                        f"_dashboard_path() returned {path} which does not exist")
        self.assertEqual(path.name, "dashboard.html")

    def test_returns_path_object(self):
        """Return value is a pathlib.Path, not a string."""
        self.assertIsInstance(pulse._dashboard_path(), Path)

    def test_falls_back_to_script_dir_when_neither_exists(self):
        """When both candidates are absent, SCRIPT_DIR/dashboard.html is returned."""
        with patch("pulse.SCRIPT_DIR", Path("/nonexistent/fake")), \
             patch("pulse.sys.prefix", "/nonexistent/prefix"):
            result = pulse._dashboard_path()
        self.assertEqual(result, Path("/nonexistent/fake") / "dashboard.html")


# ---------------------------------------------------------------------------
# TestBuildTraceAllowlistSecurity — unit-level traversal / empty / positive
# ---------------------------------------------------------------------------

class TestBuildTraceAllowlistSecurity(unittest.TestCase):
    """build_trace only allows paths returned by _discover(); everything else is
    rejected with {"error": "unknown session"} regardless of what the path looks
    like on disk.  This is the allowlist gate that prevents path traversal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _no_discover(self):
        """Patch _discover to return empty list (no known sessions)."""
        return patch("pulse._discover", return_value=[])

    def test_absolute_system_path_rejected(self):
        """/etc/passwd is not discovered → error, not read."""
        with self._no_discover():
            result = pulse.build_trace("/etc/passwd")
        self.assertEqual(result.get("error"), "unknown session")
        self.assertNotIn("steps", result)

    def test_dotdot_traversal_rejected(self):
        """../../etc/passwd traversal attempt is rejected at the allowlist gate."""
        with self._no_discover():
            result = pulse.build_trace("../../etc/passwd")
        self.assertEqual(result.get("error"), "unknown session")
        self.assertNotIn("steps", result)

    def test_empty_string_rejected(self):
        """Empty string is not a discovered path and is rejected."""
        with self._no_discover():
            result = pulse.build_trace("")
        self.assertEqual(result.get("error"), "unknown session")
        self.assertNotIn("steps", result)

    def test_discovered_path_passes_gate(self):
        """A path returned by _discover() does NOT get the unknown-session error."""
        tp = Path(self.tmp.name)
        prj_dir = tp / "projects" / "proj1"
        prj_dir.mkdir(parents=True)
        session = prj_dir / "sess.jsonl"
        _write_jsonl(session, [_claude_msg("2024-01-01T10:00:00Z",
                                           "claude-sonnet-4-5", 100, 50)])
        with patch("pulse._discover", return_value=[(session, "claude")]):
            result = pulse.build_trace(str(session))
        self.assertNotIn("error", result,
                         "discovered path should not be rejected by the gate")
        self.assertEqual(result.get("kind"), "claude")


# ---------------------------------------------------------------------------
# TestLoopbackBind — cmd_serve binds to 127.0.0.1, never 0.0.0.0
# ---------------------------------------------------------------------------

class TestLoopbackBind(unittest.TestCase):
    """cmd_serve must construct ThreadingHTTPServer with host='127.0.0.1'.
    Binding to 0.0.0.0 or '' would expose usage data beyond the local machine."""

    def test_loopback_only_bind(self):
        """ThreadingHTTPServer is constructed with ("127.0.0.1", port)."""
        bound_args = []

        class FakeServer:
            def __init__(self, server_address, handler_class):
                bound_args.append(server_address)

            def serve_forever(self):
                pass  # no-op so cmd_serve returns immediately

        with patch("pulse.ThreadingHTTPServer", FakeServer), \
             patch("pulse.refresh_index",
                   return_value=(pulse._empty_index(), False)), \
             patch("pulse.save_snapshot"), \
             patch("pulse.threading.Thread"):
            pulse.cmd_serve(19877, False)

        self.assertEqual(len(bound_args), 1,
                         "ThreadingHTTPServer must be constructed exactly once")
        self.assertEqual(bound_args[0][0], "127.0.0.1",
                         "Server must bind to 127.0.0.1, not 0.0.0.0 or ''")
        self.assertEqual(bound_args[0][1], 19877,
                         "Server must use the requested port")


# ---------------------------------------------------------------------------
# TestPerformance — build_summary on a large synthetic index
#
# NO wall-clock assertions — timing is printed to test output for human
# review only.  Flaky timing asserts would break CI on slow machines.
# The test asserts functional correctness at scale instead.
# ---------------------------------------------------------------------------

class TestPerformance(unittest.TestCase):
    """build_summary / _agg on ~88d × 30 projects × 5 models (~13,200 index cells).

    Goals:
    - Guard against O(n²) / overflow / crash on large inputs
    - Verify totals equal an independently computed expected value
    - Print elapsed ms so the "<1 s cold scan" claim can be checked manually
    """

    N_DAYS = 88          # well within _agg's 90-day window
    N_PROJECTS = 30
    MODELS = [
        "claude-sonnet-4-5", "claude-opus-4-6", "gpt-4o",
        "gemini-2.5-pro", "claude-haiku",
    ]
    ENTRY = {"in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 10, "n": 1, "cost": 0.001}

    @classmethod
    def _make_large_index(cls):
        from datetime import datetime as _dt, timedelta as _td
        idx = pulse._empty_index()
        today = _dt.now()
        for day_offset in range(cls.N_DAYS):
            date_str = (today - _td(days=day_offset)).strftime("%Y-%m-%d")
            day_bucket = {}
            for p in range(cls.N_PROJECTS):
                proj_bucket = {}
                for mdl in cls.MODELS:
                    proj_bucket[mdl] = dict(cls.ENTRY)
                day_bucket[f"proj{p}"] = proj_bucket
            idx["days"][date_str] = day_bucket
        # Populate recent ring (500 entries) so build_summary exercises that path
        from datetime import timezone
        ts_now = today.astimezone().isoformat(timespec="seconds")
        idx["recent"] = [
            [ts_now, f"proj{i % cls.N_PROJECTS}",
             cls.MODELS[i % len(cls.MODELS)],
             cls.ENTRY["in"], cls.ENTRY["out"], cls.ENTRY["cr"], cls.ENTRY["cost"]]
            for i in range(500)
        ]
        return idx

    def test_build_summary_correctness_at_scale(self):
        """build_summary totals match independently computed expected values."""
        idx = self._make_large_index()

        total_cells = self.N_DAYS * self.N_PROJECTS * len(self.MODELS)
        expected_n = total_cells * self.ENTRY["n"]
        expected_out = total_cells * self.ENTRY["out"]
        expected_cost = total_cells * self.ENTRY["cost"]

        import time as _time
        t0 = _time.perf_counter()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")):
            s = pulse.build_summary(idx, days=90)
        elapsed_ms = (_time.perf_counter() - t0) * 1000

        print(f"\n[perf] build_summary({self.N_DAYS}d × {self.N_PROJECTS}p "
              f"× {len(self.MODELS)}m = {total_cells} cells): "
              f"{elapsed_ms:.1f} ms  (claim: <1 000 ms)")

        self.assertEqual(s["window"]["n"], expected_n,
                         "window.n must equal total cell count")
        self.assertAlmostEqual(s["window"]["out"], expected_out,
                               delta=1, msg="window.out totals must match")
        self.assertAlmostEqual(s["window"]["cost"], expected_cost,
                               places=1, msg="window.cost totals must match")
        self.assertGreaterEqual(len(s["by_project"]),
                                min(self.N_PROJECTS, 20),
                                "by_project should contain up to 20 projects")
        self.assertEqual(len(s["by_model"]), len(self.MODELS),
                         "by_model should contain all 5 model keys")

    def test_agg_correctness_and_timing(self):
        """_agg totals match independently computed expected values."""
        idx = self._make_large_index()

        total_cells = self.N_DAYS * self.N_PROJECTS * len(self.MODELS)
        expected_cost = total_cells * self.ENTRY["cost"]

        import time as _time
        t0 = _time.perf_counter()
        _, _, _, total = pulse._agg(idx, 90)
        elapsed_ms = (_time.perf_counter() - t0) * 1000

        print(f"\n[perf] _agg({self.N_DAYS}d × {self.N_PROJECTS}p "
              f"× {len(self.MODELS)}m = {total_cells} cells): "
              f"{elapsed_ms:.1f} ms")

        self.assertAlmostEqual(total["cost"], expected_cost,
                               places=1, msg="_agg cost total must match")
        self.assertEqual(total["n"], total_cells,
                         "_agg n must equal total cell count")


# ---------------------------------------------------------------------------
# TestBuildForecast — _build_forecast() projection correctness
# ---------------------------------------------------------------------------

class TestBuildForecast(unittest.TestCase):
    """Direct unit tests for _build_forecast with fixed `now` datetimes."""

    def _dt(self, year, month, day):
        from datetime import datetime as _dt
        return _dt(year, month, day, 12, 0, 0)

    def test_too_early_returns_none_projected(self):
        """Day 1 of month (< MIN_FORECAST_DAY=3) → projected is None."""
        now = self._dt(2026, 6, 1)
        out = pulse._build_forecast(10.0, None, now)
        self.assertIsNone(out["projected"])
        self.assertEqual(out["day_of_month"], 1)
        self.assertEqual(out["days_in_month"], 30)

    def test_day2_too_early(self):
        """Day 2 (still < 3) → projected is None."""
        now = self._dt(2026, 6, 2)
        out = pulse._build_forecast(5.0, None, now)
        self.assertIsNone(out["projected"])

    def test_midmonth_no_limit(self):
        """Day 10, $30 spent, no limit → projected=$90, no pct, will_exceed=False."""
        now = self._dt(2026, 6, 10)  # June = 30 days
        out = pulse._build_forecast(30.0, None, now)
        self.assertAlmostEqual(out["projected"], 90.0, places=2)
        self.assertIsNone(out["projected_pct"])
        self.assertFalse(out["will_exceed"])
        self.assertIsNone(out["exceed_day"])

    def test_midmonth_with_limit_on_track(self):
        """Day 10, $30 spent, limit=$200 → projected=$90 ≤ $200 → will_exceed=False."""
        now = self._dt(2026, 6, 10)
        out = pulse._build_forecast(30.0, 200.0, now)
        self.assertAlmostEqual(out["projected"], 90.0, places=2)
        self.assertAlmostEqual(out["projected_pct"], 45.0, places=1)
        self.assertFalse(out["will_exceed"])
        self.assertIsNone(out["exceed_day"])

    def test_midmonth_with_limit_exceeding(self):
        """Day 10, $100 spent, limit=$150 → projected=$300 > $150 → will_exceed, exceed_day=15."""
        now = self._dt(2026, 6, 10)  # daily_rate=10, day_cross=150/10=15
        out = pulse._build_forecast(100.0, 150.0, now)
        self.assertTrue(out["will_exceed"])
        self.assertEqual(out["exceed_day"], 15)  # ceil(15)=15, max(10,15)=15

    def test_exceed_day_clamps_to_days_in_month(self):
        """day_cross > days_in_month → exceed_day clamped to days_in_month."""
        # Day 10, $1 spent, limit=$150 → daily_rate=0.1, day_cross=1500 → clamp to 30
        now = self._dt(2026, 6, 10)
        out = pulse._build_forecast(1.0, 150.0, now)
        # projected=3.0 which is NOT > 150 → will_exceed=False; test clamping separately
        # Use a case that does exceed: day=28, $281 spent, limit=$300, June 30 days
        # daily_rate=10.035..., day_cross=300/10.035≈29.9, ceil=30 ≤ 30 → clamp fine
        now2 = self._dt(2026, 6, 28)
        out2 = pulse._build_forecast(281.0, 300.0, now2)
        self.assertTrue(out2["will_exceed"])
        self.assertLessEqual(out2["exceed_day"], 30)

    def test_already_exceeded_exceed_day_uses_max(self):
        """month_cost already > limit → will_exceed, exceed_day = max(day, ceil(day_cross))."""
        # Day 10, $200 spent, limit=$150 → daily_rate=20, day_cross=150/20=7.5 → ceil=8
        # max(10, 8) = 10
        now = self._dt(2026, 6, 10)
        out = pulse._build_forecast(200.0, 150.0, now)
        self.assertTrue(out["will_exceed"])
        self.assertEqual(out["exceed_day"], 10)  # max(10, ceil(7.5))=max(10,8)=10

    def test_february_non_leap_days_in_month(self):
        """2026 is not a leap year → February has 28 days."""
        now = self._dt(2026, 2, 10)
        out = pulse._build_forecast(20.0, None, now)
        self.assertEqual(out["days_in_month"], 28)
        self.assertAlmostEqual(out["projected"], 20.0 / 10 * 28, places=3)

    def test_day_equals_min_forecast_day_projects(self):
        """Day == MIN_FORECAST_DAY (day 3) must project — boundary is `<`, not `<=`."""
        now = self._dt(2026, 6, 3)
        fc = pulse._build_forecast(30.0, None, now)
        self.assertIsNotNone(fc["projected"])          # 30/3*30 = 300.0
        self.assertAlmostEqual(fc["projected"], 300.0, places=2)

    def test_result_keys_present(self):
        """All documented keys are present."""
        now = self._dt(2026, 6, 10)
        out = pulse._build_forecast(30.0, 100.0, now)
        for key in ("projected", "projected_pct", "will_exceed", "exceed_day",
                    "day_of_month", "days_in_month"):
            self.assertIn(key, out, f"key '{key}' missing from forecast dict")


# ---------------------------------------------------------------------------
# TestBuildSummaryForecast — build_summary() budget.forecast structure
# ---------------------------------------------------------------------------

class TestBuildSummaryForecast(unittest.TestCase):
    def _make_idx(self, cost=30.0):
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        idx = pulse._empty_index()
        idx["days"][today] = {
            "projA": {"claude-sonnet-4-5": {
                "in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 0,
                "n": 1, "cost": cost}}}
        return idx

    def test_forecast_key_present(self):
        """build_summary output budget dict must contain 'forecast'."""
        idx = self._make_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")), \
             patch.dict("os.environ", {"RTK_PULSE_BUDGET": "100",
                                        "RTK_PULSE_BUDGET_ALERT": "80,100"}):
            s = pulse.build_summary(idx)
        self.assertIn("forecast", s["budget"])

    def test_forecast_has_required_keys(self):
        """budget.forecast has all documented keys."""
        idx = self._make_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")), \
             patch.dict("os.environ", {"RTK_PULSE_BUDGET": "100",
                                        "RTK_PULSE_BUDGET_ALERT": "80,100"}):
            s = pulse.build_summary(idx)
        fc = s["budget"]["forecast"]
        for key in ("projected", "projected_pct", "will_exceed", "exceed_day",
                    "day_of_month", "days_in_month"):
            self.assertIn(key, fc, f"forecast key '{key}' missing")

    def test_forecast_types(self):
        """forecast values have correct Python types."""
        idx = self._make_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")), \
             patch.dict("os.environ", {"RTK_PULSE_BUDGET": "100",
                                        "RTK_PULSE_BUDGET_ALERT": "80,100"}):
            s = pulse.build_summary(idx)
        fc = s["budget"]["forecast"]
        self.assertIsInstance(fc["will_exceed"], bool)
        self.assertIsInstance(fc["day_of_month"], int)
        self.assertIsInstance(fc["days_in_month"], int)

    def test_forecast_present_without_budget(self):
        """budget.forecast present even when no RTK_PULSE_BUDGET is set."""
        idx = self._make_idx()
        import os as _os
        clean = {k: v for k, v in _os.environ.items()
                 if k not in ("RTK_PULSE_BUDGET", "RTK_PULSE_BUDGET_ALERT")}
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")), \
             patch.dict("os.environ", clean, clear=True):
            s = pulse.build_summary(idx)
        self.assertIn("forecast", s["budget"])


# ---------------------------------------------------------------------------
# TestSpikeConfig — _spike_config() parsing
# ---------------------------------------------------------------------------

class TestSpikeConfig(unittest.TestCase):
    def _cfg(self, env=None):
        with patch.dict("os.environ", env or {}, clear=(env is not None)):
            if env is None:
                # Only clear the spike-specific vars so other env vars survive
                import os as _os
                clean = {k: v for k, v in _os.environ.items()
                         if k not in ("RTK_PULSE_SPIKE", "RTK_PULSE_SPIKE_MIN")}
                with patch.dict("os.environ", clean, clear=True):
                    return pulse._spike_config()
            return pulse._spike_config()

    def test_defaults(self):
        """Unset env → (3.0, 5.0, True)."""
        import os as _os
        clean = {k: v for k, v in _os.environ.items()
                 if k not in ("RTK_PULSE_SPIKE", "RTK_PULSE_SPIKE_MIN")}
        with patch.dict("os.environ", clean, clear=True):
            mult, floor, enabled = pulse._spike_config()
        self.assertAlmostEqual(mult, 3.0)
        self.assertAlmostEqual(floor, 5.0)
        self.assertTrue(enabled)

    def test_custom_multiple(self):
        """RTK_PULSE_SPIKE='4' → mult=4.0, enabled=True."""
        with patch.dict("os.environ", {"RTK_PULSE_SPIKE": "4",
                                        "RTK_PULSE_SPIKE_MIN": "5"}):
            mult, floor, enabled = pulse._spike_config()
        self.assertAlmostEqual(mult, 4.0)
        self.assertTrue(enabled)

    def test_zero_disables(self):
        """RTK_PULSE_SPIKE='0' → enabled=False."""
        with patch.dict("os.environ", {"RTK_PULSE_SPIKE": "0",
                                        "RTK_PULSE_SPIKE_MIN": "5"}):
            mult, floor, enabled = pulse._spike_config()
        self.assertFalse(enabled)

    def test_negative_disables(self):
        """Negative multiple → enabled=False."""
        with patch.dict("os.environ", {"RTK_PULSE_SPIKE": "-1",
                                        "RTK_PULSE_SPIKE_MIN": "5"}):
            mult, floor, enabled = pulse._spike_config()
        self.assertFalse(enabled)

    def test_invalid_multiple_falls_back_to_3(self):
        """Invalid RTK_PULSE_SPIKE → mult=3.0, enabled=True."""
        with patch.dict("os.environ", {"RTK_PULSE_SPIKE": "bad",
                                        "RTK_PULSE_SPIKE_MIN": "5"}):
            mult, floor, enabled = pulse._spike_config()
        self.assertAlmostEqual(mult, 3.0)
        self.assertTrue(enabled)

    def test_custom_floor(self):
        """RTK_PULSE_SPIKE_MIN='10' → floor=10.0."""
        with patch.dict("os.environ", {"RTK_PULSE_SPIKE": "3",
                                        "RTK_PULSE_SPIKE_MIN": "10"}):
            _, floor, _ = pulse._spike_config()
        self.assertAlmostEqual(floor, 10.0)

    def test_invalid_floor_falls_back_to_5(self):
        """Invalid RTK_PULSE_SPIKE_MIN → floor=5.0."""
        with patch.dict("os.environ", {"RTK_PULSE_SPIKE": "3",
                                        "RTK_PULSE_SPIKE_MIN": "bad"}):
            _, floor, _ = pulse._spike_config()
        self.assertAlmostEqual(floor, 5.0)

    def test_negative_floor_falls_back_to_5(self):
        """Negative RTK_PULSE_SPIKE_MIN → floor=5.0."""
        with patch.dict("os.environ", {"RTK_PULSE_SPIKE": "3",
                                        "RTK_PULSE_SPIKE_MIN": "-1"}):
            _, floor, _ = pulse._spike_config()
        self.assertAlmostEqual(floor, 5.0)


# ---------------------------------------------------------------------------
# TestBuildSpike — _build_spike() detection correctness
# ---------------------------------------------------------------------------

class TestBuildSpike(unittest.TestCase):
    """Fabricate an index with relative dates and verify spike detection."""

    def _reldate(self, days_ago):
        from datetime import datetime as _dt, timedelta as _td
        return (_dt.now() - _td(days=days_ago)).strftime("%Y-%m-%d")

    def _make_idx(self, prior_costs, today_cost):
        """Build an index where prior_costs is a list (index 1..N = days before today)
        and today_cost is today's cost. Zero-cost days are included in prior_costs list."""
        idx = pulse._empty_index()
        today = self._reldate(0)
        if today_cost > 0:
            idx["days"][today] = {
                "projA": {"claude-sonnet-4-5": {
                    "in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 0,
                    "n": 1, "cost": today_cost}}}
        for i, cost in enumerate(prior_costs, start=1):
            if cost > 0:
                idx["days"][self._reldate(i)] = {
                    "projA": {"claude-sonnet-4-5": {
                        "in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 0,
                        "n": 1, "cost": cost}}}
        return idx

    def _spike(self, prior_costs, today_cost, env=None):
        idx = self._make_idx(prior_costs, today_cost)
        from datetime import datetime as _dt
        now = _dt.now().astimezone()
        base_env = {"RTK_PULSE_SPIKE": "3", "RTK_PULSE_SPIKE_MIN": "5"}
        if env:
            base_env.update(env)
        with patch.dict("os.environ", base_env):
            return pulse._build_spike(idx, now, today_cost, None, None, None)

    def test_triggers_when_conditions_met(self):
        """today >= mult*baseline and >= floor and >= SPIKE_MIN_ACTIVE active days."""
        # prior: 5 active days at $2 each → baseline=2.0; today=$10 (5x, >= floor $5)
        sp = self._spike([2.0, 2.0, 2.0, 2.0, 2.0], 10.0)
        self.assertTrue(sp["triggered"])
        self.assertAlmostEqual(sp["baseline"], 2.0, places=4)
        self.assertAlmostEqual(sp["ratio"], 5.0, places=1)

    def test_not_triggered_below_floor(self):
        """today_cost < floor → not triggered even if ratio is high."""
        # today=$4, floor=$5 → not triggered
        sp = self._spike([1.0, 1.0, 1.0, 1.0, 1.0], 4.0)
        self.assertFalse(sp["triggered"])

    def test_not_triggered_with_too_few_active_days(self):
        """Only 2 active prior days → need >= SPIKE_MIN_ACTIVE=3 → not triggered."""
        sp = self._spike([2.0, 2.0, 0.0, 0.0, 0.0], 10.0)
        self.assertFalse(sp["triggered"])
        self.assertEqual(sp["active_days"], 2)

    def test_not_triggered_when_disabled(self):
        """RTK_PULSE_SPIKE=0 → enabled=False → not triggered."""
        sp = self._spike([2.0, 2.0, 2.0, 2.0, 2.0], 10.0,
                         env={"RTK_PULSE_SPIKE": "0"})
        self.assertFalse(sp["triggered"])
        self.assertFalse(sp["enabled"])

    def test_baseline_excludes_today(self):
        """Baseline is computed from SPIKE_WINDOW_DAYS BEFORE today only."""
        # prior 5 days: $2 each; today: $1 (well below 3x baseline)
        sp = self._spike([2.0, 2.0, 2.0, 2.0, 2.0], 1.0)
        # baseline should be 2.0 (not polluted by today's $1)
        self.assertAlmostEqual(sp["baseline"], 2.0, places=4)

    def test_zero_cost_days_excluded_from_mean(self):
        """$0 days must NOT count toward the mean — only active (cost > 0) days."""
        # 3 active days at $4, 4 zero days → mean of active = 4.0
        # today=$20 (5x baseline of $4, >= $5 floor) → triggered
        sp = self._spike([4.0, 4.0, 4.0, 0.0, 0.0, 0.0, 0.0], 20.0)
        self.assertEqual(sp["active_days"], 3)
        self.assertAlmostEqual(sp["baseline"], 4.0, places=4)
        self.assertTrue(sp["triggered"])

    def test_ratio_none_when_no_active_days(self):
        """No prior active days → baseline=0 → ratio=None."""
        sp = self._spike([], 10.0)
        self.assertIsNone(sp["ratio"])
        self.assertFalse(sp["triggered"])

    def test_result_keys_present(self):
        """All documented keys are present in the result."""
        sp = self._spike([2.0, 2.0, 2.0], 10.0)
        for key in ("today_cost", "baseline", "ratio", "multiple", "floor",
                    "window_days", "active_days", "enabled", "triggered", "date"):
            self.assertIn(key, sp, f"key '{key}' missing from spike dict")

    def test_not_triggered_below_multiple(self):
        """today_cost < mult*baseline → not triggered even if >= floor."""
        # baseline=10.0, today=$25, mult=3 → need $30 to trigger
        sp = self._spike([10.0, 10.0, 10.0, 10.0, 10.0], 25.0)
        self.assertFalse(sp["triggered"])

    # --- attribution tests ---

    def _make_idx_two_projects(self, projA_cost, projB_cost, prior_costs):
        """Index with two projects today (projA, projB) + single-project prior days."""
        from datetime import datetime as _dt, timedelta as _td
        idx = pulse._empty_index()
        today = _dt.now().strftime("%Y-%m-%d")
        if projA_cost > 0:
            idx["days"].setdefault(today, {})["projA"] = {
                "claude-sonnet-4-5": {
                    "in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 0,
                    "n": 1, "cost": projA_cost}}
        if projB_cost > 0:
            idx["days"].setdefault(today, {})["projB"] = {
                "claude-sonnet-4-5": {
                    "in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 0,
                    "n": 1, "cost": projB_cost}}
        for i, cost in enumerate(prior_costs, start=1):
            if cost > 0:
                day = (_dt.now() - _td(days=i)).strftime("%Y-%m-%d")
                idx["days"][day] = {
                    "projA": {"claude-sonnet-4-5": {
                        "in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 0,
                        "n": 1, "cost": cost}}}
        return idx

    def test_attribution_top_project_identified(self):
        """projA $8 > projB $2 → top_project='projA', share≈0.8."""
        from datetime import datetime as _dt
        idx = self._make_idx_two_projects(8.0, 2.0, [2.0, 2.0, 2.0, 2.0, 2.0])
        now = _dt.now().astimezone()
        today_cost = 10.0
        with patch.dict("os.environ", {"RTK_PULSE_SPIKE": "3",
                                        "RTK_PULSE_SPIKE_MIN": "5"}):
            sp = pulse._build_spike(idx, now, today_cost, None, None, None)
        self.assertEqual(sp["top_project"], "projA")
        self.assertAlmostEqual(sp["top_project_cost"], 8.0, places=3)
        self.assertAlmostEqual(sp["top_project_share"], 0.8, places=3)

    def test_attribution_none_when_project_filter_active(self):
        """When project filter is set, attribution is skipped (top_project=None)."""
        from datetime import datetime as _dt
        idx = self._make_idx_two_projects(8.0, 2.0, [2.0, 2.0, 2.0])
        now = _dt.now().astimezone()
        with patch.dict("os.environ", {"RTK_PULSE_SPIKE": "3",
                                        "RTK_PULSE_SPIKE_MIN": "5"}):
            sp = pulse._build_spike(idx, now, 8.0, "projA", None, None)
        self.assertIsNone(sp["top_project"])
        self.assertIsNone(sp["top_project_share"])

    def test_attribution_no_today_data(self):
        """No today data → top_project=None, top_project_share=None."""
        from datetime import datetime as _dt
        idx = pulse._empty_index()
        now = _dt.now().astimezone()
        with patch.dict("os.environ", {"RTK_PULSE_SPIKE": "3",
                                        "RTK_PULSE_SPIKE_MIN": "5"}):
            sp = pulse._build_spike(idx, now, 0.0, None, None, None)
        self.assertIsNone(sp["top_project"])
        self.assertIsNone(sp["top_project_share"])

    def test_attribution_keys_always_present(self):
        """top_project, top_project_cost, top_project_share always in result."""
        sp = self._spike([2.0, 2.0, 2.0], 10.0)
        for key in ("top_project", "top_project_cost", "top_project_share"):
            self.assertIn(key, sp, f"attribution key '{key}' missing from spike dict")


# ---------------------------------------------------------------------------
# TestBuildSummarySpike — build_summary() includes "spike" key
# ---------------------------------------------------------------------------

class TestBuildSummarySpike(unittest.TestCase):
    def _make_idx(self, today_cost=10.0):
        from datetime import datetime as _dt, timedelta as _td
        now = _dt.now()
        today = now.strftime("%Y-%m-%d")
        idx = pulse._empty_index()
        # today
        idx["days"][today] = {
            "projA": {"claude-sonnet-4-5": {
                "in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 0,
                "n": 1, "cost": today_cost}}}
        # 5 prior days at $2 each (active baseline)
        for i in range(1, 6):
            day = (now - _td(days=i)).strftime("%Y-%m-%d")
            idx["days"][day] = {
                "projA": {"claude-sonnet-4-5": {
                    "in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 0,
                    "n": 1, "cost": 2.0}}}
        return idx

    def test_spike_key_present(self):
        """build_summary output must contain a 'spike' key."""
        idx = self._make_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")), \
             patch.dict("os.environ", {"RTK_PULSE_SPIKE": "3",
                                        "RTK_PULSE_SPIKE_MIN": "5"}):
            s = pulse.build_summary(idx)
        self.assertIn("spike", s)

    def test_spike_has_required_keys(self):
        """spike sub-object has all documented keys including attribution."""
        idx = self._make_idx()
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")), \
             patch.dict("os.environ", {"RTK_PULSE_SPIKE": "3",
                                        "RTK_PULSE_SPIKE_MIN": "5"}):
            s = pulse.build_summary(idx)
        sp = s["spike"]
        for key in ("today_cost", "baseline", "ratio", "multiple", "floor",
                    "window_days", "active_days", "enabled", "triggered", "date",
                    "top_project", "top_project_cost", "top_project_share"):
            self.assertIn(key, sp, f"spike key '{key}' missing")

    def test_spike_triggered_when_anomalous(self):
        """With today=$10 and prior baseline=$2, spike should trigger (5x, >= $5)."""
        idx = self._make_idx(today_cost=10.0)
        with patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")), \
             patch.dict("os.environ", {"RTK_PULSE_SPIKE": "3",
                                        "RTK_PULSE_SPIKE_MIN": "5"}):
            s = pulse.build_summary(idx)
        self.assertTrue(s["spike"]["triggered"])


# ---------------------------------------------------------------------------
# TestNotifySpike — notify_spike() fire-once-per-day logic
# ---------------------------------------------------------------------------

class TestNotifySpike(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.alert_file = self.data_dir / "spike_alert.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _summary(self, triggered=True, date="2026-06-11",
                 today_cost=10.0, ratio=5.0, baseline=2.0, window_days=7,
                 top_project=None, top_project_cost=0.0, top_project_share=None):
        return {
            "spike": {
                "triggered": triggered,
                "date": date,
                "today_cost": today_cost,
                "ratio": ratio,
                "baseline": baseline,
                "window_days": window_days,
                "top_project": top_project,
                "top_project_cost": top_project_cost,
                "top_project_share": top_project_share,
            }
        }

    def _notify(self, summary):
        with patch("pulse.DATA_DIR", self.data_dir), \
             patch("pulse.SPIKE_ALERT_FILE", self.alert_file):
            pulse.notify_spike(summary)

    def test_not_triggered_is_noop(self):
        """triggered=False → subprocess never called."""
        with patch("pulse.subprocess.run") as mock_run:
            self._notify(self._summary(triggered=False))
            mock_run.assert_not_called()

    def test_fires_on_first_trigger(self):
        """First trigger on a date → notification fires."""
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "darwin"):
            self._notify(self._summary())
            mock_run.assert_called_once()

    def test_does_not_refire_same_day(self):
        """Same date → fires only once."""
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "darwin"):
            self._notify(self._summary(date="2026-06-11"))
            self._notify(self._summary(date="2026-06-11"))
            self.assertEqual(mock_run.call_count, 1)

    def test_refires_on_new_date(self):
        """New date → alerted resets; fires again."""
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "darwin"):
            self._notify(self._summary(date="2026-06-10"))
            self._notify(self._summary(date="2026-06-11"))
            self.assertEqual(mock_run.call_count, 2)

    def test_state_persisted_to_file(self):
        """After firing, spike_alert.json reflects the date and alerted=True."""
        with patch("pulse.subprocess.run"), \
             patch("pulse.sys.platform", "darwin"):
            self._notify(self._summary(date="2026-06-11"))
        state = json.loads(self.alert_file.read_text())
        self.assertEqual(state["date"], "2026-06-11")
        self.assertTrue(state["alerted"])

    def test_subprocess_error_swallowed(self):
        """OSError in subprocess does not propagate."""
        with patch("pulse.subprocess.run", side_effect=OSError("no osascript")), \
             patch("pulse.sys.platform", "darwin"):
            self._notify(self._summary())  # must not raise

    def test_linux_uses_notify_send(self):
        """On Linux with notify-send, notify-send is invoked."""
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "linux"), \
             patch("pulse.shutil.which", return_value="/usr/bin/notify-send"):
            self._notify(self._summary())
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[0], "notify-send")

    def test_linux_no_notify_send_skips(self):
        """On Linux without notify-send, no subprocess is spawned."""
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "linux"), \
             patch("pulse.shutil.which", return_value=None):
            self._notify(self._summary())
            mock_run.assert_not_called()

    def test_empty_summary_is_noop(self):
        """None or empty summary does not raise."""
        with patch("pulse.subprocess.run") as mock_run:
            pulse.notify_spike(None)
            pulse.notify_spike({})
            mock_run.assert_not_called()

    # --- attribution in notification message ---

    def test_attribution_appears_in_message(self):
        """When top_project is set, project name and cost appear in osascript arg."""
        summary = self._summary(
            top_project="my-project", top_project_cost=8.0, top_project_share=0.8)
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "darwin"), \
             patch("pulse.DATA_DIR", self.data_dir), \
             patch("pulse.SPIKE_ALERT_FILE", self.alert_file):
            pulse.notify_spike(summary)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]  # argv list
        script_arg = cmd[2]  # the -e argument
        self.assertIn("my-project", script_arg)
        self.assertIn("$8.00", script_arg)

    def test_no_attribution_when_top_project_none(self):
        """top_project=None → no attribution clause in message."""
        summary = self._summary(top_project=None)
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "darwin"), \
             patch("pulse.DATA_DIR", self.data_dir), \
             patch("pulse.SPIKE_ALERT_FILE", self.alert_file):
            pulse.notify_spike(summary)
        mock_run.assert_called_once()
        script_arg = mock_run.call_args[0][0][2]
        self.assertNotIn("Top contributor", script_arg)

    def test_long_project_name_truncated(self):
        """Project names > 40 chars are truncated with leading ellipsis."""
        long_name = "a" * 50
        summary = self._summary(top_project=long_name, top_project_cost=5.0,
                                top_project_share=0.5)
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "darwin"), \
             patch("pulse.DATA_DIR", self.data_dir), \
             patch("pulse.SPIKE_ALERT_FILE", self.alert_file):
            pulse.notify_spike(summary)
        script_arg = mock_run.call_args[0][0][2]
        # The raw 50-char name must NOT appear; the truncated form must
        self.assertNotIn("a" * 50, script_arg)
        self.assertIn("…", script_arg)

    # --- security: AppleScript escaping ---

    def test_native_notify_escapes_double_quote(self):
        """A `"` in msg must be escaped to `\\"` in the osascript -e string."""
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "darwin"):
            pulse._native_notify('a"b')
        cmd = mock_run.call_args[0][0]
        script_arg = cmd[2]
        # Must contain the escaped form, not a bare quote that closes the literal
        self.assertIn('\\"', script_arg)
        # The resulting AppleScript literal must start and end with unescaped quotes
        # (i.e. the message content does not contain a bare unescaped " mid-string)
        inner = script_arg[len('display notification "'):]
        # count unescaped quotes — there should be exactly one (the closing quote
        # before " with title ..."); a mid-string bare " would add more
        # Simpler check: the escaped sequence is present and no raw " in the content
        content_start = script_arg.find('"') + 1
        content_end = script_arg.rfind('" with title')
        content = script_arg[content_start:content_end]
        self.assertNotIn('"', content.replace('\\"', ''))

    def test_native_notify_escapes_backslash(self):
        """A backslash in msg must be doubled to \\\\ in the osascript -e string."""
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "darwin"):
            pulse._native_notify('a\\c')
        script_arg = mock_run.call_args[0][0][2]
        self.assertIn('\\\\', script_arg)

    def test_native_notify_combined_escape(self):
        """msg with both `"` and `\\` is escaped correctly in one call."""
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "darwin"):
            pulse._native_notify('a"b\\c')
        script_arg = mock_run.call_args[0][0][2]
        self.assertIn('\\"', script_arg)
        self.assertIn('\\\\', script_arg)

    def test_e2e_evil_project_name_escaped(self):
        """End-to-end: a malicious project name with `"` ends up safely escaped."""
        evil = 'evil" & do shell script "x'
        summary = self._summary(top_project=evil, top_project_cost=9.0,
                                top_project_share=0.9)
        with patch("pulse.subprocess.run") as mock_run, \
             patch("pulse.sys.platform", "darwin"), \
             patch("pulse.DATA_DIR", self.data_dir), \
             patch("pulse.SPIKE_ALERT_FILE", self.alert_file):
            pulse.notify_spike(summary)
        script_arg = mock_run.call_args[0][0][2]
        # The raw unescaped quote from the evil project name must not appear
        # (the only unescaped quotes are the outer delimiters of the AS string)
        content_start = script_arg.find('"') + 1
        content_end = script_arg.rfind('" with title')
        content = script_arg[content_start:content_end]
        self.assertNotIn('"', content.replace('\\"', ''))


# ---------------------------------------------------------------------------
# TestDashboardIntegrity — structural wiring checks for dashboard.html
#
# A true headless-browser test (Playwright / Selenium / jsdom) would require
# a new runtime dependency (npm or pip), violating the zero-dependency pillar.
# `node --check` (syntax-only parse) + these string-presence assertions are
# the maximum coverage achievable without adding a dependency.
# ---------------------------------------------------------------------------

class TestDashboardIntegrity(unittest.TestCase):
    """Key wiring markers must be present in dashboard.html.

    Catches accidental removal of SSE hooks, budget-alert UI, spike-alert UI,
    API paths, and Chart.js canvas ids that the JS depends on.
    """

    @classmethod
    def setUpClass(cls):
        cls.html = (pulse.SCRIPT_DIR / "dashboard.html").read_text()

    def test_eventsource_wired_to_events_endpoint(self):
        """EventSource connects to the /events SSE endpoint."""
        self.assertIn("EventSource('/events", self.html,
                      "EventSource must reference /events SSE route")

    def test_render_function_defined(self):
        """render() is defined — entry point called on each SSE message."""
        self.assertIn("function render(", self.html,
                      "render() function must be defined in the dashboard")

    def test_budget_banner_element_present(self):
        """#budget-banner element is present (C9 budget alert UI)."""
        self.assertIn('id="budget-banner"', self.html,
                      "#budget-banner must exist for budget alert display")

    def test_spike_banner_element_present(self):
        """#spike-banner element is present (C13 cost-spike alert UI)."""
        self.assertIn('id="spike-banner"', self.html,
                      "#spike-banner must exist for cost-spike alert display")

    def test_month_forecast_element_present(self):
        """#c-month-forecast element is present (C15 budget forecast sub-line)."""
        self.assertIn('id="c-month-forecast"', self.html,
                      "#c-month-forecast must exist for budget forecast display")

    def test_api_summary_referenced(self):
        """/api/summary is referenced for filter-change fetches or fallback."""
        self.assertIn("/api/summary", self.html,
                      "/api/summary must be referenced in the dashboard JS")

    def test_api_trace_referenced(self):
        """/api/trace is referenced for session drilldown."""
        self.assertIn("/api/trace", self.html,
                      "/api/trace must be referenced in the dashboard JS")

    def test_chart_history_canvas_exists(self):
        """chart-history canvas id is present (long-term trend panel)."""
        self.assertIn('id="chart-history"', self.html)

    def test_chart_daily_canvas_exists(self):
        """chart-daily canvas id is present (stacked daily cost chart)."""
        self.assertIn('id="chart-daily"', self.html)

    def test_chart_model_canvas_exists(self):
        """chart-model canvas id is present (by-model donut chart)."""
        self.assertIn('id="chart-model"', self.html)

    def test_chart_rate_canvas_exists(self):
        """chart-rate canvas id is present (cache hit rate meter)."""
        self.assertIn('id="chart-rate"', self.html)


# ---------------------------------------------------------------------------
# TestDigestHtml — digest_html() rendering correctness + safety
# ---------------------------------------------------------------------------

class TestDigestHtml(unittest.TestCase):
    """digest_html() must produce valid, self-contained HTML with no remote
    resources and no JavaScript, with all user-derived strings HTML-escaped."""

    def _make_digest(self, top_projects=None, delta_cost_pct=125.0):
        from datetime import datetime as _dt, timedelta as _td
        now = _dt.now()
        today = now.strftime("%Y-%m-%d")
        start = (now - _td(days=6)).strftime("%Y-%m-%d")
        if top_projects is None:
            top_projects = [{"project": "workspace-rtk", "cost": 3.5, "out": 200}]
        return {
            "generated_at": now.isoformat(timespec="seconds"),
            "period": {"start": start, "end": today, "days": 7},
            "totals": {"cost": 9.0, "out": 500, "in_total": 1200, "n": 15},
            "prev": {"cost": 4.0, "out": 200, "n": 6},
            "delta_cost_pct": delta_cost_pct,
            "cache_hit_rate": 0.45,
            "by_tool": {
                "claude": {"cost": 6.0, "out": 350, "in": 300,
                           "cc5": 0, "cc1": 0, "cr": 60, "n": 10},
                "codex": {"cost": 2.0, "out": 100, "in": 80,
                          "cc5": 0, "cc1": 0, "cr": 0, "n": 3},
                "gemini": {"cost": 1.0, "out": 50, "in": 40,
                           "cc5": 0, "cc1": 0, "cr": 0, "n": 2},
            },
            "by_day": [
                {"date": start, "cost": 4.0, "out": 200, "n": 5},
                {"date": today, "cost": 5.0, "out": 300, "n": 10},
            ],
            "busiest_day": {"date": today, "cost": 5.0},
            "top_projects": top_projects,
            "rtk": {"avg_savings_pct": 62.3, "total_saved": 450000},
        }

    def _html(self, **kw):
        return pulse.digest_html(self._make_digest(**kw))

    # -- Structure --
    def test_starts_with_doctype(self):
        """Output must begin with <!DOCTYPE html."""
        out = self._html()
        self.assertTrue(out.strip().startswith("<!DOCTYPE html"),
                        "digest_html must start with <!DOCTYPE html")

    def test_ends_with_html_close(self):
        """Output must contain closing </html>."""
        self.assertIn("</html>", self._html())

    def test_no_script_tag(self):
        """NO <script anywhere (case-insensitive) — email clients block JS."""
        out = self._html()
        self.assertNotIn("<script", out.lower(),
                         "digest_html must not emit any <script> tag")

    def test_no_remote_urls(self):
        """NO :// anywhere — catches http/https/protocol-relative remote deps."""
        out = self._html()
        self.assertNotIn("://", out,
                         "digest_html must not contain any remote URL (://)")

    # -- Content --
    def test_contains_period_dates(self):
        """Period start and end dates appear in the output."""
        d = self._make_digest()
        out = pulse.digest_html(d)
        self.assertIn(d["period"]["start"], out)
        self.assertIn(d["period"]["end"], out)

    def test_contains_total_cost(self):
        """Total cost appears formatted in the output."""
        out = self._html()
        self.assertIn("9.00", out)

    def test_contains_claude_code_label(self):
        """Friendly tool label 'Claude Code' appears in the by-tool section."""
        out = self._html()
        self.assertIn("Claude Code", out)

    def test_wow_positive_shows_up_arrow(self):
        """Positive WoW delta shows up-arrow character reference."""
        out = self._html(delta_cost_pct=25.0)
        self.assertIn("&#9650;", out)   # ▲

    def test_wow_negative_shows_down_arrow(self):
        """Negative WoW delta shows down-arrow character reference."""
        out = self._html(delta_cost_pct=-15.0)
        self.assertIn("&#9660;", out)   # ▼

    def test_wow_none_shows_na(self):
        """None WoW delta shows 'n/a'."""
        out = self._html(delta_cost_pct=None)
        self.assertIn("n/a", out)

    def test_rtk_section_present_when_rtk_set(self):
        """rtk savings section appears when rtk data is present."""
        out = self._html()
        self.assertIn("rtk savings", out)

    # -- HTML escaping (critical security property) --
    def test_project_name_is_escaped(self):
        """XSS payload in project name must be escaped — raw < must not appear."""
        evil = '<script>alert(1)</script>&"'
        out = pulse.digest_html(self._make_digest(
            top_projects=[{"project": evil, "cost": 1.0, "out": 10}]))
        # Raw <script> must NOT appear
        self.assertNotIn("<script>", out,
                         "Unescaped <script> found in project name output")
        # Escaped forms must be present
        self.assertIn("&lt;script&gt;", out,
                      "&lt;script&gt; escape not found for project name")
        self.assertIn("&amp;", out,
                      "&amp; escape not found for project name")

    # -- Empty / missing data --
    def test_empty_data_no_crash(self):
        """digest_html with empty by_tool, top_projects, None busiest/delta/rtk → valid HTML."""
        minimal = {
            "generated_at": "2026-06-11T00:00:00",
            "period": {"start": "2026-06-04", "end": "2026-06-11", "days": 7},
            "totals": {"cost": 0.0, "out": 0, "in_total": 0, "n": 0},
            "prev": {"cost": 0.0, "out": 0, "n": 0},
            "delta_cost_pct": None,
            "cache_hit_rate": 0.0,
            "by_tool": {},
            "by_day": [],
            "busiest_day": None,
            "top_projects": [],
            "rtk": None,
        }
        out = pulse.digest_html(minimal)
        self.assertIn("</html>", out)
        self.assertNotIn("<script", out.lower())
        self.assertNotIn("://", out)

    # -- cmd_digest html smoke --
    def test_cmd_digest_html_prints_doctype(self):
        """cmd_digest(7, 'html') writes a DOCTYPE to stdout."""
        from datetime import datetime as _dt, timedelta as _td
        now = _dt.now()
        today = now.strftime("%Y-%m-%d")
        idx = pulse._empty_index()
        idx["days"][today] = {
            "projA": {"claude-sonnet-4-5": {
                "in": 500, "out": 200, "cc5": 0, "cc1": 0, "cr": 100,
                "n": 5, "cost": 3.0}}}
        buf = io.StringIO()
        with patch("pulse.refresh_index", return_value=(idx, False)), \
             patch("pulse.rtk_gain", return_value=None), \
             patch("pulse.fx_thb", return_value=(32.0, "test")), \
             contextlib.redirect_stdout(buf):
            pulse.cmd_digest(7, "html")
        out = buf.getvalue()
        self.assertTrue(out.strip().startswith("<!DOCTYPE html"),
                        "cmd_digest html output must start with <!DOCTYPE html")
        self.assertNotIn("<script", out.lower())
        self.assertNotIn("://", out)


# ---------------------------------------------------------------------------
# C18 — Fleet / multi-machine (node slug, snapshot, merge, HTTP route)
# ---------------------------------------------------------------------------

class TestNodeSlug(unittest.TestCase):
    """_node_name and _node_slug."""

    def test_node_name_env_var(self):
        """RTK_PULSE_NODE overrides hostname."""
        with patch.dict("os.environ", {"RTK_PULSE_NODE": "my-laptop"}):
            self.assertEqual(pulse._node_name(), "my-laptop")

    def test_node_name_hostname_fallback(self):
        """Falls back to socket.gethostname() when env var absent."""
        env = {k: v for k, v in __import__("os").environ.items()
               if k != "RTK_PULSE_NODE"}
        with patch.dict("os.environ", env, clear=True), \
             patch("pulse.socket.gethostname", return_value="box.local"):
            self.assertEqual(pulse._node_name(), "box.local")

    def test_node_name_strips_whitespace(self):
        with patch.dict("os.environ", {"RTK_PULSE_NODE": "  host  "}):
            self.assertEqual(pulse._node_name(), "host")

    def test_slug_safe_chars(self):
        self.assertEqual(pulse._node_slug("my-laptop.local"), "my-laptop.local")

    def test_slug_replaces_slashes(self):
        slug = pulse._node_slug("../../evil")
        self.assertNotIn("/", slug)
        self.assertNotIn(".", slug[0:1],
                         "leading dot must be stripped to avoid hidden/.. files")

    def test_slug_strips_leading_dots(self):
        self.assertEqual(pulse._node_slug("..foo"), "foo")
        self.assertEqual(pulse._node_slug(".hidden"), "hidden")

    def test_slug_empty_fallback(self):
        self.assertEqual(pulse._node_slug(""), "node")
        self.assertEqual(pulse._node_slug("..."), "node")

    def test_path_traversal_guard(self):
        """RTK_PULSE_NODE='../../evil' — exported file stays inside NODES_DIR."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            nodes_dir = Path(tmp) / "nodes"
            with patch.dict("os.environ", {"RTK_PULSE_NODE": "../../evil"}), \
                 patch("pulse.NODES_DIR", nodes_dir):
                name = pulse._node_name()
                slug = pulse._node_slug(name)
                dest = (nodes_dir / (slug + ".json"))
                nodes_dir.mkdir(parents=True, exist_ok=True)
                # resolve relative to nodes_dir — must not escape
                self.assertEqual(dest.resolve().parent, nodes_dir.resolve())


class TestBuildNodeSnapshot(unittest.TestCase):
    """build_node_snapshot: shape, window cutoff, project collapse."""

    def _make_idx(self):
        idx = pulse._empty_index()
        from datetime import datetime, timedelta
        today = datetime.now().strftime("%Y-%m-%d")
        old = (datetime.now() - timedelta(days=pulse.KEEP_DAYS + 5)).strftime("%Y-%m-%d")
        # today: two projects, same model → should be summed
        idx["days"][today] = {
            "projA": {"claude-sonnet-4-5": {
                "in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 10, "n": 2, "cost": 0.001}},
            "projB": {"claude-sonnet-4-5": {
                "in": 200, "out": 80, "cc5": 0, "cc1": 0, "cr": 0, "n": 1, "cost": 0.002}},
        }
        # old day (outside window) — should be excluded
        idx["days"][old] = {
            "projC": {"claude-opus-4-7": {
                "in": 999, "out": 999, "cc5": 0, "cc1": 0, "cr": 0, "n": 5, "cost": 9.0}},
        }
        return idx, today, old

    def test_shape(self):
        idx, today, _ = self._make_idx()
        snap = pulse.build_node_snapshot(idx)
        for k in ("schema", "node", "generated_at", "days"):
            self.assertIn(k, snap)
        self.assertEqual(snap["schema"], 1)
        self.assertIsInstance(snap["node"], str)
        self.assertIsInstance(snap["days"], dict)

    def test_window_cutoff_excludes_old(self):
        idx, today, old = self._make_idx()
        snap = pulse.build_node_snapshot(idx, days=pulse.KEEP_DAYS)
        self.assertIn(today, snap["days"])
        self.assertNotIn(old, snap["days"])

    def test_project_collapse(self):
        """Two projects on same day/model are summed; project keys absent."""
        idx, today, _ = self._make_idx()
        snap = pulse.build_node_snapshot(idx)
        day_entry = snap["days"].get(today, {})
        # no project keys — only model keys
        for k in day_entry:
            self.assertFalse(k.startswith("proj"),
                             f"project name '{k}' must not appear in node snapshot")
        model_entry = day_entry.get("claude-sonnet-4-5", {})
        self.assertEqual(model_entry["out"], 50 + 80)
        self.assertEqual(model_entry["in"], 100 + 200)
        self.assertEqual(model_entry["n"], 2 + 1)


class TestReadNodes(unittest.TestCase):
    """read_nodes: missing dir, malformed JSON, wrong shape, only *.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.nodes_dir = Path(self.tmp.name) / "nodes"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, content):
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        (self.nodes_dir / name).write_text(
            content if isinstance(content, str) else json.dumps(content))

    def test_missing_dir_returns_empty(self):
        with patch("pulse.NODES_DIR", self.nodes_dir):
            result = pulse.read_nodes()
        self.assertEqual(result, [])

    def test_malformed_json_skipped(self):
        self._write("bad.json", "NOT JSON {{{")
        with patch("pulse.NODES_DIR", self.nodes_dir):
            result = pulse.read_nodes()
        self.assertEqual(result, [])

    def test_wrong_shape_skipped(self):
        # missing 'node' key
        self._write("no-node.json", {"days": {}})
        # missing 'days' key
        self._write("no-days.json", {"node": "x"})
        # not a dict
        self._write("list.json", [1, 2, 3])
        with patch("pulse.NODES_DIR", self.nodes_dir):
            result = pulse.read_nodes()
        self.assertEqual(result, [])

    def test_only_json_files(self):
        """Non-.json files in nodes/ are not read."""
        self._write("node1.json", {"node": "n1", "days": {}})
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        (self.nodes_dir / "ignore.txt").write_text('{"node":"x","days":{}}')
        with patch("pulse.NODES_DIR", self.nodes_dir):
            result = pulse.read_nodes()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["node"], "n1")

    def test_valid_file_parsed(self):
        payload = {"schema": 1, "node": "builder-1", "generated_at": "2026-06-10T00:00:00",
                   "days": {"2026-06-10": {"claude-sonnet-4-5": {
                       "in": 100, "out": 50, "cc5": 0, "cc1": 0, "cr": 0, "n": 1, "cost": 0.001}}}}
        self._write("builder-1.json", payload)
        with patch("pulse.NODES_DIR", self.nodes_dir):
            result = pulse.read_nodes()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["node"], "builder-1")


class TestBuildFleet(unittest.TestCase):
    """build_fleet: merge, live-local overlay, dedup, fleet totals."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.nodes_dir = Path(self.tmp.name) / "nodes"
        self.nodes_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _remote_snap(self, node_name, date, out=50, cost=0.001, generated_at=None):
        from datetime import datetime
        ga = generated_at or datetime.now().isoformat(timespec="seconds")
        return {
            "schema": 1,
            "node": node_name,
            "generated_at": ga,
            "days": {date: {"claude-sonnet-4-5": {
                "in": 100, "out": out, "cc5": 0, "cc1": 0, "cr": 0, "n": 1, "cost": cost}}},
        }

    def _write_remote(self, snap):
        slug = pulse._node_slug(snap["node"])
        (self.nodes_dir / (slug + ".json")).write_text(json.dumps(snap))

    def _local_idx(self, date, out=30, cost=0.0005):
        idx = pulse._empty_index()
        idx["days"][date] = {
            "proj": {"claude-sonnet-4-5": {
                "in": 50, "out": out, "cc5": 0, "cc1": 0, "cr": 0, "n": 1, "cost": cost}}}
        return idx

    def test_live_local_always_in_result(self):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        idx = self._local_idx(today)
        local_name = pulse._node_name()
        with patch("pulse.NODES_DIR", self.nodes_dir):
            result = pulse.build_fleet(idx, days=30)
        node_names = [n["node"] for n in result["nodes"]]
        self.assertIn(local_name, node_names)

    def test_live_local_supersedes_stale_file(self):
        """A nodes/ file for the same name as local is replaced by live data."""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        local_name = pulse._node_name()
        # stale remote file with old generated_at and different out
        stale = self._remote_snap(local_name, today, out=999, cost=9.99,
                                  generated_at="2020-01-01T00:00:00")
        self._write_remote(stale)
        idx = self._local_idx(today, out=42, cost=0.001)
        with patch("pulse.NODES_DIR", self.nodes_dir):
            result = pulse.build_fleet(idx, days=30)
        local_node = next(n for n in result["nodes"] if n["node"] == local_name)
        self.assertTrue(local_node["local"])
        # live data (out=42) not the stale file (out=999)
        self.assertEqual(local_node["window"]["out"], 42)

    def test_merges_remote_nodes(self):
        """Remote nodes appear alongside the local node."""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        remote = self._remote_snap("remote-box", today, out=100, cost=0.002)
        self._write_remote(remote)
        idx = self._local_idx(today)
        with patch("pulse.NODES_DIR", self.nodes_dir):
            result = pulse.build_fleet(idx, days=30)
        node_names = [n["node"] for n in result["nodes"]]
        self.assertIn("remote-box", node_names)

    def test_duplicate_names_newer_generated_at_wins(self):
        """When two files have the same node name, the newer one is kept."""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        old_snap = self._remote_snap("twin", today, out=10, generated_at="2026-01-01T00:00:00")
        new_snap = self._remote_snap("twin", today, out=99, generated_at="2026-06-10T00:00:00")
        # write both — same slug so second write overwrites first; simulate via read_nodes
        with patch("pulse.NODES_DIR", self.nodes_dir), \
             patch("pulse.read_nodes", return_value=[old_snap, new_snap]), \
             patch("pulse._node_name", return_value="local-machine"):
            result = pulse.build_fleet(pulse._empty_index(), days=30)
        twin_node = next((n for n in result["nodes"] if n["node"] == "twin"), None)
        self.assertIsNotNone(twin_node)
        self.assertEqual(twin_node["window"]["out"], 99)

    def test_fleet_totals_equal_sum_of_nodes(self):
        """fleet.window.cost = sum of all nodes' window.cost."""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        remote = self._remote_snap("remote-box", today, cost=0.05)
        self._write_remote(remote)
        idx = self._local_idx(today, cost=0.02)
        with patch("pulse.NODES_DIR", self.nodes_dir):
            result = pulse.build_fleet(idx, days=30)
        fleet_cost = result["fleet"]["window"]["cost"]
        node_total = sum(n["window"]["cost"] for n in result["nodes"])
        self.assertAlmostEqual(fleet_cost, node_total, places=6)

    def test_required_keys_in_response(self):
        from datetime import datetime
        idx = pulse._empty_index()
        with patch("pulse.NODES_DIR", self.nodes_dir):
            result = pulse.build_fleet(idx)
        for k in ("generated_at", "days", "local_node", "nodes", "fleet", "fleet_daily"):
            self.assertIn(k, result, f"key '{k}' missing from build_fleet result")


class TestHttpFleet(unittest.TestCase):
    """HTTP /api/fleet route: 200 + application/json + expected keys; days clamping."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data_dir = Path(self.tmp.name)
        nodes_dir = data_dir / "nodes"
        self._patches = [
            patch("pulse._discover", return_value=[]),
            patch("pulse.fx_thb", return_value=(32.0, "test")),
            patch("pulse.rtk_gain", return_value=None),
            patch("pulse.DATA_DIR", data_dir),
            patch("pulse.INDEX_FILE", data_dir / "index.json"),
            patch("pulse.HISTORY_FILE", data_dir / "history.jsonl"),
            patch("pulse.NODES_DIR", nodes_dir),
        ]
        for p in self._patches:
            p.start()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), pulse.Handler)
        self.port = self.srv.server_address[1]
        self._thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}")

    def _json(self, path):
        with self._get(path) as r:
            return json.loads(r.read()), r

    def test_api_fleet_returns_json(self):
        """/api/fleet → 200, application/json, required keys present."""
        data, resp = self._json("/api/fleet")
        self.assertEqual(resp.status, 200)
        self.assertIn("application/json", resp.headers.get("Content-Type", ""))
        for k in ("nodes", "fleet", "local_node", "fleet_daily"):
            self.assertIn(k, data, f"key '{k}' missing from /api/fleet")

    def test_api_fleet_days_clamp_large(self):
        """/api/fleet?days=9999 → days clamped to KEEP_DAYS."""
        data, _ = self._json(f"/api/fleet?days=9999")
        self.assertLessEqual(data["days"], pulse.KEEP_DAYS)

    def test_api_fleet_days_invalid(self):
        """/api/fleet?days=abc → days defaults to 30."""
        data, _ = self._json("/api/fleet?days=abc")
        self.assertEqual(data["days"], 30)


if __name__ == "__main__":
    unittest.main()
