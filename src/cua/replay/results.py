"""The replay result contract. Three kinds, deliberately distinct:

- SUCCESS          the checkpoint held; declared outputs are returned
- BUSINESS_OUTCOME a legitimate answer the caller must handle (MEMBER_NOT_FOUND, ALREADY_APPLIED)
- FAILURE          the engine could not reach a defined state; enough detail to debug

Recoverable conditions (interstitial dismissed, transient retry, re-login) never surface as a
result kind; they are recorded in `recoveries` so the caller can see the run was bumpy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ResultKind(StrEnum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"
    ESCALATED = "escalated"  # paused for a human; the caller should poll or wait


class FailureDetail(BaseModel):
    step_n: int | None
    action: str | None
    expected: str
    observed: str
    category: str = Field(
        ...,
        description="locator_not_found | checkpoint_failed | policy | timeout | app_error | aborted",
    )
    evidence: list[str] = Field(default_factory=list, description="paths to screenshot/dom/aria")


class RecoveryEvent(BaseModel):
    step_n: int
    interrupt_id: str
    handler: str
    attempts: int


class DriftProposal(BaseModel):
    """Emitted when a non-primary locator candidate was needed. A human approves the re-ranking."""

    step_n: int
    used_strategy: str
    used_value: str
    primary_strategy: str
    note: str = "primary candidate failed; consider promoting the used candidate"


class ReplayResult(BaseModel):
    kind: ResultKind
    capability_ref: str
    run_id: str
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome_code: str | None = None
    failure: FailureDetail | None = None
    recoveries: list[RecoveryEvent] = Field(default_factory=list)
    drift_proposals: list[DriftProposal] = Field(default_factory=list)
    steps_completed: int = 0
    duration_ms: int = 0
    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    llm_calls: int = 0  # must be 0 on the production path; asserted in tests

    def ok(self) -> bool:
        return self.kind == ResultKind.SUCCESS
