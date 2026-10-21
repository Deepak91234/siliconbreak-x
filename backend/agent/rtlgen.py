"""cl_core RTL generation and repair.

* `generate_core_rtl`: with an LLM provider the model writes cl_core from the
  validated spec; every candidate passes `rtl.safety.check_core_source` before it
  is accepted (safety findings are fed back for at most `MAX_CORRECTION_ROUNDS`
  correction rounds). If the LLM fails or keeps producing unsafe code, the
  preverified template generator is used instead and the result is labelled
  `template/<family>` with an explanation saying why. Without a provider the
  template generator is used directly.
* `repair_core_rtl`: LLM-only root-cause repair from verification diagnostics;
  without a provider it raises `RepairUnavailable`.

The returned `safety` list always comes from running the checker on the exact
source being returned. Generated Verilog is only ever written to files by the
caller; it never reaches a shell or argv.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel

from backend.agent import prompts
from backend.agent.llm.base import LLMError, LLMProvider
from backend.rtl.safety import SafetyIssue, check_core_source
from backend.rtl.templates import TemplateUnavailable, generate_core, generator_name
from backend.specifications.ir import DesignSpec

log = logging.getLogger(__name__)

MAX_CORRECTION_ROUNDS = 2
RTL_MAX_TOKENS = 32000


class RtlGeneration(BaseModel):
    verilog: str
    generator: str
    explanation: str
    safety: list[dict]


class RepairUnavailable(Exception):
    """Automatic repair needs an LLM provider."""


# LLM-facing output models
class RtlDraft(BaseModel):
    verilog: str
    explanation: str


class RepairDraft(BaseModel):
    root_cause: str
    fix_summary: str
    verilog: str


_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```\s*$", re.S)


def clean_verilog(src: str) -> str:
    """Strip a surrounding markdown fence (a common formatting slip) and normalise line endings."""
    src = (src or "").replace("\r\n", "\n").replace("\r", "\n")
    m = _FENCE.match(src)
    if m:
        src = m.group(1)
    return src.strip() + "\n"


def _issues(verilog: str, spec: DesignSpec) -> list[SafetyIssue]:
    return check_core_source(verilog, spec)


def _errors(issues: list[SafetyIssue]) -> list[str]:
    return [f"{i.code}: {i.message}" + (f" (line {i.line})" if i.line else "") for i in issues
            if i.severity == "error"]


def _dump(issues: list[SafetyIssue]) -> list[dict]:
    return [i.model_dump() for i in issues]


def template_generation(spec: DesignSpec, note: str | None = None) -> RtlGeneration:
    """The preverified template generator, checked by the same safety checker as LLM output."""
    try:
        verilog = generate_core(spec)
    except TemplateUnavailable as e:
        msg = f"The template generator cannot implement this spec: {e}"
        return RtlGeneration(verilog="", generator=generator_name(spec), explanation=(f"{note} " if note else "") + msg,
                             safety=[{"severity": "error", "code": "template-unavailable", "message": msg,
                                      "line": None}])
    explanation = (f"Preverified {spec.family} template generator (deterministic, parameterised by the spec's "
                   "TICKS_* timing parameters).")
    if note:
        explanation = f"{note} {explanation}"
    return RtlGeneration(verilog=verilog, generator=generator_name(spec), explanation=explanation,
                         safety=_dump(_issues(verilog, spec)))


async def _llm_loop(spec: DesignSpec, provider: LLMProvider, first_user: str, output_model: type[BaseModel],
                    purpose: str) -> tuple[BaseModel | None, str, list[SafetyIssue], str]:
    """Ask for cl_core and run correction rounds on safety errors.

    Returns (last draft or None, model name, safety issues of the last draft, cleaned verilog).
    LLMError propagates except for invalid_output, which gets a correction round.
    """
    system = prompts.rtl_system_prompt(spec)
    messages: list[dict] = [{"role": "user", "content": first_user}]
    model = provider.model
    draft: BaseModel | None = None
    issues: list[SafetyIssue] = []
    verilog = ""
    for round_no in range(MAX_CORRECTION_ROUNDS + 1):
        try:
            res = await provider.structured(purpose=purpose if round_no == 0 else "rtl-correction", system=system,
                                            messages=messages, output_model=output_model, max_tokens=RTL_MAX_TOKENS)
        except LLMError as e:
            if e.kind != "invalid_output" or round_no == MAX_CORRECTION_ROUNDS:
                if draft is not None:
                    return draft, model, issues, verilog
                raise
            if e.raw_text and e.raw_text.strip():
                messages = messages + [{"role": "assistant", "content": e.raw_text},
                                       {"role": "user", "content": prompts.rtl_correction_message(
                                           [e.message] + list(e.details.get("errors", []))[:20])}]
            continue
        model = res.model or model
        draft = res.value
        assert draft is not None
        verilog = clean_verilog(getattr(draft, "verilog"))
        issues = _issues(verilog, spec)
        errs = _errors(issues)
        if not errs:
            return draft, model, issues, verilog
        log.info("LLM cl_core rejected by safety checker (round %d): %d errors", round_no, len(errs))
        if round_no < MAX_CORRECTION_ROUNDS:
            messages = messages + [{"role": "assistant", "content": res.text or draft.model_dump_json()},
                                   {"role": "user", "content": prompts.rtl_correction_message(errs)}]
    return draft, model, issues, verilog


async def generate_core_rtl(spec: DesignSpec, provider: LLMProvider | None, previous_verilog: str | None = None,
                            change_request: str | None = None) -> RtlGeneration:
    if provider is None:
        return template_generation(spec)
    user = prompts.rtl_user_message(spec, previous_verilog=previous_verilog, change_request=change_request)
    try:
        draft, model, issues, verilog = await _llm_loop(spec, provider, user, RtlDraft, "rtl")
    except LLMError as e:
        log.warning("LLM RTL generation failed (%s); using the template generator", e.kind)
        return template_generation(spec, note=f"LLM RTL generation failed ({e.kind}: {e.message[:200]}); "
                                              "used the preverified template instead.")
    errs = _errors(issues)
    if draft is not None and not errs:
        return RtlGeneration(verilog=verilog, generator=f"llm:{model}",
                             explanation=getattr(draft, "explanation", "").strip(), safety=_dump(issues))
    fallback = template_generation(
        spec, note=f"The LLM-written cl_core still had {len(errs)} safety error(s) after {MAX_CORRECTION_ROUNDS} "
                   f"correction rounds ({'; '.join(errs[:3])}); used the preverified template instead.")
    if fallback.verilog:
        return fallback
    # No template either: return the LLM candidate with its real safety findings (the pipeline rejects it).
    return RtlGeneration(verilog=verilog, generator=f"llm:{model}",
                         explanation=getattr(draft, "explanation", "").strip() if draft is not None else "",
                         safety=_dump(issues))


async def repair_core_rtl(spec: DesignSpec, verilog: str, diagnostics: dict, attempt: int,
                          provider: LLMProvider | None) -> RtlGeneration:
    if provider is None:
        raise RepairUnavailable("automatic repair needs an LLM provider (set ANTHROPIC_API_KEY)")
    user = prompts.repair_user_message(spec, verilog, diagnostics or {}, attempt)
    draft, model, issues, new_verilog = await _llm_loop(spec, provider, user, RepairDraft, "repair")
    assert draft is not None
    root = getattr(draft, "root_cause", "").strip()
    fix = getattr(draft, "fix_summary", "").strip()
    explanation = f"Root cause: {root}\nFix: {fix}" if root or fix else ""
    return RtlGeneration(verilog=new_verilog, generator=f"llm:{model}", explanation=explanation,
                         safety=_dump(issues))
