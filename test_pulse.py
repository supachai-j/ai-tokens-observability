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


if __name__ == "__main__":
    unittest.main()
