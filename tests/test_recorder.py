"""Recorder logic without a browser: parameterisation, secrets, frame-aware postconditions, templating."""

from __future__ import annotations

from cua.agent.loop import DiscoveryOutcome, RecordedAction
from cua.agent.recorder import DEFAULT_INTERRUPTS, DEFAULT_OUTCOMES, Recorder, savings_locator
from cua.artifact.schema import (
    ActionType,
    AppRef,
    Checkpoint,
    LocatorCandidate,
    LocatorStrategy,
    ParamSpec,
    ParamType,
    Sensitivity,
)
from cua.surface.base import ProbeResult

LOGIN = "http://localhost:7860/bank/login"
FRAMES = "http://localhost:7860/bank/frames"
NAV = "http://localhost:7860/bank/nav"
SEARCH = "http://localhost:7860/bank/search"
MEMBER = "http://localhost:7860/bank/member/10023"


def probe(desc: str, role: str, text: str, frame: str | None = None) -> ProbeResult:
    return ProbeResult(
        candidates=[
            LocatorCandidate(
                strategy=LocatorStrategy.ROLE_NAME, value=f"{role}|{text}", confidence=0.95, frame=frame
            ),
            LocatorCandidate(strategy=LocatorStrategy.COORDS, value="1,1", confidence=0.2, frame=frame),
        ],
        description=desc,
        text=text,
        tag="input",  # legacy markup: text fields AND submit buttons are both <input>; role decides
        frame=frame,
    )


def act(
    name: str, probe_: ProbeResult | None, before: list[str], after: list[str], text: str | None = None
) -> RecordedAction:
    return RecordedAction(
        turn=1,
        name=name,
        input={"text": text} if text else {},
        url_before=before[0],
        url_after=after[0],
        urls_before=before,
        urls_after=after,
        probe=probe_,
        rationale="Sign in, then search member 10023",
    )


def _outcome() -> DiscoveryOutcome:
    a = [
        act("left_click", probe("textbox 'User'", "textbox", "User"), [LOGIN], [LOGIN]),
        act("type", None, [LOGIN], [LOGIN], text="svc_automation_7f3"),
        act("left_click", probe("textbox 'Password'", "textbox", "Password"), [LOGIN], [LOGIN]),
        act("type", None, [LOGIN], [LOGIN], text="Lcu-demo-9x2Q"),
        act("left_click", probe("button 'Sign In'", "button", "Sign In"), [LOGIN], [FRAMES, NAV, SEARCH]),
        act("screenshot", None, [FRAMES, NAV, SEARCH], [FRAMES, NAV, SEARCH]),
        act(
            "left_click",
            probe("textbox 'Member ID'", "textbox", "Member ID", "main"),
            [FRAMES, NAV],
            [FRAMES, NAV, SEARCH],
        ),
        act("type", None, [FRAMES, NAV, SEARCH], [FRAMES, NAV, SEARCH], text="10023"),
        act(
            "left_click",
            probe("button 'Search'", "button", "Search", "main"),
            [FRAMES, NAV, SEARCH],
            [FRAMES, NAV, MEMBER],
        ),
    ]
    return DiscoveryOutcome(
        status="success", summary="done", outputs={"savings_balance": "$10,059.06"}, actions=a
    )


def test_recorder_builds_contract() -> None:
    rec = Recorder(
        cap_id="lookup_member_balance",
        name="Lookup",
        version="1.0.0",
        app=AppRef(vendor="LegacyCU", product="Core Servicing", version_fingerprint="abc"),
        run_id="r1",
        model="claude-sonnet-5",
    )
    params = {"member_id": "10023"}
    cap = rec.build(
        _outcome(),
        goal="Look up member 10023 and read their savings balance",
        entry_url=LOGIN,
        params=params,
        param_specs={
            "member_id": ParamSpec(
                type=ParamType.STRING, description="member id", sensitivity=Sensitivity.PII
            )
        },
        output_specs={"savings_balance": (ParamType.DECIMAL, "savings")},
        output_locators={"savings_balance": savings_locator()},
        secrets={"username": "svc_automation_7f3", "password": "Lcu-demo-9x2Q"},
        interrupts=DEFAULT_INTERRUPTS,
        outcomes=DEFAULT_OUTCOMES,
        success=Checkpoint(text_contains="Current Balance", url_pattern=r"/bank/member/"),
    )
    # no secrets or PII anywhere in the serialised artifact
    dumped = cap.model_dump_json()
    for leak in ("svc_automation_7f3", "Lcu-demo-9x2Q", "10023", "$10,059.06"):
        assert leak not in dumped, leak
    assert cap.goal == "Look up member {member_id} and read their savings balance"
    assert set(cap.secrets) == {"username", "password"}
    # click+type merged into TYPE steps carrying the click's locator
    kinds = [(s.n, s.action) for s in cap.steps]
    assert kinds == [
        (1, ActionType.TYPE),
        (2, ActionType.TYPE),
        (3, ActionType.CLICK),
        (4, ActionType.TYPE),
        (5, ActionType.CLICK),
        (6, ActionType.EXTRACT),
    ]
    assert cap.steps[0].value == "${secrets.username}" and cap.steps[1].value == "${secrets.password}"
    assert cap.steps[3].value == "${inputs.member_id}"
    # postconditions derived from frame-level URL deltas
    assert cap.steps[2].postcondition and cap.steps[2].postcondition.url_pattern == r"/bank/search"
    assert (
        cap.steps[4].postcondition
        and cap.steps[4].postcondition.url_pattern == r"/bank/member/(?P<member_id>[^/?&]+)"
    )
    assert cap.steps[3].postcondition is None
    # extraction locator is label-anchored, not a literal value
    assert all(
        c.strategy == LocatorStrategy.LABEL_RELATIVE
        for c in cap.outputs["savings_balance"].extract.candidates
    )


def test_undeclared_secret_ref_rejected() -> None:
    import pytest
    from pydantic import ValidationError

    from cua.artifact.schema import Capability, Locator, Provenance, Step

    with pytest.raises(ValidationError, match="undeclared secret"):
        Capability(
            id="x",
            version="1.0.0",
            name="x",
            goal="g",
            app=AppRef(vendor="v", product="p", version_fingerprint="f"),
            entry_url="http://localhost/",
            steps=[
                Step(
                    n=1,
                    action=ActionType.TYPE,
                    value="${secrets.nope}",
                    target=Locator(
                        description="f",
                        candidates=[
                            LocatorCandidate(strategy=LocatorStrategy.TEXT, value="x", confidence=0.5)
                        ],
                    ),
                )
            ],
            success=Checkpoint(text_contains="x"),
            provenance=Provenance(discovery_run_id="r", model="m"),
        )
