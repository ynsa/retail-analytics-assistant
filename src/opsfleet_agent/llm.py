"""Gemini client with a timeout, a fixed retry count, the budget check and a span (spec 00).

Without a timeout and a retry limit, a test call hung on 2026-11-11, so both are mandatory here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

from opsfleet_agent.config import ModelSettings
from opsfleet_agent.errors import AgentError, ErrorType
from opsfleet_agent.limits import TurnBudget
from opsfleet_agent.tracing import span

RETRY_STATUS_CODES = [429, 500, 502, 503, 504]
MIN_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class LlmResponse:
    text: str
    parsed: Any  # an instance of the schema model, when a schema was given
    tokens_in: int
    tokens_out: int
    model: str


def _is_timeout(exc: BaseException) -> bool:
    return any("Timeout" in cls.__name__ for cls in type(exc).__mro__)


def classify_llm_error(exc: BaseException) -> AgentError:
    """Turn an SDK or network exception into an AgentError with one error type."""
    if isinstance(exc, AgentError):
        return exc
    if _is_timeout(exc):
        return AgentError(
            ErrorType.LLM_TIMEOUT,
            "The language model did not answer in time.",
            str(exc),
        )
    if isinstance(exc, genai_errors.APIError) and exc.code == 429:
        return AgentError(
            ErrorType.LLM_RATE_LIMITED,
            "The language model is busy (rate limit). Please try again in a minute.",
            str(exc),
        )
    return AgentError(
        ErrorType.LLM_ERROR,
        "The language model returned an error.",
        f"{type(exc).__name__}: {exc}",
    )


class GeminiClient:
    def __init__(
        self, settings: ModelSettings, api_key: str | None, client: Any | None = None
    ) -> None:
        self._settings = settings
        self._api_key = api_key
        # Created once, on the first call, so tests and --help need no key. The client must stay
        # referenced: a genai.Client closes its HTTP connection when Python collects it.
        self._client = client

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
                    retry_options=types.HttpRetryOptions(
                        attempts=self._settings.max_retries + 1,
                        http_status_codes=RETRY_STATUS_CODES,
                    ),
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
    ) -> LlmResponse:
        """One model call. The budget is charged first. The timeout never exceeds the time left in the turn."""
        budget.charge_llm()
        timeout_s = min(
            self._settings.timeout_s, max(MIN_TIMEOUT_S, budget.remaining_seconds())
        )
        model = self._settings.default
        # The timeout of this one request: never more than the time left in the turn.
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),
        )
        if schema is not None:
            config.response_mime_type = "application/json"
            config.response_schema = schema
        with span(f"llm.{purpose}", "llm", model=model, purpose=purpose) as sp:
            try:
                response = self._get_client().models.generate_content(
                    model=model, contents=prompt, config=config
                )
            except Exception as exc:
                raise classify_llm_error(exc) from exc
            usage = getattr(response, "usage_metadata", None)
            tokens_in = int(getattr(usage, "prompt_token_count", 0) or 0)
            tokens_out = int(getattr(usage, "candidates_token_count", 0) or 0)
            budget.record_tokens(tokens_in, tokens_out)
            sp.set_attribute("tokens_in", tokens_in)
            sp.set_attribute("tokens_out", tokens_out)
            parsed = getattr(response, "parsed", None) if schema is not None else None
            if schema is not None and not isinstance(parsed, schema):
                raise AgentError(
                    ErrorType.LLM_ERROR,
                    "The language model returned an answer in a wrong format.",
                    "structured output did not match the schema",
                )
            return LlmResponse(
                text=response.text or "",
                parsed=parsed,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                model=model,
            )
