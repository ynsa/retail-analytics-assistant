from datetime import UTC, datetime

import pytest

from opsfleet_agent.errors import ErrorType, LimitExceeded
from opsfleet_agent.limits import DailyLedger, TurnBudget, next_reset
from opsfleet_agent.store.db import connect


def test_llm_call_limit(budget):
    for _ in range(8):
        budget.charge_llm()
    with pytest.raises(LimitExceeded) as exc:
        budget.charge_llm()
    assert exc.value.limit == "per_turn.max_llm_calls"
    assert exc.value.error_type is ErrorType.LIMIT_EXCEEDED
    assert "8 model calls" in exc.value.user_message


def test_query_limit(budget):
    for _ in range(6):
        budget.charge_query()
    with pytest.raises(LimitExceeded, match="max_bq_queries"):
        budget.charge_query()


def test_token_limit(budget):
    budget.charge_llm()
    budget.record_tokens(60_000, 10)
    with pytest.raises(LimitExceeded, match="max_tokens"):
        budget.charge_llm()


@pytest.mark.parametrize(("kind", "seconds"), [("question", 60), ("report", 150)])
def test_time_limit(config, ledger, kind, seconds):
    now = [0.0]
    b = TurnBudget(config.settings.limits, ledger, kind=kind, monotonic=lambda: now[0])
    now[0] = seconds - 0.1
    b.charge_llm()
    now[0] = seconds
    with pytest.raises(LimitExceeded, match="max_seconds") as exc:
        b.charge_llm()
    assert f"{seconds} s" in exc.value.user_message


def test_daily_llm_limit_persists_after_restart(config):
    path = config.settings.data_path("app.db")
    first = connect(path)
    ledger = DailyLedger(first, config.settings.limits)
    daily = config.settings.limits.per_day.max_llm_calls
    ledger.add(llm_calls=daily)
    first.close()

    second = connect(path)  # a restart: a new connection reads the same counters
    ledger2 = DailyLedger(second, config.settings.limits)
    assert ledger2.today()["llm_calls"] == daily
    with pytest.raises(LimitExceeded, match="per_day.max_llm_calls"):
        TurnBudget(config.settings.limits, ledger2).charge_llm()
    second.close()


def test_daily_bytes_limit(budget, ledger):
    ledger.add(bq_bytes_billed=20_000_000_000)
    with pytest.raises(LimitExceeded, match="per_day.max_bq_bytes_billed") as exc:
        budget.charge_query()
    assert "20 GB" in exc.value.user_message


def test_daily_counter_resets_at_midnight_pacific_time(conn, config):
    """Google resets the free-tier quota at midnight Pacific time (spec 00, note of 2026-11-12).
    In November, Pacific time is UTC-8: the day changes at 08:00 UTC, not at 00:00 UTC."""
    day = [datetime(2026, 11, 11, 23, 0, tzinfo=UTC)]  # 15:00 on 11 Nov in Los Angeles
    ledger = DailyLedger(conn, config.settings.limits, clock=lambda: day[0])
    ledger.add(llm_calls=config.settings.limits.per_day.max_llm_calls)
    assert ledger.day() == "2026-11-11"
    day[0] = datetime(2026, 11, 12, 0, 1, tzinfo=UTC)  # midnight UTC: same Pacific day
    assert ledger.day() == "2026-11-11"
    with pytest.raises(LimitExceeded) as exc:
        ledger.check_llm()
    assert "resets at midnight Pacific time" in exc.value.user_message
    day[0] = datetime(2026, 11, 12, 8, 1, tzinfo=UTC)  # 00:01 on 12 Nov in Los Angeles
    assert ledger.day() == "2026-11-12"
    assert ledger.today()["llm_calls"] == 0
    ledger.check_llm()  # no exception


def test_daily_counter_follows_the_reset_time_zone_setting(conn, config):
    limits = config.settings.limits
    utc_limits = limits.model_copy(
        update={"per_day": limits.per_day.model_copy(update={"reset_timezone": "UTC"})}
    )
    now = datetime(2026, 11, 12, 0, 1, tzinfo=UTC)
    assert DailyLedger(conn, utc_limits, clock=lambda: now).day() == "2026-11-12"
    assert DailyLedger(conn, limits, clock=lambda: now).day() == "2026-11-11"


def test_old_utc_rows_stay_readable(conn, config):
    """An earlier version wrote the UTC date. The row of the same date counts on."""
    conn.execute("INSERT INTO usage_daily (day, llm_calls) VALUES ('2026-11-12', 16)")
    now = datetime(2026, 11, 12, 20, 0, tzinfo=UTC)  # 12:00 on 12 Nov in Los Angeles
    ledger = DailyLedger(conn, config.settings.limits, clock=lambda: now)
    assert ledger.today()["llm_calls"] == 16
    ledger.add(llm_calls=1)
    assert ledger.today()["llm_calls"] == 17


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (
            datetime(2026, 11, 12, 7, 0, tzinfo=UTC),
            datetime(2026, 11, 12, 8, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 11, 12, 9, 0, tzinfo=UTC),
            datetime(2026, 11, 13, 8, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            datetime(2026, 7, 2, 7, 0, tzinfo=UTC),
        ),
    ],
)
def test_next_reset_is_the_next_pacific_midnight(now, expected):
    assert next_reset(now, "America/Los_Angeles") == expected


def test_wrong_reset_time_zone_is_a_config_error(config):
    from pydantic import ValidationError

    from opsfleet_agent.config import PerDayLimits

    with pytest.raises(ValidationError, match="not a known time zone"):
        PerDayLimits(max_llm_calls=1, max_bq_bytes_billed=1, reset_timezone="Mars/Base")


def test_budget_totals(budget):
    budget.charge_llm()
    budget.record_tokens(1000, 50)
    budget.charge_query()
    budget.record_bytes(10_485_760)
    totals = budget.totals()
    assert totals["llm_calls"] == 1 and totals["bq_queries"] == 1
    assert totals["tokens_in"] == 1000 and totals["bytes_billed"] == 10_485_760


def test_report_limits_and_reserved_calls(config, ledger):
    clock = [0.0]
    budget = TurnBudget(config.settings.limits, ledger, monotonic=lambda: clock[0])
    budget.use_report_limits()
    clock[0] = 100.0
    budget.check_time()  # a question would stop at 60 s, a report goes on
    budget.reserve_llm_calls(2)
    for _ in range(6):
        budget.charge_llm()
    with pytest.raises(LimitExceeded):
        budget.charge_llm()  # the last 2 calls are reserved
    budget.reserve_llm_calls(0)
    budget.charge_llm()
    assert budget.llm_calls_left() == 1
    clock[0] = 150.0
    with pytest.raises(LimitExceeded) as err:
        budget.check_time()
    assert err.value.user_message.startswith("The report took longer than 150 s.")
