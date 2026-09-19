"""Live tests: need `playwright install chromium` and the recorded artifact under evidence/capabilities.
Marked `live`; CI runs them in the browser job. The engine's own auto-start brings up the local mock bank + API."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from cua.artifact.store import ArtifactStore
from cua.cli import run_replay

pytestmark = pytest.mark.live
ARTIFACTS = Path("evidence/capabilities")


@pytest.fixture(autouse=True)
def _evidence_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test runs must not add folders to the committed evidence."""
    from cua.config import settings

    monkeypatch.setattr(settings, "evidence_dir", tmp_path)


def _artifact():  # type: ignore[no-untyped-def]
    caps = ArtifactStore(ARTIFACTS).list() if ARTIFACTS.exists() else []
    if not caps:
        pytest.skip("no recorded artifact yet")
    return caps[0]


def _replay(member_id: str, fault: str | None):  # type: ignore[no-untyped-def]
    return asyncio.run(
        run_replay(
            _artifact(),
            {"member_id": member_id},
            fault=fault,
            tenant=None,
            attended=False,
            idempotency_key=None,
            approved=False,
            dry_run=False,
        )
    )


def test_replay_success_returns_typed_output() -> None:
    res = _replay("10041", None)
    assert res.kind.value == "success" and res.llm_calls == 0
    assert res.outputs["savings_balance"].replace(".", "").isdigit()


def test_replay_not_found_is_business_outcome() -> None:
    res = _replay("99999", "not_found")
    assert (
        res.kind.value == "business_outcome" and res.outcome_code == "MEMBER_NOT_FOUND" and res.llm_calls == 0
    )


def test_replay_recovers_from_interstitial() -> None:
    res = _replay("10041", "interstitial")
    assert res.kind.value == "success" and [r.interrupt_id for r in res.recoveries] == ["notice_interstitial"]


def test_replay_restarts_after_session_expiry() -> None:
    res = _replay("10041", "session_expired")
    assert res.kind.value == "success" and [r.interrupt_id for r in res.recoveries] == ["session_expired"]


@pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"), reason="discovery needs a real key")
def test_discovery_smoke() -> None:
    from cua.cli import run_discovery

    _path, out = asyncio.run(
        run_discovery(
            "Look up member 10023 and read their savings balance",
            "http://localhost:7860/bank/login",
            "lookup_member_balance",
            {"member_id": "10023"},
        )
    )
    assert out.status in {"success", "business_outcome"}
