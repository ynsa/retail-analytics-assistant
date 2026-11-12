"""The start of a chat (spec 37, B2.6). The CLI and the local UI call `start_session()`.

Each problem before the first question raises `StartError` with the message and the exit code of
B2.6. The start also runs the retention cleanup of spec 35 (B2.5).
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from opsfleet_agent.config import AppConfig
from opsfleet_agent.errors import AgentError, ConfigError
from opsfleet_agent.identity import UserContext, resolve_user
from opsfleet_agent.memory.conversations import load_for_resume
from opsfleet_agent.observability.retention import PruneResult, prune
from opsfleet_agent.store.db import connect

_log = logging.getLogger("opsfleet_agent.startup")
_ADC_WARNING = ".*authenticated using end user credentials.*"


class StartError(Exception):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


@dataclass
class Started:
    session: object  # AssistantSession (not imported here: the import is slow)
    user: UserContext
    resumed: bool
    warnings: list[str] = field(default_factory=list)
    pruned: PruneResult | None = None


def check_user(config: AppConfig, user_id: str) -> UserContext:
    try:
        return resolve_user(config.users, user_id)
    except ConfigError as exc:
        raise StartError(f"User '{user_id}' is not in config/users.yaml.", 2) from exc


def check_secrets(config: AppConfig) -> None:
    """A missing `.env` value stops the start and names the variable."""
    for name, value in (
        ("GOOGLE_API_KEY", config.secrets.google_api_key),
        ("GOOGLE_CLOUD_PROJECT", config.secrets.google_cloud_project),
    ):
        if not value:
            raise StartError(f"{name} is not set. See the setup guide.", 2)


def check_resume(config: AppConfig, user_id: str, conversation_id: str) -> None:
    """The conversation of this user, at most 90 days old (spec 34)."""
    settings = config.settings
    conn = connect(settings.data_path("app.db"))
    try:
        load_for_resume(
            conn,
            conversation_id,
            user_id,
            datetime.now(UTC),
            settings.context.resume_max_days,
        )
    except AgentError as exc:
        raise StartError(exc.user_message, 2) from exc
    finally:
        conn.close()


def start_error(exc: BaseException) -> str:
    """A clear message for a failed start, for example BigQuery down without a schema cache."""
    from opsfleet_agent.resilience.errors_map import map_bq_error

    return f"The assistant could not start. {map_bq_error(exc).user_message}"


def run_retention(config: AppConfig, now: datetime) -> PruneResult | None:
    """Spec 35, B2.5. A failed cleanup is logged and does not stop the start."""
    conn = connect(config.settings.data_path("app.db"))
    try:
        return prune(conn, config.settings, now)
    except Exception:  # noqa: BLE001 - the chat starts also without the cleanup
        _log.exception("retention cleanup failed")
        return None
    finally:
        conn.close()


def start_session(
    config: AppConfig,
    user_id: str,
    resume: str | None = None,
    refresh_cache: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Started:
    user = check_user(config, user_id)
    check_secrets(config)
    if resume:
        check_resume(config, user.id, resume)

    from opsfleet_agent.session import (
        AssistantSession,
        build_services,
        check_user_brands,
    )

    try:
        with warnings.catch_warnings():
            # google-auth warns about user credentials without a quota project. The free
            # public dataset needs none, and the warning is not for the chat user.
            warnings.filterwarnings("ignore", message=_ADC_WARNING)
            services = build_services(config, refresh_cache=refresh_cache)
    except ConfigError as exc:
        raise StartError(str(exc), 2) from exc
    except AgentError as exc:  # for example the daily BigQuery limit of the value cache
        raise StartError(
            f"The assistant could not start. {exc.user_message}", 3
        ) from exc
    except Exception as exc:
        raise StartError(start_error(exc), 3) from exc
    try:
        brand_warnings = check_user_brands(config, services.values, user.id)
    except ConfigError as exc:
        raise StartError(str(exc), 2) from exc
    pruned = run_retention(config, clock())
    if resume:
        session = AssistantSession.resume(config, user, services, resume)
    else:
        session = AssistantSession(config, user, services)
    return Started(session, user, bool(resume), brand_warnings, pruned)
