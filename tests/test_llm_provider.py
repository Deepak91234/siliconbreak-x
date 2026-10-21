"""LLM provider plumbing (Anthropic request shape / error mapping with a fake SDK client) and
the LLM intent path with a scripted fake provider. No network access."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from backend.agent.intent import IntentDraft, interpret_request
from backend.agent.llm import get_provider
from backend.agent.llm.anthropic_provider import AnthropicProvider
from backend.agent.llm.base import LLMError, LLMProvider, parse_structured_text
from backend.agent.rtlgen import RtlDraft
from backend.core.config import get_settings
from backend.specifications.ir import DesignSpec
from tests.fake_llm import FakeProvider, llm_error

ROOT = Path(__file__).resolve().parents[1]
DEMO1 = DesignSpec.model_validate_json((ROOT / "examples/demo1_counter_spec.json").read_text())
DEMO2 = DesignSpec.model_validate_json((ROOT / "examples/demo2_counter_down_reset_spec.json").read_text())
SECRET = "sk-ant-test-0123456789abcdef"


# ------------------------------------------------------------------ provider selection

def test_get_provider_none_without_key():
    s = dataclasses.replace(get_settings(), anthropic_api_key=None)
    assert get_provider(s) is None


def test_get_provider_with_key_builds_anthropic_provider():
    s = dataclasses.replace(get_settings(), anthropic_api_key=SECRET, llm_model="claude-opus-5-5")
    p = get_provider(s)
    assert isinstance(p, AnthropicProvider) and isinstance(p, LLMProvider)
    assert p.model == "claude-opus-5-5"
    assert SECRET not in repr(p)


# ------------------------------------------------------------------ Anthropic request / response handling

class _FakeMessages:
    def __init__(self, outcome):
        self.outcome = outcome
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class _FakeClient:
    def __init__(self, outcome):
        self.messages = _FakeMessages(outcome)
        self.beta = SimpleNamespace(messages=_FakeMessages(outcome))


def _resp(text: str, stop: str = "end_turn", **extra):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
                           model="claude-opus-5-5", usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                           _request_id="req_1", **extra)


def _provider(outcome, **kw) -> tuple[AnthropicProvider, _FakeClient]:
    client = _FakeClient(outcome)
    return AnthropicProvider(SECRET, "claude-opus-5-5", client=client, **kw), client


def test_build_request_shape():
    p, _ = _provider(None)
    kw = p.build_request(purpose="rtl", system="sys", messages=[{"role": "user", "content": "hi"}],
                         output_model=RtlDraft, max_tokens=1000)
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"]["effort"] == "high"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert "verilog" in kw["output_config"]["format"]["schema"]["properties"]
    for banned in ("thinking", "temperature", "top_p", "top_k"):
        assert banned not in kw
    with pytest.raises(ValueError):
        p.build_request(purpose="rtl", system="s", messages=[{"role": "assistant", "content": "x"}],
                        output_model=RtlDraft, max_tokens=10)


def test_intent_draft_schema_is_transformable():
    schema = anthropic.transform_schema(IntentDraft)
    assert schema["type"] == "object" and "kind" in schema["properties"]


async def test_structured_success_uses_beta_with_fallbacks():
    p, client = _provider(_resp('{"verilog": "module cl_core; endmodule", "explanation": "e"}'))
    res = await p.structured(purpose="rtl", system="s", messages=[{"role": "user", "content": "go"}],
                             output_model=RtlDraft)
    assert res.value.explanation == "e" and res.model == "claude-opus-5-5" and res.request_id == "req_1"
    assert client.beta.messages.kwargs["fallbacks"] == "default"
    assert client.messages.kwargs is None


async def test_structured_without_fallbacks_uses_plain_messages():
    p, client = _provider(_resp('{"verilog": "v", "explanation": "e"}'), fallbacks=False)
    await p.structured(purpose="rtl", system="s", messages=[{"role": "user", "content": "go"}], output_model=RtlDraft)
    assert client.messages.kwargs is not None and "fallbacks" not in client.messages.kwargs


async def test_refusal_is_classified_before_parsing():
    details = SimpleNamespace(category="cyber", explanation="declined", recommended_model=None)
    p, _ = _provider(_resp("", stop="refusal", stop_details=details))
    with pytest.raises(LLMError) as ei:
        await p.structured(purpose="rtl", system="s", messages=[{"role": "user", "content": "go"}],
                           output_model=RtlDraft)
    assert ei.value.kind == "refusal"


async def test_max_tokens_is_invalid_output_with_raw_text():
    p, _ = _provider(_resp('{"verilog": "mod', stop="max_tokens"))
    with pytest.raises(LLMError) as ei:
        await p.structured(purpose="rtl", system="s", messages=[{"role": "user", "content": "go"}],
                           output_model=RtlDraft)
    assert ei.value.kind == "invalid_output" and ei.value.raw_text == '{"verilog": "mod'


def _status_error(cls, code: int):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(code, request=req, headers={"request-id": "req_x", "retry-after": "7"})
    return cls(f"bad key {SECRET}", response=resp, body=None)


@pytest.mark.parametrize("cls,code,kind", [
    (anthropic.AuthenticationError, 401, "auth"),
    (anthropic.RateLimitError, 429, "rate_limit"),
    (anthropic.InternalServerError, 500, "api"),
])
async def test_status_errors_are_mapped_and_key_scrubbed(cls, code, kind):
    p, _ = _provider(_status_error(cls, code))
    with pytest.raises(LLMError) as ei:
        await p.structured(purpose="rtl", system="s", messages=[{"role": "user", "content": "go"}],
                           output_model=RtlDraft)
    e = ei.value
    assert e.kind == kind and e.status_code == code and e.request_id == "req_x"
    assert SECRET not in str(e) and SECRET not in repr(e.as_dict())
    if kind == "rate_limit":
        assert e.retry_after_s == 7.0


async def test_connection_error_is_network():
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    p, _ = _provider(err)
    with pytest.raises(LLMError) as ei:
        await p.structured(purpose="rtl", system="s", messages=[{"role": "user", "content": "go"}],
                           output_model=RtlDraft)
    assert ei.value.kind == "network"


def test_parse_structured_text_errors():
    with pytest.raises(LLMError) as ei:
        parse_structured_text("not json", RtlDraft)
    assert ei.value.kind == "invalid_output"
    with pytest.raises(LLMError) as ei:
        parse_structured_text('{"verilog": 1}', RtlDraft)
    assert ei.value.kind == "invalid_output" and ei.value.details["errors"]


# ------------------------------------------------------------------ LLM intent path (fake provider)

def _flat_behavior(spec: DesignSpec) -> dict:
    b = spec.behavior.model_dump()
    flat = {k: None for k in ("output", "direction", "direction_control", "reset_input", "reset_value",
                              "initial_value", "enable_input", "wrap", "pattern", "request_input",
                              "request_after", "request_state")}
    flat.update({"rate_modes": [], "source_inputs": [], "states": []})
    flat.update(b)
    return flat


def draft_for(spec: DesignSpec, kind: str = "new_design", **over) -> dict:
    d = spec.model_dump()
    spec_draft = {"title": d["title"], "summary": d["summary"],
                  "inputs": d["inputs"],
                  "outputs": [{"name": o["name"], "indices": o["indices"], "description": o["description"]}
                              for o in d["outputs"]],
                  "behavior": _flat_behavior(spec), "assumptions": d["assumptions"]}
    out = {"kind": kind, "spec": spec_draft, "message": "ok", "change_summary": [], "assumptions": [],
           "unsupported_reasons": []}
    out.update(over)
    return out


async def test_llm_intent_new_design():
    fake = FakeProvider([draft_for(DEMO1, message="A 4-bit counter.")])
    r = await interpret_request("Build a 4-bit counter...", None, fake)
    assert r.kind == "new_design" and r.interpreter == "llm:fake-model"
    assert r.spec is not None and r.spec.behavior == DEMO1.behavior
    assert fake.calls[0]["purpose"] == "intent"
    assert "<user_request>" in fake.calls[0]["messages"][0]["content"]


async def test_llm_intent_modify_computes_change_summary():
    fake = FakeProvider([draft_for(DEMO2, kind="modify")])
    r = await interpret_request("Change the counter so it counts backward, and use BTNC to reset it.", DEMO1, fake)
    assert r.kind == "modify" and r.spec.behavior.direction == "down"
    assert "direction: up -> down" in r.change_summary
    assert "<current_spec>" in fake.calls[0]["messages"][0]["content"]


async def test_llm_intent_invalid_spec_gets_correction_round():
    bad = draft_for(DEMO1)
    bad["spec"]["behavior"]["reset_value"] = 99  # does not fit 4 bits -> validate_spec error
    bad["spec"]["behavior"]["reset_input"] = None
    fake = FakeProvider([bad, draft_for(DEMO1)])
    r = await interpret_request("Build a 4-bit counter", None, fake)
    assert r.kind == "new_design" and r.spec is not None
    second = fake.calls[1]
    assert second["purpose"] == "intent-correction"
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]
    assert "value-range" in second["messages"][-1]["content"]


async def test_llm_intent_structural_error_gets_correction_round():
    bad = draft_for(DEMO1)
    bad["spec"]["behavior"]["direction"] = "left"  # not valid for a counter
    fake = FakeProvider([bad, draft_for(DEMO1)])
    r = await interpret_request("Build a 4-bit counter", None, fake)
    assert r.kind == "new_design"
    assert "behavior.direction" in fake.calls[1]["messages"][-1]["content"]


async def test_llm_intent_gives_up_after_two_corrections():
    bad = draft_for(DEMO1)
    bad["spec"]["outputs"][0]["indices"] = [0, 1, 2, 99]
    fake = FakeProvider([bad, bad, bad])
    r = await interpret_request("Build a 4-bit counter", None, fake)
    assert len(fake.calls) == 3
    assert r.kind == "clarify" and r.spec is None and r.unsupported_reasons


async def test_llm_intent_unsupported():
    fake = FakeProvider([{"kind": "unsupported", "spec": None, "message": "VGA is not supported.",
                          "change_summary": [], "assumptions": [], "unsupported_reasons": ["VGA output"]}])
    r = await interpret_request("Draw on VGA", None, fake)
    assert r.kind == "unsupported" and r.spec is None and r.unsupported_reasons == ["VGA output"]


async def test_llm_network_failure_falls_back_to_offline_rules():
    fake = FakeProvider([llm_error("network", "unreachable")])
    r = await interpret_request("Build a 4-bit counter shown on LEDs 0-3. It should count at one increment per "
                                "second normally and three increments per second when I hold BTNU.", None, fake)
    assert r.interpreter == "offline-rules" and r.kind == "new_design"
    assert "LLM request failed" in r.message


async def test_llm_refusal_is_reported_unsupported():
    fake = FakeProvider([llm_error("refusal", "declined")])
    r = await interpret_request("Build a counter", None, fake)
    assert r.kind == "unsupported" and r.spec is None and r.unsupported_reasons == ["declined"]
