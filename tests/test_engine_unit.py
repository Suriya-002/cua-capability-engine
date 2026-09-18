"""Engine logic that doesn't need a browser: substitution, transforms, idempotency store, input validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.artifact.schema import Capability
from cua.replay.engine import IdempotencyStore, ReplayEngine


def _engine(secrets: dict[str, str] | None = None) -> ReplayEngine:
    from unittest.mock import MagicMock

    from cua.policy.redaction import Redactor

    return ReplayEngine(MagicMock(), MagicMock(), MagicMock(), Redactor(), secrets=secrets)


def test_substitute_params_and_secrets() -> None:
    e = _engine({"password": "s3cret"})
    assert e._substitute("${inputs.member_id}", {"member_id": "10023"}) == "10023"
    assert e._substitute("id=${inputs.a}/${inputs.b}", {"a": "1", "b": "2"}) == "id=1/2"
    assert e._substitute("${secrets.password}", {}) == "s3cret"
    assert e._substitute("${secrets.missing}", {}) == ""
    assert e._substitute(None, {}) is None
    # engine registers secrets with the redactor so they never reach evidence
    assert e.redactor.redact("pw=s3cret") == "pw=[REDACTED]"


def test_output_transforms(sample_capability: Capability) -> None:
    out = ReplayEngine._transform_outputs(sample_capability, {"savings_balance": "$1,234.56", "other": " x "})
    assert out == {"savings_balance": "1234.56", "other": " x "}


def test_input_validation(sample_capability: Capability) -> None:
    with pytest.raises(ValueError, match="missing"):
        ReplayEngine._validate_inputs(sample_capability, {})
    with pytest.raises(ValueError, match="match"):
        ReplayEngine._validate_inputs(sample_capability, {"member_id": "abc"})
    ReplayEngine._validate_inputs(sample_capability, {"member_id": "10023"})


def test_idempotency_store(tmp_path: Path) -> None:
    s = IdempotencyStore(tmp_path / "idem.json")
    assert s.get("c@1.0.0", "k1") is None
    s.put("c@1.0.0", "k1", {"run_id": "r1", "outputs": {"conf": "SA-1"}})
    assert IdempotencyStore(tmp_path / "idem.json").get("c@1.0.0", "k1")["outputs"] == {"conf": "SA-1"}
