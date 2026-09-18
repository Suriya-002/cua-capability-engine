from __future__ import annotations

from pathlib import Path

import pytest

from cua.artifact.schema import ActionType, RiskClass
from cua.policy.engine import Policy, PolicyViolation

POLICY = Path(__file__).parent.parent / "policies" / "default.yaml"


@pytest.fixture
def policy() -> Policy:
    return Policy.load(POLICY)


def test_host_allowlist(policy: Policy) -> None:
    assert policy.check(ActionType.CLICK, "http://localhost:7860/bank/search").allowed
    d = policy.check(ActionType.CLICK, "http://evil.example/bank/search")
    assert not d.allowed and "host" in d.reason


def test_denied_path(policy: Policy) -> None:
    d = policy.check(ActionType.NAVIGATE, "http://localhost:7860/bank/admin")
    assert not d.allowed and "denied" in d.reason


def test_outside_bank_prefix(policy: Policy) -> None:
    assert not policy.check(ActionType.CLICK, "http://localhost:7860/other").allowed


def test_risk_classification(policy: Policy) -> None:
    assert policy.classify(ActionType.CLICK, "Search", "http://localhost/bank/search") == RiskClass.SAFE
    assert (
        policy.classify(ActionType.CLICK, "Open Account", "http://localhost/bank/x") == RiskClass.IRREVERSIBLE
    )
    assert policy.classify(ActionType.CLICK, "Save changes", "http://localhost/bank/x") == RiskClass.RISKY
    # typing into a field named 'transfer' is not itself irreversible
    assert policy.classify(ActionType.TYPE, "transfer", "http://localhost/bank/x") == RiskClass.SAFE


def test_unattended_blocks_irreversible_unless_approved(policy: Policy) -> None:
    url = "http://localhost:7860/bank/member/10023/subaccount/new"
    d = policy.check(ActionType.CLICK, url, "Open Account", unattended=True)
    assert not d.allowed and d.risk == RiskClass.IRREVERSIBLE
    d2 = policy.check(ActionType.CLICK, url, "Open Account", unattended=True, approved_irreversible=True)
    assert d2.allowed


def test_enforce_raises(policy: Policy) -> None:
    with pytest.raises(PolicyViolation):
        policy.enforce(ActionType.CLICK, "http://localhost:7860/bank/wire/new", "Send")
