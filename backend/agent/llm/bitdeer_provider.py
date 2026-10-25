"""Bitdeer Model Studio implementation of the LLMProvider protocol.

Bitdeer serves chat completions in the common OpenAI-compatible shape at
https://api-inference.bitdeer.ai/v1 (Bearer key). This adapter talks to it with
httpx directly (no extra SDK):

  * Structured output: asks for `response_format: json_schema` first; if the
    server rejects that parameter it retries once with `json_object`. The schema
    is always also stated in the system prompt, and the reply is validated
    locally with the same `parse_structured_text` every provider uses, so a
    model that ignores the format still cannot push unvalidated data onward.
  * Reasoning models may wrap JSON in <think> blocks or ``` fences; those are
    stripped before validation (the raw text is kept for diagnostics).
  * The API key lives only in this object; it is scrubbed from every error.
  * HTTP 402 (no credit) is reported as an `api` error with that explanation,
    so the caller can show an honest degraded mode.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from .base import _THINK_RE, LLMError, LLMResult, T, clean_json_text, first_json_object, parse_structured_text  # noqa: F401 (clean_json_text re-exported)

log = logging.getLogger(__name__)

PROVIDER_NAME = "bitdeer"
DEFAULT_TIMEOUT_S = 600.0

class BitdeerProvider:
    name = PROVIDER_NAME

    def __init__(self, api_key: str, model: str, *, base_url: str = "https://api-inference.bitdeer.ai/v1",
                 timeout_s: float = DEFAULT_TIMEOUT_S, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not api_key:
            raise ValueError("BitdeerProvider needs an API key")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._secret = api_key
        self._transport = transport

    def __repr__(self) -> str:
        return f"BitdeerProvider(model={self.model!r}, base_url={self.base_url!r})"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(self.timeout_s, connect=10.0),
                                 headers={"Authorization": f"Bearer {self._secret}"}, transport=self._transport)

    def _scrub(self, text: str) -> str:
        return text.replace(self._secret, "***") if self._secret and text else text

    async def list_models(self) -> list[str]:
        """GET /models (OpenAI-compatible). Raises LLMError when unavailable."""
        try:
            async with self._client() as c:
                r = await c.get("/models")
        except httpx.HTTPError as e:
            raise LLMError("network", self._scrub(f"could not reach Bitdeer: {e}"), provider=self.name) from None
        if r.status_code != 200:
            raise self._status_error(r)
        data = r.json()
        return sorted(str(m.get("id")) for m in data.get("data", []) if isinstance(m, dict) and m.get("id"))

    def build_request(self, *, system: str, messages: list[dict], output_model: type[T], max_tokens: int,
                      fmt: str = "json_schema") -> dict:
        schema = output_model.model_json_schema()
        sys_text = (f"{system}\n\nRespond with a single JSON object that validates against this JSON Schema "
                    f"(no prose, no code fences):\n{json.dumps(schema)}")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": sys_text}, *messages],
            "max_tokens": max_tokens,
            "temperature": 0,
            # discourages the sentence loops seen from some models at temperature 0
            "frequency_penalty": 0.3,
        }
        if fmt == "json_schema":
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": output_model.__name__, "schema": schema}}
        elif fmt == "json_object":
            body["response_format"] = {"type": "json_object"}
        return body

    async def structured(self, *, purpose: str, system: str, messages: list[dict], output_model: type[T],
                         max_tokens: int = 16000) -> LLMResult[T]:
        if not messages or messages[-1].get("role") != "user":
            raise ValueError("messages must end with a user turn")
        t0 = time.monotonic()
        resp = None
        for fmt in ("json_schema", "json_object", "none"):
            body = self.build_request(system=system, messages=messages, output_model=output_model,
                                      max_tokens=max_tokens, fmt=fmt)
            try:
                async with self._client() as c:
                    resp = await c.post("/chat/completions", json=body)
            except httpx.TimeoutException as e:
                raise LLMError("network", self._scrub(f"request to Bitdeer timed out: {e}"), provider=self.name,
                               model=self.model) from None
            except httpx.HTTPError as e:
                raise LLMError("network", self._scrub(f"could not reach Bitdeer: {e}"), provider=self.name,
                               model=self.model) from None
            if resp.status_code == 400 and "response_format" in resp.text and fmt != "none":
                log.info("Bitdeer rejected response_format=%s; retrying with a weaker format", fmt)
                continue
            break
        assert resp is not None
        if resp.status_code != 200:
            raise self._status_error(resp)
        data = resp.json()
        duration = time.monotonic() - t0
        request_id = resp.headers.get("x-request-id") or data.get("id")
        served = data.get("model") or self.model
        choice = (data.get("choices") or [{}])[0]
        finish = choice.get("finish_reason")
        raw = ((choice.get("message") or {}).get("content")) or ""
        usage = data.get("usage") or {}
        log.info("LLM call: provider=bitdeer purpose=%s model=%s finish=%s duration=%.1fs usage=%s",
                 purpose, served, finish, duration, usage)
        if finish == "length":
            # Some models finish a valid answer and then run on (e.g. repeating a sentence) until max_tokens.
            # A complete JSON object at the start that validates against the schema is the model's real answer.
            obj = first_json_object(_THINK_RE.sub("", raw))
            if obj is not None:
                try:
                    value = parse_structured_text(obj, output_model, provider=self.name, model=served,
                                                  request_id=request_id)
                except LLMError:
                    value = None
                if value is not None:
                    log.warning("Bitdeer output hit max_tokens after a complete answer; using the first JSON object")
                    return LLMResult[output_model](  # type: ignore[valid-type]
                        value=value, text=obj, model=served, provider=self.name, stop_reason="length_salvaged",
                        usage=usage, duration_s=round(duration, 3), request_id=request_id)
            raise LLMError("invalid_output", f"output was truncated (finish_reason=length, max_tokens={max_tokens})",
                           provider=self.name, model=served, request_id=request_id, raw_text=raw,
                           details={"usage": usage})
        if finish == "content_filter":
            raise LLMError("refusal", "the provider's content filter stopped the response", provider=self.name,
                           model=served, request_id=request_id)
        text = clean_json_text(raw)
        try:
            value = parse_structured_text(text, output_model, provider=self.name, model=served, request_id=request_id)
        except LLMError as e:
            e.details = {**e.details, "usage": usage}   # billed even though unusable: keep the real usage
            raise
        return LLMResult[output_model](  # type: ignore[valid-type]
            value=value, text=text, model=served, provider=self.name, stop_reason=finish, usage=usage,
            duration_s=round(duration, 3), request_id=request_id)

    def _status_error(self, r: httpx.Response) -> LLMError:
        try:
            detail = r.json()
            msg = (detail.get("error") or {}).get("message") if isinstance(detail.get("error"), dict) \
                else detail.get("error") or detail.get("message") or r.text
        except ValueError:
            msg = r.text
        msg = self._scrub(str(msg)[:500])
        common = {"provider": self.name, "model": self.model, "status_code": r.status_code,
                  "request_id": r.headers.get("x-request-id")}
        if r.status_code in (401, 403):
            return LLMError("auth", f"Bitdeer rejected the API key ({r.status_code}): {msg}", **common)
        if r.status_code == 402:
            return LLMError("api", f"Bitdeer reports insufficient credit (402): {msg}", **common)
        if r.status_code == 429:
            return LLMError("rate_limit", f"Bitdeer rate limit reached: {msg}", **common)
        return LLMError("api", f"Bitdeer API error {r.status_code}: {msg}", **common)
