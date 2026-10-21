"""cl_core generation (template + LLM via a fake provider) and repair."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.agent.rtlgen import (MAX_CORRECTION_ROUNDS, RepairUnavailable, RtlGeneration, clean_verilog,
                                  generate_core_rtl, repair_core_rtl)
from backend.rtl.templates import generate_core, generator_name
from backend.specifications.ir import DesignSpec
from tests.fake_llm import FakeProvider, llm_error

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "examples").glob("*.json"))
DEMO1 = DesignSpec.model_validate_json((ROOT / "examples/demo1_counter_spec.json").read_text())
GOOD = generate_core(DEMO1)  # a known-safe cl_core used as the fake model's "answer"
UNSAFE = GOOD.replace("endmodule", 'always @(posedge clk) $display("hi");\nendmodule')


def errs(gen: RtlGeneration) -> list[dict]:
    return [s for s in gen.safety if s["severity"] == "error"]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
async def test_offline_uses_template_and_passes_safety(path):
    spec = DesignSpec.model_validate_json(path.read_text())
    gen = await generate_core_rtl(spec, None)
    assert gen.generator == generator_name(spec)
    assert gen.verilog == generate_core(spec)
    assert "module cl_core" in gen.verilog
    assert errs(gen) == []
    assert gen.explanation


async def test_offline_template_unavailable_reports_error():
    d = json.loads((ROOT / "examples/led_chase_spec.json").read_text())
    d["outputs"][0]["indices"] = [0]
    d["behavior"]["pattern"] = "chase"
    gen = await generate_core_rtl(DesignSpec.model_validate(d), None)
    assert gen.verilog == ""
    assert [s["code"] for s in errs(gen)] == ["template-unavailable"]


async def test_llm_generation_accepted_when_safe():
    fake = FakeProvider([{"verilog": GOOD, "explanation": "counts with a tick divider"}])
    gen = await generate_core_rtl(DEMO1, fake, previous_verilog=GOOD, change_request="make it faster")
    assert gen.generator == "llm:fake-model"
    assert gen.verilog.strip() == GOOD.strip()
    assert gen.explanation == "counts with a tick divider"
    assert errs(gen) == []
    call = fake.calls[0]
    assert call["purpose"] == "rtl"
    assert "cl_core" in call["system"]
    assert "make it faster" in call["messages"][0]["content"]


async def test_llm_markdown_fence_is_stripped():
    fake = FakeProvider([{"verilog": "```verilog\n" + GOOD + "```", "explanation": "x"}])
    gen = await generate_core_rtl(DEMO1, fake)
    assert gen.generator == "llm:fake-model" and not gen.verilog.startswith("```")
    assert clean_verilog("```v\nmodule a;\nendmodule\n```") == "module a;\nendmodule\n"


async def test_llm_safety_errors_trigger_correction_round():
    fake = FakeProvider([{"verilog": UNSAFE, "explanation": "v1"}, {"verilog": GOOD, "explanation": "v2"}])
    gen = await generate_core_rtl(DEMO1, fake)
    assert gen.generator == "llm:fake-model" and gen.explanation == "v2"
    assert len(fake.calls) == 2
    second = fake.calls[1]
    assert second["purpose"] == "rtl-correction"
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]
    assert "system-task" in second["messages"][-1]["content"]


async def test_persistently_unsafe_llm_output_falls_back_to_template():
    fake = FakeProvider([{"verilog": UNSAFE, "explanation": "bad"}] * (MAX_CORRECTION_ROUNDS + 1))
    gen = await generate_core_rtl(DEMO1, fake)
    assert len(fake.calls) == MAX_CORRECTION_ROUNDS + 1
    assert gen.generator == "template/counter"
    assert gen.verilog == GOOD
    assert "safety error" in gen.explanation
    assert errs(gen) == []


async def test_llm_api_failure_falls_back_to_template():
    fake = FakeProvider([llm_error("network", "connection reset")])
    gen = await generate_core_rtl(DEMO1, fake)
    assert gen.generator == "template/counter"
    assert "network" in gen.explanation
    assert errs(gen) == []


async def test_invalid_output_gets_correction_round():
    fake = FakeProvider([llm_error("invalid_output", "not JSON", raw_text="{oops"),
                         {"verilog": GOOD, "explanation": "ok"}])
    gen = await generate_core_rtl(DEMO1, fake)
    assert gen.generator == "llm:fake-model"
    assert fake.calls[1]["messages"][1] == {"role": "assistant", "content": "{oops"}


async def test_repair_without_provider_raises():
    with pytest.raises(RepairUnavailable):
        await repair_core_rtl(DEMO1, GOOD, {"failing_checks": []}, 1, None)


async def test_repair_with_fake_provider():
    broken = GOOD.replace("4'd1", "4'd2")
    fake = FakeProvider([{"root_cause": "step was 2", "fix_summary": "step by 1", "verilog": GOOD}])
    gen = await repair_core_rtl(DEMO1, broken, {"failing_checks": [{"summary": "count skipped values"}]}, 1, fake)
    assert gen.generator == "llm:fake-model"
    assert "Root cause: step was 2" in gen.explanation and "Fix: step by 1" in gen.explanation
    assert errs(gen) == []
    msg = fake.calls[0]["messages"][0]["content"]
    assert fake.calls[0]["purpose"] == "repair"
    assert "count skipped values" in msg and "attempt 1" in msg


async def test_repair_returns_real_safety_findings_when_still_unsafe():
    fake = FakeProvider([{"root_cause": "r", "fix_summary": "f", "verilog": UNSAFE}] * (MAX_CORRECTION_ROUNDS + 1))
    gen = await repair_core_rtl(DEMO1, GOOD, {}, 2, fake)
    assert gen.generator == "llm:fake-model"
    assert any(s["code"] == "system-task" for s in errs(gen))


async def test_repair_propagates_llm_errors():
    fake = FakeProvider([llm_error("rate_limit", "slow down")])
    with pytest.raises(Exception) as ei:
        await repair_core_rtl(DEMO1, GOOD, {}, 1, fake)
    assert getattr(ei.value, "kind", None) == "rate_limit"
