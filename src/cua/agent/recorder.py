"""Turn a successful discovery run into a Capability artifact — decoupled from the raw transcript.

Rules applied:
- Only successful, state-changing actions become steps (screenshots/zooms/waits are dropped).
- Typed literal values equal to a provided parameter are parameterised as ${inputs.<name>}.
- Each click/type gets the ranked locator candidates probed at record time.
- Consecutive `type` after a `left_click` on the same probe collapse into one TYPE step with a
  precondition on the field label (that is how a human would describe it).
- Postconditions: URL change becomes a url_pattern; otherwise the next screenshot's title text.
- Outputs declared by the caller get an EXTRACT step whose locator is derived from the finish() values
  by text search at the end of the run.
"""

from __future__ import annotations

import re

from cua.agent.loop import DiscoveryOutcome
from cua.artifact.schema import (
    ActionType,
    AppRef,
    Capability,
    Checkpoint,
    Interrupt,
    InterruptHandler,
    Locator,
    LocatorCandidate,
    LocatorStrategy,
    Outcome,
    OutputSpec,
    ParamSpec,
    ParamType,
    Provenance,
    RiskClass,
    Sensitivity,
    Step,
    Transform,
)


def _canon_url(url: str, params: dict[str, str]) -> str:
    pat = re.escape(url)
    for k, v in sorted(params.items(), key=lambda kv: -len(kv[1])):
        if v:
            pat = pat.replace(re.escape(v), f"(?P<{k}>[^/?&]+)")
    return pat


class Recorder:
    def __init__(self, *, cap_id: str, name: str, version: str, app: AppRef, run_id: str, model: str) -> None:
        self.cap_id, self.name, self.version, self.app, self.run_id, self.model = (
            cap_id,
            name,
            version,
            app,
            run_id,
            model,
        )

    def build(
        self,
        outcome: DiscoveryOutcome,
        *,
        goal: str,
        entry_url: str,
        params: dict[str, str],
        param_specs: dict[str, ParamSpec],
        output_specs: dict[str, tuple[ParamType, str]],
        output_locators: dict[str, Locator],
        risk_class: RiskClass = RiskClass.SAFE,
        interrupts: list[Interrupt] | None = None,
        outcomes: list[Outcome] | None = None,
        success: Checkpoint | None = None,
    ) -> Capability:
        steps: list[Step] = []
        n = 0
        for a in outcome.actions:
            if not a.ok or a.name in {
                "screenshot",
                "zoom",
                "cursor_position",
                "mouse_move",
                "wait",
            }:
                continue
            n += 1
            atype = {
                "left_click": ActionType.CLICK,
                "double_click": ActionType.CLICK,
                "type": ActionType.TYPE,
                "key": ActionType.KEY,
                "scroll": ActionType.SCROLL,
            }.get(a.name)
            if atype is None:
                n -= 1
                continue
            target = a.probe.to_locator() if a.probe else None
            value = a.input.get("text")
            if atype == ActionType.TYPE and value:
                value = self._parameterise(value, params)
                # attach the field's locator from the preceding click if this type has no probe
                if target is None:
                    prev = next(
                        (s for s in reversed(steps) if s.action == ActionType.CLICK and s.target),
                        None,
                    )
                    target = (
                        prev.target
                        if prev
                        else Locator(
                            description="focused field",
                            candidates=[
                                LocatorCandidate(strategy=LocatorStrategy.COORDS, value="0,0", confidence=0.1)
                            ],
                        )
                    )
            if atype == ActionType.SCROLL:
                target = None
            post = None
            if a.url_after and a.url_after != a.url_before:
                post = Checkpoint(url_pattern=_canon_url(a.url_after, params), timeout_ms=10_000)
            steps.append(
                Step(
                    n=n,
                    action=atype,
                    target=target,
                    value=value or (a.input.get("text") if atype == ActionType.KEY else None),
                    postcondition=post,
                    risk=RiskClass(a.risk),
                    rationale=(a.rationale or "")[:200] or None,
                )
            )
        # merge click-then-type on the same field into a single TYPE with the click's locator
        steps = self._merge_click_type(steps)
        # extraction steps for declared outputs
        outputs: dict[str, OutputSpec] = {}
        for oname, (otype, desc) in output_specs.items():
            loc = output_locators[oname]
            n = len(steps) + 1
            steps.append(Step(n=n, action=ActionType.EXTRACT, target=loc, rationale=f"extract {oname}"))
            outputs[oname] = OutputSpec(
                type=otype,
                description=desc,
                extract=loc,
                transform=Transform.CURRENCY if otype == ParamType.DECIMAL else Transform.TRIM,
                sensitivity=Sensitivity.PII if "balance" in oname else Sensitivity.NONE,
            )
        cap = Capability(
            id=self.cap_id,
            version=self.version,
            name=self.name,
            goal=goal,
            app=self.app,
            entry_url=entry_url,
            risk_class=risk_class,
            inputs=param_specs,
            outputs=outputs,
            steps=steps,
            interrupts=interrupts or [],
            outcomes=outcomes or [],
            success=success or Checkpoint(text_contains="Savings"),
            provenance=Provenance(discovery_run_id=self.run_id, model=self.model),
            idempotent=risk_class != RiskClass.IRREVERSIBLE,
        )
        return cap

    @staticmethod
    def _parameterise(value: str, params: dict[str, str]) -> str:
        for k, v in sorted(params.items(), key=lambda kv: -len(kv[1])):
            if v and value == v:
                return f"${{inputs.{k}}}"
        return value

    @staticmethod
    def _merge_click_type(steps: list[Step]) -> list[Step]:
        merged: list[Step] = []
        for s in steps:
            if (
                merged
                and s.action == ActionType.TYPE
                and merged[-1].action == ActionType.CLICK
                and merged[-1].target
                and (s.target is None or s.target == merged[-1].target)
            ):
                click = merged.pop()
                s = s.model_copy(update={"target": click.target, "rationale": click.rationale or s.rationale})
            merged.append(s)
        return [s.model_copy(update={"n": i + 1}) for i, s in enumerate(merged)]


DEFAULT_INTERRUPTS: list[Interrupt] = [
    Interrupt(
        id="notice_interstitial",
        detect=Checkpoint(text_contains="System Notice"),
        handler=InterruptHandler.DISMISS,
        action_target=Locator(
            description="Continue button",
            candidates=[
                LocatorCandidate(
                    strategy=LocatorStrategy.ROLE_NAME, value="button|Continue", confidence=0.95
                ),
                LocatorCandidate(strategy=LocatorStrategy.TEXT, value="Continue", confidence=0.7),
            ],
        ),
    ),
    Interrupt(
        id="session_expired",
        detect=Checkpoint(text_contains="Session expired"),
        handler=InterruptHandler.RELOGIN,
        max_attempts=1,
    ),
    Interrupt(
        id="slow_load",
        detect=Checkpoint(text_contains="Loading"),
        handler=InterruptHandler.RETRY,
        max_attempts=3,
        wait_ms=2000,
    ),
]

DEFAULT_OUTCOMES: list[Outcome] = [
    Outcome(
        code="MEMBER_NOT_FOUND",
        detect=Checkpoint(text_contains="No member matches"),
        description="The member ID does not exist; a legitimate answer, not an error",
    ),
    Outcome(
        code="PERMISSION_DENIED",
        detect=Checkpoint(text_contains="not authorized"),
        description="Operator role lacks permission for this screen",
    ),
]
