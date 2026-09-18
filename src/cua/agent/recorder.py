"""Turn a successful discovery run into a Capability artifact — decoupled from the raw transcript.

Rules applied:
- Only successful, state-changing actions become steps (screenshots/zooms/waits are dropped).
- A typed literal equal to a provided parameter becomes ${inputs.<name>}; one equal to a supplied
  credential becomes ${secrets.<name>}. Neither literal is ever written to the artifact.
- Each click/type gets the ranked locator candidates probed at record time.
- click-then-type on the same control collapses into one TYPE step with the click's locator.
- Postconditions come from the *state delta* the action caused: a URL that appears in any frame
  after the action and was not there before, canonicalised (param values -> named groups). This
  works for framesets, where the page URL never changes and only a frame navigates.
- The last state-changing step inherits the success checkpoint so a failed final click is
  attributed to that click, not to "success check".
- The goal is templated: param values are replaced by {name} so no PII lands in the artifact.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from cua.agent.loop import DiscoveryOutcome, RecordedAction
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
    SecretSpec,
    Sensitivity,
    Step,
    Transform,
)

_STATE_CHANGING = {
    "left_click": ActionType.CLICK,
    "double_click": ActionType.CLICK,
    "type": ActionType.TYPE,
    "key": ActionType.KEY,
    "scroll": ActionType.SCROLL,
}


def _canon_url(url: str, params: dict[str, str]) -> str:
    """Regex for the path (+query) of a URL with parameter values turned into named groups."""
    u = urlparse(url)
    path = u.path + (f"?{u.query}" if u.query else "")
    pat = re.escape(path)
    for k, v in sorted(params.items(), key=lambda kv: -len(kv[1])):
        if v:
            pat = pat.replace(re.escape(v), f"(?P<{k}>[^/?&]+)")
    return pat


def _template(text: str, params: dict[str, str]) -> str:
    for k, v in sorted(params.items(), key=lambda kv: -len(kv[1])):
        if v:
            text = text.replace(v, f"{{{k}}}")
    return text


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
        secrets: dict[str, str] | None = None,
        risk_class: RiskClass = RiskClass.SAFE,
        interrupts: list[Interrupt] | None = None,
        outcomes: list[Outcome] | None = None,
        success: Checkpoint | None = None,
    ) -> Capability:
        secrets = secrets or {}
        actions = [a for a in outcome.actions if a.ok and a.name in _STATE_CHANGING]
        steps: list[Step] = []
        used_secrets: set[str] = set()
        last_post: str | None = None
        for i, a in enumerate(actions):
            atype = _STATE_CHANGING[a.name]
            target = a.probe.to_locator() if a.probe else None
            value: str | None = a.input.get("text")
            if atype == ActionType.TYPE and value:
                value, sec = self._parameterise(value, params, secrets)
                if sec:
                    used_secrets.add(sec)
                if target is None:
                    prev = next(
                        (s for s in reversed(steps) if s.action == ActionType.CLICK and s.target), None
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
            post = self._postcondition(a, actions[i + 1] if i + 1 < len(actions) else None, params)
            if post and post.url_pattern == last_post:
                post = None  # same state as the previous step: nothing changed, nothing to assert
            elif post:
                last_post = post.url_pattern
            steps.append(
                Step(
                    n=len(steps) + 1,
                    action=atype,
                    target=target,
                    value=value if atype in {ActionType.TYPE, ActionType.KEY} else None,
                    postcondition=self._postcondition(
                        a, actions[i + 1] if i + 1 < len(actions) else None, params
                    ),
                    risk=RiskClass(a.risk),
                    rationale=_template((a.rationale or "")[:200], params) or None,
                )
            )
        steps = self._merge_click_type(steps)

        success_cp = success or Checkpoint(text_contains="Current Balance")
        # the last state-changing step owns the success condition: a failure there is *its* failure
        if steps and steps[-1].postcondition is None:
            steps[-1] = steps[-1].model_copy(update={"postcondition": success_cp})

        outputs: dict[str, OutputSpec] = {}
        for oname, (otype, desc) in output_specs.items():
            loc = output_locators[oname]
            steps.append(
                Step(n=len(steps) + 1, action=ActionType.EXTRACT, target=loc, rationale=f"extract {oname}")
            )
            outputs[oname] = OutputSpec(
                type=otype,
                description=desc,
                extract=loc,
                transform=Transform.CURRENCY if otype == ParamType.DECIMAL else Transform.TRIM,
                sensitivity=Sensitivity.PII if "balance" in oname else Sensitivity.NONE,
            )

        return Capability(
            id=self.cap_id,
            version=self.version,
            name=self.name,
            goal=_template(goal, params),
            app=self.app,
            entry_url=entry_url,
            risk_class=risk_class,
            inputs=param_specs,
            secrets={
                k: SecretSpec(description=f"{k} used to sign in to the application")
                for k in sorted(used_secrets)
            },
            outputs=outputs,
            steps=steps,
            interrupts=interrupts or [],
            outcomes=outcomes or [],
            success=success_cp,
            provenance=Provenance(discovery_run_id=self.run_id, model=self.model),
            idempotent=risk_class != RiskClass.IRREVERSIBLE,
        )

    # --- helpers --------------------------------------------------------------------------------
    @staticmethod
    def _parameterise(value: str, params: dict[str, str], secrets: dict[str, str]) -> tuple[str, str | None]:
        for k, v in sorted(secrets.items(), key=lambda kv: -len(kv[1])):
            if v and value == v:
                return f"${{secrets.{k}}}", k
        for k, v in sorted(params.items(), key=lambda kv: -len(kv[1])):
            if v and value == v:
                return f"${{inputs.{k}}}", None
        return value, None

    @staticmethod
    def _postcondition(
        a: RecordedAction, nxt: RecordedAction | None, params: dict[str, str]
    ) -> Checkpoint | None:
        """New URL (in any frame) after the action = the state it produced."""
        before = set(a.urls_before or [a.url_before])
        after = a.urls_after or ([a.url_after] if a.url_after else [])
        # the next action's starting state is the most settled view of this action's result
        if nxt and nxt.urls_before:
            after = list(nxt.urls_before)
        new = [u for u in after if u and u not in before and not u.startswith("about:")]
        if not new:
            return None
        return Checkpoint(url_pattern=_canon_url(new[-1], params), timeout_ms=10_000)

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
                and merged[-1].postcondition is None
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
                    strategy=LocatorStrategy.ROLE_NAME, value="button|Continue", confidence=0.95, frame="main"
                ),
                LocatorCandidate(
                    strategy=LocatorStrategy.TEXT, value="Continue", confidence=0.7, frame="main"
                ),
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


def savings_locator() -> Locator:
    """Extraction locator for the Savings balance cell. No literal values: label-anchored xpaths only."""
    return Locator(
        description="Savings 'Current Balance' cell",
        candidates=[
            LocatorCandidate(
                strategy=LocatorStrategy.LABEL_RELATIVE,
                value="xpath=//tr[td[normalize-space()='Savings']]/td[2]",
                confidence=0.9,
                frame="main",
                note="row labelled Savings, second column",
            ),
            LocatorCandidate(
                strategy=LocatorStrategy.LABEL_RELATIVE,
                value="xpath=//td[normalize-space()='Savings']/following-sibling::td[1]",
                confidence=0.8,
                frame="main",
            ),
        ],
    )
