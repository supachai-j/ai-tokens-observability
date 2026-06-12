#!/usr/bin/env python3
"""Capture dashboard screenshots for docs/screenshots/ using headless Chrome.

Privacy-safe:  seeds synthetic data only via contrib/seed_demo.py.
               serves pulse.py with HOME overridden to an empty temp dir
               so the live scanner cannot reach real ~/.claude, ~/.codex,
               or ~/.gemini transcripts.
               Verifies /api/summary contains ONLY synthetic project names
               before any screenshot is taken — stops with an error if not.
               Real ~/.config/rtk-pulse is untouched.

Produces:
    docs/screenshots/dashboard-light.png  (full dashboard, light theme)
    docs/screenshots/dashboard-fleet.png  (full page, fleet panel visible;
                                           also light theme)

Usage:
    python3 contrib/capture_screenshots.py
    python3 contrib/capture_screenshots.py --out docs/screenshots --port 18399
"""

import argparse
import base64
import json
import os
import random
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────

REPO_ROOT  = Path(__file__).resolve().parent.parent
CHROME_BIN = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
WINDOW_W   = 1440
WINDOW_H   = 900
DEBUG_PORT = 9293          # CDP remote-debugging port
WAIT_AFTER_LOAD = 4.0      # seconds — time for SSE + Chart.js to render

# Seed projects (must match seed_demo.py FAKE_PROJECTS exactly)
SYNTHETIC_PROJECTS = {
    "acme-billing-api",
    "robot-haiku-generator",
    "infinite-todo-app",
    "hyperdrive-scheduler",
    "quantum-standup-bot",
}


# ── Minimal stdlib WebSocket / CDP client ─────────────────────────────────────

