"""Live tests: need `playwright install chromium`. Marked `live`; CI runs them in the browser job."""

from __future__ import annotations

import asyncio
import os
import socket
import threading
from pathlib import Path

import pytest
import uvicorn

from cua.artifact.store import ArtifactStore
from cua.cli import run_replay

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def server() -> str:
    from mockbank.app import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    import time

    time.sleep(0.8)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True


@pytest.mark.skipif(not Path("evidence/capabilities").exists(), reason="no recorded artifact yet")
def test_replay_not_found_is_business_outcome(server: str) -> None:
    cap = next(iter(ArtifactStore(Path("evidence/capabilities")).list()))
    cap = cap.model_copy(update={"entry_url": cap.entry_url.replace("localhost:7860", server.split("//")[1])})
    res = asyncio.run(
        run_replay(
            cap,
            {"member_id": "99999"},
            fault="not_found",
            tenant=None,
            attended=False,
            idempotency_key=None,
            approved=False,
            dry_run=False,
        )
    )
    assert (
        res.kind.value == "business_outcome" and res.outcome_code == "MEMBER_NOT_FOUND" and res.llm_calls == 0
    )


@pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"), reason="discovery needs a real key")
def test_discovery_smoke(server: str) -> None:
    from cua.cli import run_discovery

    _path, out = asyncio.run(
        run_discovery(
            "Look up member 10023 and read their savings balance",
            f"{server}/bank/login",
            "lookup_member_balance",
            {"member_id": "10023"},
        )
    )
    assert out.status in {"success", "business_outcome"}
