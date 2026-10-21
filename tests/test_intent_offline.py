"""Offline (no LLM) intent interpretation: Demo 1 / Demo 2 and honest refusals."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.agent.intent import IntentResult, interpret_request, summarize_changes
from backend.specifications.ir import DesignSpec
from backend.specifications.validate import validate_spec

ROOT = Path(__file__).resolve().parents[1]
DEMO1 = DesignSpec.model_validate_json((ROOT / "examples/demo1_counter_spec.json").read_text())
DEMO2 = DesignSpec.model_validate_json((ROOT / "examples/demo2_counter_down_reset_spec.json").read_text())

DEMO1_TEXTS = [
    "Build a 4-bit counter shown on LEDs 0-3. It should count at one increment per second normally and three "
    "increments per second when I hold BTNU.",
    "Build a 4-bit binary counter using LEDs 0-3 that increments slowly but runs three times faster when I hold BTNU.",
]
DEMO2_TEXT = "Change the counter so it counts backward, and use BTNC to reset it."


def behaviour(spec: DesignSpec):
    """Everything that determines hardware behaviour (names of signals aside from references are kept)."""
    d = spec.model_dump()
    return (d["behavior"], [(i["port"], i["index"], i["debounce_ms"]) for i in d["inputs"]],
            [o["indices"] for o in d["outputs"]])


@pytest.mark.parametrize("text", DEMO1_TEXTS)
async def test_demo1_offline_matches_example(text):
    r = await interpret_request(text, None, None)
    assert isinstance(r, IntentResult)
    assert r.kind == "new_design" and r.interpreter == "offline-rules"
    assert r.spec is not None
    assert behaviour(r.spec) == behaviour(DEMO1)
    assert validate_spec(r.spec).ok
    assert r.message


async def test_demo2_offline_modifies_demo1():
    r1 = await interpret_request(DEMO1_TEXTS[0], None, None)
    r2 = await interpret_request(DEMO2_TEXT, r1.spec, None)
    assert r2.kind == "modify" and r2.interpreter == "offline-rules"
    assert r2.spec is not None
    b = r2.spec.behavior
    assert b.direction == "down"
    assert r2.spec.input(b.reset_input).port == "btnC"
    fast = b.rate_modes[0]
    assert fast.hz == 3 and r2.spec.input(fast.when_input).port == "btnU"
    assert behaviour(r2.spec) == behaviour(DEMO2)
    assert validate_spec(r2.spec).ok
    # change summary lists the concrete changes
    joined = " | ".join(r2.change_summary)
    assert "direction: up -> down" in joined
    assert "btnC" in joined


def test_summarize_changes_demo1_to_demo2():
    ch = summarize_changes(DEMO1, DEMO2)
    assert any(c.startswith("added input reset_btn (btnC)") for c in ch)
    assert "direction: up -> down" in ch
    assert any(c.startswith("reset_input: none -> reset_btn") for c in ch)
    assert summarize_changes(DEMO1, DEMO1) == []


@pytest.mark.parametrize("text,needle", [
    ("Show the count on the seven-segment display", "seven-segment"),
    ("Send hello over UART to my laptop", "UART"),
    ("Draw a bouncing square on a VGA monitor", "VGA"),
    ("Read a temperature sensor on the PMOD header and light LEDs", "PMOD"),
])
async def test_unsupported_requests_are_refused_honestly(text, needle):
    r = await interpret_request(text, None, None)
    assert r.kind == "unsupported"
    assert r.spec is None
    assert r.unsupported_reasons
    assert needle.lower() in " ".join(r.unsupported_reasons).lower()


@pytest.mark.parametrize("text,family", [
    ("Blink LED 0 at 2 Hz", "blinker"),
    ("Make a traffic light on LEDs 0-2", "traffic_light"),
    ("Build an 8-bit counter on LEDs 0-7 that counts up twice per second", "counter"),
])
async def test_simple_requests(text, family):
    r = await interpret_request(text, None, None)
    assert r.kind == "new_design", r.message
    assert r.spec is not None and r.spec.family == family
    assert validate_spec(r.spec).ok


async def test_empty_and_unknown_requests_clarify():
    assert (await interpret_request("   ", None, None)).kind == "clarify"
    r = await interpret_request("Please do something nice for me", None, None)
    assert r.kind == "clarify" and r.spec is None and r.message


async def test_too_long_request_clarifies():
    r = await interpret_request("blink " * 3000, None, None)
    assert r.kind == "clarify" and r.spec is None


def test_example_specs_round_trip():
    for p in sorted((ROOT / "examples").glob("*.json")):
        DesignSpec.model_validate(json.loads(p.read_text()))
