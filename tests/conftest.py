from __future__ import annotations

from pathlib import Path

import pytest

from cua.artifact.schema import (
    ActionType,
    AppRef,
    Capability,
    Checkpoint,
    Locator,
    LocatorCandidate,
    LocatorStrategy,
    Outcome,
    OutputSpec,
    ParamSpec,
    ParamType,
    Provenance,
    Sensitivity,
    Step,
    Transform,
)
from cua.evidence.writer import EvidenceWriter
from cua.policy.redaction import Redactor


def loc(desc: str, *cands: tuple[LocatorStrategy, str, float]) -> Locator:
    return Locator(
        description=desc,
        candidates=[LocatorCandidate(strategy=s, value=v, confidence=c) for s, v, c in cands],
    )


@pytest.fixture
def sample_capability() -> Capability:
    return Capability(
        id="lookup_member_balance",
        version="1.0.0",
        name="Lookup member balance",
        goal="Look up a member and read the savings balance",
        app=AppRef(vendor="LegacyCU", product="Core Servicing", version_fingerprint="abc123"),
        entry_url="http://localhost:7860/bank/login",
        inputs={
            "member_id": ParamSpec(
                type=ParamType.STRING,
                description="member id",
                pattern=r"\d{5}",
                sensitivity=Sensitivity.PII,
            )
        },
        outputs={
            "savings_balance": OutputSpec(
                type=ParamType.DECIMAL,
                description="savings",
                extract=loc("savings cell", (LocatorStrategy.CSS_STRUCTURAL, "td", 0.9)),
                transform=Transform.CURRENCY,
            )
        },
        steps=[
            Step(
                n=1,
                action=ActionType.TYPE,
                target=loc(
                    "member id field",
                    (LocatorStrategy.LABEL_RELATIVE, "Member ID", 0.9),
                    (LocatorStrategy.COORDS, "300,120", 0.2),
                ),
                value="${inputs.member_id}",
            ),
            Step(
                n=2,
                action=ActionType.CLICK,
                target=loc("search", (LocatorStrategy.ROLE_NAME, "button|Search", 0.95)),
                postcondition=Checkpoint(url_pattern=r"/bank/member/"),
            ),
            Step(
                n=3,
                action=ActionType.EXTRACT,
                target=loc("savings cell", (LocatorStrategy.CSS_STRUCTURAL, "td", 0.9)),
            ),
        ],
        outcomes=[
            Outcome(
                code="MEMBER_NOT_FOUND",
                detect=Checkpoint(text_contains="No member matches"),
                description="no such member",
            )
        ],
        success=Checkpoint(text_contains="Current Balance"),
        provenance=Provenance(discovery_run_id="test", model="claude-sonnet-5"),
    )


@pytest.fixture
def redactor() -> Redactor:
    return Redactor(["canary-secret-XYZ"])


@pytest.fixture
def evidence(tmp_path: Path, redactor: Redactor) -> EvidenceWriter:
    return EvidenceWriter(tmp_path, "test", redactor, run_id="r1")
