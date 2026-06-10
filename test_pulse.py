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
        old = (today - timedelta(days=pulse.KEEP_DAYS + 10)).strftime("%Y-%m-%d")
        recent = (today - timedelta(days=5)).strftime("%Y-%m-%d")
        hist = self._write_history([old, recent])

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
        self.assertNotIn(old, dates, "entry older than KEEP_DAYS should be pruned")
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


if __name__ == "__main__":
    unittest.main()
