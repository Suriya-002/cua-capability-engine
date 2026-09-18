from __future__ import annotations

import asyncio

import pytest

from cua.escalation.session import Controller, IllegalTransition, SessionController, SessionState
from cua.evidence.writer import EvidenceWriter


def test_full_handoff_cycle(evidence: EvidenceWriter) -> None:
    s = SessionController(evidence, "cap@1.0.0")
    assert s.engine_may_act
    req = s.escalate("locator not found", step_n=2, screenshot_path=None)
    assert s.state == SessionState.PAUSED and s.controller == Controller.NOBODY and not s.engine_may_act
    s.acquire("suriya")
    assert s.state == SessionState.HUMAN
    s.record_human_step(kind="click", text="Continue")
    assert req.human_steps and req.acquired_by == "suriya"
    s.release("dismissed notice")
    assert s.state == SessionState.RESUMING and s.controller == Controller.ENGINE
    s.resume_ok()
    assert s.state == SessionState.AUTOMATION and s.engine_may_act and s.request is None
    import json

    types = [json.loads(ln)["type"] for ln in (evidence.dir / "events.jsonl").read_text().splitlines()]
    assert types.count("control_transition") == 4 and "human_step" in types


def test_illegal_transitions(evidence: EvidenceWriter) -> None:
    s = SessionController(evidence, "cap@1.0.0")
    with pytest.raises(IllegalTransition):
        s.acquire("x")  # nothing to acquire
    s.escalate("r", step_n=1, screenshot_path=None)
    with pytest.raises(IllegalTransition):
        s.release("x")  # must acquire first
    s.abort("gave up")
    with pytest.raises(IllegalTransition):
        s.acquire("x")


def test_resume_failed_reopens(evidence: EvidenceWriter) -> None:
    s = SessionController(evidence, "cap@1.0.0")
    s.escalate("r", step_n=1, screenshot_path=None)
    s.acquire("op")
    s.release("done")
    s.resume_failed("still on notice page")
    assert s.state == SessionState.PAUSED


async def test_engine_waits_for_resume(evidence: EvidenceWriter) -> None:
    s = SessionController(evidence, "cap@1.0.0")
    s.escalate("r", step_n=1, screenshot_path=None)

    async def operator() -> None:
        await asyncio.sleep(0.05)
        s.acquire("op")
        s.release("ok")
        s.resume_ok()

    task = asyncio.create_task(operator())
    assert await s.wait_for_resume(1.0)
    await task
    assert s.state == SessionState.AUTOMATION


async def test_wait_times_out(evidence: EvidenceWriter) -> None:
    s = SessionController(evidence, "cap@1.0.0")
    s.escalate("r", step_n=1, screenshot_path=None)
    assert not await s.wait_for_resume(0.05)