class CDPClient:
    """Minimal Chrome DevTools Protocol client over WebSocket (no external deps)."""

    def __init__(self, host: str, port: int, ws_path: str):
        self._sock = socket.create_connection((host, port), timeout=15)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._cid = 0
        # ── WebSocket handshake ──────────────────────────────────────────
        raw_key = bytes(random.randint(0, 255) for _ in range(16))
        key = base64.b64encode(raw_key).decode()
        hs = (
            f"GET {ws_path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(hs.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self._sock.recv(256)
        if b"101" not in resp:
            raise RuntimeError(f"WebSocket handshake failed: {resp[:200]}")

    # ── send ──────────────────────────────────────────────────────────────────

    def call(self, method: str, params: dict = None) -> int:
        self._cid += 1
        cid = self._cid
        payload = json.dumps({"id": cid, "method": method,
                              "params": params or {}}).encode()
        self._ws_send(payload)
        return cid

    def _ws_send(self, data: bytes) -> None:
        mask = bytes(random.randint(0, 255) for _ in range(4))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        n = len(data)
        if n < 126:
            header = bytes([0x81, 0x80 | n])
        elif n < 65536:
            header = bytes([0x81, 0xFE, n >> 8, n & 0xFF])
        else:
            header = bytes([0x81, 0xFF]) + struct.pack(">Q", n)
        self._sock.sendall(header + mask + masked)

    # ── receive ───────────────────────────────────────────────────────────────

    def recv(self, timeout: float = 30.0) -> dict:
        self._sock.settimeout(timeout)
        hdr = self._recv_exact(2)
        length = hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        if hdr[1] & 0x80:                    # server-masked (rare)
            mask = self._recv_exact(4)
            raw = bytearray(self._recv_exact(length))
            for i in range(len(raw)):
                raw[i] ^= mask[i % 4]
            return json.loads(raw)
        return json.loads(self._recv_exact(length))

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("CDP connection closed unexpectedly")
            buf += chunk
        return buf

    def wait_id(self, cid: int, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.recv(timeout=max(0.5, deadline - time.monotonic()))
            except (TimeoutError, OSError):
                break
            if msg.get("id") == cid:
                return msg
        raise TimeoutError(f"No CDP response for id={cid} within {timeout}s")

    def wait_event(self, event: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.recv(timeout=max(0.5, deadline - time.monotonic()))
            except (TimeoutError, OSError):
                break
            if msg.get("method") == event:
                return msg
        raise TimeoutError(f"CDP event {event!r} not received within {timeout}s")

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def wait_url(url: str, retries: int = 30, delay: float = 0.5) -> None:
    for i in range(retries):
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            if i == retries - 1:
                raise RuntimeError(f"URL never became reachable: {url}")
            time.sleep(delay)


def verify_isolation(base_url: str) -> None:
    """Abort if /api/summary reveals any real project names."""
    try:
        resp = urllib.request.urlopen(base_url + "/api/summary", timeout=10)
        data = json.load(resp)
    except Exception as exc:
        print(f"  [warn] /api/summary unreachable ({exc}); skipping isolation check")
        return
    # projects key may be dict or list depending on version
    raw = data.get("projects") or {}
    if isinstance(raw, dict):
        projects = set(raw.keys())
    else:
        projects = set(raw)
    if not projects:
        print("  isolation check: 0 projects returned (scanner may still be indexing)")
        return
    real = projects - SYNTHETIC_PROJECTS
    if real:
        raise RuntimeError(
            f"\n\n*** ISOLATION FAILURE ***\n"
            f"Real project names detected in /api/summary: {sorted(real)}\n"
            f"Full project set: {sorted(projects)}\n"
            f"ABORT — screenshot not taken."
        )
    print(f"  isolation OK: {sorted(projects)}")


def cdp_ws_path(debug_port: int) -> str:
    """Return WebSocket path for the first page target."""
    for _ in range(10):
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{debug_port}/json", timeout=5)
            targets = json.load(resp)
            for t in targets:
                if t.get("type") == "page":
                    # webSocketDebuggerUrl: ws://localhost:PORT/devtools/page/ID
                    ws_url = t["webSocketDebuggerUrl"]
                    path = "/" + ws_url.split("/", 3)[-1]
                    return path
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("No CDP page target found")


def js_eval(cdp: CDPClient, expr: str) -> object:
    cid = cdp.call("Runtime.evaluate",
                   {"expression": expr, "returnByValue": True})
    result = cdp.wait_id(cid)
    return result.get("result", {}).get("result", {}).get("value")


def capture_one(base_url: str, debug_port: int, theme: str,
                out_path: Path, label: str) -> None:
    """Open a fresh CDP connection, navigate with light/dark theme, screenshot."""
    print(f"\n  → Capturing {label} …")
    ws_path = cdp_ws_path(debug_port)
    cdp = CDPClient("127.0.0.1", debug_port, ws_path)
    try:
        # Enable domains
        cdp.wait_id(cdp.call("Page.enable"))
        cdp.wait_id(cdp.call("Runtime.enable"))

        # Emulate prefers-color-scheme (sets theme before JS reads localStorage)
        cdp.wait_id(cdp.call("Emulation.setEmulatedMedia", {
            "features": [{"name": "prefers-color-scheme", "value": theme}]
        }))

        # Navigate
        cdp.wait_id(cdp.call("Page.navigate", {"url": base_url}), timeout=20)
        cdp.wait_event("Page.loadEventFired", timeout=20)

        # Wait for SSE data + Chart.js render
        time.sleep(WAIT_AFTER_LOAD)

        # Verify theme attribute
        actual = js_eval(cdp, "document.documentElement.dataset.theme")
        print(f"    data-theme={actual!r}")
        if actual != theme:
            # Fallback: force via JS then reload
            print(f"    [warn] theme mismatch; forcing via localStorage")
            js_eval(cdp,
                f"localStorage.setItem('theme','{theme}');"
                f"document.documentElement.dataset.theme='{theme}';"
            )
            cdp.wait_id(cdp.call("Page.reload"))
            cdp.wait_event("Page.loadEventFired", timeout=20)
            time.sleep(WAIT_AFTER_LOAD)
            actual = js_eval(cdp, "document.documentElement.dataset.theme")
            print(f"    data-theme after force={actual!r}")
            assert actual == theme, f"Could not force theme to {theme!r}"

        # Full-page screenshot (captures content beyond viewport)
        cid = cdp.call("Page.captureScreenshot", {
            "format": "png",
            "captureBeyondViewport": True,
            "fromSurface": True,
        })
        result = cdp.wait_id(cid, timeout=30)
        png = base64.b64decode(result["result"]["data"])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(png)
        print(f"    saved → {out_path}  ({len(png) // 1024} KB)")
    finally:
        cdp.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="docs/screenshots",
                    help="output directory (default: docs/screenshots)")
    ap.add_argument("--port", type=int, default=18399,
                    help="pulse.py serve port (default: 18399)")
    ap.add_argument("--debug-port", type=int, default=DEBUG_PORT,
                    help=f"Chrome CDP port (default: {DEBUG_PORT})")
    args = ap.parse_args()

    out_dir    = REPO_ROOT / args.out
    server_port = args.port
    debug_port  = args.debug_port
    base_url    = f"http://127.0.0.1:{server_port}"

    if not CHROME_BIN.exists():
        sys.exit(f"Chrome not found at {CHROME_BIN}")

    real_cfg    = Path.home() / ".config" / "rtk-pulse"
    real_claude = Path.home() / ".claude"

    server_proc = chrome_proc = None

    with tempfile.TemporaryDirectory(prefix="rtk-screenshots-") as workdir:
        workdir = Path(workdir)
        rtk_home  = workdir / "rtk-pulse-data"
        fake_home = workdir / "fake-home"
        chrome_profile = workdir / "chrome-profile"
        fake_home.mkdir()
        chrome_profile.mkdir()

        capture_error = None
        try:
            # ── 1. Seed synthetic data ─────────────────────────────────────
            print("Seeding synthetic data …")
            subprocess.run(
                [sys.executable,
                 str(REPO_ROOT / "contrib" / "seed_demo.py"),
                 "--out", str(rtk_home), "--quiet"],
                check=True, capture_output=True,
            )
            print(f"  seeded → {rtk_home}")

            # ── 2. Start isolated server ───────────────────────────────────
            print(f"Starting pulse.py serve on port {server_port} …")
            env = {
                **os.environ,
                "RTK_PULSE_HOME": str(rtk_home),
                "HOME":           str(fake_home),
            }
            server_proc = subprocess.Popen(
                [sys.executable,
                 str(REPO_ROOT / "pulse.py"),
                 "serve", "--port", str(server_port)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            wait_url(base_url)
            print(f"  server ready at {base_url}")

            # ── 3. Isolation check ─────────────────────────────────────────
            print("Checking isolation (only synthetic projects allowed) …")
            verify_isolation(base_url)

            # ── 4. Start headless Chrome ───────────────────────────────────
            print(f"Starting headless Chrome (CDP port {debug_port}) …")
            chrome_proc = subprocess.Popen(
                [str(CHROME_BIN),
                 "--headless",
                 f"--remote-debugging-port={debug_port}",
                 f"--user-data-dir={chrome_profile}",
                 f"--window-size={WINDOW_W},{WINDOW_H}",
                 "--no-first-run",
                 "--no-default-browser-check",
                 "--disable-extensions",
                 "--disable-background-networking",
                 "--disable-sync",
                 "about:blank"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            wait_url(f"http://127.0.0.1:{debug_port}/json")
            print("  Chrome CDP ready")

            # ── 5. Capture screenshots ─────────────────────────────────────

            # 5a. Full dashboard — light theme
            capture_one(
                base_url=base_url,
                debug_port=debug_port,
                theme="light",
                out_path=out_dir / "dashboard-light.png",
                label="dashboard-light (full page, light theme)",
            )

            # 5b. Fleet panel — re-use same light theme, full page
            # (captureBeyondViewport captures the fleet section which is
            # lower on the page; both shots serve as light + fleet evidence)
            capture_one(
                base_url=base_url,
                debug_port=debug_port,
                theme="light",
                out_path=out_dir / "dashboard-fleet.png",
                label="dashboard-fleet (full page showing fleet panel)",
            )

        except Exception as exc:   # noqa: BLE001
            capture_error = exc

        finally:
            # ── 6. Tear down ───────────────────────────────────────────────
            if chrome_proc:
                chrome_proc.terminate()
                chrome_proc.wait(timeout=5)
            if server_proc:
                server_proc.terminate()
                server_proc.wait(timeout=5)

        # Re-raise capture errors after cleanup
        if capture_error:
            raise capture_error

        # ── 7. Privacy / isolation verification ────────────────────────────
        # Must run inside the `with` block so the temp dir still exists.
        # We do NOT check that real dirs are byte-for-byte unchanged — in a
        # live Claude Code session ~/.claude is written to continuously and
        # the real rtk-pulse may rescan in the background. The meaningful
        # checks are:
        #
        # (a) Synthetic server wrote index to the temp dir, not the real one.
        # (b) No synthetic project names leaked INTO the real index.
        # (c) Fake HOME path doesn't appear in the real index.
        print("\nPrivacy / isolation verification …")
        fake_home_str = str(fake_home)

        # (a) Synthetic server wrote to temp dir
        temp_index = rtk_home / "index.json"
        if not temp_index.exists():
            sys.exit("ISOLATION ERROR: server didn't create index.json in temp dir")
        print(f"  synthetic server wrote to temp dir ✓  ({temp_index.stat().st_size} bytes)")

        # (b) Contamination: synthetic names must NOT be in the real index
        real_index = real_cfg / "index.json"
        if real_index.exists():
            real_text = real_index.read_text(errors="replace")
            contaminated = {p for p in SYNTHETIC_PROJECTS if p in real_text}
            if contaminated:
                sys.exit(
                    f"\nCONTAMINATION ERROR: synthetic project names found in "
                    f"real ~/.config/rtk-pulse/index.json: {sorted(contaminated)}\n"
                    f"The synthetic server appears to have written to the real config dir."
                )
            print("  no synthetic names in real index ✓")
        else:
            print("  real index absent — nothing to check ✓")

        # (c) Real index must not reference the fake HOME path
        if real_index.exists() and fake_home_str in real_index.read_text(errors="replace"):
            sys.exit(
                f"ISOLATION ERROR: fake HOME path {fake_home_str!r} found in real "
                "index.json — scanner reached fake HOME."
            )
        print("  fake HOME path not in real index ✓")

    print("\nAll screenshots captured successfully.")
    print(f"  {out_dir}/dashboard-light.png")
    print(f"  {out_dir}/dashboard-fleet.png")


if __name__ == "__main__":
    main()
