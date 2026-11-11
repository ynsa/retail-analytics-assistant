"""Gemini and BigQuery clients with fake SDK objects: timeouts, error types, caps, spans."""

from __future__ import annotations

import concurrent.futures
import json
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest
from google.api_core import exceptions as gexc
from google.genai import errors as genai_errors
from pydantic import BaseModel

from opsfleet_agent import tracing
from opsfleet_agent.bq import BigQueryService
from opsfleet_agent.errors import AgentError, ErrorType
from opsfleet_agent.llm import GeminiClient, classify_llm_error


class Out(BaseModel):
    sql: str


class FakeModels:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        if isinstance(self.behavior, BaseException):
            raise self.behavior
        return self.behavior


def fake_genai(behavior):
    return SimpleNamespace(models=FakeModels(behavior))


def test_llm_success_records_tokens_and_span(config, budget):
    tracing.init_tracing(config.settings.data_path("traces"))
    usage = SimpleNamespace(prompt_token_count=120, candidates_token_count=30)
    response = SimpleNamespace(
        text='{"sql": "SELECT 1"}', parsed=Out(sql="SELECT 1"), usage_metadata=usage
    )
    client = GeminiClient(
        config.settings.models, api_key=None, client=fake_genai(response)
    )
    out = client.generate(
        budget, system="s", prompt="p", schema=Out, purpose="write_sql"
    )
    assert out.parsed.sql == "SELECT 1" and out.tokens_in == 120
    assert budget.llm_calls == 1 and budget.tokens_out == 30
    lines = [
        json.loads(x)
        for f in config.settings.data_path("traces").glob("*.jsonl")
        for x in f.read_text().splitlines()
    ]
    span = next(r for r in lines if r["name"] == "llm.write_sql")
    assert (
        span["kind"] == "llm"
        and span["attributes"]["tokens_in"] == 120
        and span["status"] == "ok"
    )


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ReadTimeout("slow"), ErrorType.LLM_TIMEOUT),
        (
            genai_errors.ClientError(429, {"error": {"message": "quota"}}),
            ErrorType.LLM_RATE_LIMITED,
        ),
        (
            genai_errors.ServerError(503, {"error": {"message": "down"}}),
            ErrorType.LLM_ERROR,
        ),
        (RuntimeError("boom"), ErrorType.LLM_ERROR),
    ],
)
def test_llm_errors_have_one_type(config, budget, exc, expected):
    client = GeminiClient(config.settings.models, api_key=None, client=fake_genai(exc))
    with pytest.raises(AgentError) as err:
        client.generate(budget, system="s", prompt="p")
    assert err.value.error_type is expected
    assert classify_llm_error(exc).error_type is expected


def test_llm_without_key_gives_clear_error(config, budget):
    client = GeminiClient(config.settings.models, api_key=None)
    with pytest.raises(AgentError, match="GOOGLE_API_KEY"):
        client.generate(budget, system="s", prompt="p")


def test_llm_wrong_structured_output_is_an_error(config, budget):
    response = SimpleNamespace(text="oops", parsed=None, usage_metadata=None)
    client = GeminiClient(
        config.settings.models, api_key=None, client=fake_genai(response)
    )
    with pytest.raises(AgentError) as err:
        client.generate(budget, system="s", prompt="p", schema=Out)
    assert "wrong format" in err.value.user_message


# --- BigQuery ---------------------------------------------------------------------------------


class FakeJob:
    def __init__(self, frame=None, estimate=1000, billed=10_485_760, error=None):
        self.total_bytes_processed = estimate
        self.total_bytes_billed = billed
        self.job_id = "job-1"
        self.started = self.ended = None
        self._frame = frame
        self._error = error
        self.cancelled = False

    def result(self, timeout=None, max_results=None):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(to_dataframe=lambda: self._frame.head(max_results))

    def cancel(self):
        self.cancelled = True


