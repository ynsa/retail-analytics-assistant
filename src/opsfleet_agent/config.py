"""Configuration: config/settings.yaml, config/users.yaml and the secrets in .env (spec 00)."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from opsfleet_agent.errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelSettings(_Strict):
    default: str
    timeout_s: float = Field(gt=0)
    max_retries: int = Field(ge=0, le=5)


class BigQuerySettings(_Strict):
    dataset: str
    tables: list[str]
    location: str = "US"


class PerTurnLimits(_Strict):
    max_llm_calls: int = Field(gt=0)
    max_seconds_question: float = Field(gt=0)
    max_seconds_report: float = Field(gt=0)
    max_bq_queries: int = Field(gt=0)
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)


class PerQueryLimits(_Strict):
    max_bytes_billed: int = Field(gt=0)
    timeout_s: float = Field(gt=0)
    max_rows_to_llm: int = Field(gt=0)


class PerDayLimits(_Strict):
    max_llm_calls: int = Field(gt=0)
    max_bq_bytes_billed: int = Field(gt=0)


class Limits(_Strict):
    per_turn: PerTurnLimits
    per_query: PerQueryLimits
    per_day: PerDayLimits


class Paths(_Strict):
    data_dir: Path


class Settings(_Strict):
    models: ModelSettings
    bigquery: BigQuerySettings
    limits: Limits
    paths: Paths

    def data_path(self, *parts: str) -> Path:
        base = self.paths.data_dir
        if not base.is_absolute():
            base = PROJECT_ROOT / base
        return base.joinpath(*parts)


class Role(StrEnum):
    MANAGER = "manager"
    CEO = "ceo"


class UserEntry(_Strict):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    role: Role
    brands: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_brands(self) -> UserEntry:
        if self.role is Role.MANAGER and not self.brands:
            raise ValueError(f"user '{self.id}' is a manager without brands")
        if self.role is Role.CEO and self.brands:
            raise ValueError(
                f"user '{self.id}' is a ceo: remove the brand list (ceo sees all brands)"
            )
        return self


class UsersConfig(_Strict):
    users: list[UserEntry]

    @model_validator(mode="after")
    def _unique_ids(self) -> UsersConfig:
        ids = [u.id for u in self.users]
        if len(ids) != len(set(ids)):
            raise ValueError("user IDs in users.yaml must be unique")
        return self


class Secrets(_Strict):
    google_api_key: str | None
    google_cloud_project: str | None


class AppConfig(_Strict):
    settings: Settings
    users: UsersConfig
    secrets: Secrets


def _read_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"Configuration file is not valid YAML: {path}: {exc}"
        ) from exc


def load_config(
    config_dir: Path | None = None, env_file: Path | None = None
) -> AppConfig:
    """Read and check all configuration. A wrong file stops the start with a clear message."""
    config_dir = config_dir or PROJECT_ROOT / "config"
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    try:
        settings = Settings.model_validate(_read_yaml(config_dir / "settings.yaml"))
        users = UsersConfig.model_validate(_read_yaml(config_dir / "users.yaml"))
    except ValidationError as exc:
        raise ConfigError(f"Configuration is not valid:\n{exc}") from exc
    secrets = Secrets(
        google_api_key=os.environ.get("GOOGLE_API_KEY") or None,
        google_cloud_project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None,
    )
    return AppConfig(settings=settings, users=users, secrets=secrets)
