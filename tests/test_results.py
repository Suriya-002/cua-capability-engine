from __future__ import annotations

from cua.replay.results import FailureDetail, ReplayResult, ResultKind


def test_three_way_contract_is_distinct() -> None:
    ok = ReplayResult(
        kind=ResultKind.SUCCESS,
        capability_ref="c@1.0.0",
        run_id="r",
        outputs={"savings_balance": "123.45"},
    )
    bo = ReplayResult(
        kind=ResultKind.BUSINESS_OUTCOME,
        capability_ref="c@1.0.0",
        run_id="r",
        outcome_code="MEMBER_NOT_FOUND",
    )
    fl = ReplayResult(
        kind=ResultKind.FAILURE,
        capability_ref="c@1.0.0",
        run_id="r",
        failure=FailureDetail(
            step_n=2,
            action="click",
            expected="url ~ /member/",
            observed="url=/search",
            category="checkpoint_failed",
            evidence=["shots/002-s02-failure.png"],
        ),
    )
    assert ok.ok() and not bo.ok() and not fl.ok()
    assert bo.failure is None and fl.outcome_code is None
    assert all(r.llm_calls == 0 for r in (ok, bo, fl))
