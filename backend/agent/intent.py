"""Natural-language request -> DesignSpec (or an honest refusal / question / answer).

Two interpreters share one result type:

* LLM path (`provider` given): the model answers with a deliberately *simplified*
  LLM-facing draft (`IntentDraft`: a flat behaviour object with every family field
  present, nullable where it does not apply). The draft is converted into the
  strict `DesignSpec` IR and run through `validate_spec`; structural or
  validation errors are fed back for at most `MAX_CORRECTION_ROUNDS` correction
  rounds. A spec is only ever returned after both checks pass.
* Offline rules (`provider` None, or the LLM call failed for a transport/API
  reason): the deterministic parser in `offline_parser`.

Nothing in here executes or forwards user text anywhere except into the LLM
prompt (wrapped as data) and the regex-based offline parser.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel, ValidationError

from backend.agent import prompts
from backend.agent.llm.base import LLMError, LLMProvider, format_validation_errors
from backend.agent.offline_parser import interpret_offline
from backend.specifications.ir import DesignSpec
from backend.specifications.validate import validate_spec

log = logging.getLogger(__name__)

OFFLINE_INTERPRETER = "offline-rules"
MAX_CORRECTION_ROUNDS = 2
MAX_REQUEST_CHARS = 8000
INTENT_MAX_TOKENS = 32000

IntentKind = Literal["new_design", "modify", "question", "unsupported", "clarify"]


class IntentResult(BaseModel):
    kind: IntentKind
    spec: Optional[DesignSpec] = None
    message: str = ""
    change_summary: list[str] = []
    assumptions: list[str] = []
    unsupported_reasons: list[str] = []
    interpreter: str = OFFLINE_INTERPRETER


# ============================================================================ LLM-facing draft models
# Flat, schema-friendly shapes (no discriminated unions, no custom validators).
# Every field is required so the JSON schema is unambiguous; fields that do not
# apply to the chosen family are null (or [] for lists).


class InputDraft(BaseModel):
    name: str
    port: Literal["btnC", "btnU", "btnL", "btnR", "btnD", "sw"]
    index: Optional[int]
    debounce_ms: float
    description: str


class OutputDraft(BaseModel):
    name: str
    indices: list[int]
    description: str


class RateModeDraft(BaseModel):
    name: str
    hz: float
    when_input: Optional[str]
    when_level: Literal[0, 1]


class DirectionControlDraft(BaseModel):
    input: str
    high_means: Literal["up", "down"]


class TrafficStateDraft(BaseModel):
    name: str
    duration_s: float
    lights: int


class BehaviorDraft(BaseModel):
    family: Literal["counter", "blinker", "led_pattern", "traffic_light"]
    output: str
    direction: Optional[Literal["up", "down", "left", "right"]]
    direction_control: Optional[DirectionControlDraft]
    rate_modes: list[RateModeDraft]
    reset_input: Optional[str]
    reset_value: Optional[int]
    initial_value: Optional[int]
    enable_input: Optional[str]
    wrap: Optional[bool]
    pattern: Optional[Literal["together", "alternate", "mirror_inputs", "invert_inputs", "chase", "bounce", "fill"]]
    source_inputs: list[str]
    states: list[TrafficStateDraft]
    request_input: Optional[str]
    request_after: Optional[str]
    request_state: Optional[TrafficStateDraft]


class SpecDraft(BaseModel):
    title: str
    summary: str
    inputs: list[InputDraft]
    outputs: list[OutputDraft]
    behavior: BehaviorDraft
    assumptions: list[str]


class IntentDraft(BaseModel):
    kind: IntentKind
    spec: Optional[SpecDraft]
    message: str
    change_summary: list[str]
    assumptions: list[str]
    unsupported_reasons: list[str]


_FAMILY_FIELDS: dict[str, tuple[str, ...]] = {
    "counter": ("output", "direction", "direction_control", "rate_modes", "reset_input", "reset_value",
                "initial_value", "enable_input", "wrap"),
    "blinker": ("output", "pattern", "rate_modes", "enable_input"),
    "led_pattern": ("output", "pattern", "source_inputs", "direction", "direction_control", "rate_modes",
                    "enable_input", "reset_input"),
    "traffic_light": ("output", "states", "request_input", "request_after", "request_state", "reset_input"),
}
_DIRECTIONS = {"counter": ("up", "down"), "led_pattern": ("left", "right")}
_PATTERNS = {"blinker": ("together", "alternate"),
             "led_pattern": ("mirror_inputs", "invert_inputs", "chase", "bounce", "fill")}


class DraftError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def draft_to_spec(draft: SpecDraft) -> DesignSpec:
    """Convert the flat LLM draft into the strict IR; raises DraftError with fixable messages."""
    b = draft.behavior
    fam = b.family
    problems: list[str] = []
    raw = b.model_dump()
    behavior: dict[str, Any] = {"family": fam}
    for f in _FAMILY_FIELDS[fam]:
        v = raw.get(f)
        if v is None or (isinstance(v, list) and not v and f in ("source_inputs",)):
            continue  # IR default applies
        behavior[f] = v
    if "direction" in behavior and behavior["direction"] not in _DIRECTIONS.get(fam, ()):
        problems.append(f"behavior.direction: '{behavior['direction']}' is not valid for family '{fam}' "
                        f"(use one of {list(_DIRECTIONS.get(fam, ()))})")
    if fam in _PATTERNS:
        if b.pattern is None:
            if fam == "led_pattern":
                problems.append("behavior.pattern: required for led_pattern")
        elif b.pattern not in _PATTERNS[fam]:
            problems.append(f"behavior.pattern: '{b.pattern}' is not valid for family '{fam}' "
                            f"(use one of {list(_PATTERNS[fam])})")
    if problems:
        raise DraftError(problems)
    data = {
        "title": draft.title,
        "summary": draft.summary,
        "inputs": [{k: v for k, v in i.model_dump().items()} for i in draft.inputs],
        "outputs": [{"name": o.name, "port": "led", "indices": o.indices, "description": o.description}
                    for o in draft.outputs],
        "behavior": behavior,
        "assumptions": list(draft.assumptions),
    }
    try:
        return DesignSpec.model_validate(data)
    except ValidationError as e:
        raise DraftError(format_validation_errors(e)) from None


# ============================================================================ change summary


def _sig(i: Any) -> str:
    return f"{i.port}[{i.index}]" if i.port == "sw" else i.port


def summarize_changes(old: DesignSpec, new: DesignSpec) -> list[str]:
    """Concrete, human-readable differences between two specs (used for 'modify')."""
    out: list[str] = []
    if old.family != new.family:
        return [f"family: {old.family} -> {new.family} (new behaviour)"]
    old_in = {i.name: i for i in old.inputs}
    new_in = {i.name: i for i in new.inputs}
    for n, i in new_in.items():
        if n not in old_in:
            out.append(f"added input {n} ({_sig(i)})")
        elif _sig(old_in[n]) != _sig(i):
            out.append(f"input {n}: {_sig(old_in[n])} -> {_sig(i)}")
    for n, i in old_in.items():
        if n not in new_in:
            out.append(f"removed input {n} ({_sig(i)})")
    old_out = {o.name: o for o in old.outputs}
    for o in new.outputs:
        if o.name not in old_out:
            out.append(f"added output {o.name} on LEDs {o.indices}")
        elif old_out[o.name].indices != o.indices:
            out.append(f"output {o.name}: LEDs {old_out[o.name].indices} -> {o.indices}")
    for n in old_out:
        if n not in {o.name for o in new.outputs}:
            out.append(f"removed output {n}")
    ob, nb = old.behavior.model_dump(), new.behavior.model_dump()
    for k in nb:
        if k == "family" or ob.get(k) == nb[k]:
            continue
        if k == "rate_modes":
            def fmt(ms: list[dict]) -> str:
                return ", ".join(f"{m['name']} {m['hz']:g} Hz" + (f" when {m['when_input']}={m['when_level']}"
                                                                  if m["when_input"] else "") for m in ms) or "none"
            out.append(f"rate modes: {fmt(ob.get(k) or [])} -> {fmt(nb[k])}")
        else:
            def val(v: Any) -> str:
                return "none" if v is None or v == [] else str(v)
            out.append(f"{k}: {val(ob.get(k))} -> {val(nb[k])}")
    return out


# ============================================================================ entry point


def _offline(text: str, current: DesignSpec | None, note: str | None = None) -> IntentResult:
    r = interpret_offline(text, current)
    change_summary = list(r.change_summary)
    if r.kind == "modify" and r.spec is not None and current is not None and not change_summary:
        change_summary = summarize_changes(current, r.spec)
    message = r.message
    if note:
        message = f"{note} {message}".strip()
    return IntentResult(kind=r.kind, spec=r.spec, message=message, change_summary=change_summary,
                        assumptions=list(r.assumptions), unsupported_reasons=list(r.unsupported_reasons),
                        interpreter=OFFLINE_INTERPRETER)


async def interpret_request(text: str, current: DesignSpec | None, provider: LLMProvider | None) -> IntentResult:
    text = (text or "").strip()
    if not text:
        return IntentResult(kind="clarify", message="The request is empty. Describe the circuit you want.",
                            interpreter=OFFLINE_INTERPRETER if provider is None else f"llm:{provider.model}")
    if len(text) > MAX_REQUEST_CHARS:
        return IntentResult(kind="clarify", message=f"The request is too long ({len(text)} characters; the limit is "
                            f"{MAX_REQUEST_CHARS}). Please describe the circuit more briefly.",
                            interpreter=OFFLINE_INTERPRETER if provider is None else f"llm:{provider.model}")
    if provider is None:
        return _offline(text, current)
    try:
        return await _interpret_llm(text, current, provider)
    except LLMError as e:
        if e.kind == "refusal":
            return IntentResult(kind="unsupported", message="The language model declined to interpret this request.",
                                unsupported_reasons=[e.message], interpreter=f"llm:{e.model or provider.model}")
        log.warning("LLM intent interpretation failed (%s); falling back to offline rules", e.kind)
        return _offline(text, current, note=f"The LLM request failed ({e.kind}: {e.message[:200]}); "
                                            "used the offline rules instead.")


async def _interpret_llm(text: str, current: DesignSpec | None, provider: LLMProvider) -> IntentResult:
    messages: list[dict] = [{"role": "user", "content": prompts.intent_user_message(text, current)}]
    model = provider.model
    problems: list[str] = []
    for round_no in range(MAX_CORRECTION_ROUNDS + 1):
        purpose = "intent" if round_no == 0 else "intent-correction"
        try:
            res = await provider.structured(purpose=purpose, system=prompts.INTENT_SYSTEM, messages=messages,
                                            output_model=IntentDraft, max_tokens=INTENT_MAX_TOKENS)
        except LLMError as e:
            # Schema-invalid output gets a correction round like any other invalid answer.
            if e.kind != "invalid_output" or round_no == MAX_CORRECTION_ROUNDS:
                raise
            problems = [e.message] + list(e.details.get("errors", []))[:20]
            raw = e.raw_text if e.raw_text and e.raw_text.strip() else None
            if raw:
                messages = messages + [{"role": "assistant", "content": raw}]
                messages.append({"role": "user", "content": prompts.intent_correction_message(problems)})
            continue
        model = res.model or model
        interpreter = f"llm:{model}"
        d = res.value
        assert d is not None
        kind = d.kind
        if kind in ("question", "unsupported", "clarify"):
            reasons = list(d.unsupported_reasons)
            if kind == "unsupported" and not reasons:
                reasons = [d.message] if d.message else ["the request is outside what CircuitLoop can build"]
            return IntentResult(kind=kind, spec=None, message=d.message, assumptions=list(d.assumptions),
                                unsupported_reasons=reasons, interpreter=interpreter)
        if kind == "modify" and current is None:
            kind = "new_design"
        problems = []
        spec: DesignSpec | None = None
        if d.spec is None:
            problems = [f"kind '{d.kind}' requires a complete spec, but spec was null"]
        else:
            try:
                spec = draft_to_spec(d.spec)
            except DraftError as e:
                problems = e.problems
        if spec is not None:
            report = validate_spec(spec)
            problems = [f"{i.code}: {i.message}" + (f" (at {i.path})" if i.path else "")
                        for i in report.issues if i.severity == "error"]
        if not problems and spec is not None:
            assumptions = list(dict.fromkeys(list(d.assumptions) + list(spec.assumptions)))
            change_summary = list(d.change_summary)
            if kind == "modify" and current is not None and not change_summary:
                change_summary = summarize_changes(current, spec)
            return IntentResult(kind=kind, spec=spec, message=d.message, change_summary=change_summary,
                                assumptions=assumptions, interpreter=interpreter)
        log.info("intent draft rejected (round %d): %d problems", round_no, len(problems))
        if round_no < MAX_CORRECTION_ROUNDS:
            messages = messages + [{"role": "assistant", "content": res.text or d.model_dump_json()}]
            messages.append({"role": "user", "content": prompts.intent_correction_message(problems)})
    return IntentResult(
        kind="clarify", spec=None, interpreter=f"llm:{model}", unsupported_reasons=problems[:10],
        message=(f"The language model could not produce a valid specification after {MAX_CORRECTION_ROUNDS} "
                 "correction rounds. Please rephrase or simplify the request."))
