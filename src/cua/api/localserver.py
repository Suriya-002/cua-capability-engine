"""Start the local mock bank + API if the target is localhost and nothing is listening.

Used by the CLI so `cua discover` / `cua replay` work from a fresh shell with no second window.
The child is killed at interpreter exit. Never used against non-local hosts.
"""

from __future__ import annotations

import atexit
import subprocess
import sys
import time
from urllib.parse import urlparse

import httpx

_child: subprocess.Popen[bytes] | None = None


def ensure_server(url: str, *, wait_s: float = 15.0) -> bool:
    """Return True if a server is reachable (possibly one we just started)."""
    u = urlparse(url)
    if u.hostname not in {"localhost", "127.0.0.1"}:
        return True
    base = f"{u.scheme}://{u.netloc}"
    if _healthy(base):
        return True
    global _child
    port = str(u.port or 7860)
    _child = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "cua.api.app:app", "--port", port, "--log-level", "warning"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    atexit.register(_stop)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _healthy(base):
            print(f"[cua] started local server on {base} (pid {_child.pid})", file=sys.stderr)
            return True
        time.sleep(0.4)
    return False


def _healthy(base: str) -> bool:
    try:
        return httpx.get(f"{base}/health", timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


def _stop() -> None:
    if _child and _child.poll() is None:
        _child.terminate()
        try:
            _child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _child.kill()
