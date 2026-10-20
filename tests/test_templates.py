"""Template cl_core generators: structure, lint at both parameter sets, and behaviour in Verilator.

The behaviour checks are written straight from the normative semantics in
docs/contracts.md ("Behavioural semantics the oracle must check") and read the
real harness trace; they do not use the oracle subsystem, so a bug in either
one cannot hide the other.

Timing notes for the assertions (sim clock 10 kHz):
  * an input change at raw cycle r reaches the core after the 2-FF synchronizer
    and the debouncer, i.e. after edge r + DEBOUNCE + 1;
  * a cl_tick_div step first appears TICKS + 1 cycles after its timer starts
    (one cycle of pulse register), then exactly every TICKS cycles;
  * traffic-light states last exactly TICKS_STATE_<S> cycles.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from backend.rtl.templates import TemplateUnavailable, generate_core, generator_name
from backend.rtl.wrapper import core_interface, core_ports, generate_wrapper, library_paths
from backend.specifications.board import REPO_ROOT
from backend.specifications.ir import DesignSpec
from backend.specifications.timing import param_dict
from backend.verification import verilator as V

SIM_CLK = 10_000
REAL_CLK = 100_000_000
EXAMPLES = REPO_ROOT / "examples"
EXAMPLE_FILES = (
    "demo1_counter_spec.json",
    "demo2_counter_down_reset_spec.json",
    "blinker_spec.json",
    "led_chase_spec.json",
    "led_mirror_spec.json",
    "traffic_light_spec.json",
)


# --------------------------------------------------------------------------- spec builders


def example(name: str) -> DesignSpec:
    return DesignSpec.model_validate(json.loads((EXAMPLES / name).read_text()))


def btn(name: str, port: str, debounce_ms: float = 10) -> dict:
    return {"name": name, "port": port, "debounce_ms": debounce_ms}


def sw(name: str, index: int, debounce_ms: float = 10) -> dict:
    return {"name": name, "port": "sw", "index": index, "debounce_ms": debounce_ms}


def make_spec(title: str, inputs: list[dict], indices: list[int], behavior: dict, out: str = "leds",
              extra_outputs: list[dict] | None = None) -> DesignSpec:
    outputs = [{"name": out, "port": "led", "indices": indices}] + (extra_outputs or [])
    return DesignSpec.model_validate(
        {"title": title, "inputs": inputs, "outputs": outputs, "behavior": {"output": out, **behavior}}
    )


def counter_variant(kind: str) -> DesignSpec:
    if kind == "up_saturate":
        return make_spec("2-bit saturating up counter", [], [4, 5],
                         {"family": "counter", "direction": "up", "wrap": False,
                          "rate_modes": [{"name": "normal", "hz": 2}]})
    if kind == "down_saturate":
        return make_spec("3-bit saturating down counter", [], [0, 1, 2],
                         {"family": "counter", "direction": "down", "wrap": False, "initial_value": 2,
                          "rate_modes": [{"name": "normal", "hz": 2}]})
    if kind == "enable_dirctl":
        return make_spec("counter with enable and direction switch", [sw("en_sw", 0), sw("dir_sw", 1)], [0, 1, 2, 3],
                         {"family": "counter", "direction": "up", "enable_input": "en_sw",
                          "direction_control": {"input": "dir_sw", "high_means": "down"},
                          "rate_modes": [{"name": "normal", "hz": 4}]})
    raise KeyError(kind)


def blinker_variant(kind: str) -> DesignSpec:
    if kind == "alternate4":
        return make_spec("alternate blinker", [], [0, 1, 2, 3],
                         {"family": "blinker", "pattern": "alternate", "rate_modes": [{"name": "slow", "hz": 1}]})
    if kind == "alternate3_enable":
        return make_spec("alternate blinker with enable", [sw("en_sw", 2)], [8, 9, 10],
                         {"family": "blinker", "pattern": "alternate", "enable_input": "en_sw",
                          "rate_modes": [{"name": "slow", "hz": 2}]})
    if kind == "single_btn_fast":
        return make_spec("single LED blinker", [btn("fast_btn", "btnR")], [15],
                         {"family": "blinker", "pattern": "together",
                          "rate_modes": [{"name": "fast", "hz": 4, "when_input": "fast_btn"},
                                         {"name": "slow", "hz": 1}]})
    raise KeyError(kind)


def pattern_variant(kind: str) -> DesignSpec:
    rate = [{"name": "normal", "hz": 4}]
    if kind == "chase_right4":
        return make_spec("chase right", [], [0, 1, 2, 3],
                         {"family": "led_pattern", "pattern": "chase", "direction": "right", "rate_modes": rate})
    if kind == "chase_left4_enable":
        return make_spec("chase with enable", [sw("en_sw", 0)], [12, 13, 14, 15],
                         {"family": "led_pattern", "pattern": "chase", "direction": "left",
                          "enable_input": "en_sw", "rate_modes": rate})
    if kind == "bounce4_reset":
        return make_spec("bounce with reset", [btn("reset_btn", "btnC")], [0, 1, 2, 3],
                         {"family": "led_pattern", "pattern": "bounce", "reset_input": "reset_btn",
                          "rate_modes": rate})
    if kind == "bounce2_right":
        return make_spec("two-LED bounce", [], [6, 7],
                         {"family": "led_pattern", "pattern": "bounce", "direction": "right", "rate_modes": rate})
    if kind == "fill_left4":
        return make_spec("fill left", [], [0, 1, 2, 3],
                         {"family": "led_pattern", "pattern": "fill", "direction": "left", "rate_modes": rate})
    if kind == "fill_right3_dirctl":
        return make_spec("fill right with direction switch", [sw("dir_sw", 5)], [0, 1, 2],
                         {"family": "led_pattern", "pattern": "fill", "direction": "right",
                          "direction_control": {"input": "dir_sw", "high_means": "up"}, "rate_modes": rate})
    if kind == "invert4_enable_reset":
        return make_spec("inverted switches with hold and reset",
                         [sw("a", 0), sw("b", 1), sw("c", 2), sw("d", 3), btn("hold_btn", "btnU"),
                          btn("reset_btn", "btnC")],
                         [0, 1, 2, 3],
                         {"family": "led_pattern", "pattern": "invert_inputs", "source_inputs": ["a", "b", "c", "d"],
                          "enable_input": "hold_btn", "reset_input": "reset_btn"})
    raise KeyError(kind)


def traffic_variant(kind: str) -> DesignSpec:
    if kind == "two_state_no_request":
        return make_spec("two-state light", [], [14, 15],
                         {"family": "traffic_light",
                          "states": [{"name": "a", "duration_s": 0.5, "lights": 1},
                                     {"name": "b", "duration_s": 1.5, "lights": 2}]},
                         out="lights")
    if kind == "request_after_first":
        return make_spec("request after the first state", [sw("req_sw", 3)], [0, 1, 2],
                         {"family": "traffic_light",
                          "states": [{"name": "go", "duration_s": 1, "lights": 1},
                                     {"name": "slow", "duration_s": 0.5, "lights": 2},
                                     {"name": "stop", "duration_s": 1, "lights": 4}],
                          "request_input": "req_sw", "request_after": "go",
                          "request_state": {"name": "allred", "duration_s": 0.7, "lights": 0}},
                         out="lights")
    if kind == "request_state_without_input":
        return make_spec("request state never used", [], [0, 1],
                         {"family": "traffic_light",
                          "states": [{"name": "on", "duration_s": 0.3, "lights": 3},
                                     {"name": "off", "duration_s": 0.2, "lights": 0}],
                          "request_after": "on",
                          "request_state": {"name": "extra", "duration_s": 1, "lights": 1}},
                         out="lights")
    raise KeyError(kind)


# Edge-case specs that are only linted (their behaviour is trivial or covered elsewhere).
def lint_only_specs() -> dict[str, DesignSpec]:
    rate = [{"name": "fast", "hz": 8, "when_input": "go_btn", "when_level": 0}, {"name": "normal", "hz": 1}]
    return {
        "fill_w1": make_spec("one-LED fill", [btn("go_btn", "btnD")], [3],
                             {"family": "led_pattern", "pattern": "fill", "rate_modes": rate}),
        "fill_w16_all": make_spec("16-LED fill", [btn("go_btn", "btnD"), btn("en", "btnU"), btn("rst", "btnC"),
                                                  sw("dir", 0)], list(range(16)),
                                  {"family": "led_pattern", "pattern": "fill", "rate_modes": rate,
                                   "enable_input": "en", "reset_input": "rst",
                                   "direction_control": {"input": "dir", "high_means": "down"}}),
        "bounce_w16_dirctl_ignored": make_spec("16-LED bounce", [sw("dir", 0)], list(range(16)),
                                               {"family": "led_pattern", "pattern": "bounce",
                                                "rate_modes": [{"name": "normal", "hz": 5}],
                                                "direction_control": {"input": "dir"}}),
        "mirror_w1": make_spec("one-switch mirror", [sw("s", 9)], [9],
                               {"family": "led_pattern", "pattern": "mirror_inputs", "source_inputs": ["s"]}),
        "mirror_with_unused_rate": make_spec("mirror with a rate mode", [sw("s", 0), sw("t", 1)], [0, 1],
                                             {"family": "led_pattern", "pattern": "mirror_inputs",
                                              "source_inputs": ["s", "t"],
                                              "rate_modes": [{"name": "normal", "hz": 1}]}),
        "blinker_alt_w1_four_modes": make_spec(
            "one-LED alternate blinker", [btn("a", "btnL"), btn("b", "btnR"), sw("c", 4)], [0],
            {"family": "blinker", "pattern": "alternate",
             "rate_modes": [{"name": "m1", "hz": 10, "when_input": "a"}, {"name": "m2", "hz": 5, "when_input": "b"},
                            {"name": "m3", "hz": 0.5, "when_input": "c", "when_level": 0}, {"name": "m4", "hz": 1}]}),
        "traffic_8_states_wide": make_spec(
            "eight-state light", [btn("rq", "btnU"), btn("rst", "btnC")], list(range(16)),
            {"family": "traffic_light",
             "states": [{"name": f"s{i}", "duration_s": 20 if i == 0 else 0.25 * (i + 1), "lights": 1 << (2 * i)}
                        for i in range(8)],
             "request_input": "rq", "request_after": "s7",
             "request_state": {"name": "rq_state", "duration_s": 2, "lights": 0xFFFF},
             "reset_input": "rst"},
            out="lights"),
        "counter_extra_output_unused_input": make_spec(
            "counter with a spare output", [btn("spare_btn", "btnL")], [0, 1],
            {"family": "counter", "rate_modes": [{"name": "normal", "hz": 1}]},
            out="count", extra_outputs=[{"name": "spare", "port": "led", "indices": [7, 8]}]),
        "names_collide_with_internals": make_spec(
            "ports named like internal signals",
            [btn("value", "btnU"), btn("step", "btnC"), sw("run", 0), sw("period", 1), sw("phase", 2)],
            [0, 1, 2, 3],
            {"family": "counter", "reset_input": "step", "enable_input": "run",
             "direction_control": {"input": "period"},
             "rate_modes": [{"name": "fast", "hz": 3, "when_input": "value"}, {"name": "normal", "hz": 1}]},
            out="count"),
        "traffic_names_collide": make_spec(
            "traffic ports named like internals", [btn("state", "btnU"), btn("timer", "btnC")], [0, 1, 2],
            {"family": "traffic_light",
             "states": [{"name": "g", "duration_s": 1, "lights": 1}, {"name": "r", "duration_s": 1, "lights": 4}],
             "request_input": "state", "request_after": "r",
             "request_state": {"name": "w", "duration_s": 1, "lights": 6}, "reset_input": "timer"},
            out="lamps"),
    }


# --------------------------------------------------------------------------- harness helpers


def write_design(spec: DesignSpec, ws: Path) -> list[Path]:
    rtl = ws / "rtl"
    rtl.mkdir(parents=True, exist_ok=True)
    (rtl / "basys3_top.v").write_text(generate_wrapper(spec))
    (rtl / "cl_core.v").write_text(generate_core(spec))
    return [rtl / "basys3_top.v", rtl / "cl_core.v"] + library_paths()


def _fmt(msgs: list[V.LintMessage]) -> str:
    return "\n".join(f"{m.severity} {m.code} {m.file}:{m.line} {m.message}" for m in msgs)


async def lint_both(spec: DesignSpec, ws: Path) -> list[Path]:
    srcs = write_design(spec, ws)
    for clk in (REAL_CLK, SIM_CLK):
        res = await V.lint(srcs, ws, param_dict(spec, clk))
        assert res.messages == [], f"lint at {clk} Hz:\n{_fmt(res.messages)}"
        assert res.ok, f"lint at {clk} Hz failed:\n{res.process.text[-2000:]}"
    return srcs


@dataclass
class Sim:
    spec: DesignSpec
    ws: Path
    binary: Path
    params: dict[str, int]

    def deb(self, name: str) -> int:
        return self.params[f"DEBOUNCE_{name.upper()}"]

    async def run(self, name: str, events: list[tuple[int, str, int]], end: int) -> V.Trace:
        stim = self.ws / f"{name}.stim"
        lines = [f"end {end}"] + [f"{c} {port} {val}" for c, port, val in events]
        stim.write_text("\n".join(lines) + "\n")
        run = await V.run_harness(self.binary, self.ws, stim, self.ws / f"{name}.trace")
        assert run.ok, run.process.text[-2000:]
        trace = V.parse_trace(run.trace_path)
        assert trace.end_cycle == end
        assert_unused_leds_off(self.spec, trace)
        return trace


async def build_sim(spec: DesignSpec, ws: Path) -> Sim:
    srcs = await lint_both(spec, ws)
    params = param_dict(spec, SIM_CLK)
    build = await V.build_harness(srcs, ws, params, ws / "obj", trace=False)
    assert build.ok, build.process.text[-3000:]
    return Sim(spec, ws, build.binary, params)


def core_value(spec: DesignSpec, led: int) -> int:
    """Behaviour output value: core bit i is led[indices[i]]."""
    out = spec.output(spec.behavior.output)
    return sum(((led >> idx) & 1) << k for k, idx in enumerate(out.indices))


def series(spec: DesignSpec, trace: V.Trace) -> list[tuple[int, int]]:
    """[(cycle, core value)] at cycle 0 and at every change of the behaviour output."""
    pts: list[tuple[int, int]] = []
    for cycle, led in trace.changes["led"]:
        v = core_value(spec, led)
        if not pts or pts[-1][1] != v:
            pts.append((cycle, v))
    return pts


def value_at(pts: list[tuple[int, int]], cycle: int) -> int:
    val = pts[0][1]
    for c, v in pts:
        if c > cycle:
            break
        val = v
    return val


def between(pts: list[tuple[int, int]], start: int, stop: int) -> list[tuple[int, int]]:
    return [(c, v) for c, v in pts if start < c <= stop]


def intervals(pts: list[tuple[int, int]]) -> list[int]:
    """Cycles between consecutive changes.

    The span from the cycle-0 initial sample to the first change is excluded: the
    tick is registered, so the first change lands one cycle after the period
    boundary (asserted separately with near(pts[1][0], t + 1, 1)).
    """
    return [b[0] - a[0] for a, b in zip(pts, pts[1:]) if a[0] != 0]


def assert_unused_leds_off(spec: DesignSpec, trace: V.Trace) -> None:
    driven = 0
    for idx in spec.output(spec.behavior.output).indices:
        driven |= 1 << idx
    for cycle, led in trace.changes["led"]:
        assert led & ~driven == 0, f"cycle {cycle}: LEDs outside the behaviour output lit (led=0x{led:04x})"


def near(actual: int, expected: int, tol: int = 4) -> bool:
    return abs(actual - expected) <= tol


def rotl(v: int, w: int) -> int:
    return ((v << 1) | (v >> (w - 1))) & ((1 << w) - 1)


def rotr(v: int, w: int) -> int:
    return ((v >> 1) | ((v & 1) << (w - 1))) & ((1 << w) - 1)


# --------------------------------------------------------------------------- structure (no tools)

ALL_SPECS: dict[str, DesignSpec] = {
    **{f"example:{n}": example(n) for n in EXAMPLE_FILES},
    **{f"counter:{k}": counter_variant(k) for k in ("up_saturate", "down_saturate", "enable_dirctl")},
    **{f"blinker:{k}": blinker_variant(k) for k in ("alternate4", "alternate3_enable", "single_btn_fast")},
    **{f"pattern:{k}": pattern_variant(k) for k in (
        "chase_right4", "chase_left4_enable", "bounce4_reset", "bounce2_right", "fill_left4",
        "fill_right3_dirctl", "invert4_enable_reset")},
    **{f"traffic:{k}": traffic_variant(k) for k in (
        "two_state_no_request", "request_after_first", "request_state_without_input")},
    **{f"lint:{k}": s for k, s in lint_only_specs().items()},
}


@pytest.mark.parametrize("key", sorted(ALL_SPECS))
def test_core_structure(key: str) -> None:
    spec = ALL_SPECS[key]
    core = generate_core(spec)
    header = core_interface(spec)
    preamble = f"// GENERATED BY CIRCUITLOOP (trusted generator) -- do not edit by hand\n" \
               f"// Generator: {generator_name(spec)}\n`default_nettype none\n"
    # The module header is exactly core_interface(spec), right after the banner.
    assert core.startswith(preamble + header + "\n")
    assert core.rstrip().endswith("endmodule\n`default_nettype wire")
    assert core.count("module cl_core") == 1 and core.count("endmodule") == 1
    body = core[len(preamble + header):]
    assert not re.search(r"\binitial\b", body), "no initial blocks"
    assert "always @*" not in body and "always @(*)" not in body, "no combinational always blocks"
    clocks = re.findall(r"always\s*@\s*\(([^)]*)\)", body)
    assert clocks and all(c.strip() == "posedge clk" for c in clocks), clocks
    assert "$" not in body.replace("$clog2", ""), "no system tasks"
    assert "`include" not in core and "`define" not in core
    assert core.isascii()
    # Every port name appears only as itself: internal signals never reuse a port name.
    decls = re.findall(r"^\s*(?:wire|reg)\s+(?:\[[^\]]+\]\s*)?([A-Za-z_][A-Za-z0-9_]*)", body, re.M)
    ports = {p.name for p in core_ports(spec)}
    assert not (set(decls) & ports), set(decls) & ports


def test_generator_names() -> None:
    assert generator_name(example("demo1_counter_spec.json")) == "template/counter"
    assert generator_name(example("blinker_spec.json")) == "template/blinker"
    assert generator_name(example("led_chase_spec.json")) == "template/led_pattern"
    assert generator_name(example("traffic_light_spec.json")) == "template/traffic_light"


# The counter generator's Phase-1-proven output for Demo 1, frozen byte for byte.
DEMO1_CORE = """// GENERATED BY CIRCUITLOOP (trusted generator) -- do not edit by hand
// Generator: template/counter
`default_nettype none
module cl_core #(
    parameter integer CLK_HZ = 100000000,  // clock frequency in Hz
    parameter integer TICKS_FAST = 33333333,  // cycles between steps in mode 'fast' (3 steps/s)
    parameter integer TICKS_NORMAL = 100000000  // cycles between steps in mode 'normal' (1 steps/s)
) (
    input  wire clk,  // 100 MHz board clock (CLK_HZ in simulation)
    input  wire speed_btn,  // btnU: synchronized, debounced 10 ms, active-high
    output wire [3:0] count  // bit0->led[0], bit1->led[1], bit2->led[2], bit3->led[3]
);
    localparam integer TMAX = ((TICKS_NORMAL) > (TICKS_FAST) ? (TICKS_NORMAL) : (TICKS_FAST));
    localparam integer TW   = $clog2(TMAX + 1);

    wire          counting_up = 1'b1;
    wire          hold_reset  = 1'b0;
    wire          run         = 1'b1;
    wire [TW-1:0] period =
        (speed_btn == 1'b1) ? TICKS_FAST[TW-1:0] :
        TICKS_NORMAL[TW-1:0];
    wire          step;

    cl_tick_div #(.WIDTH(TW)) u_tick (
        .clk(clk), .clear(hold_reset), .enable(run), .period(period), .tick(step)
    );

    reg [3:0] value = 4'd0;

    always @(posedge clk) begin
        if (hold_reset) begin
            value <= 4'd0;
        end else if (step) begin
            value <= counting_up ? (value + 4'd1) : (value - 4'd1);
        end
    end

    assign count = value;

    /* verilator lint_off UNUSEDPARAM */
    localparam integer UNUSED_CLK_HZ = CLK_HZ;
    /* verilator lint_on UNUSEDPARAM */
endmodule
`default_nettype wire
"""


def test_counter_demo1_output_unchanged() -> None:
    assert generate_core(example("demo1_counter_spec.json")) == DEMO1_CORE


def test_internal_names_avoid_port_names() -> None:
    core = generate_core(lint_only_specs()["names_collide_with_internals"])
    assert "reg [3:0] value_i1 = 4'd0;" in core
    assert "cl_tick_div #(.WIDTH(TW)) u_tick (" in core
    assert ".clear(hold_reset), .enable(run_i1), .period(period_i1), .tick(step_i1)" in core
    assert "wire          hold_reset  = step;" in core


def test_unused_outputs_and_inputs_are_tied_off() -> None:
    core = generate_core(lint_only_specs()["counter_extra_output_unused_input"])
    assert "assign spare = 2'd0;" in core
    assert "wire _unused_inputs = &{1'b0, spare_btn};" in core


def test_examples_validate_and_demo2_is_demo1_modified() -> None:
    for name in EXAMPLE_FILES:
        example(name)  # raises on any schema problem
    d1, d2 = example("demo1_counter_spec.json"), example("demo2_counter_down_reset_spec.json")
    b1, b2 = d1.behavior, d2.behavior
    assert b2.family == "counter" and b2.direction == "down"
    assert b2.reset_input == "reset_btn" and d2.input("reset_btn").port == "btnC"
    assert [m.model_dump() for m in b2.rate_modes] == [m.model_dump() for m in b1.rate_modes]
    assert d2.input("speed_btn").port == "btnU" and b2.rate_modes[0].hz == 3 * b2.rate_modes[-1].hz
    assert d2.outputs == d1.outputs and b2.wrap == b1.wrap
    t = example("traffic_light_spec.json").behavior
    assert [s.name for s in t.states] == ["green", "yellow", "red"]
    assert [s.duration_s for s in t.states] == [4, 1, 3]
    assert t.request_after == "red" and t.request_state.name == "walk"
    assert example("traffic_light_spec.json").input(t.request_input).port == "btnL"


@pytest.mark.parametrize(
    "spec_dict, match",
    [
        (dict(inputs=[sw("a", 0)], indices=[0, 1],
              behavior={"family": "led_pattern", "pattern": "mirror_inputs", "source_inputs": ["a"]}),
         "one source input per output bit"),
        (dict(inputs=[], indices=[0, 1, 2],
              behavior={"family": "led_pattern", "pattern": "chase"}),
         "needs at least one rate mode"),
        (dict(inputs=[], indices=[0, 1],
              behavior={"family": "traffic_light", "states": [{"name": "a", "duration_s": 1, "lights": 4},
                                                              {"name": "b", "duration_s": 1, "lights": 1}]}),
         "do not fit"),
        (dict(inputs=[btn("r", "btnU")], indices=[0, 1],
              behavior={"family": "traffic_light", "request_input": "r",
                        "states": [{"name": "a", "duration_s": 1, "lights": 1},
                                   {"name": "b", "duration_s": 1, "lights": 2}]}),
         "needs both request_state and request_after"),
        (dict(inputs=[btn("r", "btnU")], indices=[0, 1],
              behavior={"family": "traffic_light", "request_input": "r", "request_after": "zz",
                        "request_state": {"name": "w", "duration_s": 1, "lights": 3},
                        "states": [{"name": "a", "duration_s": 1, "lights": 1},
                                   {"name": "b", "duration_s": 1, "lights": 2}]}),
         "not one of the states"),
        (dict(inputs=[], indices=[0, 1],
              behavior={"family": "traffic_light", "request_after": "a",
                        "request_state": {"name": "a", "duration_s": 1, "lights": 3},
                        "states": [{"name": "a", "duration_s": 1, "lights": 1},
                                   {"name": "b", "duration_s": 1, "lights": 2}]}),
         "must be unique"),
        (dict(inputs=[], indices=[0, 1],
              behavior={"family": "counter", "reset_value": 4, "rate_modes": [{"name": "n", "hz": 1}]}),
         "does not fit"),
    ],
)
def test_template_unavailable(spec_dict: dict, match: str) -> None:
    spec = make_spec("unsupported", spec_dict["inputs"], spec_dict["indices"], spec_dict["behavior"])
    with pytest.raises(TemplateUnavailable, match=match):
        generate_core(spec)


# --------------------------------------------------------------------------- lint (Verilator)


@pytest.mark.verilator
@pytest.mark.parametrize("key", sorted(ALL_SPECS))
async def test_lint_clean_at_real_and_sim_params(key: str, tmp_path: Path) -> None:
    await lint_both(ALL_SPECS[key], tmp_path)


# --------------------------------------------------------------------------- counter behaviour


@pytest.mark.verilator
async def test_demo1_counter_behaviour(tmp_path: Path) -> None:
    spec = example("demo1_counter_spec.json")
    sim = await build_sim(spec, tmp_path)
    t, tf, d = sim.params["TICKS_NORMAL"], sim.params["TICKS_FAST"], sim.deb("speed_btn")

    pts = series(spec, await sim.run("free", [], 18 * t))
    assert pts[0] == (0, 0)
    assert near(pts[1][0], t + 1, 1)
    assert [v for _, v in pts] == [k % 16 for k in range(len(pts))]  # +1 mod 16, wraps 15 -> 0
    assert len(pts) == 18 and set(intervals(pts)) == {t}

    press, release = 2 * t + t // 2, 2 * t + t // 2 + 9 * tf
    pts = series(spec, await sim.run("fast", [(press, "btnU", 1), (release, "btnU", 0)], 12 * t))
    fast = between(pts, press + d + 2 * tf, release)
    assert len(fast) >= 6 and set(intervals(fast)) == {tf}
    slow = between(pts, release + d + 2 * t, 12 * t)
    assert len(slow) >= 2 and set(intervals(slow)) == {t}
    assert all(((b - a) & 0xF) == 1 for (_, a), (_, b) in zip(pts, pts[1:]))


@pytest.mark.verilator
async def test_demo2_counter_down_with_reset(tmp_path: Path) -> None:
    spec = example("demo2_counter_down_reset_spec.json")
    sim = await build_sim(spec, tmp_path)
    t, tf, d = sim.params["TICKS_NORMAL"], sim.params["TICKS_FAST"], sim.deb("reset_btn")

    pts = series(spec, await sim.run("free", [], 18 * t))
    assert pts[0] == (0, 0) and near(pts[1][0], t + 1, 1)
    assert [v for _, v in pts] == [(-k) % 16 for k in range(len(pts))]  # 0, 15, 14, ... wraps
    assert len(pts) == 18 and set(intervals(pts)) == {t}

    # Hold BTNC for 2.5 periods in the middle: value 0 while held, next step one full period after release.
    press, release = 5 * t + t // 3, 7 * t + 5 * t // 6
    pts = series(spec, await sim.run("reset", [(press, "btnC", 1), (release, "btnC", 0)], 12 * t))
    assert value_at(pts, press) == (-5) % 16
    held = between(pts, press + d + 4, release + d)
    assert value_at(pts, press + d + 4) == 0 and held == []
    after = between(pts, release + d, 12 * t)
    assert near(after[0][0], release + d + t + 2) and after[0][1] == 15
    assert set(intervals(after)) == {t}

    # BTNU still selects the 3x fast mode (Demo 1 behaviour kept), now counting down.
    press, release = t // 2, t // 2 + 10 * tf
    pts = series(spec, await sim.run("fast", [(press, "btnU", 1), (release, "btnU", 0)], 6 * t))
    fast = between(pts, press + d + 2 * tf, release)
    assert len(fast) >= 6 and set(intervals(fast)) == {tf}
    assert all(((a - b) & 0xF) == 1 for (_, a), (_, b) in zip(pts, pts[1:]))


@pytest.mark.verilator
@pytest.mark.parametrize("kind", ["up_saturate", "down_saturate", "enable_dirctl"])
async def test_counter_variants(kind: str, tmp_path: Path) -> None:
    spec = counter_variant(kind)
    sim = await build_sim(spec, tmp_path)
    t = sim.params["TICKS_NORMAL"]
    if kind == "up_saturate":
        pts = series(spec, await sim.run("free", [], 10 * t))
        assert pts == [(0, 0), (t + 1, 1), (2 * t + 1, 2), (3 * t + 1, 3)]  # then saturates at 3
    elif kind == "down_saturate":
        pts = series(spec, await sim.run("free", [], 10 * t))
        assert pts == [(0, 2), (t + 1, 1), (2 * t + 1, 0)]  # initial_value 2, saturates at 0
    else:
        d = sim.deb("en_sw")
        en_on, dir_down, en_off = 2 * t, 7 * t + t // 2, 10 * t + t // 2
        pts = series(spec, await sim.run(
            "en_dir", [(en_on, "sw", 0b01), (dir_down, "sw", 0b11), (en_off, "sw", 0b10)], 14 * t))
        assert between(pts, 0, en_on + d) == []  # disabled at power-on: no steps
        up = between(pts, en_on + d, dir_down + d)
        assert [v for _, v in up] == [1, 2, 3, 4, 5][: len(up)] and len(up) == 5
        assert near(up[0][0], en_on + d + t + 2)
        down = between(pts, dir_down + d, en_off + d)
        assert [v for _, v in down] == [4, 3, 2][: len(down)] and len(down) == 3
        assert between(pts, en_off + d + 2, 14 * t) == []  # enable low: holds


# --------------------------------------------------------------------------- blinker behaviour


@pytest.mark.verilator
async def test_blinker_example(tmp_path: Path) -> None:
    spec = example("blinker_spec.json")
    sim = await build_sim(spec, tmp_path)
    ts, tf, d = sim.params["TICKS_SLOW"], sim.params["TICKS_FAST"], sim.deb("fast_sw")
    assert ts == SIM_CLK // 2 and tf == SIM_CLK // 4  # toggles at 2*hz

    pts = series(spec, await sim.run("free", [], 12 * ts))
    assert pts[0] == (0, 0) and near(pts[1][0], ts + 1, 1)
    assert [v for _, v in pts] == [0x00, 0xFF] * (len(pts) // 2) + [0x00] * (len(pts) % 2)  # together, 50% duty
    assert len(pts) == 12 and set(intervals(pts)) == {ts}

    on, off = 3 * ts + ts // 2, 3 * ts + ts // 2 + 12 * tf
    pts = series(spec, await sim.run("fast", [(on, "sw", 1), (off, "sw", 0)], off + 8 * ts))
    assert all(v in (0x00, 0xFF) for _, v in pts)
    fast = between(pts, on + d + 2 * tf, off)
    assert len(fast) >= 8 and set(intervals(fast)) == {tf}
    slow = between(pts, off + d + 2 * ts, off + 8 * ts)
    assert len(slow) >= 4 and set(intervals(slow)) == {ts}


@pytest.mark.verilator
@pytest.mark.parametrize("kind", ["alternate4", "alternate3_enable", "single_btn_fast"])
async def test_blinker_variants(kind: str, tmp_path: Path) -> None:
    spec = blinker_variant(kind)
    sim = await build_sim(spec, tmp_path)
    ts = sim.params["TICKS_SLOW"]
    if kind == "alternate4":
        pts = series(spec, await sim.run("free", [], 9 * ts))
        assert pts[0] == (0, 0b1010)  # bit0 = 0, bit1 = 1, ...
        assert [v for _, v in pts] == [0b1010, 0b0101] * 4 + [0b1010]
        assert near(pts[1][0], ts + 1, 1) and set(intervals(pts)) == {ts}
    elif kind == "alternate3_enable":
        d = sim.deb("en_sw")
        on, off = 3 * ts, 10 * ts + ts // 2
        pts = series(spec, await sim.run("enable", [(on, "sw", 1 << 2), (off, "sw", 0)], 14 * ts))
        assert pts[0] == (0, 0) and value_at(pts, on + d - 2) == 0  # disabled: all off
        run = between(pts, on, off + d + 4)  # enable-off reaches the LEDs ~2 cycles after it is debounced
        assert near(run[0][0], on + d + 1) and run[0][1] == 0b010  # restarts from the power-on phase
        assert [v for _, v in run[:-1]] == [0b010, 0b101] * 4  # then alternates
        assert near(run[1][0], run[0][0] + ts, 2) and set(intervals(run[1:-1])) == {ts}
        assert run[-1][1] == 0 and near(run[-1][0], off + d + 1)  # enable low: all off again
        assert between(pts, off + d + 2, 14 * ts) == []
    else:
        tf, d = sim.params["TICKS_FAST"], sim.deb("fast_btn")
        press, release = 2 * ts + 100, 2 * ts + 100 + 9 * tf
        pts = series(spec, await sim.run("fast", [(press, "btnR", 1), (release, "btnR", 0)], 14 * ts))
        assert pts[0] == (0, 0)
        assert set(intervals(between(pts, press + d + 2 * tf, release))) == {tf}
        assert set(intervals(between(pts, release + d + 2 * ts, 14 * ts))) == {ts}


# --------------------------------------------------------------------------- led_pattern behaviour


@pytest.mark.verilator
async def test_led_chase_example(tmp_path: Path) -> None:
    spec = example("led_chase_spec.json")
    sim = await build_sim(spec, tmp_path)
    t, tf, d = sim.params["TICKS_NORMAL"], sim.params["TICKS_FAST"], sim.deb("dir_sw")

    pts = series(spec, await sim.run("free", [], 11 * t))
    exp = [1]
    while len(exp) < len(pts):
        exp.append(rotl(exp[-1], 8))
    assert [v for _, v in pts] == exp and len(pts) == 11  # 0x01 .. 0x80 then wraps to 0x01
    assert pts[0] == (0, 1) and near(pts[1][0], t + 1, 1) and set(intervals(pts)) == {t}

    # SW0 up -> moves towards LED0; BTNC -> back to LED0 with the timer restarted.
    dir_on, rst_on, rst_off = 4 * t + t // 2, 8 * t + t // 2, 9 * t + t // 4
    pts = series(spec, await sim.run(
        "dir_reset", [(dir_on, "sw", 1), (rst_on, "btnC", 1), (rst_off, "btnC", 0)], 13 * t))
    assert all(bin(v).count("1") == 1 for _, v in pts)
    assert [v for _, v in between(pts, 0, dir_on)] == [2, 4, 8, 16]
    right = between(pts, dir_on + d, rst_on)
    assert [v for _, v in right] == [8, 4, 2, 1][: len(right)] and len(right) == 4
    assert value_at(pts, rst_on + d + 4) == 1 and between(pts, rst_on + d + 4, rst_off + d) == []
    after = between(pts, rst_off + d, 13 * t)
    assert near(after[0][0], rst_off + d + t + 2) and [v for _, v in after][:2] == [0x80, 0x40]

    press, release = t // 2, t // 2 + 10 * tf
    pts = series(spec, await sim.run("fast", [(press, "btnU", 1), (release, "btnU", 0)], 6 * t))
    fast = between(pts, press + d + 2 * tf, release)
    assert len(fast) >= 6 and set(intervals(fast)) == {tf}
    assert all(b == rotl(a, 8) for (_, a), (_, b) in zip(pts, pts[1:]))


@pytest.mark.verilator
async def test_led_mirror_example(tmp_path: Path) -> None:
    spec = example("led_mirror_spec.json")
    sim = await build_sim(spec, tmp_path)
    d = sim.deb("s0")
    # The upper switches are not part of the design: their LEDs must stay off (checked in Sim.run).
    events = [(200, "sw", 0xB5), (1000, "sw", 0xFF0F), (2000, "sw", 0x0000), (3000, "sw", 0x5A5A)]
    pts = series(spec, await sim.run("mirror", events, 4000))
    assert pts[0] == (0, 0)
    assert [v for _, v in pts] == [0x00, 0xB5, 0x0F, 0x00, 0x5A]
    for (c, _v), (raw, _p, _val) in zip(pts[1:], events):
        assert near(c, raw + d + 2), (c, raw)


@pytest.mark.verilator
@pytest.mark.parametrize("kind", ["chase_right4", "chase_left4_enable", "bounce4_reset", "bounce2_right",
                                  "fill_left4", "fill_right3_dirctl", "invert4_enable_reset"])
async def test_led_pattern_variants(kind: str, tmp_path: Path) -> None:
    spec = pattern_variant(kind)
    sim = await build_sim(spec, tmp_path)
    t = sim.params.get("TICKS_NORMAL", 0)

    if kind == "chase_right4":
        pts = series(spec, await sim.run("free", [], 9 * t))
        assert [v for _, v in pts] == [1, 8, 4, 2, 1, 8, 4, 2, 1]  # starts at bit 0, moves down, wraps
        assert set(intervals(pts)) == {t}

    elif kind == "chase_left4_enable":
        d = sim.deb("en_sw")
        on, off = 2 * t, 6 * t + t // 2
        pts = series(spec, await sim.run("enable", [(on, "sw", 1), (off, "sw", 0)], 10 * t))
        assert between(pts, 0, on + d) == [] and pts[0] == (0, 1)  # enable low at power-on: holds bit 0
        run = between(pts, on, off + d)
        assert [v for _, v in run] == [2, 4, 8, 1] and near(run[0][0], on + d + t + 2)
        assert between(pts, off + d + 2, 10 * t) == []  # holds again

    elif kind == "bounce4_reset":
        d = sim.deb("reset_btn")
        pts = series(spec, await sim.run("free", [], 14 * t))
        assert [v for _, v in pts] == [1, 2, 4, 8, 4, 2, 1, 2, 4, 8, 4, 2, 1, 2]  # end bits lit one step each
        assert set(intervals(pts)) == {t}
        press, release = 4 * t + t // 2, 5 * t + t // 2  # during the downward sweep (value 4)
        pts = series(spec, await sim.run("reset", [(press, "btnC", 1), (release, "btnC", 0)], 10 * t))
        assert value_at(pts, press) == 4
        assert value_at(pts, press + d + 4) == 1 and between(pts, press + d + 4, release + d) == []
        after = between(pts, release + d, 10 * t)
        assert [v for _, v in after] == [2, 4, 8, 4][: len(after)] and len(after) >= 3  # heading up again
        assert near(after[0][0], release + d + t + 2)

    elif kind == "bounce2_right":
        pts = series(spec, await sim.run("free", [], 7 * t))
        assert [v for _, v in pts] == [1, 2, 1, 2, 1, 2, 1]  # bounce always starts at bit 0 moving up
        assert set(intervals(pts)) == {t}

    elif kind == "fill_left4":
        pts = series(spec, await sim.run("free", [], 11 * t))
        assert [v for _, v in pts] == [0, 1, 3, 7, 15, 0, 1, 3, 7, 15, 0]
        assert set(intervals(pts)) == {t}

    elif kind == "fill_right3_dirctl":
        d = sim.deb("dir_sw")
        pts = series(spec, await sim.run("free", [], 9 * t))
        assert [v for _, v in pts] == [0, 0b100, 0b110, 0b111, 0, 0b100, 0b110, 0b111, 0]  # top bit first
        on = 8 * t + t // 2  # just after a clear; switch high -> 'up' = left: bit 0 first
        pts = series(spec, await sim.run("dirctl", [(on, "sw", 1 << 5)], 13 * t))
        assert [v for _, v in between(pts, on + d, 13 * t)] == [0b001, 0b011, 0b111, 0]
        legal = {0, 0b001, 0b011, 0b111, 0b100, 0b110}
        assert all(v in legal for _, v in pts)  # always a bar graph

    elif kind == "invert4_enable_reset":
        d = sim.deb("a")
        # power-on: all conditioned inputs low -> all LEDs on; hold (enable) is low -> output frozen
        ev = [
            (200, "sw", 0b0011),       # sources change while held: no effect
            (1000, "btnC", 1),         # reset: follow the live (inverted) inputs
            (1500, "sw", 0b0101),      # still following while reset is held
            (2000, "btnC", 0),         # release: frozen again
            (2500, "sw", 0b1111),      # no effect
            (3000, "btnU", 1),         # enable: follow
            (3500, "sw", 0b1000),
        ]
        pts = series(spec, await sim.run("hold", ev, 4500))
        assert [v for _, v in pts] == [0b1111, 0b1100, 0b1010, 0b0000, 0b0111]
        assert near(pts[1][0], 1000 + d + 2) and near(pts[2][0], 1500 + d + 2)
        assert near(pts[3][0], 3000 + d + 2) and near(pts[4][0], 3500 + d + 2)


# --------------------------------------------------------------------------- traffic_light behaviour


def state_runs(pts: list[tuple[int, int]], end: int) -> list[tuple[int, int, int]]:
    """[(start cycle, duration, lights)] for every state visit fully inside the trace."""
    out = []
    for (c, v), (nc, _nv) in zip(pts, pts[1:]):
        out.append((c, nc - c, v))
    return out


@pytest.mark.verilator
async def test_traffic_light_example(tmp_path: Path) -> None:
    spec = example("traffic_light_spec.json")
    sim = await build_sim(spec, tmp_path)
    g, y, r, w = (sim.params[f"TICKS_STATE_{n}"] for n in ("GREEN", "YELLOW", "RED", "WALK"))
    d = sim.deb("walk_btn")
    G, Y, R, W = 0b0001, 0b0010, 0b0100, 0b1100
    cycle = g + y + r

    # No request: green -> yellow -> red -> green ..., exact durations, starts in green with timer 0.
    pts = series(spec, await sim.run("free", [], 3 * cycle + 10))
    assert state_runs(pts, 3 * cycle) == [
        (0, g, G), (g, y, Y), (g + y, r, R),
        (cycle, g, G), (cycle + g, y, Y), (cycle + g + y, r, R),
        (2 * cycle, g, G), (2 * cycle + g, y, Y), (2 * cycle + g + y, r, R),
    ]

    # A short BTNL press during green inserts 'walk' after the next red, once; a press during walk
    # is latched again and served after the following red.
    second = 90_000 - 80_000 + cycle  # inside the walk phase
    ev = [(1000, "btnL", 1), (1300, "btnL", 0), (second, "btnL", 1), (second + 300, "btnL", 0)]
    end = 3 * cycle + 2 * w  # G Y R W G Y R W G Y R
    pts = series(spec, await sim.run("request", ev, end + 10))
    runs = state_runs(pts, end)
    assert [lv for _, _, lv in runs] == [G, Y, R, W, G, Y, R, W, G, Y, R]
    assert [dur for _, dur, _ in runs] == [g, y, r, w, g, y, r, w, g, y, r]
    assert runs[3][0] == cycle and runs[3][0] < second + d < runs[4][0]

    # Reset during yellow: back to green (held), latch cleared, green lasts a full period after release.
    press, rst_on, rst_off = 1000, g + y // 2, g + y + r // 2
    ev = [(press, "btnL", 1), (press + 300, "btnL", 0), (rst_on, "btnC", 1), (rst_off, "btnC", 0)]
    end = rst_off + 2 * cycle
    pts = series(spec, await sim.run("reset", ev, end))
    assert value_at(pts, rst_on - 1) == Y
    assert near(between(pts, rst_on, end)[0][0], rst_on + d + 1) and value_at(pts, rst_on + d + 4) == G
    assert between(pts, rst_on + d + 4, rst_off + d) == []
    runs = state_runs(between(pts, rst_on + d + 4, end), end)
    assert near(runs[0][0], rst_off + d + g + 1) and runs[0][2] == Y
    assert [lv for _, _, lv in runs] == [Y, R, G, Y, R][: len(runs)] and len(runs) >= 4  # no walk: latch cleared
    assert [dur for _, dur, _ in runs[:3]] == [y, r, g]


@pytest.mark.verilator
@pytest.mark.parametrize("kind", ["two_state_no_request", "request_after_first", "request_state_without_input"])
async def test_traffic_light_variants(kind: str, tmp_path: Path) -> None:
    spec = traffic_variant(kind)
    sim = await build_sim(spec, tmp_path)
    p = sim.params
    if kind == "two_state_no_request":
        a, b = p["TICKS_STATE_A"], p["TICKS_STATE_B"]
        pts = series(spec, await sim.run("free", [], 3 * (a + b) - 1))  # stop before the next switch
        assert pts == [(0, 1), (a, 2), (a + b, 1), (2 * a + b, 2), (2 * (a + b), 1), (3 * a + 2 * b, 2)]
    elif kind == "request_after_first":
        go, slow, stop, allred = (p[f"TICKS_STATE_{n}"] for n in ("GO", "SLOW", "STOP", "ALLRED"))
        d = sim.deb("req_sw")
        # Switch goes up during 'stop' and stays up: only its rising edge counts -> exactly one insertion.
        up = go + slow + stop // 2
        end = up + 3 * (go + slow + stop) + allred
        pts = series(spec, await sim.run("req", [(up, "sw", 1 << 3)], end))
        runs = state_runs(pts, end)
        assert [lv for _, _, lv in runs] == [1, 2, 4, 1, 0, 2, 4, 1, 2, 4, 1, 2][: len(runs)]
        assert len(runs) >= 10 and runs[3][0] < up + d < runs[4][0] or runs[2][0] < up + d < runs[3][0]
        assert [dur for _, dur, _ in runs[:9]] == [go, slow, stop, go, allred, slow, stop, go, slow]
    else:
        on, off = p["TICKS_STATE_ON"], p["TICKS_STATE_OFF"]
        pts = series(spec, await sim.run("free", [], 4 * (on + off) - 1))
        assert [v for _, v in pts] == [3, 0] * 4  # 'extra' is never entered without a request input
        assert set(intervals(pts)) == {on, off}


@pytest.mark.parametrize("pattern", ["chase", "bounce"])
def test_single_led_moving_dot_is_refused(pattern: str) -> None:
    rate = [{"name": "normal", "hz": 1}]
    spec = make_spec(f"one-LED {pattern}", [], [3], {"family": "led_pattern", "pattern": pattern, "rate_modes": rate})
    with pytest.raises(TemplateUnavailable):
        generate_core(spec)
