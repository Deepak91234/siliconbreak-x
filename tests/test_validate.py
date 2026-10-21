"""Board-capability validation of DesignSpecs."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from backend.specifications.ir import DesignSpec
from backend.specifications.validate import ValidationIssue, ValidationReport, validate_spec

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "examples").glob("*.json"))


def load(name: str) -> dict:
    return json.loads((ROOT / "examples" / name).read_text())


def codes(report: ValidationReport, severity: str = "error") -> list[str]:
    return [i.code for i in report.issues if i.severity == severity]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_examples_validate(path):
    r = validate_spec(DesignSpec.model_validate_json(path.read_text()))
    assert isinstance(r, ValidationReport)
    assert r.ok, [i.model_dump() for i in r.issues]
    for i in r.issues:
        assert isinstance(i, ValidationIssue)
        assert set(i.model_dump()) >= {"severity", "code", "message"}


@pytest.mark.parametrize("pattern", ["chase", "bounce"])
def test_single_led_chase_or_bounce_is_an_error(pattern):
    d = load("led_chase_spec.json")
    d["outputs"][0]["indices"] = [d["outputs"][0]["indices"][0]]
    d["behavior"]["pattern"] = pattern
    r = validate_spec(DesignSpec.model_validate(d))
    assert not r.ok
    assert "pattern-needs-two-leds" in codes(r)


def test_single_led_fill_is_only_a_warning():
    d = load("led_chase_spec.json")
    d["outputs"][0]["indices"] = [0]
    d["behavior"]["pattern"] = "fill"
    d["behavior"].pop("direction_control", None)
    r = validate_spec(DesignSpec.model_validate(d))
    assert "pattern-needs-two-leds" not in codes(r)
    assert "pattern-one-led" in codes(r, "warning")


def test_counter_reset_value_out_of_range():
    d = load("demo2_counter_down_reset_spec.json")
    d["behavior"]["reset_value"] = 16  # 4-bit output
    r = validate_spec(DesignSpec.model_validate(d))
    assert not r.ok and "value-range" in codes(r)


def test_animated_pattern_without_rate_is_error():
    d = load("led_chase_spec.json")
    d["behavior"]["rate_modes"] = []
    d["behavior"].pop("direction_control", None)
    r = validate_spec(DesignSpec.model_validate(d))
    assert "pattern-needs-rate" in codes(r)


def test_mirror_width_mismatch_is_error():
    d = load("led_mirror_spec.json")
    d["outputs"][0]["indices"] = d["outputs"][0]["indices"] + [15]
    r = validate_spec(DesignSpec.model_validate(d))
    assert "source-width-mismatch" in codes(r)


def test_fast_rate_warns_about_camera():
    d = copy.deepcopy(load("demo1_counter_spec.json"))
    d["behavior"]["rate_modes"][-1]["hz"] = 500
    r = validate_spec(DesignSpec.model_validate(d))
    assert "camera-too-fast" in codes(r, "warning")
