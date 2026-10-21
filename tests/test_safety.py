"""rtl/safety.check_core_source: template output is clean, dangerous constructs are rejected."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.rtl.safety import SafetyIssue, check_core_source, has_errors
from backend.rtl.templates import generate_core
from backend.specifications.ir import DesignSpec

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "examples").glob("*.json"))
DEMO1 = DesignSpec.model_validate_json((ROOT / "examples/demo1_counter_spec.json").read_text())
SRC = generate_core(DEMO1)


def errors(src: str, spec: DesignSpec = DEMO1) -> list[str]:
    return [i.code for i in check_core_source(src, spec) if i.severity == "error"]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_template_output_has_no_errors(path):
    spec = DesignSpec.model_validate_json(path.read_text())
    issues = check_core_source(generate_core(spec), spec)
    assert all(isinstance(i, SafetyIssue) for i in issues)
    assert not [i for i in issues if i.severity == "error"], [i.model_dump() for i in issues]


def _before_end(extra: str) -> str:
    return SRC.replace("endmodule", extra + "\nendmodule")


@pytest.mark.parametrize("name,src,code", [
    ("display", _before_end('always @(posedge clk) $display("x");'), "system-task"),
    ("fopen", _before_end('integer fd;\nalways @(posedge clk) fd <= $fopen("a");'), "system-task"),
    ("readmem", _before_end('reg [3:0] m [0:3];\ninitial $readmemh("x.hex", m);'), "system-task"),
    ("initial", _before_end("reg q;\ninitial q = 1'b0;"), "initial-block"),
    ("include", '`include "evil.v"\n' + SRC, "directive"),
    ("define", "`define X 1\n" + SRC, "directive"),
    ("dpi", _before_end('import "DPI-C" function int f();'), "dpi"),
    ("extra-module", SRC + "\nmodule evil(input wire a); endmodule\n", "module-count"),
    ("renamed", SRC.replace("module cl_core", "module my_core"), "module-name"),
    ("negedge", SRC.replace("posedge clk", "negedge clk", 1), "clock-edge"),
    ("derived-clock", SRC.replace("posedge clk", "posedge slow_clk", 1), "derived-clock"),
    ("input-as-clock", SRC.replace("posedge clk", "posedge speed_btn", 1), "clock-edge"),
    ("non-ascii", SRC + "// café\n", "non-ascii"),
    ("latch", _before_end("reg q;\nalways @* begin\n  if (speed_btn) q = 1'b1;\nend"), "latch"),
])
def test_dangerous_constructs_are_errors(name, src, code):
    assert code in errors(src), errors(src)


def test_port_mismatch_is_error():
    src = SRC.replace("input  wire speed_btn,", "input  wire speed_btn,\n    input  wire extra_in,")
    assert src != SRC
    assert errors(src)


def test_complete_comb_block_is_not_a_latch():
    src = _before_end("reg q;\nalways @* begin\n  q = 1'b0;\n  if (speed_btn) q = 1'b1;\nend")
    assert "latch" not in errors(src)


def test_oversize_source_is_error():
    assert errors(SRC + ("// padding padding padding padding\n" * 20000))


def test_has_errors_accepts_dicts_and_models():
    issues = check_core_source(SRC.replace("module cl_core", "module x"), DEMO1)
    assert has_errors(issues)
    assert has_errors([i.model_dump() for i in issues])
    assert not has_errors(check_core_source(SRC, DEMO1))
