from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cua.artifact.schema import (
    ActionType,
    Capability,
    Checkpoint,
    Locator,
    LocatorCandidate,
    LocatorStrategy,
    RiskClass,
    Step,
    TenantOverride,
)
from cua.artifact.store import ArtifactStore


def test_round_trip(sample_capability: Capability, tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    p = store.save(sample_capability)
    assert p.name == "lookup_member_balance@1.0.0.json"
    again = store.load(p)
    assert again == sample_capability
    assert json.loads(p.read_text())["schema_version"] == "1.0"


def test_candidates_sorted_by_confidence() -> None:
    loc = Locator(
        description="x",
        candidates=[
            LocatorCandidate(strategy=LocatorStrategy.COORDS, value="1,1", confidence=0.2),
            LocatorCandidate(strategy=LocatorStrategy.TEXT, value="Go", confidence=0.8),
        ],
    )
    assert loc.candidates[0].strategy == LocatorStrategy.TEXT


def test_unknown_input_reference_rejected(sample_capability: Capability) -> None:
    data = sample_capability.model_dump()
    data["steps"][0]["value"] = "${inputs.nope}"
    with pytest.raises(ValidationError, match="unknown input"):
        Capability.model_validate(data)


def test_step_numbering_enforced(sample_capability: Capability) -> None:
    data = sample_capability.model_dump()
    data["steps"][1]["n"] = 5
    with pytest.raises(ValidationError, match="numbered"):
        Capability.model_validate(data)


def test_irreversible_requires_non_idempotent(sample_capability: Capability) -> None:
    data = sample_capability.model_dump()
    data["risk_class"] = RiskClass.IRREVERSIBLE.value
    with pytest.raises(ValidationError, match="idempotent"):
        Capability.model_validate(data)
    data["idempotent"] = False
    assert Capability.model_validate(data).risk_class == RiskClass.IRREVERSIBLE


def test_checkpoint_must_assert_something() -> None:
    with pytest.raises(ValidationError):
        Checkpoint()


def test_tenant_override_applies(sample_capability: Capability) -> None:
    ov = TenantOverride(
        entry_url="http://localhost:7860/bank/login?tenant=b",
        locators={
            1: Locator(
                description="member number",
                candidates=[
                    LocatorCandidate(
                        strategy=LocatorStrategy.LABEL_RELATIVE,
                        value="Member Number",
                        confidence=0.9,
                    )
                ],
            )
        },
    )
    cap = sample_capability.model_copy(update={"tenant_overrides": {"b": ov}})
    b = cap.apply_tenant("b")
    assert b.entry_url.endswith("tenant=b")
    assert b.steps[0].target and b.steps[0].target.candidates[0].value == "Member Number"
    assert cap.apply_tenant(None) == cap


def test_describe_is_human_readable(sample_capability: Capability) -> None:
    d = sample_capability.describe()
    assert "MEMBER_NOT_FOUND" in d and "member_id: string" in d and "savings_balance: decimal" in d


def test_json_schema_exports() -> None:
    s = json.loads(ArtifactStore.json_schema())
    assert "steps" in s["properties"] and "inputs" in s["properties"]


def test_step_requires_target_for_click() -> None:
    with pytest.raises(ValidationError, match="requires a target"):
        Step(n=1, action=ActionType.CLICK)
