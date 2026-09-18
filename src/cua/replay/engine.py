"""Deterministic replay. No model in the loop — `llm_calls` is asserted 0 in tests.

Per step:
  0. If not AUTOMATION (human holds the session) -> wait.
  1. Run interrupt detectors (interstitial, session expiry, slow load). Handle; count as recovery.
  2. Run outcome detectors -> return BUSINESS_OUTCOME if a terminal outcome is visible.
  3. Verify precondition (if any).
  4. Resolve locator via ranked candidates. Non-primary win => drift proposal.
  5. Policy gate (unattended). Irreversible steps need caller approval + approved artifact, else escalate.
  6. Act. 7. Verify postcondition -> failure is attributed to THIS step with expected/observed/evidence.
After steps: verify success checkpoint; transform and return outputs.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from cua.artifact.schema import (
    ActionType,
    ApprovalState,
    Capability,
    Checkpoint,
    Interrupt,
    InterruptHandler,
    ParamType,
    RiskClass,
    Step,
    Transform,
)
from cua.escalation.session import SessionController
from cua.evidence.writer import EvidenceWriter
from cua.policy.engine import Policy
from cua.policy.redaction import Redactor
from cua.replay.results import DriftProposal, FailureDetail, RecoveryEvent, ReplayResult, ResultKind
from cua.surface.base import Action, Surface

PARAM_REF = re.compile(r"\$\{inputs\.([a-zA-Z_][a-zA-Z0-9_]*)\}")


class IdempotencyStore:
    """Tiny file-backed store. Production: a table keyed by (capability_ref, key) with TTL."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, Any]] = json.loads(path.read_text()) if path.exists() else {}

    def get(self, cap_ref: str, key: str) -> dict[str, Any] | None:
        return self._data.get(f"{cap_ref}:{key}")

    def put(self, cap_ref: str, key: str, result: dict[str, Any]) -> None:
        self._data[f"{cap_ref}:{key}"] = result
        self.path.write_text(json.dumps(self._data, indent=2, default=str))


