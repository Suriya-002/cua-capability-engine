"""Replay control flow against a scripted fake Surface: interrupts after a step, restart on relogin,
business outcome on postcondition failure, escalation + human completion. No browser, no LLM."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from cua.artifact.schema import (
    ActionType,
    ApprovalState,
    Capability,
    Checkpoint,
    Interrupt,
    InterruptHandler,
    Locator,
    LocatorCandidate,
    LocatorStrategy,
)
from cua.escalation.session import SessionController
from cua.evidence.writer import EvidenceWriter
from cua.policy.engine import Policy
from cua.policy.redaction import Redactor
from cua.replay.engine import ReplayEngine
from cua.replay.results import ResultKind
from cua.surface.base import Action, ActResult, Observation, ProbeResult, Resolution

POLICY = Path(__file__).parent.parent / "policies" / "default.yaml"


class FakeSurface:
    """State machine: a list of (urls, text) screens; actions advance through a scripted transition table."""

    def __init__(
        self, screens: dict[str, tuple[list[str], str]], transitions: dict[tuple[str, str], str], start: str
    ) -> None:
        self.screens, self.transitions, self.state = screens, transitions, start
        self.log: list[str] = []

    async def start(self, entry_url: str | None = None) -> None: ...
    async def stop(self) -> None: ...
    async def observe(self, *, with_aria: bool = False) -> Observation:
        return Observation(png=b"png", scale=1.0, width=1, height=1, url=self.screens[self.state][0][0])

    async def act(self, action: Action) -> ActResult:
        key = (self.state, action.name if action.handle is None else str(action.handle))
        self.log.append(f"{self.state}:{key[1]}")
        if action.name == "extract":
            return ActResult(True, extracted="$1,234.50")
        self.state = self.transitions.get(key, self.state)
        return ActResult(True)

    async def probe(self, x: int, y: int) -> ProbeResult:
        return ProbeResult()

    async def resolve(self, locator: Locator, timeout_ms: int) -> Resolution | None:
        c = locator.candidates[0]
        if c.value == "cell" and self.state != "member":
            return None  # the Savings cell only exists on the member detail screen
        return Resolution(handle=c.value, candidate=c, candidate_index=0, text=c.value)

    async def read_text(self) -> str:
        return self.screens[self.state][1]

    async def current_url(self) -> str:
        return self.screens[self.state][0][0]

    async def all_urls(self) -> list[str]:
        return self.screens[self.state][0]

    async def dom_snapshot(self) -> str | None:
        return None

    human_queue: list[dict[str, Any]] = []

    async def drain_human_events(self) -> list[dict[str, Any]]:
        q, self.human_queue = self.human_queue, []
        return q

    async def fingerprint(self) -> str:
        return "fp"

    def install_human_recorder(self, callback: Any) -> None: ...


def loc(v: str) -> Locator:
    return Locator(
        description=v, candidates=[LocatorCandidate(strategy=LocatorStrategy.TEXT, value=v, confidence=0.9)]
    )


def cap_with(interrupts: list[Interrupt]) -> Capability:
    from cua.artifact.schema import (
        AppRef,
        Outcome,
        OutputSpec,
        ParamSpec,
        ParamType,
        Provenance,
        Step,
        Transform,
    )

    return Capability(
        id="c",
        version="1.0.0",
        name="c",
        goal="g",
        app=AppRef(vendor="v", product="p", version_fingerprint="fp"),
        entry_url="http://localhost:7860/bank/login",
        approval=ApprovalState.APPROVED,
        inputs={"member_id": ParamSpec(type=ParamType.STRING, description="id")},
        outputs={
            "savings_balance": OutputSpec(
                type=ParamType.DECIMAL, description="s", extract=loc("cell"), transform=Transform.CURRENCY
            )
        },
        steps=[
            Step(
                n=1,
                action=ActionType.CLICK,
                target=loc("Search"),
                postcondition=Checkpoint(url_pattern=r"/bank/member/", timeout_ms=1500),
            ),
            Step(n=2, action=ActionType.EXTRACT, target=loc("cell")),
        ],
        interrupts=interrupts,
        outcomes=[
            Outcome(
                code="MEMBER_NOT_FOUND", detect=Checkpoint(text_contains="No member matches"), description="d"
            )
        ],
        success=Checkpoint(text_contains="Current Balance", timeout_ms=1500),
        provenance=Provenance(discovery_run_id="r", model="m"),
    )


SCREENS = {
    "search": (["http://localhost:7860/bank/search"], "Member Search"),
    "notice": (["http://localhost:7860/bank/notice"], "System Notice Continue"),
    "member": (["http://localhost:7860/bank/member/10023"], "Current Balance Savings"),
    "notfound": (["http://localhost:7860/bank/member/99999"], "No member matches"),
    "expired": (["http://localhost:7860/bank/login"], "Session expired"),
    "error": (["http://localhost:7860/bank/member/10023"], "Internal Server Error"),
}
NOTICE = Interrupt(
    id="notice_interstitial",
    detect=Checkpoint(text_contains="System Notice"),
    handler=InterruptHandler.DISMISS,
    action_target=loc("Continue"),
)
EXPIRED = Interrupt(
    id="session_expired",
    detect=Checkpoint(text_contains="Session expired"),
    handler=InterruptHandler.RELOGIN,
    max_attempts=1,
)


def engine(
    surface: FakeSurface, tmp_path: Path, session: SessionController | None = None
) -> tuple[ReplayEngine, EvidenceWriter]:
    ev = EvidenceWriter(tmp_path, "t", Redactor())
    return ReplayEngine(
        surface, Policy.load(POLICY), ev, Redactor(), session=session, unattended=session is None
    ), ev


def test_interrupt_after_step_is_recovered(tmp_path: Path) -> None:
    s = FakeSurface(SCREENS, {("search", "Search"): "notice", ("notice", "Continue"): "member"}, "search")
    e, _ = engine(s, tmp_path)
    r = asyncio.run(e.run(cap_with([NOTICE]), {"member_id": "10023"}))
    assert r.kind == ResultKind.SUCCESS and r.outputs == {"savings_balance": "1234.50"}
    assert [x.interrupt_id for x in r.recoveries] == ["notice_interstitial"] and r.llm_calls == 0


def test_postcondition_failure_becomes_business_outcome(tmp_path: Path) -> None:
    s = FakeSurface(SCREENS, {("search", "Search"): "notfound"}, "search")
    e, _ = engine(s, tmp_path)
    r = asyncio.run(e.run(cap_with([]), {"member_id": "99999"}))
    assert r.kind == ResultKind.BUSINESS_OUTCOME and r.outcome_code == "MEMBER_NOT_FOUND"


def test_session_expiry_restarts_flow_once(tmp_path: Path) -> None:
    # first Search -> expired; navigate (relogin) -> search; second Search -> member
    s = FakeSurface(SCREENS, {("search", "Search"): "expired", ("expired", "navigate"): "search"}, "search")
    seen = {"n": 0}
    orig = s.act

    async def act(a: Action) -> ActResult:
        if a.name != "navigate" and str(a.handle) == "Search":
            seen["n"] += 1
            if seen["n"] == 2:
                s.transitions[("search", "Search")] = "member"
        return await orig(a)

    s.act = act  # type: ignore[method-assign]
    e, ev = engine(s, tmp_path)
    r = asyncio.run(e.run(cap_with([EXPIRED]), {"member_id": "10023"}))
    assert r.kind == ResultKind.SUCCESS and seen["n"] == 2
    assert "restart" in (ev.dir / "events.jsonl").read_text()


def test_hard_failure_unattended_has_debuggable_detail(tmp_path: Path) -> None:
    s = FakeSurface(SCREENS, {("search", "Search"): "error"}, "search")
    e, _ = engine(s, tmp_path)
    r = asyncio.run(e.run(cap_with([NOTICE]), {"member_id": "10023"}))
    # url matched /bank/member/ so step 1 passes; the extract step cannot find its target on the error page.
    # The failure is attributed to THAT step, with what was looked for and the evidence captured there.
    assert r.kind == ResultKind.FAILURE and r.failure and r.failure.category == "locator_not_found"
    assert r.failure.step_n == 2 and "cell" in r.failure.observed and r.failure.evidence


async def _attended_run(tmp_path: Path) -> tuple[Any, list[str]]:
    s = FakeSurface(SCREENS, {("search", "Search"): "search"}, "search")  # click does nothing: stuck
    ev = EvidenceWriter(tmp_path, "t", Redactor())
    session = SessionController(ev, "c@1.0.0")
    e = ReplayEngine(
        s, Policy.load(POLICY), ev, Redactor(), session=session, unattended=False, escalation_wait_s=5
    )

    async def operator() -> None:
        while session.request is None:
            await asyncio.sleep(0.05)
        session.acquire("tester")
        s.human_queue.append(
            {"kind": "click", "tag": "input", "text": "Search", "url": "http://x/bank/search", "ts": 1}
        )
        s.state = "member"  # the human completes the step by hand
        session.release("did the search manually")

    task = asyncio.create_task(operator())
    r = await e.run(cap_with([]), {"member_id": "10023"})
    await task
    return r, (ev.dir / "events.jsonl").read_text().splitlines()


def test_escalation_human_completes_step(tmp_path: Path) -> None:
    r, lines = asyncio.run(_attended_run(tmp_path))
    assert r.kind == ResultKind.SUCCESS and r.llm_calls == 0
    types = [__import__("json").loads(ln)["type"] for ln in lines]
    assert "intervention_request" in types and "human_step" in types and "step_completed_by_human" in types
    assert types.count("control_transition") == 5  # paused, human, resuming, automation, completed


async def _attended_run_wrong_state(tmp_path: Path) -> Any:
    # the member screen 500s (url matches, so step 1 passes); step 2 cannot find the Savings cell -> escalate.
    # after the hand-back the app is healthy again, but the human left the screen on the search form
    s = FakeSurface(SCREENS, {("search", "Search"): "error"}, "search")
    ev = EvidenceWriter(tmp_path, "t", Redactor())
    session = SessionController(ev, "c@1.0.0")
    e = ReplayEngine(
        s, Policy.load(POLICY), ev, Redactor(), session=session, unattended=False, escalation_wait_s=5
    )

    async def operator() -> None:
        while session.request is None:
            await asyncio.sleep(0.05)
        session.acquire("tester")
        s.transitions[("search", "Search")] = "member"  # the outage is over
        s.state = "search"  # but the human handed back from the search screen
        session.release("retried, app is back")

    task = asyncio.create_task(operator())
    r = await e.run(cap_with([]), {"member_id": "10023"})
    await task
    assert any(x.endswith(":navigate") for x in s.log), s.log
    return r, (ev.dir / "events.jsonl").read_text()


def test_handoff_in_unverifiable_state_restarts_flow(tmp_path: Path) -> None:
    r, events = asyncio.run(_attended_run_wrong_state(tmp_path))
    assert r.kind == ResultKind.SUCCESS and r.llm_calls == 0
    assert "restart_after_handoff" in events