class FakeBQClient:
    def __init__(self, job: FakeJob, dry_estimate: int = 1000, dry_error=None):
        self.job = job
        self.dry_estimate = dry_estimate
        self.dry_error = dry_error
        self.configs = []

    def query(self, sql, job_config=None, location=None, job_retry="default"):
        self.configs.append((job_config, job_retry))
        if job_config.dry_run:
            if self.dry_error is not None:
                raise self.dry_error
            return SimpleNamespace(total_bytes_processed=self.dry_estimate)
        return self.job


def service(config, client):
    s = config.settings
    return BigQueryService(client, s.bigquery, s.limits.per_query)


def test_bq_success_sets_cap_labels_and_no_retry(config, budget):
    frame = pd.DataFrame({"n": [1, 2, 3]})
    client = FakeBQClient(FakeJob(frame))
    result = service(config, client).query(
        "SELECT 1", budget, user_id="maya", purpose="answer"
    )
    assert (
        result.row_count == 3
        and not result.truncated
        and result.bytes_billed == 10_485_760
    )
    job_config, job_retry = client.configs[1]
    assert job_config.maximum_bytes_billed == 1_000_000_000
    assert (
        job_config.labels["user"] == "maya"
        and job_config.labels["app"] == "opsfleet-agent"
    )
    assert job_retry is None
    assert budget.bq_queries == 1 and budget.bytes_billed == 10_485_760


def test_bq_row_cap_truncates(config, budget):
    frame = pd.DataFrame({"n": range(500)})
    result = service(config, FakeBQClient(FakeJob(frame))).query("SELECT 1", budget)
    assert result.row_count == 200 and result.truncated


def test_bq_dry_run_over_cap_blocks_before_the_query(config, budget):
    client = FakeBQClient(FakeJob(pd.DataFrame()), dry_estimate=2_000_000_000)
    with pytest.raises(AgentError) as err:
        service(config, client).query("SELECT 1", budget)
    assert err.value.error_type is ErrorType.BQ_BYTES_CAP
    assert budget.bq_queries == 0  # the free dry run is not counted


def test_bq_timeout_cancels_the_job(config, budget):
    job = FakeJob(error=concurrent.futures.TimeoutError())
    with pytest.raises(AgentError) as err:
        service(config, FakeBQClient(job)).query("SELECT 1", budget)
    assert err.value.error_type is ErrorType.BQ_TIMEOUT and job.cancelled


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (gexc.BadRequest("Syntax error: Unexpected identifier"), ErrorType.BQ_SYNTAX),
        (gexc.NotFound("Table not found"), ErrorType.BQ_NOT_FOUND),
        (gexc.Forbidden("Access Denied"), ErrorType.BQ_PERMISSION),
        (
            gexc.BadRequest("Query exceeded limit for bytes billed: 1000000000."),
            ErrorType.BQ_BYTES_CAP,
        ),
        (gexc.InternalServerError("oops"), ErrorType.BQ_ERROR),
    ],
)
def test_bq_errors_have_one_type(config, budget, exc, expected):
    client = FakeBQClient(FakeJob(pd.DataFrame()), dry_error=exc)
    with pytest.raises(AgentError) as err:
        service(config, client).query("SELECT 1", budget)
    assert err.value.error_type is expected


def test_llm_client_is_created_once_and_kept(config, budget, monkeypatch):
    """Regression: a genai.Client that is not kept closes its connection before the request."""
    import opsfleet_agent.llm as llm_module

    usage = SimpleNamespace(prompt_token_count=1, candidates_token_count=1)
    response = SimpleNamespace(text="ok", parsed=None, usage_metadata=usage)
    created = []

    def factory(**kwargs):
        created.append(kwargs)
        return fake_genai(response)

    monkeypatch.setattr(llm_module.genai, "Client", factory)
    client = GeminiClient(config.settings.models, api_key="test-key")
    client.generate(budget, system="s", prompt="p")
    client.generate(budget, system="s", prompt="p")
    assert len(created) == 1