class ReplayEngine:
    def __init__(
        self,
        surface: Surface,
        policy: Policy,
        evidence: EvidenceWriter,
        redactor: Redactor,
        *,
        session: SessionController | None = None,
        idempotency: IdempotencyStore | None = None,
        unattended: bool = True,
        escalation_wait_s: float = 600,
        relogin: Any = None,  # async callable(surface) -> None
    ) -> None:
        self.s, self.policy, self.ev, self.redactor = surface, policy, evidence, redactor
        self.session = session
        self.idem = idempotency
        self.unattended = unattended
        self.escalation_wait_s = escalation_wait_s
        self.relogin = relogin

    # ------------------------------------------------------------------------------ public
    async def run(
        self,
        cap: Capability,
        inputs: dict[str, str],
        *,
        tenant: str | None = None,
        idempotency_key: str | None = None,
        approved_irreversible: bool = False,
        dry_run: bool = False,
    ) -> ReplayResult:
        t0 = time.monotonic()
        cap = cap.apply_tenant(tenant)
        res = ReplayResult(kind=ResultKind.FAILURE, capability_ref=cap.ref, run_id=self.ev.run_id)
        self._validate_inputs(cap, inputs)
        for k, spec in cap.inputs.items():
            if spec.sensitivity.value != "none" and inputs.get(k):
                self.redactor.register(inputs[k])
        self.ev.event(
            "replay_start",
            capability=cap.ref,
            tenant=tenant,
            dry_run=dry_run,
            inputs=list(inputs),
            approval=cap.approval.value,
            risk=cap.risk_class.value,
        )

        # idempotency: irreversible capabilities must not be applied twice with the same key
        if not cap.idempotent:
            if not idempotency_key:
                return self._fail(
                    res,
                    None,
                    None,
                    "idempotency_key required for irreversible capability",
                    "none supplied",
                    "policy",
                    t0,
                )
            prior = self.idem.get(cap.ref, idempotency_key) if self.idem else None
            if prior:
                res.kind, res.outcome_code, res.outputs = (
                    ResultKind.BUSINESS_OUTCOME,
                    "ALREADY_APPLIED",
                    prior.get("outputs", {}),
                )
                self.ev.event("idempotent_replay", key=idempotency_key, prior_run=prior.get("run_id"))
                return self._finish(res, t0)
        if (
            self.unattended
            and self.policy.doc.require_approved_for_unattended
            and cap.approval != ApprovalState.APPROVED
            and not dry_run
        ):
            self.ev.event("approval_gate", approval=cap.approval.value)
            return self._fail(
                res,
                None,
                None,
                "artifact approved for unattended replay",
                cap.approval.value,
                "policy",
                t0,
            )

        await self.s.start(cap.entry_url)
        fp = await self.s.fingerprint()
        if fp != cap.app.version_fingerprint:
            self.ev.event("fingerprint_mismatch", expected=cap.app.version_fingerprint, observed=fp)
            res.drift_proposals.append(
                DriftProposal(
                    step_n=0,
                    used_strategy="fingerprint",
                    used_value=fp,
                    primary_strategy="fingerprint",
                    note="app fingerprint changed; review before trusting",
                )
            )

        try:
            for step in cap.steps:
                outcome = await self._run_step(cap, step, inputs, res, approved_irreversible, dry_run)
                if outcome is not None:
                    return self._finish(outcome, t0)
                res.steps_completed = step.n
            # success checkpoint
            ok, observed = await self._check(cap.success)
            if not ok:
                return await self._fail_with_evidence(
                    res, None, None, self._describe(cap.success), observed, "checkpoint_failed", t0
                )
            res.kind = ResultKind.SUCCESS
            res.outputs = self._transform_outputs(cap, res.outputs)
            if not cap.idempotent and idempotency_key and self.idem and not dry_run:
                self.idem.put(cap.ref, idempotency_key, {"run_id": res.run_id, "outputs": res.outputs})
            if self.session:
                self.session.complete()
            return self._finish(res, t0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return await self._fail_with_evidence(
                res,
                res.steps_completed + 1,
                None,
                "engine to complete step",
                f"{type(e).__name__}: {e}",
                "app_error",
                t0,
            )
        finally:
            await self.s.stop()

    # ------------------------------------------------------------------------------ step
    async def _run_step(
        self,
        cap: Capability,
        step: Step,
        inputs: dict[str, str],
        res: ReplayResult,
        approved: bool,
        dry_run: bool,
    ) -> ReplayResult | None:
        # 0) control
        if (
            self.session
            and not self.session.engine_may_act
            and not await self.session.wait_for_resume(self.escalation_wait_s)
        ):
            return self._fail(
                res,
                step.n,
                step.action.value,
                "human to hand back control",
                "timeout",
                "aborted",
                time.monotonic(),
            )
        # 1) interrupts
        for intr in cap.interrupts:
            hit, _ = await self._check(intr.detect, quick=True)
            if hit:
                recovered = await self._handle_interrupt(intr, step, res)
                if not recovered:
                    return await self._escalate_or_fail(res, step, f"unrecoverable interrupt '{intr.id}'")
        # 2) business outcomes
        for oc in cap.outcomes:
            hit, _ = await self._check(oc.detect, quick=True)
            if hit:
                res.kind, res.outcome_code = ResultKind.BUSINESS_OUTCOME, oc.code
                self.ev.event("business_outcome", step=step.n, code=oc.code)
                return res
        # 3) precondition
        if step.precondition:
            ok, observed = await self._check(step.precondition)
            if not ok:
                return await self._fail_with_evidence(
                    res,
                    step.n,
                    step.action.value,
                    self._describe(step.precondition),
                    observed,
                    "checkpoint_failed",
                    time.monotonic(),
                )
        # 4) resolve
        handle, target_text = None, None
        if step.target:
            r = await self.s.resolve(step.target, step.timeout_ms)
            if r is None:
                return await self._escalate_or_fail(
                    res,
                    step,
                    f"locator not found: {step.target.description}",
                    category="locator_not_found",
                )
            handle, target_text = r.handle, r.text
            if r.candidate_index > 0:
                res.drift_proposals.append(
                    DriftProposal(
                        step_n=step.n,
                        used_strategy=r.candidate.strategy.value,
                        used_value=r.candidate.value,
                        primary_strategy=step.target.candidates[0].strategy.value,
                    )
                )
                self.ev.event("drift", step=step.n, used=r.candidate.strategy.value, index=r.candidate_index)
        # 5) policy
        url = await self.s.current_url()
        d = self.policy.check(
            step.action,
            url,
            target_text,
            unattended=self.unattended,
            approved_irreversible=approved,
        )
        effective_risk = max(
            [d.risk, step.risk], key=[RiskClass.SAFE, RiskClass.RISKY, RiskClass.IRREVERSIBLE].index
        )
        self.ev.event(
            "policy_check",
            step=step.n,
            allowed=d.allowed,
            risk=effective_risk.value,
            reason=d.reason,
        )
        if not d.allowed:
            if effective_risk == RiskClass.IRREVERSIBLE and self.session:
                return await self._escalate_or_fail(
                    res, step, f"irreversible step needs a human: {d.reason}", category="policy"
                )
            return self._fail(
                res,
                step.n,
                step.action.value,
                "action permitted by policy",
                d.reason,
                "policy",
                time.monotonic(),
            )
        if dry_run:
            self.ev.event("dry_run_step", step=step.n, action=step.action.value, resolved=handle is not None)
            return None
        # 6) act
        value = self._substitute(step.value, inputs)
        act = self._to_action(step, handle, value)
        obs_before = await self.s.observe()
        self.ev.screenshot(obs_before.png, f"s{step.n:02d}-before")
        ar = await self.s.act(act)
        self.ev.event(
            "step",
            n=step.n,
            action=step.action.value,
            target=(step.target.description if step.target else None),
            value=("${...}" if step.value and PARAM_REF.search(step.value) else step.value),
            ok=ar.ok,
            error=ar.error,
        )
        if not ar.ok:
            return await self._fail_with_evidence(
                res,
                step.n,
                step.action.value,
                "action to succeed",
                ar.error or "unknown",
                "app_error",
                time.monotonic(),
            )
        if step.action == ActionType.EXTRACT and ar.extracted is not None:
            oname = next((k for k, v in cap.outputs.items() if v.extract == step.target), f"step{step.n}")
            res.outputs[oname] = ar.extracted
        # 7) postcondition
        if step.postcondition:
            ok, observed = await self._check(step.postcondition)
            if not ok:
                # a business outcome may have appeared instead of the expected state
                for oc in cap.outcomes:
                    hit, _ = await self._check(oc.detect, quick=True)
                    if hit:
                        res.kind, res.outcome_code = ResultKind.BUSINESS_OUTCOME, oc.code
                        self.ev.event("business_outcome", step=step.n, code=oc.code)
                        return res
                return await self._fail_with_evidence(
                    res,
                    step.n,
                    step.action.value,
                    self._describe(step.postcondition),
                    observed,
                    "checkpoint_failed",
                    time.monotonic(),
                )
        return None

    # ------------------------------------------------------------------------------ helpers
    async def _handle_interrupt(self, intr: Interrupt, step: Step, res: ReplayResult) -> bool:
        for attempt in range(1, intr.max_attempts + 1):
            self.ev.event("interrupt", id=intr.id, handler=intr.handler.value, attempt=attempt, step=step.n)
            if intr.handler == InterruptHandler.DISMISS and intr.action_target:
                r = await self.s.resolve(intr.action_target, 3000)
                if r:
                    await self.s.act(Action(name="left_click", handle=r.handle))
            elif intr.handler == InterruptHandler.RETRY:
                await asyncio.sleep(intr.wait_ms / 1000)
            elif intr.handler == InterruptHandler.RELOGIN and self.relogin:
                await self.relogin(self.s)
            elif intr.handler == InterruptHandler.ESCALATE:
                return False
            hit, _ = await self._check(intr.detect, quick=True)
            if not hit:
                res.recoveries.append(
                    RecoveryEvent(
                        step_n=step.n,
                        interrupt_id=intr.id,
                        handler=intr.handler.value,
                        attempts=attempt,
                    )
                )
                return True
        return False

    async def _escalate_or_fail(
        self, res: ReplayResult, step: Step, reason: str, *, category: str = "checkpoint_failed"
    ) -> ReplayResult | None:
        if self.session is None:
            return await self._fail_with_evidence(
                res,
                step.n,
                step.action.value,
                "engine to proceed",
                reason,
                category,
                time.monotonic(),
            )
        obs = await self.s.observe(with_aria=True)
        shot = self.ev.screenshot(obs.png, f"s{step.n:02d}-escalate")
        req = self.session.escalate(
            reason,
            step_n=step.n,
            screenshot_path=str(shot),
            context={
                "url": obs.url,
                "title": obs.title,
                "action": step.action.value,
                "target": step.target.description if step.target else None,
            },
        )
        self.ev.event("intervention_request", request_id=req.id, reason=reason, step=step.n)
        if not await self.session.wait_for_resume(self.escalation_wait_s):
            self.session.abort("no operator responded")
            res.kind = ResultKind.ESCALATED
            res.failure = FailureDetail(
                step_n=step.n,
                action=step.action.value,
                expected="operator to resume",
                observed="timeout",
                category="aborted",
                evidence=[str(shot)],
            )
            return res
        # human handed back: re-verify this step's precondition (or that the interrupt cleared)
        if step.precondition:
            ok, observed = await self._check(step.precondition)
            if not ok:
                self.session.resume_failed(observed)
                return await self._escalate_or_fail(
                    res, step, f"after handoff, precondition still false: {observed}"
                )
        self.session.resume_ok()
        self.ev.event("resumed", step=step.n)
        return None  # caller re-runs the step

    async def _check(self, cp: Checkpoint, *, quick: bool = False) -> tuple[bool, str]:
        deadline = time.monotonic() + (0.3 if quick else cp.timeout_ms / 1000)
        observed = ""
        while True:
            url = await self.s.current_url()
            text = await self.s.read_text()
            ok = True
            if cp.url_pattern and not re.search(cp.url_pattern, url):
                ok, observed = False, f"url={url}"
            if ok and cp.text_contains and cp.text_contains not in text:
                ok, observed = False, f"text lacks '{cp.text_contains}'"
            if ok and cp.text_absent and cp.text_absent in text:
                ok, observed = False, f"text contains '{cp.text_absent}'"
            if ok and cp.locator:
                r = await self.s.resolve(cp.locator, 800)
                if r is None:
                    ok, observed = False, f"locator '{cp.locator.description}' not visible"
            if ok or time.monotonic() > deadline:
                return ok, observed or "ok"
            await asyncio.sleep(0.25)

    @staticmethod
    def _describe(cp: Checkpoint) -> str:
        parts = []
        if cp.url_pattern:
            parts.append(f"url ~ /{cp.url_pattern}/")
        if cp.text_contains:
            parts.append(f"text contains '{cp.text_contains}'")
        if cp.text_absent:
            parts.append(f"text absent '{cp.text_absent}'")
        if cp.locator:
            parts.append(f"visible: {cp.locator.description}")
        return " AND ".join(parts)

    @staticmethod
    def _substitute(value: str | None, inputs: dict[str, str]) -> str | None:
        if value is None:
            return None
        return PARAM_REF.sub(lambda m: inputs.get(m.group(1), ""), value)

    @staticmethod
    def _to_action(step: Step, handle: Any, value: str | None) -> Action:
        match step.action:
            case ActionType.CLICK:
                if handle is None and step.target:
                    c = step.target.candidates[-1]
                    x, y = (int(v) for v in c.value.split(","))
                    return Action(name="left_click", coordinate=(x, y))
                return Action(name="left_click", handle=handle)
            case ActionType.TYPE:
                return Action(name="type", handle=handle, text=value or "")
            case ActionType.SELECT:
                return Action(name="select", handle=handle, text=value or "")
            case ActionType.KEY:
                return Action(name="key", text=value or "Enter")
            case ActionType.NAVIGATE:
                return Action(name="navigate", url=value)
            case ActionType.SCROLL:
                return Action(name="scroll", scroll_direction="down", scroll_amount=3)
            case ActionType.EXTRACT:
                return Action(name="extract", handle=handle)
            case ActionType.WAIT_FOR:
                return Action(name="wait", duration=0.5)
        raise ValueError(step.action)

    @staticmethod
    def _validate_inputs(cap: Capability, inputs: dict[str, str]) -> None:
        for k, spec in cap.inputs.items():
            if spec.required and k not in inputs:
                raise ValueError(f"missing required input '{k}'")
            if k in inputs and spec.pattern and not re.fullmatch(spec.pattern, inputs[k]):
                raise ValueError(f"input '{k}' does not match {spec.pattern}")

    @staticmethod
    def _transform_outputs(cap: Capability, raw: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in raw.items():
            spec = cap.outputs.get(k)
            if spec is None:
                out[k] = v
                continue
            s = str(v)
            try:
                if spec.transform == Transform.CURRENCY:
                    out[k] = str(Decimal(re.sub(r"[^\d.\-]", "", s)))
                elif spec.transform == Transform.INTEGER or spec.type == ParamType.INTEGER:
                    out[k] = int(re.sub(r"[^\d\-]", "", s))
                else:
                    out[k] = s.strip()
            except (InvalidOperation, ValueError):
                out[k] = s
        return out

    def _fail(
        self,
        res: ReplayResult,
        step_n: int | None,
        action: str | None,
        expected: str,
        observed: str,
        category: str,
        t0: float,
    ) -> ReplayResult:
        res.kind = ResultKind.FAILURE
        res.failure = FailureDetail(
            step_n=step_n, action=action, expected=expected, observed=observed, category=category
        )
        self.ev.event("failure", step=step_n, category=category, expected=expected, observed=observed)
        return res

    async def _fail_with_evidence(
        self,
        res: ReplayResult,
        step_n: int | None,
        action: str | None,
        expected: str,
        observed: str,
        category: str,
        t0: float,
    ) -> ReplayResult:
        paths: list[str] = []
        try:
            obs = await self.s.observe(with_aria=True)
            paths.append(str(self.ev.screenshot(obs.png, f"s{step_n or 0:02d}-failure")))
            paths += [str(p) for p in self.ev.failure_snapshot(await self.s.dom_snapshot(), obs.aria)]
        except Exception:
            pass
        r = self._fail(res, step_n, action, expected, observed, category, t0)
        assert r.failure
        r.failure.evidence = paths
        return r

    def _finish(self, res: ReplayResult, t0: float) -> ReplayResult:
        res.duration_ms = int((time.monotonic() - t0) * 1000)
        self.ev.event(
            "replay_end",
            kind=res.kind.value,
            outcome=res.outcome_code,
            steps=res.steps_completed,
            recoveries=len(res.recoveries),
            drift=len(res.drift_proposals),
            duration_ms=res.duration_ms,
            llm_calls=res.llm_calls,
        )
        self.ev.summary(result=json.loads(res.model_dump_json()))
        return res
