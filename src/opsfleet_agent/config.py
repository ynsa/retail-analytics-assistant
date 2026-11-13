"""Configuration: config/settings.yaml, config/users.yaml and the secrets in .env (spec 00)."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    # Spec 36: one attempt with this model when the main model fails. None turns it off.
    fallback: str | None = None


class ContextSettings(_Strict):
    """The context of each model call and the memory of a conversation (spec 34, B3)."""

    max_tokens_per_call: int = Field(default=14_000, gt=0)
    chars_per_token: float = Field(default=3.5, gt=0)
    window_max_turns: int = Field(default=8, ge=2)
    fold_turns: int = Field(default=4, ge=1)
    summary_max_words: int = Field(default=400, gt=0)
    history_answer_max_tokens: int = Field(default=300, gt=0)
    result_preview_rows: int = Field(default=5, ge=0)
    result_cache_days: int = Field(default=30, gt=0)
    resume_max_days: int = Field(default=90, gt=0)
    # Budgets of the parts of B2.1 (tokens). The builder cuts the parts in a fixed order.
    history_tokens: int = Field(default=3_000, gt=0)
    summary_tokens: int = Field(default=600, gt=0)
    data_tokens: int = Field(default=6_000, gt=0)
    cut_rows: int = Field(default=20, gt=0)
    route_history_turns: int = Field(default=2, ge=0)
    summary_min_seconds: float = Field(default=10, ge=0)


class ResilienceSettings(_Strict):
    """Retries, timeouts and circuit breakers (spec 36, B3). The Gemini timeout and retry count
    are `models.timeout_s` and `models.max_retries`. The query timeout is
    `limits.per_query.timeout_s`."""

    bq_dry_run_timeout_s: float = Field(default=10, gt=0)
    bq_retries: int = Field(default=1, ge=0, le=3)
    retry_waits_s: list[float] = Field(default_factory=lambda: [1.0, 2.0])
    retry_jitter: float = Field(default=0.5, ge=0, le=1)
    retry_after_max_s: float = Field(default=10, ge=0)
    min_time_left_s: float = Field(default=5, ge=0)
    breaker_threshold: int = Field(default=3, gt=0)
    breaker_open_s: float = Field(default=60, gt=0)
    graph_recursion_limit: int = Field(default=60, gt=0)
    sqlite_busy_timeout_s: float = Field(default=5, gt=0)


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
    # Google resets the Gemini free-tier daily quota at midnight Pacific time, so the daily
    # counter uses the date of this time zone (2026-11-12).
    reset_timezone: str = "America/Los_Angeles"

    @model_validator(mode="after")
    def _check_timezone(self) -> PerDayLimits:
        try:
            ZoneInfo(self.reset_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"limits.per_day.reset_timezone '{self.reset_timezone}' is not a known time zone"
            ) from exc
        return self


class Limits(_Strict):
    per_turn: PerTurnLimits
    per_query: PerQueryLimits
    per_day: PerDayLimits


class Paths(_Strict):
    data_dir: Path


class AgentSettings(_Strict):
    """The agent core (spec 31, B3)."""

    max_attempts_per_step: int = Field(default=3, gt=0)
    max_steps_per_question: int = Field(default=3, gt=0)
    max_user_message_chars: int = Field(default=4000, gt=0)
    answer_max_rows: int = Field(default=20, gt=0)
    persona_file: Path = Path("config/persona.md")
    persona_max_chars: int = Field(default=2000, gt=0)


class ReportSettings(_Strict):
    """Reports (spec 32, B3). `dir` is a folder inside `paths.data_dir`."""

    max_planned_queries: int = Field(default=4, gt=0)
    max_charts: int = Field(default=3, ge=0)
    chart_types: list[str] = Field(default_factory=lambda: ["bar", "line", "pie"])
    max_words: int = Field(default=1200, gt=0)
    max_findings: int = Field(default=6, gt=0)
    min_action_items: int = Field(default=3, ge=0)
    max_action_items: int = Field(default=7, gt=0)
    max_table_rows: int = Field(default=15, gt=0)
    max_chart_points: int = Field(default=60, gt=0)
    max_bar_categories: int = Field(default=20, gt=0)
    max_pie_slices: int = Field(default=6, gt=0)
    max_listed: int = Field(default=50, gt=0)
    max_file_bytes: int = Field(default=2_000_000, gt=0)
    dir: str = "reports"


class EvalPrices(_Strict):
    """Paid-tier prices in USD for 1M tokens. For the cost estimate only (spec 23)."""

    input_per_m: float = Field(default=0.30, ge=0)
    output_per_m: float = Field(default=2.50, ge=0)


class EvalSettings(_Strict):
    """Settings of the evaluation runner (spec 23, B3)."""

    judge_model: str = "gemini-3.5-flash-lite"
    judge_temperature: float = Field(default=0.0, ge=0)
    call_interval_s: float = Field(default=4.0, ge=0)
    cap_margin: float = Field(default=0.2, ge=0)
    ask_above_calls: int = Field(default=10, ge=0)
    human_sample: int = Field(default=10, ge=1)
    default_case_bytes: int = Field(default=40_000_000, ge=0)
    snapshot_max_bytes_billed: int = Field(default=100_000_000, gt=0)
    keep_runs_days: int = Field(default=30, ge=1)
    rate_limit_wait_s: float = Field(default=60.0, ge=0)
    tokens_in_per_call: int = Field(default=4000, ge=0)
    tokens_out_per_call: int = Field(default=300, ge=0)
    judge_tokens_in_per_call: int = Field(default=3000, ge=0)
    judge_tokens_out_per_call: int = Field(default=400, ge=0)
    prices: dict[str, EvalPrices] = Field(
        default_factory=lambda: {"gemini-3.5-flash-lite": EvalPrices()}
    )


class TracingSettings(_Strict):
    """Spec 35, B3."""

    capture_prompts: bool = True  # masked prompts and outputs in the `llm` spans
    attribute_max_chars: int = Field(default=2000, gt=0)
    otlp_endpoint: str | None = None  # off. Needs the user's approval before any use.


class RetentionSettings(_Strict):
    """Spec 35, B2.5: `prune()` at the start of `chat` and in `purge`."""

    traces_days: int = Field(default=30, gt=0)
    logs_days: int = Field(default=30, gt=0)
    turn_text_days: int = Field(default=90, gt=0)
    turn_metrics_days: int = Field(default=365, gt=0)


class StatsSettings(_Strict):
    """Spec 35, B3."""

    default_days: int = Field(default=7, gt=0)
    max_days: int = Field(default=90, gt=0)
    export_dir: str = "exports"  # inside paths.data_dir: data/exports/


class CliSettings(_Strict):
    """Spec 37, B3."""

    show_footer: bool = True
    status_line: bool = True


class Settings(_Strict):
    models: ModelSettings
    bigquery: BigQuerySettings
    limits: Limits
    paths: Paths
    agent: AgentSettings = Field(default_factory=AgentSettings)
    reports: ReportSettings = Field(default_factory=ReportSettings)
    resilience: ResilienceSettings = Field(default_factory=ResilienceSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    tracing: TracingSettings = Field(default_factory=TracingSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    stats: StatsSettings = Field(default_factory=StatsSettings)
    cli: CliSettings = Field(default_factory=CliSettings)

    def project_path(self, path: Path) -> Path:
        """A path from the settings: relative paths start at the project root."""
        return path if path.is_absolute() else PROJECT_ROOT / path

    eval: EvalSettings = Field(default_factory=EvalSettings)

    def data_path(self, *parts: str) -> Path:
        base = self.paths.data_dir
        if not base.is_absolute():
            base = PROJECT_ROOT / base
        return base.joinpath(*parts)

    def reports_dir(self) -> Path:
        """The folder of the report files: `<data_dir>/reports` (spec 32, B2.5)."""
        return self.data_path(self.reports.dir)


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
