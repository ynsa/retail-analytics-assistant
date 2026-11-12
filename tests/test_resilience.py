"""Resilience (spec 36, B4 cases 1-25). Fake clients, a fake clock, and no real wait.

The `sleeps` fixture (conftest) collects the retry waits instead of a real `time.sleep`.
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest
from google.api_core import exceptions as gexc
from google.genai import errors as genai_errors
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from opsfleet_agent.bq import BigQueryService
from opsfleet_agent.errors import AgentError, ErrorType, LimitExceeded
from opsfleet_agent.limits import DailyLedger, TurnBudget
from opsfleet_agent.llm import GeminiClient
from opsfleet_agent.resilience import messages
from opsfleet_agent.resilience.breaker import BreakerBoard, CircuitBreaker
from opsfleet_agent.resilience.errors_map import map_bq_error, map_gemini_error
from opsfleet_agent.resilience.policies import RetryHooks, ServiceFailure
from opsfleet_agent.store.db import retry_locked
from tests.conftest import (
    FakeBQ,
    FakeLLM,
    frame_result,
    read_spans,
    route_reply,
    sql_reply,
)

MAIN = "gemini-3.5-flash-lite"
FALLBACK = "gemini-3.1-flash-lite"
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)  # 02:00 in Los Angeles
T_ITEMS = "`bigquery-public-data.thelook_ecommerce.order_items`"
ORDERS_SQL = f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {T_ITEMS} oi"
DOWN = messages.text("llm_down")


class Out(BaseModel):
    sql: str


# --- fakes ---------------------------------------------------------------------------------------


def ok(text: str = "ok", tokens: int = 10):
    usage = SimpleNamespace(prompt_token_count=tokens, candidates_token_count=2)
    return SimpleNamespace(text=text, parsed=None, usage_metadata=usage)


def server_error(code: int = 503) -> genai_errors.ServerError:
    return genai_errors.ServerError(code, {"error": {"message": "unavailable"}})


def client_error(
    code: int, message: str = "bad", details=None
) -> genai_errors.APIError:
    body = {"error": {"code": code, "message": message, "details": details or []}}
    return genai_errors.ClientError(code, body)


def rate_limited(delay: str | None = None, daily: bool = False):
    details = []
    if delay:
        details.append(
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}
        )
    if daily:
        details.append(
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                ],
            }
        )
    return client_error(429, "Resource exhausted", details)


class ScriptedModels:
    """`generate_content` returns or raises the next item of the script of each model."""

    def __init__(self, scripts: dict[str, list]):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: list[str] = []
        self.configs: list = []

    def generate_content(self, *, model, contents, config):
        self.calls.append(model)
        self.configs.append(config)
        item = self.scripts[model].pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def gemini(config, scripts, clock, *, fallback=FALLBACK, retries=2):
    models = config.settings.models.model_copy(
        update={"default": MAIN, "fallback": fallback, "max_retries": retries}
    )
    sdk = ScriptedModels(scripts)
    client = GeminiClient(
        models,
        api_key=None,
        client=SimpleNamespace(models=sdk),
        resilience=config.settings.resilience,
        hooks=RetryHooks(rand=lambda: 0.0),
        clock=clock,
    )
    return client, sdk


def call(client, budget, **kwargs):
    return client.generate(budget, system="s", prompt="p", purpose="test", **kwargs)


# --- B4 1-9: Gemini retries and the fallback model ----------------------------------------------


def test_1_gemini_503_then_success_waits_about_one_second(
    config, budget, clock, sleeps
):
    client, sdk = gemini(config, {MAIN: [server_error(), ok()]}, clock)
    result = call(client, budget)
    assert result.text == "ok" and not result.fallback_used
    assert sdk.calls == [MAIN, MAIN] and sleeps == [1.0]


def test_retry_wait_has_a_random_part_of_up_to_50_percent(
    config, budget, clock, sleeps
):
    client, _ = gemini(config, {MAIN: [server_error(), server_error(), ok()]}, clock)
    client.hooks = RetryHooks(rand=lambda: 1.0)
    call(client, budget)
    assert sleeps == [1.5, 3.0]


def test_2_retry_after_of_4_seconds_replaces_the_wait(config, budget, clock, sleeps):
    client, sdk = gemini(config, {MAIN: [rate_limited("4s"), ok()]}, clock)
    call(client, budget)
    assert sdk.calls == [MAIN, MAIN] and sleeps == [4.0]


def test_retry_after_header_is_read():
    response = SimpleNamespace(headers={"retry-after": "7"})
    exc = genai_errors.ClientError(429, {"error": {"message": "slow"}}, response)
    assert map_gemini_error(exc).retry_after == 7.0


def test_3_retry_after_of_30_seconds_goes_to_the_fallback_at_once(
    config, budget, clock, sleeps
):
    client, sdk = gemini(
        config, {MAIN: [rate_limited("30s")], FALLBACK: [ok("fb")]}, clock
    )
    result = call(client, budget)
    assert sdk.calls == [MAIN, FALLBACK] and sleeps == []
    assert result.fallback_used and result.model == FALLBACK and result.text == "fb"


def test_4_daily_quota_no_retry_and_breaker_open_until_midnight(
    config, budget, clock, sleeps
):
    """Without a fallback model, a daily-quota 429 ends the turn with the daily message."""
    client, sdk = gemini(
        config, {MAIN: [rate_limited("40s", daily=True)]}, clock, fallback=None
    )
    with pytest.raises(AgentError) as err:
        call(client, budget)
    assert err.value.error_type is ErrorType.LLM_RATE_LIMITED
    assert err.value.user_message == messages.text("llm_daily")
    assert sdk.calls == [MAIN] and sleeps == []
    breaker = client.breakers.get(f"gemini:{MAIN}")
    assert breaker.state == "open" and breaker.reason == "daily_quota"
    # 10:00 UTC on 12 Nov is 02:00 in Los Angeles: the quota resets at 08:00 UTC on 13 Nov.
    assert breaker.open_until == datetime(2026, 11, 13, 8, 0, tzinfo=UTC)


# Owner decision (2026-11-12, spec 36 B7): after a daily-quota error of the main model, the same
# turn tries the fallback model. "Try again tomorrow" shows only when the fallback also fails.


def test_daily_quota_tries_the_fallback_in_the_same_turn(config, budget, clock, sleeps):
    client, sdk = gemini(
        config, {MAIN: [rate_limited("40s", daily=True)], FALLBACK: [ok("fb")]}, clock
    )
    result = call(client, budget)
    assert result.fallback_used and result.model == FALLBACK and result.text == "fb"
    assert sdk.calls == [MAIN, FALLBACK] and sleeps == []
    assert budget.totals()["fallback_calls"] == 1
    breaker = client.breakers.get(f"gemini:{MAIN}")
    assert breaker.state == "open" and breaker.reason == "daily_quota"


@pytest.mark.parametrize(
    "fallback_error",
    [server_error(), rate_limited(daily=True), rate_limited("1s")],
    ids=["server", "daily", "busy"],
)
def test_daily_quota_and_failed_fallback_give_the_daily_message(
    config, budget, clock, fallback_error
):
    client, sdk = gemini(
        config,
        {MAIN: [rate_limited(daily=True)], FALLBACK: [fallback_error]},
        clock,
    )
    with pytest.raises(AgentError) as err:
        call(client, budget)
    assert err.value.error_type is ErrorType.LLM_RATE_LIMITED
    assert err.value.user_message == messages.text("llm_daily")
    assert sdk.calls == [MAIN, FALLBACK]


def test_daily_quota_and_a_refusing_fallback_keep_the_refusal(config, budget, clock):
    """The fallback refused the request itself (a 400): that message is more useful."""
    client, sdk = gemini(
        config,
        {MAIN: [rate_limited(daily=True)], FALLBACK: [client_error(400)]},
        clock,
    )
    with pytest.raises(AgentError) as err:
        call(client, budget)
    assert err.value.user_message != messages.text("llm_daily")
    assert sdk.calls == [MAIN, FALLBACK]


def test_after_a_daily_quota_the_next_turn_goes_to_the_fallback_directly(
    config, ledger, clock
):
    client, sdk = gemini(
        config,
        {MAIN: [rate_limited(daily=True)], FALLBACK: [ok("fb1"), ok("fb2")]},
        clock,
    )
    first = call(client, TurnBudget(config.settings.limits, ledger))
    second = call(client, TurnBudget(config.settings.limits, ledger))
    assert first.fallback_used and second.fallback_used
    assert sdk.calls == [
        MAIN,
        FALLBACK,
        FALLBACK,
    ]  # the open breaker skips the main model


def test_after_a_daily_quota_both_models_down_give_the_daily_message(
    config, ledger, clock
):
    client, sdk = gemini(
        config,
        {MAIN: [rate_limited(daily=True)], FALLBACK: [server_error(), server_error()]},
        clock,
    )
    for _ in range(2):
        with pytest.raises(AgentError) as err:
            call(client, TurnBudget(config.settings.limits, ledger))
        assert err.value.user_message == messages.text("llm_daily")
    assert sdk.calls == [MAIN, FALLBACK, FALLBACK]


def test_5_main_fails_three_times_and_the_fallback_answers(
    config, budget, clock, sleeps
):
    client, sdk = gemini(
        config,
        {MAIN: [server_error()] * 3, FALLBACK: [ok("fb")]},
        clock,
    )
    result = call(client, budget, schema=None)
    assert result.fallback_used and result.model == FALLBACK
    assert sdk.calls == [MAIN, MAIN, MAIN, FALLBACK] and sleeps == [1.0, 2.0]
    totals = budget.totals()
    assert totals["llm_calls"] == 1  # the turn limit counts the logical call once
    assert totals["llm_attempts"] == 4 and totals["fallback_calls"] == 1


def test_fallback_gets_the_same_request_and_schema(config, budget, clock):
    response = SimpleNamespace(
        text='{"sql": "SELECT 1"}', parsed=Out(sql="SELECT 1"), usage_metadata=None
    )
    client, sdk = gemini(
        config, {MAIN: [server_error()] * 3, FALLBACK: [response]}, clock
    )
    result = call(client, budget, schema=Out)
    assert result.parsed.sql == "SELECT 1"
    main_config, fb_config = sdk.configs[0], sdk.configs[-1]
    assert fb_config.response_schema is Out is main_config.response_schema
    assert fb_config.system_instruction == main_config.system_instruction == "s"


def test_6_main_and_fallback_fail_gives_the_service_message(
    config, budget, clock, sleeps
):
    client, sdk = gemini(
        config,
        {MAIN: [server_error()] * 3, FALLBACK: [server_error(502)]},
        clock,
    )
    with pytest.raises(AgentError) as err:
        call(client, budget)
    assert err.value.error_type is ErrorType.LLM_ERROR
    assert err.value.user_message == DOWN
    assert "/reports, /open, /export, /undo" in DOWN
    assert sdk.calls == [MAIN, MAIN, MAIN, FALLBACK]


def test_timeout_after_the_fallback_keeps_the_timeout_message(config, budget, clock):
    client, _ = gemini(
        config,
        {MAIN: [httpx.ReadTimeout("slow")] * 3, FALLBACK: [httpx.ReadTimeout("slow")]},
        clock,
    )
    with pytest.raises(AgentError) as err:
        call(client, budget)
    assert err.value.error_type is ErrorType.LLM_TIMEOUT
    assert err.value.user_message == messages.text("llm_timeout")


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (client_error(400, "Invalid argument"), "llm_down"),
        (
            client_error(400, "API key not valid. Please pass a valid API key."),
            "llm_key",
        ),
        (client_error(401, "unauthenticated"), "llm_key"),
        (client_error(402, "billing"), "llm_billing"),
        (client_error(403, "permission denied"), "llm_key"),
    ],
)
def test_7_client_errors_have_no_retry_no_fallback(
    config, budget, clock, sleeps, error, key
):
    client, sdk = gemini(config, {MAIN: [error], FALLBACK: [ok()]}, clock)
    with pytest.raises(AgentError) as err:
        call(client, budget)
    assert err.value.error_type is ErrorType.LLM_ERROR
    assert err.value.user_message == messages.text(key)
    assert sdk.calls == [MAIN] and sleeps == []
    assert client.breakers.get(f"gemini:{MAIN}").state == "closed"


def test_7_safety_block_has_no_retry_no_fallback(config, budget, clock, sleeps):
    blocked = SimpleNamespace(
        text=None,
        parsed=None,
        usage_metadata=None,
        prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
        candidates=[],
    )
    client, sdk = gemini(config, {MAIN: [blocked], FALLBACK: [ok()]}, clock)
    with pytest.raises(AgentError) as err:
        call(client, budget)
    assert err.value.user_message == messages.text("llm_refused")
    assert sdk.calls == [MAIN] and sleeps == []


def test_8_no_attempt_with_less_than_5_seconds_left(config, ledger, clock):
    now = [0.0]
    budget = TurnBudget(config.settings.limits, ledger, monotonic=lambda: now[0])
    now[0] = 56.0  # 4 s left of 60 s
    client, sdk = gemini(config, {MAIN: [ok()]}, clock)
    with pytest.raises(LimitExceeded) as err:
        call(client, budget)
    assert err.value.limit == "per_turn.max_seconds" and sdk.calls == []
    assert budget.llm_calls == 0 and ledger.today()["llm_calls"] == 0


def test_8_no_retry_when_the_wait_does_not_fit_in_the_turn(
    config, ledger, clock, sleeps
):
    now = [0.0]
    budget = TurnBudget(config.settings.limits, ledger, monotonic=lambda: now[0])
    client, sdk = gemini(
        config, {MAIN: [rate_limited("8s"), ok()]}, clock, fallback=None
    )

    def slow_first(**kwargs):
        now[0] = (
            50.0  # the first attempt took 50 s: 10 s left, the wait of 8 s does not fit
        )
        return ScriptedModels.generate_content(sdk, **kwargs)

    client._client = SimpleNamespace(
        models=SimpleNamespace(generate_content=slow_first)
    )
    with pytest.raises(LimitExceeded):
        call(client, budget)
    assert sdk.calls == [MAIN] and sleeps == []


def test_attempt_timeout_is_capped_by_the_time_left(config, ledger, clock):
    now = [0.0]
    budget = TurnBudget(config.settings.limits, ledger, monotonic=lambda: now[0])
    now[0] = 45.0
    client, sdk = gemini(config, {MAIN: [ok()]}, clock)
    call(client, budget)
    assert sdk.configs[0].http_options.timeout == 13_000  # 15 s left minus 2 s


def test_9_each_attempt_counts_in_the_daily_ledger(config, budget, ledger, clock):
    client, _ = gemini(
        config,
        {MAIN: [server_error(), server_error(), server_error()], FALLBACK: [ok()]},
        clock,
    )
    call(client, budget)
    assert ledger.today()["llm_calls"] == 4 and budget.llm_calls == 1


def test_retry_and_fallback_events_in_the_llm_span(config, budget, clock):
    from opsfleet_agent import tracing

    tracing.init_tracing(config.settings.data_path("traces"))
    client, _ = gemini(config, {MAIN: [server_error()] * 3, FALLBACK: [ok()]}, clock)
    call(client, budget)
    spans = read_spans(config)
    llm = next(s for s in spans if s["name"] == "llm.test")
    assert llm["attributes"]["fallback_used"] is True
    assert llm["attributes"]["model"] == FALLBACK


# --- B4 10-13: circuit breakers -----------------------------------------------------------------


def test_breaker_state_machine():
    clock = Clock()
    breaker = CircuitBreaker("x", threshold=3, open_seconds=60, clock=clock)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.state == "closed" and breaker.allow()
    breaker.record_success()
    assert breaker.failures == 0  # a success resets the count
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == "open" and not breaker.allow()
    clock.advance(60)
    assert breaker.state == "half_open"
    assert breaker.allow() and not breaker.allow()  # only one trial call


def test_10_three_failures_open_the_breaker_and_the_fourth_call_has_no_network(
    config, ledger, clock
):
    client, sdk = gemini(
        config, {MAIN: [server_error()] * 3}, clock, fallback=None, retries=0
    )
    for _ in range(3):
        with pytest.raises(AgentError):
            call(client, TurnBudget(config.settings.limits, ledger))
    assert client.breakers.get(f"gemini:{MAIN}").state == "open"
    with pytest.raises(ServiceFailure) as err:
        call(client, TurnBudget(config.settings.limits, ledger))
    assert err.value.circuit_open and err.value.user_message == DOWN
    assert sdk.calls == [MAIN] * 3  # no fourth network call


def test_open_main_breaker_sends_the_call_to_the_fallback(config, ledger, clock):
    client, sdk = gemini(
        config, {MAIN: [server_error()] * 3, FALLBACK: [ok("fb")] * 4}, clock, retries=0
    )
    for _ in range(3):
        call(client, TurnBudget(config.settings.limits, ledger))
    assert client.breakers.get(f"gemini:{MAIN}").state == "open"
    result = call(client, TurnBudget(config.settings.limits, ledger))
    assert result.fallback_used and sdk.calls[-1] == FALLBACK
    assert sdk.calls.count(MAIN) == 3


def test_11_after_60_seconds_a_good_trial_closes_the_breaker(config, ledger, clock):
    client, _ = gemini(
        config, {MAIN: [server_error()] * 3 + [ok()]}, clock, fallback=None, retries=0
    )
    for _ in range(3):
        with pytest.raises(AgentError):
            call(client, TurnBudget(config.settings.limits, ledger))
    clock.advance(60)
    call(client, TurnBudget(config.settings.limits, ledger))
    assert client.breakers.get(f"gemini:{MAIN}").state == "closed"


def test_12_after_60_seconds_a_bad_trial_opens_the_breaker_again(config, ledger, clock):
    client, _ = gemini(
        config, {MAIN: [server_error()] * 4}, clock, fallback=None, retries=0
    )
    for _ in range(4):
        if client.breakers.get(f"gemini:{MAIN}").state == "open":
            clock.advance(60)
        with pytest.raises(AgentError):
            call(client, TurnBudget(config.settings.limits, ledger))
    breaker = client.breakers.get(f"gemini:{MAIN}")
    assert breaker.state == "open" and breaker.open_until == clock.now + timedelta(
        seconds=60
    )


# --- BigQuery fakes ------------------------------------------------------------------------------


class Job:
    def __init__(self, frame=None, error=None):
        self.total_bytes_processed = 1000
        self.total_bytes_billed = 10_485_760
        self.job_id = "job-1"
        self.started = self.ended = None
        self.frame = frame if frame is not None else pd.DataFrame({"n": [1]})
        self.error = error
        self.cancelled = False
        self.result_args: dict = {}

    def result(self, timeout=None, max_results=None, job_retry="default"):
        self.result_args = {"timeout": timeout, "job_retry": job_retry}
        if self.error is not None:
            raise self.error
        return SimpleNamespace(to_dataframe=lambda: self.frame)

    def cancel(self):
        self.cancelled = True


class ScriptedBQ:
    """Dry runs and jobs from two scripts. An exception in a script is raised."""

    def __init__(self, dry: list | None = None, jobs: list | None = None):
        self.dry = list(dry or [])
        self.jobs = list(jobs or [])
        self.calls: list[dict] = []

    def query(
        self,
        sql,
        job_config=None,
        location=None,
        retry=None,
        timeout=None,
        job_retry="x",
    ):
        self.calls.append(
            {
                "dry": job_config.dry_run,
                "retry": retry,
                "timeout": timeout,
                "job_retry": job_retry,
                "config": job_config,
            }
        )
        if job_config.dry_run:
            item = self.dry.pop(0) if self.dry else 1000
            if isinstance(item, BaseException):
                raise item
            return SimpleNamespace(total_bytes_processed=item)
        item = self.jobs.pop(0) if self.jobs else Job()
        if isinstance(item, BaseException):
            raise item
        return item


def bq_service(config, client, clock=None):
    s = config.settings
    breaker = CircuitBreaker("bigquery", 3, 60, clock or Clock())
    return BigQueryService(
        client,
        s.bigquery,
        s.limits.per_query,
        resilience=s.resilience,
        breaker=breaker,
        hooks=RetryHooks(rand=lambda: 0.0),
    )


def backend_error() -> gexc.InternalServerError:
    return gexc.InternalServerError("backend", errors=[{"reason": "backendError"}])


# --- B4 13-19: BigQuery ---------------------------------------------------------------------------


def test_13_sql_errors_do_not_count_in_the_breaker(config, ledger):
    client = ScriptedBQ(dry=[gexc.BadRequest("Syntax error")] * 4)
    service = bq_service(config, client)
    for _ in range(4):
        with pytest.raises(AgentError) as err:
            service.query("SELECT x", TurnBudget(config.settings.limits, ledger))
        assert err.value.error_type is ErrorType.BQ_SYNTAX
    assert service.breaker.state == "closed" and service.breaker.failures == 0


def test_14_backend_error_then_success_makes_two_attempts(config, budget, sleeps):
    client = ScriptedBQ(jobs=[backend_error(), Job()])
    result = bq_service(config, client).query("SELECT 1", budget)
    assert result.row_count == 1
    assert [c["dry"] for c in client.calls] == [True, False, False]
    assert sleeps == [1.0] and budget.bq_queries == 1


def test_14_dry_run_rate_limit_is_retried_once(config, budget, sleeps):
    limited = gexc.Forbidden("rate", errors=[{"reason": "rateLimitExceeded"}])
    client = ScriptedBQ(dry=[limited, limited])
    with pytest.raises(AgentError) as err:
        bq_service(config, client).query("SELECT 1", budget)
    assert err.value.error_type is ErrorType.BQ_ERROR
    assert err.value.user_message == messages.text("bq_down")
    assert len(client.calls) == 2 and sleeps == [1.0]


def test_15_invalid_query_has_no_retry(config, budget, sleeps):
    invalid = gexc.BadRequest("Syntax error", errors=[{"reason": "invalidQuery"}])
    client = ScriptedBQ(dry=[invalid])
    with pytest.raises(AgentError) as err:
        bq_service(config, client).query("SELECT x", budget)
    assert err.value.error_type is ErrorType.BQ_SYNTAX
    assert len(client.calls) == 1 and sleeps == []


def test_16_job_over_30_seconds_is_cancelled(config, budget, sleeps):
    job = Job(error=concurrent.futures.TimeoutError())
    client = ScriptedBQ(jobs=[job])
    with pytest.raises(AgentError) as err:
        bq_service(config, client).query("SELECT 1", budget)
    assert err.value.error_type is ErrorType.BQ_TIMEOUT and job.cancelled
    assert err.value.user_message == messages.text("bq_timeout", seconds="30")
    assert int(client.calls[1]["config"].job_timeout_ms) == 30_000
    assert job.result_args["timeout"] == 30 and sleeps == []


def test_job_timeout_is_capped_by_the_time_left(config, ledger):
    now = [0.0]
    budget = TurnBudget(config.settings.limits, ledger, monotonic=lambda: now[0])
    now[0] = 40.0
    client = ScriptedBQ()
    bq_service(config, client).query("SELECT 1", budget)
    assert int(client.calls[1]["config"].job_timeout_ms) == 20_000


def test_bigquery_breaker_opens_after_three_failures(config, ledger, sleeps):
    client = ScriptedBQ(dry=[backend_error()] * 6)
    service = bq_service(config, client)
    for _ in range(3):
        with pytest.raises(AgentError):
            service.query("SELECT 1", TurnBudget(config.settings.limits, ledger))
    assert service.breaker.state == "open" and not service.available()
    with pytest.raises(ServiceFailure) as err:
        service.query("SELECT 1", TurnBudget(config.settings.limits, ledger))
    assert err.value.circuit_open and len(client.calls) == 6  # no new call


def test_19_client_query_has_no_library_retries(config, budget):
    client = ScriptedBQ()
    bq_service(config, client).query("SELECT 1", budget)
    for c in client.calls:
        assert c["job_retry"] is None
        assert c["retry"].timeout == 10.0
    assert client.calls[0]["timeout"] == 10  # the dry run timeout


def test_20_genai_client_makes_one_attempt(config, budget, monkeypatch):
    import opsfleet_agent.llm as llm_module

    created = []
    sdk = ScriptedModels({MAIN: [ok()]})

    def factory(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(models=sdk)

    monkeypatch.setattr(llm_module.genai, "Client", factory)
    models = config.settings.models.model_copy(update={"default": MAIN})
    GeminiClient(models, api_key="test-key").generate(budget, system="s", prompt="p")
    assert created[0]["http_options"].retry_options.attempts == 1
    assert sdk.configs[0].http_options.retry_options.attempts == 1


# --- B2.5: one message for each error type ------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "error_type", "key"),
    [
        (httpx.ReadTimeout("slow"), ErrorType.LLM_TIMEOUT, "llm_timeout"),
        (rate_limited(), ErrorType.LLM_RATE_LIMITED, "llm_busy"),
        (rate_limited(daily=True), ErrorType.LLM_RATE_LIMITED, "llm_daily"),
        (client_error(401), ErrorType.LLM_ERROR, "llm_key"),
        (client_error(402), ErrorType.LLM_ERROR, "llm_billing"),
        (server_error(), ErrorType.LLM_ERROR, "llm_down"),
        (httpx.ConnectError("no network"), ErrorType.LLM_ERROR, "llm_down"),
    ],
)
def test_gemini_error_messages(exc, error_type, key):
    failure = map_gemini_error(exc)
    assert failure.error_type is error_type and failure.user_message == messages.text(
        key
    )


@pytest.mark.parametrize(
    ("exc", "error_type", "message"),
    [
        (
            gexc.Forbidden("Access Denied", errors=[{"reason": "accessDenied"}]),
            ErrorType.BQ_PERMISSION,
            messages.text("bq_permission"),
        ),
        (
            concurrent.futures.TimeoutError(),
            ErrorType.BQ_TIMEOUT,
            messages.text("bq_timeout", seconds="30"),
        ),
        (backend_error(), ErrorType.BQ_ERROR, messages.text("bq_down")),
        (gexc.ServiceUnavailable("down"), ErrorType.BQ_ERROR, messages.text("bq_down")),
        (
            gexc.BadRequest("Syntax error"),
            ErrorType.BQ_SYNTAX,
            "The query has an error.",
        ),
        (
            gexc.NotFound("Not found: Table"),
            ErrorType.BQ_NOT_FOUND,
            "The query uses a table or a column that does not exist.",
        ),
        (
            gexc.BadRequest("Query exceeded limit for bytes billed: 1000000000."),
            ErrorType.BQ_BYTES_CAP,
            "This query reads too much data. Please narrow the period or the brands.",
        ),
    ],
)
def test_bigquery_error_messages(exc, error_type, message):
    failure = map_bq_error(exc)
    assert failure.error_type is error_type and failure.user_message == message


def test_expired_login_gives_the_gcloud_message():
    from google.auth.exceptions import RefreshError

    failure = map_bq_error(RefreshError("Reauthentication is needed."))
    assert failure.error_type is ErrorType.BQ_PERMISSION
    assert "gcloud auth application-default login" in failure.user_message


def test_failure_messages_end_with_the_trace():
    assert messages.with_trace("The AI service is busy.", "4f2a9c1e00ff") == (
        "The AI service is busy. Trace: 4f2a9c1e."
    )
    assert messages.is_failure("llm_error") and messages.is_failure("internal_error")
    assert not messages.is_failure("limit_exceeded")
    assert not messages.is_failure("guard_policy_block")


# --- B4 17-18: BigQuery down in the graph -------------------------------------------------------


class DownBQ(FakeBQ):
    def available(self) -> bool:
        return False


def test_17_bigquery_breaker_open_data_question_makes_one_model_call(make_session):
    llm = FakeLLM([route_reply(), sql_reply(ORDERS_SQL)])
    session, llm, bq = make_session("maya", llm=llm, bq=DownBQ())
    result = session.ask("How many orders last month?")
    assert result.error_type == "bq_error" and len(llm.calls) == 1
    assert result.answer.startswith(messages.text("bq_down"))
    assert f"Trace: {result.trace_id[:8]}." in result.answer and bq.queries == []


def test_18_bigquery_breaker_open_schema_question_still_works(make_session):
    llm = FakeLLM([route_reply("schema", schema_kind="access")])
    session, _, _ = make_session("maya", llm=llm, bq=DownBQ())
    result = session.ask("Which brands can I see?")
    assert result.error_type is None and "Calvin Klein and Carhartt" in result.answer


def test_report_plan_also_stops_when_bigquery_is_down(make_session):
    llm = FakeLLM([route_reply("report", report_request={"action": "create"})])
    session, llm, _ = make_session("maya", llm=llm, bq=DownBQ())
    result = session.ask("Create a report for Q1 2026")
    assert result.error_type == "bq_error" and len(llm.calls) == 1


# --- B4 21-24: no crash -------------------------------------------------------------------------

NODES = (
    "load_context",
    "route",
    "write_sql",
    "guard",
    "run_query",
    "mask_result",
    "write_answer",
    "scan_output",
    "finish",
)


@pytest.mark.parametrize("failing", NODES)
def test_21_an_exception_in_any_node_gives_a_message_and_the_chat_continues(
    make_session, monkeypatch, failing, config
):
    from opsfleet_agent.agent.nodes import common

    real_span = common.span
    armed = [True]

    def span(name, kind, **attrs):
        if armed[0] and name == f"node.{failing}":
            armed[0] = False
            raise RuntimeError("a bug")
        return real_span(name, kind, **attrs)

    monkeypatch.setattr(common, "span", span)
    llm = FakeLLM(
        [
            route_reply(),
            sql_reply(ORDERS_SQL),
            "There were 1,200 orders.",
            route_reply("chitchat", chitchat_kind="greeting"),
        ]
    )
    bq = FakeBQ([frame_result(pd.DataFrame({"orders": [1200]}))])
    session, _, _ = make_session("ceo", llm=llm, bq=bq)
    first = session.ask("How many orders last month?")
    assert first.error_type == "internal_error"
    assert first.answer.startswith(messages.text("internal"))
    assert f"Trace: {first.trace_id[:8]}." in first.answer
    log = "".join(
        f.read_text() for f in config.settings.data_path("logs").glob("*.jsonl")
    )
    assert "RuntimeError: a bug" in log and first.trace_id in log
    llm.replies = [route_reply("chitchat", chitchat_kind="greeting")]
    assert session.ask("hi").answer.startswith("Hello, Daniel.")


def test_22_graph_recursion_error_gives_the_internal_message(make_session, config):
    resilience = config.settings.resilience.model_copy(
        update={"graph_recursion_limit": 3}
    )
    settings = config.settings.model_copy(update={"resilience": resilience})
    llm = FakeLLM([route_reply(), sql_reply(ORDERS_SQL), "x"])
    bq = FakeBQ([frame_result(pd.DataFrame({"orders": [1]}))])
    session, _, _ = make_session("ceo", llm=llm, bq=bq)
    session.config = session.config.model_copy(update={"settings": settings})
    result = session.ask("How many orders?")
    assert result.error_type == "internal_error" and result.status == "error"
    assert result.answer.startswith(messages.text("internal"))


class InterruptedBQ(FakeBQ):
    def __init__(self):
        super().__init__([KeyboardInterrupt()])
        self.cancelled = 0

    def cancel_running(self) -> bool:
        self.cancelled += 1
        return True


def test_23_ctrl_c_during_the_query_stops_the_turn_and_the_chat_continues(make_session):
    llm = FakeLLM([route_reply(), sql_reply(ORDERS_SQL)])
    bq = InterruptedBQ()
    session, llm, _ = make_session("ceo", llm=llm, bq=bq)
    result = session.ask("How many orders?")
    assert result.answer == "Stopped." and result.status == "cancelled"
    assert bq.cancelled == 1
    llm.replies = [route_reply("chitchat", chitchat_kind="greeting")]
    assert session.ask("hi").answer.startswith("Hello, Daniel.")


class FailingSaver(InMemorySaver):
    def put(self, config, checkpoint, metadata, new_versions):
        raise sqlite3.OperationalError("disk I/O error")


def test_24_a_failed_checkpoint_write_still_shows_the_answer(
    config, conn, persona_file
):
    from opsfleet_agent.agent.context import PromptLibrary
    from opsfleet_agent.agent.state import Services
    from opsfleet_agent.identity import resolve_user
    from opsfleet_agent.session import AssistantSession
    from tests.conftest import load_test_schema, load_test_values

    services = Services(
        settings=config.settings,
        llm=FakeLLM([route_reply("chitchat", chitchat_kind="greeting")]),
        bq=FakeBQ(),
        schema=load_test_schema(),
        values=load_test_values(),
        prompts=PromptLibrary(persona_path=persona_file),
    )
    session = AssistantSession(
        config,
        resolve_user(config.users, "maya"),
        services,
        conn=conn,
        checkpointer=FailingSaver(),
    )
    result = session.ask("hi")
    assert result.answer.startswith("Hello, Maya.")
    assert result.answer.endswith("_This turn is not saved._") and not result.saved


class LockedOnce:
    """A connection whose first `execute` says "database is locked"."""

    def __init__(self, conn):
        self.conn = conn
        self.failed = False

    def __enter__(self):
        return self.conn.__enter__()

    def __exit__(self, *args):
        return self.conn.__exit__(*args)

    def execute(self, *args):
        if not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("database is locked")
        return self.conn.execute(*args)


def test_25_sqlite_locked_once_is_retried_once(config, conn):
    ledger = DailyLedger(LockedOnce(conn), config.settings.limits)  # type: ignore[arg-type]
    ledger.add(llm_calls=1)
    assert DailyLedger(conn, config.settings.limits).today()["llm_calls"] == 1


def test_25_sqlite_locked_twice_raises():
    calls = []

    def locked():
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        retry_locked(locked)
    assert len(calls) == 2


def test_breaker_board_keeps_one_breaker_for_each_name():
    board = BreakerBoard()
    assert board.get("gemini:a") is board.get("gemini:a")
    assert board.get("gemini:a") is not board.get("bigquery")
