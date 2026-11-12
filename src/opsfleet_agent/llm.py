"""Gemini client with timeouts, retries, a fallback model and circuit breakers (specs 00 and 36).

Without a timeout and a retry limit, a test call hung on 2026-11-11, so both are mandatory here.
The SDK retries are off (`HttpRetryOptions(attempts=1)`). Only the policy of spec 36 retries:

1. The budget is charged once for the logical call (the turn limit of 8 calls). Each retry and the
   fallback attempt count in the daily ledger, because the free tier counts each request.
2. The main model gets 1 attempt and at most `models.max_retries` retries for a temporary failure.
   Each attempt has the timeout `min(models.timeout_s, time left − 2 s)`, and no attempt starts
   with less than `resilience.min_time_left_s` left in the turn.
3. After a temporary failure, or when the breaker of the main model is open, the fallback model
   (`models.fallback`) gets 1 attempt with the same request and the same JSON schema.
4. A breaker for each model: 3 temporary failures in a row stop the calls to that model for 60 s.
   A daily-quota error stops them until midnight Pacific time.

The `llm` span gets the events `retry` and `circuit_open`, and the attributes `model` (the model
that answered) and `fallback_used`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from google import genai
from google.genai import types
from opentelemetry.trace import Span
from pydantic import BaseModel

from opsfleet_agent.config import ModelSettings, ResilienceSettings
from opsfleet_agent.errors import AgentError, ErrorType, LimitExceeded
from opsfleet_agent.limits import TurnBudget, next_reset
from opsfleet_agent.resilience.breaker import BreakerBoard
from opsfleet_agent.resilience.errors_map import (
    daily_quota_failure,
    gemini_unavailable,
    map_gemini_error,
    refused,
)
from opsfleet_agent.resilience.policies import (
    RetryHooks,
    RetryPolicy,
    ServiceFailure,
    run_with_retries,
)
from opsfleet_agent.tracing import span

MIN_TIMEOUT_S = 2.0
TIMEOUT_MARGIN_S = 2.0
RESET_TIMEZONE = "America/Los_Angeles"
# The detail of the error when the structured output does not match the schema. The route node
# (spec 31) retries once on this error.
SCHEMA_MISMATCH = "structured output did not match the schema"
SAFETY_FINISH_REASONS = frozenset(
    {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY"}
)


@dataclass(frozen=True)
class LlmResponse:
    text: str
    parsed: Any  # an instance of the schema model, when a schema was given
    tokens_in: int
    tokens_out: int
    model: str  # the model that answered
    fallback_used: bool = False


def classify_llm_error(exc: BaseException) -> AgentError:
    """Turn an SDK or network exception into an AgentError with one error type (spec 36)."""
    return map_gemini_error(exc)


def _name(value: Any) -> str:
    return str(getattr(value, "name", value) or "")


def is_safety_block(response: Any) -> bool:
    """The prompt or the answer was blocked: a block reason, or a candidate that ended for a
    safety reason without text."""
    feedback = getattr(response, "prompt_feedback", None)
    if feedback is not None and getattr(feedback, "block_reason", None):
        return True
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return False
    reasons = {_name(getattr(c, "finish_reason", "")) for c in candidates}
    return bool(reasons & SAFETY_FINISH_REASONS)


class GeminiClient:
    def __init__(
        self,
        settings: ModelSettings,
        api_key: str | None,
        client: Any | None = None,
        *,
        resilience: ResilienceSettings | None = None,
        breakers: BreakerBoard | None = None,
        hooks: RetryHooks | None = None,
        reset_timezone: str = RESET_TIMEZONE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._api_key = api_key
        # Created once, on the first call, so tests and --help need no key. The client must stay
        # referenced: a genai.Client closes its HTTP connection when Python collects it.
        self._client = client
        self._resilience = resilience or ResilienceSettings()
        self._clock = clock or (lambda: datetime.now(UTC))
        self.breakers = breakers or BreakerBoard(
            self._resilience.breaker_threshold,
            self._resilience.breaker_open_s,
            self._clock,
        )
        self.hooks = hooks or RetryHooks()
        self._reset_timezone = reset_timezone
        self.policy = RetryPolicy(
            retries=settings.max_retries,
            waits_s=tuple(self._resilience.retry_waits_s),
            jitter=self._resilience.retry_jitter,
            retry_after_max_s=self._resilience.retry_after_max_s,
        )

    @property
    def model(self) -> str:
        return self._settings.default

    @property
    def fallback_model(self) -> str | None:
        fallback = self._settings.fallback
        return fallback if fallback and fallback != self._settings.default else None

    def _get_client(self) -> Any:
        if self._client is None:
            if not self._api_key:
                raise AgentError(
                    ErrorType.LLM_ERROR,
                    "The Gemini API key is not set (GOOGLE_API_KEY in .env).",
                )
            self._client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(
                    timeout=int(self._settings.timeout_s * 1000),
                    retry_options=sdk_retry_options(),
                ),
            )
        return self._client

    def generate(
        self,
        budget: TurnBudget,
        *,
        system: str,
        prompt: str,
        schema: type[BaseModel] | None = None,
        purpose: str = "generate",
        temperature: float = 0.0,
        prompt_version: str | None = None,
        allow_fallback: bool = True,
    ) -> LlmResponse:
        """One logical model call. The budget is charged first. The timeout never exceeds the
        time left in the turn."""
        budget.check_time_left(self._resilience.min_time_left_s)
        budget.charge_llm()
        main = self._settings.default
        fallback = self.fallback_model if allow_fallback else None
        request = _Request(system, prompt, schema, temperature)
        with span(
            f"llm.{purpose}",
            "llm",
            model=main,
            purpose=purpose,
            prompt_version=prompt_version,
        ) as sp:
            sp.set_attribute("fallback_used", False)
            charged = [False]  # the first attempt is in charge_llm()

            def charge() -> None:
                if charged[0]:
                    budget.check_time_left(self._resilience.min_time_left_s)
                    budget.charge_attempt()
                charged[0] = True

            failure: ServiceFailure | None = None
            circuit_open = False
            main_breaker = self.breakers.get(f"gemini:{main}")
            if main_breaker.allow():
                try:
                    response = self._guarded(
                        main_breaker,
                        lambda: self._with_retries(main, request, budget, sp, charge),
                    )
                except ServiceFailure as exc:
                    if not exc.temporary or fallback is None:
                        raise self._final(exc, None) from exc
                    failure = exc
                else:
                    return self._done(sp, budget, response, fallback_used=False)
            else:
                circuit_open = True
                sp.add_event("circuit_open", {"breaker": main_breaker.name})
                if main_breaker.reason == "daily_quota":
                    failure = daily_quota_failure()
                if fallback is None:
                    raise gemini_unavailable(failure, circuit_open=True)

            assert fallback is not None
            fb_breaker = self.breakers.get(f"gemini:{fallback}")
            if not fb_breaker.allow():
                sp.add_event("circuit_open", {"breaker": fb_breaker.name})
                raise gemini_unavailable(failure, circuit_open=True)
            sp.add_event("fallback", {"model": fallback})

            def fallback_attempt() -> LlmResponse:
                charge()
                try:
                    return self._attempt(fallback, request, budget)
                except AgentError:
                    raise
                except Exception as exc:
                    raise map_gemini_error(exc) from exc

            try:
                response = self._guarded(fb_breaker, fallback_attempt)
            except ServiceFailure as exc:
                raise self._final(exc, failure, circuit_open) from exc
            return self._done(sp, budget, response, fallback_used=True)

    # --- helpers ---------------------------------------------------------------------------------
    def _with_retries(
        self,
        model: str,
        request: _Request,
        budget: TurnBudget,
        sp: Span,
        charge: Callable[[], None],
    ) -> Any:
        min_left = self._resilience.min_time_left_s

        def on_retry(retry_no: int, failure: ServiceFailure, wait: float) -> None:
            budget.check_time_left(min_left + wait)  # the wait and the attempt must fit
            sp.add_event(
                "retry",
                {
                    "attempt": retry_no + 1,
                    "error_type": failure.error_type.value,
                    "kind": failure.kind,
                    "wait_s": round(wait, 2),
                },
            )

        return run_with_retries(
            lambda _n: self._attempt(model, request, budget),
            policy=self.policy,
            classify=map_gemini_error,
            before_attempt=lambda _n: charge(),
            on_retry=on_retry,
            hooks=self.hooks,
        )

    def _attempt(self, model: str, request: _Request, budget: TurnBudget) -> Any:
        """One request to one model. The timeout of this request is never more than the time
        left in the turn."""
        timeout_s = min(
            self._settings.timeout_s,
            max(MIN_TIMEOUT_S, budget.remaining_seconds() - TIMEOUT_MARGIN_S),
        )
        config = types.GenerateContentConfig(
            system_instruction=request.system,
            temperature=request.temperature,
            http_options=types.HttpOptions(
                timeout=int(timeout_s * 1000), retry_options=sdk_retry_options()
            ),
        )
        if request.schema is not None:
            config.response_mime_type = "application/json"
            config.response_schema = request.schema
        response = self._get_client().models.generate_content(
            model=model, contents=request.prompt, config=config
        )
        if is_safety_block(response) and not (getattr(response, "text", "") or ""):
            raise refused()
        usage = getattr(response, "usage_metadata", None)
        tokens_in = int(getattr(usage, "prompt_token_count", 0) or 0)
        tokens_out = int(getattr(usage, "candidates_token_count", 0) or 0)
        budget.record_tokens(tokens_in, tokens_out)
        parsed = (
            getattr(response, "parsed", None) if request.schema is not None else None
        )
        if request.schema is not None and not isinstance(parsed, request.schema):
            raise AgentError(
                ErrorType.LLM_ERROR,
                "The language model returned an answer in a wrong format.",
                SCHEMA_MISMATCH,
            )
        return LlmResponse(
            text=response.text or "",
            parsed=parsed,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
        )

    def _guarded(self, breaker: Any, call: Callable[[], LlmResponse]) -> LlmResponse:
        """Run the call and update the breaker. A limit of the turn or Ctrl-C says nothing
        about the service, so the breaker only lets the next call be the trial."""
        try:
            response = call()
        except LimitExceeded:
            breaker.release()
            raise
        except ServiceFailure as exc:
            self._record(breaker, exc)
            raise
        except AgentError:
            breaker.record_success()  # the service answered, for example in a wrong format
            raise
        except BaseException:
            breaker.release()
            raise
        breaker.record_success()
        return response

    def _record(self, breaker: Any, failure: ServiceFailure) -> None:
        if failure.daily_quota:
            breaker.record_failure(
                until=next_reset(self._clock(), self._reset_timezone)
            )
        elif failure.temporary:
            breaker.record_failure()
        else:
            breaker.record_success()  # the service answered: a 400 is not an outage

    @staticmethod
    def _final(
        failure: ServiceFailure,
        main_failure: ServiceFailure | None,
        circuit_open: bool = False,
    ) -> ServiceFailure:
        if main_failure is None:
            if failure.temporary or failure.daily_quota:
                return gemini_unavailable(failure, circuit_open)
            return failure
        if not failure.temporary:
            return failure  # the fallback model refused the request itself (400, key, safety)
        return gemini_unavailable(failure, circuit_open)

    @staticmethod
    def _done(
        sp: Span, budget: TurnBudget, response: LlmResponse, fallback_used: bool
    ) -> LlmResponse:
        sp.set_attribute("model", response.model)
        sp.set_attribute("fallback_used", fallback_used)
        sp.set_attribute("tokens_in", response.tokens_in)
        sp.set_attribute("tokens_out", response.tokens_out)
        if fallback_used:
            budget.fallback_calls += 1
            return LlmResponse(
                text=response.text,
                parsed=response.parsed,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                model=response.model,
                fallback_used=True,
            )
        return response


@dataclass(frozen=True)
class _Request:
    system: str
    prompt: str
    schema: type[BaseModel] | None
    temperature: float


def sdk_retry_options() -> types.HttpRetryOptions:
    """Spec 36, B2.2: the SDK makes one attempt. Only the policy of this module retries."""
    return types.HttpRetryOptions(attempts=1)
