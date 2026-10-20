"""Deterministic, preverified cl_core generators for the supported circuit families.

These are used (a) when no LLM provider is configured, (b) as known-good
references in the test suite, and (c) never as a silent substitute for a failed
LLM generation -- the pipeline always records which generator produced a
revision.
"""

from __future__ import annotations

from backend.specifications.ir import (
    CounterBehavior,
    DesignSpec,
    RateMode,
)
from backend.rtl.wrapper import GENERATED_BANNER, core_interface


class TemplateUnavailable(Exception):
    pass


def _ticks_name(mode: RateMode) -> str:
    return f"TICKS_{mode.name.upper()}"


def _max_chain(names: list[str]) -> str:
    expr = names[0]
    for n in names[1:]:
        expr = f"(({n}) > ({expr}) ? ({n}) : ({expr}))"
    return expr


def _period_mux(modes: list[RateMode], tw: str = "TW") -> str:
    """Priority mux selecting the active TICKS_* value (first matching mode wins)."""
    lines = []
    for m in modes[:-1]:
        lvl = "1'b1" if m.when_level == 1 else "1'b0"
        lines.append(f"        ({m.when_input} == {lvl}) ? {_ticks_name(m)}[{tw}-1:0] :")
    lines.append(f"        {_ticks_name(modes[-1])}[{tw}-1:0];")
    return "\n".join(lines)


def _counter_core(spec: DesignSpec, b: CounterBehavior) -> str:
    out = spec.output(b.output)
    w = out.width
    maxv = (1 << w) - 1
    if not (0 <= b.reset_value <= maxv and 0 <= b.initial_value <= maxv):
        raise TemplateUnavailable("reset/initial value does not fit the output width")
    ticks = [_ticks_name(m) for m in b.rate_modes]

    if b.direction_control is not None:
        dc = b.direction_control
        up_expr = dc.input if dc.high_means == "up" else f"~{dc.input}"
    else:
        up_expr = "1'b1" if b.direction == "up" else "1'b0"
    reset_expr = b.reset_input or "1'b0"
    enable_expr = b.enable_input or "1'b1"
    inc = f"value + {w}'d1" if b.wrap else f"(value == {w}'d{maxv}) ? value : value + {w}'d1"
    dec = f"value - {w}'d1" if b.wrap else f"(value == {w}'d0) ? value : value - {w}'d1"

    return f"""// {GENERATED_BANNER}
// Generator: template/counter
`default_nettype none
{core_interface(spec)}
    localparam integer TMAX = {_max_chain(ticks)};
    localparam integer TW   = $clog2(TMAX + 1);

    wire          counting_up = {up_expr};
    wire          hold_reset  = {reset_expr};
    wire          run         = {enable_expr};
    wire [TW-1:0] period =
{_period_mux(b.rate_modes)}
    wire          step;

    cl_tick_div #(.WIDTH(TW)) u_tick (
        .clk(clk), .clear(hold_reset), .enable(run), .period(period), .tick(step)
    );

    reg [{w - 1}:0] value = {w}'d{b.initial_value};

    always @(posedge clk) begin
        if (hold_reset) begin
            value <= {w}'d{b.reset_value};
        end else if (step) begin
            value <= counting_up ? ({inc}) : ({dec});
        end
    end

    assign {b.output} = value;

    /* verilator lint_off UNUSEDPARAM */
    localparam integer UNUSED_CLK_HZ = CLK_HZ;
    /* verilator lint_on UNUSEDPARAM */
endmodule
`default_nettype wire
"""


def generate_core(spec: DesignSpec) -> str:
    b = spec.behavior
    if isinstance(b, CounterBehavior):
        return _counter_core(spec, b)
    raise TemplateUnavailable(f"no template generator for family '{spec.family}' yet")
