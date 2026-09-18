"""Runtime settings. Secrets come only from the environment; nothing here is ever serialized."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUA_", env_file=".env", extra="ignore")

    anthropic_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="ANTHROPIC_API_KEY")
    model: str = "claude-sonnet-5"
    effort: str = "medium"
    policy_path: Path = Path("policies/default.yaml")
    evidence_dir: Path = Path("evidence")
    mockbank_url: str = "http://localhost:7860/bank"
    headless: bool = True
    viewport_width: int = 1280
    viewport_height: int = 800
    max_discovery_steps: int = 40
    discovery_timeout_s: int = 300
    step_timeout_ms: int = 10_000
    redact_literals: str = ""
    discovery_token: str = ""
    discovery_daily_cap: int = 5
    idempotency_store: Path = Path(".idempotency.json")

    @property
    def redact_values(self) -> list[str]:
        vals = [v.strip() for v in self.redact_literals.split(",") if v.strip()]
        key = self.anthropic_api_key.get_secret_value()
        if key:
            vals.append(key)
        return vals


settings = Settings()
