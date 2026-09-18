"""Allowlist + risk classification. Wraps every `act()` in discovery AND replay.

The policy is data (policies/*.yaml) so a tenant can tighten it without code changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field

from cua.artifact.schema import ActionType, RiskClass


class PolicyViolation(RuntimeError):
    pass


class RiskRule(BaseModel):
    pattern: str
    risk: RiskClass
    _re: re.Pattern[str] | None = None

    def matches(self, text: str) -> bool:
        if self._re is None:
            object.__setattr__(self, "_re", re.compile(self.pattern, re.IGNORECASE))
        assert self._re is not None
        return bool(self._re.search(text))


class PolicyDoc(BaseModel):
    name: str = "default"
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_path_patterns: list[str] = Field(default_factory=lambda: [".*"])
    denied_path_patterns: list[str] = Field(default_factory=list)
    allowed_actions: list[ActionType] = Field(default_factory=lambda: list(ActionType))
    risk_rules: list[RiskRule] = Field(default_factory=list)
    unattended_max_risk: RiskClass = RiskClass.RISKY
    require_approved_for_unattended: bool = True
    max_steps: int = 60


@dataclass(frozen=True)
class Decision:
    allowed: bool
    risk: RiskClass
    reason: str


class Policy:
    def __init__(self, doc: PolicyDoc) -> None:
        self.doc = doc

    @classmethod
    def load(cls, path: Path) -> Policy:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(PolicyDoc.model_validate(data))

    # --- checks ---------------------------------------------------------------------------
    def url_allowed(self, url: str) -> tuple[bool, str]:
        u = urlparse(url)
        host = u.hostname or ""
        if self.doc.allowed_hosts and host not in self.doc.allowed_hosts:
            return False, f"host '{host}' not in allowlist"
        for pat in self.doc.denied_path_patterns:
            if re.search(pat, u.path):
                return False, f"path '{u.path}' matches denied pattern '{pat}'"
        if not any(re.search(p, u.path) for p in self.doc.allowed_path_patterns):
            return False, f"path '{u.path}' not in allowed patterns"
        return True, "ok"

    def classify(self, action: ActionType, target_text: str | None, url: str) -> RiskClass:
        haystack = " ".join(filter(None, [target_text, urlparse(url).path]))
        worst = RiskClass.SAFE
        order = [RiskClass.SAFE, RiskClass.RISKY, RiskClass.IRREVERSIBLE]
        if action in {ActionType.CLICK, ActionType.KEY, ActionType.SELECT}:
            for rule in self.doc.risk_rules:
                if rule.matches(haystack) and order.index(rule.risk) > order.index(worst):
                    worst = rule.risk
        return worst

    def check(
        self,
        action: ActionType,
        url: str,
        target_text: str | None = None,
        *,
        unattended: bool = False,
        approved_irreversible: bool = False,
    ) -> Decision:
        ok, why = self.url_allowed(url)
        if not ok:
            return Decision(False, RiskClass.SAFE, why)
        if action not in self.doc.allowed_actions:
            return Decision(False, RiskClass.SAFE, f"action '{action.value}' not permitted")
        risk = self.classify(action, target_text, url)
        order = [RiskClass.SAFE, RiskClass.RISKY, RiskClass.IRREVERSIBLE]
        if unattended and order.index(risk) > order.index(self.doc.unattended_max_risk):
            if risk == RiskClass.IRREVERSIBLE and approved_irreversible:
                return Decision(True, risk, "irreversible step explicitly approved by caller")
            return Decision(False, risk, f"{risk.value} action requires human confirmation")
        return Decision(True, risk, "ok")

    def enforce(self, *args: object, **kwargs: object) -> Decision:
        d = self.check(*args, **kwargs)  # type: ignore[arg-type]
        if not d.allowed:
            raise PolicyViolation(d.reason)
        return d
