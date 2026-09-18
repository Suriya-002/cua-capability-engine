"""Who is in control of the live session, and how control moves.

    AUTOMATION --escalate--> PAUSED --acquire--> HUMAN --release--> RESUMING --verify--> AUTOMATION
                                 \\--cancel-->  ABORTED               \\--abort-->   ABORTED

Invariants:
- Exactly one controller at a time. The engine will not act unless state == AUTOMATION.
- Every transition is an evidence event (who, why, when).
- Human actions during HUMAN are recorded as `human_step` events by the surface's recorder hook.
- RESUMING re-verifies the current step's precondition before the engine continues; if it fails,
  the request is re-opened rather than the engine guessing.

This module is transport-agnostic: the operator UI (FastAPI + noVNC) just calls these methods.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from cua.evidence.writer import EvidenceWriter


class SessionState(StrEnum):
    AUTOMATION = "automation"
    PAUSED = "paused"
    HUMAN = "human"
    RESUMING = "resuming"
    ABORTED = "aborted"
    COMPLETED = "completed"


class Controller(StrEnum):
    ENGINE = "engine"
    HUMAN = "human"
    NOBODY = "nobody"


class IllegalTransition(RuntimeError):
    pass


@dataclass
class InterventionRequest:
    id: str
    run_id: str
    capability_ref: str
    step_n: int | None
    reason: str
    screenshot_path: str | None
    context: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    acquired_by: str | None = None
    resolution: str | None = None
    human_steps: list[dict[str, Any]] = field(default_factory=list)


_LEGAL: dict[SessionState, set[SessionState]] = {
    SessionState.AUTOMATION: {SessionState.PAUSED, SessionState.COMPLETED, SessionState.ABORTED},
    SessionState.PAUSED: {SessionState.HUMAN, SessionState.ABORTED},
    SessionState.HUMAN: {SessionState.RESUMING, SessionState.ABORTED},
    SessionState.RESUMING: {SessionState.AUTOMATION, SessionState.PAUSED, SessionState.ABORTED},
    SessionState.ABORTED: set(),
    SessionState.COMPLETED: set(),
}


class SessionController:
    def __init__(self, evidence: EvidenceWriter, capability_ref: str, on_escalate: Any = None) -> None:
        self.on_escalate = (
            on_escalate  # callable(InterventionRequest); routes the request to an operator surface
        )
        self.state = SessionState.AUTOMATION
        self.controller = Controller.ENGINE
        self._ev = evidence
        self._cap = capability_ref
        self.request: InterventionRequest | None = None
        self._resume_event = asyncio.Event()
        self._resume_event.set()

    # --- transitions ----------------------------------------------------------------------
    def _to(self, new: SessionState, controller: Controller, **why: Any) -> None:
        if new not in _LEGAL[self.state]:
            raise IllegalTransition(f"{self.state.value} -> {new.value}")
        self._ev.event(
            "control_transition",
            from_state=self.state.value,
            to_state=new.value,
            controller=controller.value,
            **why,
        )
        self.state, self.controller = new, controller

    def escalate(
        self,
        reason: str,
        *,
        step_n: int | None,
        screenshot_path: str | None,
        context: dict[str, Any] | None = None,
    ) -> InterventionRequest:
        req = InterventionRequest(
            id=uuid.uuid4().hex[:10],
            run_id=self._ev.run_id,
            capability_ref=self._cap,
            step_n=step_n,
            reason=reason,
            screenshot_path=screenshot_path,
            context=context or {},
        )
        self.request = req
        self._resume_event.clear()
        self._to(SessionState.PAUSED, Controller.NOBODY, reason=reason, request_id=req.id, step=step_n)
        if self.on_escalate:
            self.on_escalate(req)
        return req

    def acquire(self, operator: str) -> None:
        if self.request is None:
            raise IllegalTransition("no intervention request to acquire")
        self.request.acquired_by = operator
        self._to(SessionState.HUMAN, Controller.HUMAN, operator=operator, request_id=self.request.id)

    def record_human_step(self, **step: Any) -> None:
        if self.state != SessionState.HUMAN or self.request is None:
            return
        self.request.human_steps.append(step)
        self._ev.event("human_step", request_id=self.request.id, **step)

    def release(self, resolution: str) -> None:
        if self.request is None:
            raise IllegalTransition("no active request")
        self.request.resolution = resolution
        self._to(
            SessionState.RESUMING,
            Controller.ENGINE,
            resolution=resolution,
            request_id=self.request.id,
        )
        self._resume_event.set()  # wake the engine; it re-verifies, then calls resume_ok/resume_failed

    def resume_ok(self) -> None:
        self._to(SessionState.AUTOMATION, Controller.ENGINE, note="precondition re-verified")
        self.request = None
        self._resume_event.set()

    def resume_failed(self, reason: str) -> None:
        """Precondition still false after the human handed back: re-open, don't guess."""
        self._resume_event.clear()
        self._to(SessionState.PAUSED, Controller.NOBODY, reason=f"resume check failed: {reason}")

    def abort(self, reason: str) -> None:
        self._to(SessionState.ABORTED, Controller.NOBODY, reason=reason)
        self._resume_event.set()

    def complete(self) -> None:
        self._to(SessionState.COMPLETED, Controller.NOBODY)

    # --- engine-side waiting --------------------------------------------------------------
    async def wait_for_resume(self, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(self._resume_event.wait(), timeout=timeout_s)
        except TimeoutError:
            return False
        return self.state in {SessionState.AUTOMATION, SessionState.RESUMING}

    @property
    def engine_may_act(self) -> bool:
        return self.state == SessionState.AUTOMATION and self.controller == Controller.ENGINE
