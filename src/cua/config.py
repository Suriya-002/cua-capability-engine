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
    def secrets(self) -> dict[str, str]:
        """CUA_SECRET_<NAME>=value -> {"name": value}. Supplied to discovery and replay, never persisted."""
        import os

        prefix = "CUA_SECRET_"
        found = {k[len(prefix) :].lower(): v for k, v in os.environ.items() if k.startswith(prefix) and v}
        # .env is loaded by pydantic-settings for declared fields only; read it for secrets too
        env_file = Path(".env")
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith(prefix) and "=" in line:
                    k, _, v = line.partition("=")
                    found.setdefault(k[len(prefix) :].strip().lower(), v.strip())
        return found

    @property
    def redact_values(self) -> list[str]:
        vals = [v.strip() for v in self.redact_literals.split(",") if v.strip()]
        key = self.anthropic_api_key.get_secret_value()
        if key:
            vals.append(key)
        vals.extend(self.secrets.values())
        return vals


settings = Settings()
