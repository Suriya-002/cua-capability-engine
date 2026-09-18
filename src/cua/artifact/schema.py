"""The capability artifact.

Design goals (see docs/adr/0002-artifact-schema.md):
- A *contract*, not a step list: typed inputs, typed outputs, success condition, business outcomes.
- Locators are ranked candidate lists so replay degrades gracefully and drift is observable.
- Nothing in here is ever a secret or raw PII: inputs carry a `sensitivity` tag so the engine
  redacts them everywhere; discovery-time literal values are parameterised before emission.
- Versioned (semver + schema_version), reviewable (approval state), measurable (stability).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
PARAM_REF = re.compile(r"\$\{inputs\.([a-zA-Z_][a-zA-Z0-9_]*)\}")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ----------------------------------------------------------------------------- locators
class LocatorStrategy(StrEnum):
    """Ordered roughly by how well each survives cosmetic change on legacy surfaces."""

    ROLE_NAME = "role_name"  # accessible role + name ("button" / "Search")
    LABEL_RELATIVE = "label_relative"  # nearest visible label text ("Member ID" -> input)
    TEXT = "text"  # exact visible text of the control
    ARIA_PATH = "aria_path"  # path in the accessibility snapshot
    CSS_STRUCTURAL = "css_structural"  # nth-child chain; brittle but precise
    IMAGE_ANCHOR = "image_anchor"  # reserved: template-match on a cropped screenshot (desktop)
    COORDS = "coords"  # last resort; only valid for the recorded viewport


class LocatorCandidate(Strict):
    strategy: LocatorStrategy
    value: str = Field(..., description="Strategy-specific selector payload")
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    frame: str | None = Field(None, description="Frame name/path for framesets; None = main")
    note: str | None = None


class Locator(Strict):
    description: str = Field(..., description="Human-readable: what this control is")
    candidates: list[LocatorCandidate] = Field(..., min_length=1)

    @field_validator("candidates")
    @classmethod
    def _sorted_by_confidence(cls, v: list[LocatorCandidate]) -> list[LocatorCandidate]:
        return sorted(v, key=lambda c: c.confidence, reverse=True)


# ----------------------------------------------------------------------------- checkpoints
class Checkpoint(Strict):
    """A condition that must hold. Any combination; all present fields must be satisfied."""

    locator: Locator | None = None
    url_pattern: str | None = Field(None, description="Regex on current URL")
    text_contains: str | None = Field(None, description="Visible page text must contain this")
    text_absent: str | None = None
    timeout_ms: int = 10_000

    @model_validator(mode="after")
    def _non_empty(self) -> Checkpoint:
        if not any([self.locator, self.url_pattern, self.text_contains, self.text_absent]):
            raise ValueError("checkpoint must assert at least one thing")
        return self


# ----------------------------------------------------------------------------- steps
class ActionType(StrEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    KEY = "key"
    SCROLL = "scroll"
    WAIT_FOR = "wait_for"
    EXTRACT = "extract"


class RiskClass(StrEnum):
    SAFE = "safe"  # read-only or trivially reversible
    RISKY = "risky"  # changes state but reversible by an operator
    IRREVERSIBLE = "irreversible"  # money movement, account creation, deletion


class Step(Strict):
    n: int = Field(..., ge=1)
    action: ActionType
    target: Locator | None = None
    value: str | None = Field(None, description="Literal or ${inputs.name} reference")
    precondition: Checkpoint | None = None
    postcondition: Checkpoint | None = Field(
        None, description="Verified after acting; failure here is attributed to THIS step"
    )
    risk: RiskClass = RiskClass.SAFE
    timeout_ms: int = 10_000
    rationale: str | None = Field(None, description="Why the model took this step (from discovery)")

    @model_validator(mode="after")
    def _target_required(self) -> Step:
        needs_target = {ActionType.CLICK, ActionType.TYPE, ActionType.SELECT, ActionType.EXTRACT}
        if self.action in needs_target and self.target is None:
            raise ValueError(f"step {self.n}: action {self.action} requires a target")
        if self.action in {ActionType.TYPE, ActionType.NAVIGATE, ActionType.KEY} and not self.value:
            raise ValueError(f"step {self.n}: action {self.action} requires a value")
        return self


# ----------------------------------------------------------------------------- runtime conditions
class InterruptHandler(StrEnum):
    DISMISS = "dismiss"  # click the dismiss target then continue
    RETRY = "retry"  # wait and re-run the current step
    RELOGIN = "relogin"  # run the `relogin` sub-flow then re-run the step
    ESCALATE = "escalate"  # hand to a human


class Interrupt(Strict):
    """A recoverable condition that may appear at any point (interstitial, session expiry, slow load)."""

    id: str
    detect: Checkpoint
    handler: InterruptHandler
    action_target: Locator | None = Field(None, description="What to click for DISMISS")
    max_attempts: int = 2
    wait_ms: int = 1500


class Outcome(Strict):
    """An expected *business* result the caller must receive — not a failure (e.g. MEMBER_NOT_FOUND)."""

    code: str = Field(..., pattern=r"^[A-Z][A-Z0-9_]*$")
    detect: Checkpoint
    description: str
    terminal: bool = True


# ----------------------------------------------------------------------------- contract
class ParamType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"


class Sensitivity(StrEnum):
    NONE = "none"
    PII = "pii"  # masked in evidence/logs, never in artifact
    SECRET = "secret"  # never logged in any form; supplied per-invocation only


class ParamSpec(Strict):
    type: ParamType
    description: str
    required: bool = True
    pattern: str | None = None
    sensitivity: Sensitivity = Sensitivity.NONE
    example: str | None = Field(None, description="Synthetic example only; never a real value")


class Transform(StrEnum):
    NONE = "none"
    CURRENCY = "currency"  # "$1,234.56" -> 1234.56
    TRIM = "trim"
    INTEGER = "integer"


class OutputSpec(Strict):
    type: ParamType
    description: str
    extract: Locator
    transform: Transform = Transform.NONE
    sensitivity: Sensitivity = Sensitivity.NONE


class AppRef(Strict):
    vendor: str
    product: str
    version_fingerprint: str = Field(
        ..., description="Hash of stable landmarks (title, login form shape). Drift signal."
    )


class ApprovalState(StrEnum):
    DRAFT = "draft"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"


class Stability(Strict):
    runs: int = 0
    successes: int = 0
    last_run_at: datetime | None = None

    @property
    def score(self) -> float | None:
        return None if self.runs == 0 else self.successes / self.runs


class Provenance(Strict):
    discovery_run_id: str
    model: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    author: str = "cua-discovery"


class TenantOverride(Strict):
    """Per-tenant specialisation. Keys are step numbers; values replace the base locator/value."""

    entry_url: str | None = None
    locators: dict[int, Locator] = Field(default_factory=dict)
    values: dict[int, str] = Field(default_factory=dict)
    extra_steps: list[Step] = Field(default_factory=list)


class Capability(Strict):
    schema_version: str = "1.0"
    id: str = Field(..., pattern=r"^[a-z][a-z0-9_]*$")
    version: str = Field(..., description="semver")
    name: str
    goal: str = Field(..., description="Natural-language goal used at discovery")
    app: AppRef
    entry_url: str
    risk_class: RiskClass = RiskClass.SAFE
    inputs: dict[str, ParamSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    steps: list[Step] = Field(..., min_length=1)
    interrupts: list[Interrupt] = Field(default_factory=list)
    outcomes: list[Outcome] = Field(default_factory=list)
    success: Checkpoint
    tenant_overrides: dict[str, TenantOverride] = Field(default_factory=dict)
    approval: ApprovalState = ApprovalState.DRAFT
    stability: Stability = Field(default_factory=Stability)
    provenance: Provenance
    idempotent: bool = Field(
        True, description="If False, invocations must carry an idempotency_key (engine-enforced)"
    )

    @field_validator("version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not SEMVER.match(v):
            raise ValueError("version must be semver, e.g. 1.0.0")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> Capability:
        expected = list(range(1, len(self.steps) + 1))
        if [s.n for s in self.steps] != expected:
            raise ValueError("steps must be numbered 1..N without gaps")
        for s in self.steps:
            if s.value:
                for ref in PARAM_REF.findall(s.value):
                    if ref not in self.inputs:
                        raise ValueError(f"step {s.n} references unknown input '{ref}'")
        if self.risk_class == RiskClass.IRREVERSIBLE and self.idempotent:
            raise ValueError("irreversible capabilities must set idempotent=False")
        codes = [o.code for o in self.outcomes]
        if len(codes) != len(set(codes)):
            raise ValueError("outcome codes must be unique")
        return self

    # --- convenience --------------------------------------------------------------------
    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def describe(self) -> str:
        """Human/agent-readable summary. This is what a catalog shows."""
        ins = ", ".join(f"{k}: {v.type.value}{'' if v.required else '?'}" for k, v in self.inputs.items())
        outs = ", ".join(f"{k}: {v.type.value}" for k, v in self.outputs.items())
        codes = ", ".join(o.code for o in self.outcomes) or "—"
        return (
            f"{self.ref}  [{self.approval.value}, risk={self.risk_class.value}]\n"
            f"  {self.name}: {self.goal}\n"
            f"  inputs:   {ins or '—'}\n"
            f"  outputs:  {outs or '—'}\n"
            f"  outcomes: {codes}\n"
            f"  steps:    {len(self.steps)}  interrupts: {len(self.interrupts)}"
        )

    def apply_tenant(self, tenant: str | None) -> Capability:
        if not tenant or tenant not in self.tenant_overrides:
            return self
        ov = self.tenant_overrides[tenant]
        data: dict[str, Any] = self.model_dump(mode="python")
        if ov.entry_url:
            data["entry_url"] = ov.entry_url
        for s in data["steps"]:
            if s["n"] in ov.locators:
                s["target"] = ov.locators[s["n"]].model_dump(mode="python")
            if s["n"] in ov.values:
                s["value"] = ov.values[s["n"]]
        if ov.extra_steps:
            data["steps"].extend(x.model_dump(mode="python") for x in ov.extra_steps)
            data["steps"].sort(key=lambda s: s["n"])
        return Capability.model_validate(data)
