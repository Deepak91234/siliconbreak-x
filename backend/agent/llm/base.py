"""Provider-neutral LLM interface used by the CircuitLoop agent.

Every LLM call in CircuitLoop asks for *structured* output: the caller names a
pydantic model and receives a validated instance (or an `LLMError`). Free-form
text never flows into the pipeline, so intent parsing, RTL generation and
repair all go through the same narrow `structured()` entry point. That keeps
the attack surface small (nothing the model says reaches a shell or argv) and
lets the unit tests swap in a fake provider that implements this Protocol.
"""

from __future__ import annotations

import json
import re
from typing import Any, Generic, Literal, Optional, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

T = TypeVar("T", bound=BaseModel)

LLMErrorKind = Literal["auth", "rate_limit", "refusal", "invalid_output", "network", "api"]
LLM_ERROR_KINDS: tuple[str, ...] = ("auth", "rate_limit", "refusal", "invalid_output", "network", "api")


class LLMResult(BaseModel, Generic[T]):
    """One completed structured LLM call.

    `value` is the validated output model; `text` is the raw JSON text it was
    parsed from (kept so a correction round can show the model what it sent).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    value: Optional[T] = None
    text: Optional[str] = None
    model: str
    provider: str
    stop_reason: Optional[str] = None
    usage: dict = {}
    duration_s: float = 0.0
    request_id: Optional[str] = None


class LLMError(Exception):
    """A failed LLM call, classified so callers can react without parsing messages.

    kind:
      auth            bad / missing / unauthorized API key
      rate_limit      throttled (retry_after_s when the server said so)
      refusal         the model declined (explanation from stop_details)
      invalid_output  truncated, unparsable or schema-invalid output (raw_text kept)
      network         connection failure or timeout
      api             any other API-side error (status_code when known)
    """

    def __init__(
        self,
        kind: LLMErrorKind,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
        raw_text: str | None = None,
        retry_after_s: float | None = None,
        details: dict | None = None,
    ) -> None:
        if kind not in LLM_ERROR_KINDS:
            raise ValueError(f"unknown LLMError kind {kind!r}")
        super().__init__(message)
        self.kind: str = kind
        self.message = message
        self.provider = provider
        self.model = model
        self.status_code = status_code
        self.request_id = request_id
        self.raw_text = raw_text
        self.retry_after_s = retry_after_s
        self.details = details or {}

    def __str__(self) -> str:
        return f"[{self.kind}] {self.message}"

    def as_dict(self) -> dict:
        """JSON-safe summary for logs/events (never contains credentials)."""
        return {
            "kind": self.kind,
            "message": self.message,
            "provider": self.provider,
            "model": self.model,
            "status_code": self.status_code,
            "request_id": self.request_id,
            "retry_after_s": self.retry_after_s,
            "details": self.details,
        }


@runtime_checkable
class LLMProvider(Protocol):
    """What the agent needs from an LLM backend.

    `purpose` is a short tag ("intent", "rtl", "repair", ...) that providers may
    use for effort selection and logging; `messages` uses the Anthropic-style
    [{"role": "user"|"assistant", "content": str}, ...] shape and must end with
    a user turn (no assistant prefill).
    """

    name: str
    model: str

    async def structured(
        self,
        *,
        purpose: str,
        system: str,
        messages: list[dict],
        output_model: type[T],
        max_tokens: int = 16000,
    ) -> LLMResult[T]: ...


# ------------------------------------------------------------------ JSON extraction (unconstrained replies)

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def first_json_object(text: str) -> str | None:
    """The first complete top-level JSON object in text (string-aware brace matching), or None."""
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def clean_json_text(text: str) -> str:
    t = _THINK_RE.sub("", text or "").strip()
    m = _FENCE_RE.match(t)
    if m:
        t = m.group(1).strip()
    if t and t[0] not in "{[":
        start = t.find("{")
        end = t.rfind("}")
        if 0 <= start < end:
            t = t[start:end + 1]
    return t



def parse_structured_text(text: str, output_model: type[T], *, provider: str | None = None,
                          model: str | None = None, request_id: str | None = None) -> T:
    """Validate JSON text against `output_model`, mapping failures to invalid_output.

    Shared by real and fake providers so both report malformed output the same
    way (with the raw text attached for a correction round).
    """
    if not text or not text.strip():
        raise LLMError("invalid_output", "the model returned no JSON text", provider=provider, model=model,
                       request_id=request_id, raw_text=text)
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError("invalid_output", f"output is not valid JSON: {e.msg} at position {e.pos}",
                       provider=provider, model=model, request_id=request_id, raw_text=text) from None
    try:
        return output_model.model_validate(data)
    except ValidationError as e:
        raise LLMError("invalid_output", f"output does not match {output_model.__name__}: "
                       + "; ".join(format_validation_errors(e)[:8]),
                       provider=provider, model=model, request_id=request_id, raw_text=text,
                       details={"errors": format_validation_errors(e)}) from None


def format_validation_errors(e: ValidationError) -> list[str]:
    """Compact `path: message` lines suitable for feeding back to the model."""
    out = []
    for err in e.errors(include_url=False):
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        out.append(f"{loc}: {err.get('msg', 'invalid')}")
    return out
