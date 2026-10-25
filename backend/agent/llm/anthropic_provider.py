"""Anthropic (Claude) implementation of the LLMProvider protocol.

Request shape (verified against the installed anthropic SDK 1.15.0):
  * `AsyncAnthropic(api_key=...)`; the key lives only inside the SDK client and is
    scrubbed from every error message we produce. It is never logged.
  * Structured output via `output_config={"format": {"type": "json_schema", ...},
    "effort": ...}` using the SDK's own `transform_schema` on the pydantic model.
    We deliberately do *not* use `messages.parse()`: its post-parser validates the
    text while the HTTP response is decoded, i.e. before `stop_reason` can be
    inspected, so a refusal or a max_tokens truncation would surface as an opaque
    pydantic error instead of a classified LLMError.
  * Claude Opus 5.5 always thinks: no `thinking` parameter, no temperature/top_p,
    no assistant prefill (the last message must be a user turn). Depth is set via
    `output_config.effort` ("high" for spec and RTL work).
  * Server-side refusal fallbacks: `fallbacks="default"` with the beta header
    `server-side-fallback-2026-07-01` through `client.beta.messages.create`, which
    in this SDK version accepts both `fallbacks` and `output_config`. It can be
    disabled per provider (`fallbacks=False`) if an account or region rejects it.
  * An explicit request timeout is passed so the SDK does not refuse large
    `max_tokens` values for non-streaming calls.
  * Some schemas compile to a grammar larger than the API accepts (400 "The
    compiled grammar is too large"). Then the request is retried once without
    the constrained format: the schema is stated in the system prompt and the
    reply is validated locally with the same pydantic model, so unvalidated
    data still cannot pass. The provider remembers which output models need
    this so later calls skip the doomed constrained attempt.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import anthropic
import httpx2

from .base import LLMError, LLMResult, T, clean_json_text, parse_structured_text

log = logging.getLogger(__name__)

PROVIDER_NAME = "anthropic"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_TIMEOUT_S = 900.0

# Spec interpretation, RTL generation and repair are all correctness-critical.
EFFORT_BY_PURPOSE = {"intent": "high", "intent-correction": "high", "rtl": "high", "rtl-correction": "high",
                     "repair": "high", "smoke": "low"}
DEFAULT_EFFORT = "high"


class AnthropicProvider:
    """LLMProvider backed by the Anthropic Messages API."""

    name = PROVIDER_NAME

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        client: Any | None = None,
        fallbacks: bool = True,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = 2,
    ) -> None:
        if not api_key:
            raise ValueError("AnthropicProvider needs an API key")
        self.model = model
        self.fallbacks = fallbacks
        self.timeout_s = timeout_s
        self._secret = api_key  # only used to scrub error text; never logged or returned
        self._unconstrained: set[str] = set()  # output models whose schema the API cannot compile
        self._client = client if client is not None else anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=max_retries)

    def __repr__(self) -> str:  # never include the key
        return f"AnthropicProvider(model={self.model!r}, fallbacks={self.fallbacks})"

    # ------------------------------------------------------------------ request
    def build_request(self, *, purpose: str, system: str, messages: list[dict],
                      output_model: type[T], max_tokens: int, constrained: bool = True) -> dict:
        """Keyword arguments for messages.create (exposed for tests and the smoke script).

        `constrained=False` drops the json_schema format and states the schema in the
        system prompt instead (for schemas whose grammar the API refuses to compile).
        """
        if not messages or messages[-1].get("role") != "user":
            raise ValueError("messages must end with a user turn (assistant prefill is not supported)")
        schema = anthropic.transform_schema(output_model)
        output_config: dict[str, Any] = {"effort": EFFORT_BY_PURPOSE.get(purpose, DEFAULT_EFFORT)}
        if constrained:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        else:
            system = (f"{system}\n\nRespond with a single JSON object that validates against this JSON Schema "
                      f"(no prose, no code fences):\n{json.dumps(schema)}")
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_config": output_config,
            "timeout": httpx2.Timeout(self.timeout_s, connect=10.0),
        }
        if self.fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    async def structured(self, *, purpose: str, system: str, messages: list[dict],
                         output_model: type[T], max_tokens: int = 16000) -> LLMResult[T]:
        constrained = output_model.__name__ not in self._unconstrained
        t0 = time.monotonic()
        try:
            resp = await self._create(self.build_request(purpose=purpose, system=system, messages=messages,
                                                         output_model=output_model, max_tokens=max_tokens,
                                                         constrained=constrained))
        except Exception as e:  # mapped most-specific-first below
            err = self.map_error(e)
            if constrained and _schema_too_large(err):
                log.warning("Anthropic cannot compile the %s schema (%s); retrying with the schema in the prompt",
                            output_model.__name__, err.message[:120])
                self._unconstrained.add(output_model.__name__)
                constrained = False
                try:
                    resp = await self._create(self.build_request(purpose=purpose, system=system, messages=messages,
                                                                 output_model=output_model, max_tokens=max_tokens,
                                                                 constrained=False))
                except Exception as e2:
                    err = self.map_error(e2)
                    log.warning("LLM call failed: purpose=%s model=%s kind=%s status=%s request_id=%s",
                                purpose, self.model, err.kind, err.status_code, err.request_id)
                    raise err from None
            else:
                log.warning("LLM call failed: purpose=%s model=%s kind=%s status=%s request_id=%s",
                            purpose, self.model, err.kind, err.status_code, err.request_id)
                raise err from None
        duration = time.monotonic() - t0
        request_id = getattr(resp, "_request_id", None)
        served_by = getattr(resp, "model", None) or self.model
        stop = getattr(resp, "stop_reason", None)
        usage = _usage_dict(getattr(resp, "usage", None))
        fallback_blocks = [b for b in (getattr(resp, "content", None) or []) if getattr(b, "type", None) == "fallback"]
        if fallback_blocks:
            usage["fallback_switches"] = len(fallback_blocks)
        log.info("LLM call: purpose=%s model=%s stop=%s duration=%.1fs request_id=%s usage=%s",
                 purpose, served_by, stop, duration, request_id,
                 {k: usage.get(k) for k in ("input_tokens", "output_tokens")})

        if stop == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            explanation = getattr(details, "explanation", None) if details is not None else None
            raise LLMError("refusal", self._scrub(explanation or "the model declined this request"),
                           provider=self.name, model=served_by, request_id=request_id,
                           details={"category": category,
                                    "recommended_model": getattr(details, "recommended_model", None)})
        if stop in ("max_tokens", "model_context_window_exceeded"):
            raise LLMError("invalid_output", f"output was truncated (stop_reason={stop}, max_tokens={max_tokens})",
                           provider=self.name, model=served_by, request_id=request_id,
                           raw_text=_text_of(resp))
        if stop not in (None, "end_turn", "stop_sequence"):
            raise LLMError("invalid_output", f"unexpected stop_reason {stop!r} for a structured request",
                           provider=self.name, model=served_by, request_id=request_id, raw_text=_text_of(resp))

        text = _text_of(resp) if constrained else clean_json_text(_text_of(resp))
        value = parse_structured_text(text, output_model, provider=self.name, model=served_by, request_id=request_id)
        return LLMResult[output_model](  # type: ignore[valid-type]
            value=value, text=text, model=served_by, provider=self.name, stop_reason=stop,
            usage=usage, duration_s=round(duration, 3), request_id=request_id)

    async def _create(self, kwargs: dict) -> Any:
        if self.fallbacks:
            return await self._client.beta.messages.create(**kwargs)
        return await self._client.messages.create(**kwargs)

    # ------------------------------------------------------------------ errors
    def map_error(self, e: BaseException) -> LLMError:
        """Classify SDK exceptions, most specific class first.

        APITimeoutError subclasses APIConnectionError and every HTTP error
        subclasses APIStatusError, so the order below matters.
        """
        request_id = None
        status = None
        response = getattr(e, "response", None)
        if response is not None:
            request_id = response.headers.get("request-id")
            status = getattr(response, "status_code", None)
        msg = self._scrub(getattr(e, "message", None) or str(e) or type(e).__name__)
        common = {"provider": self.name, "model": self.model, "status_code": status, "request_id": request_id}

        if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            return LLMError("auth", f"Anthropic rejected the API key or its permissions ({status}): {msg}", **common)
        if isinstance(e, anthropic.RateLimitError):
            retry_after = None
            try:
                retry_after = float(response.headers.get("retry-after")) if response is not None else None
            except (TypeError, ValueError):
                retry_after = None
            return LLMError("rate_limit", f"Anthropic rate limit reached: {msg}", retry_after_s=retry_after, **common)
        if isinstance(e, anthropic.APIStatusError):
            return LLMError("api", f"Anthropic API error {status}: {msg}", **common)
        if isinstance(e, anthropic.APITimeoutError):
            return LLMError("network", f"request to Anthropic timed out: {msg}", **common)
        if isinstance(e, anthropic.APIConnectionError):
            return LLMError("network", f"could not reach the Anthropic API: {msg}", **common)
        if isinstance(e, LLMError):
            return e
        return LLMError("api", f"{type(e).__name__}: {msg}", **common)

    def _scrub(self, text: str) -> str:
        if self._secret and text:
            text = text.replace(self._secret, "***")
        return text


def _schema_too_large(err: LLMError) -> bool:
    """400s where the API refuses the json_schema format itself (not the request content)."""
    m = (err.message or "").lower()
    return err.status_code == 400 and ("grammar" in m or ("schema" in m and ("too large" in m or "too complex" in m)))


def _text_of(resp: Any) -> str:
    parts = []
    for block in getattr(resp, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def _usage_dict(usage: Any) -> dict:
    if usage is None:
        return {}
    try:
        return usage.model_dump(mode="json", exclude_none=True)
    except Exception:  # fakes / older SDK objects
        return {k: getattr(usage, k) for k in ("input_tokens", "output_tokens") if hasattr(usage, k)}
