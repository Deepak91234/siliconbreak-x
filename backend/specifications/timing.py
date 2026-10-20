"""Deterministic conversion of physical timing (Hz, seconds, ms) into cycle counts.

The trusted wrapper exposes every count as a Verilog parameter of basys3_top so
the identical RTL can be built for the real 100 MHz clock or simulated with an
accelerated clock by overriding the parameters (verilator -G...).

Naming contract (shared with the RTL generator and the LLM prompt):
    CLK_HZ                 clock frequency the counts were computed for
    TICKS_<MODE>           cycles between behaviour steps for rate mode <MODE>
                           (counter increment, pattern step, or blinker toggle)
    TICKS_STATE_<STATE>    cycles spent in traffic-light state <STATE>
    DEBOUNCE_<INPUT>       debounce stable-cycles for input <INPUT> (wrapper only)
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .ir import BlinkerBehavior, DesignSpec, TrafficLightBehavior

MAX_TICKS = 2**31 - 1  # Verilog 'integer' parameters are 32-bit signed
MIN_TICKS = 2


@dataclass(frozen=True)
class TimingParam:
    name: str
    value: int
    core: bool  # passed into cl_core (False = consumed by the wrapper only)
    meaning: str


def _round(x: Fraction) -> int:
    return int(x + Fraction(1, 2))  # round half up, x >= 0


def _hz(v: float) -> Fraction:
    return Fraction(str(v))


def timing_params(spec: DesignSpec, clk_hz: int) -> list[TimingParam]:
    """All timing parameters for `spec` at clock `clk_hz`, in declaration order."""
    b = spec.behavior
    out: list[TimingParam] = [TimingParam("CLK_HZ", clk_hz, True, "clock frequency in Hz")]
    clk = Fraction(clk_hz)

    for m in getattr(b, "rate_modes", []) or []:
        if isinstance(b, BlinkerBehavior):
            ticks = _round(clk / (2 * _hz(m.hz)))
            meaning = f"cycles between LED toggles in mode '{m.name}' ({m.hz:g} Hz blink)"
        else:
            ticks = _round(clk / _hz(m.hz))
            meaning = f"cycles between steps in mode '{m.name}' ({m.hz:g} steps/s)"
        out.append(TimingParam(f"TICKS_{m.name.upper()}", ticks, True, meaning))

    if isinstance(b, TrafficLightBehavior):
        states = list(b.states) + ([b.request_state] if b.request_state else [])
        for s in states:
            ticks = _round(clk * Fraction(str(s.duration_s)))
            out.append(
                TimingParam(f"TICKS_STATE_{s.name.upper()}", ticks, True, f"cycles in state '{s.name}' ({s.duration_s:g} s)")
            )

    for i in spec.inputs:
        cycles = _round(clk * Fraction(str(i.debounce_ms)) / 1000) if i.debounce_ms > 0 else 0
        out.append(
            TimingParam(f"DEBOUNCE_{i.name.upper()}", cycles, False, f"debounce cycles for {i.board_signal} ({i.debounce_ms:g} ms)")
        )
    return out


def timing_problems(spec: DesignSpec, clk_hz: int) -> list[str]:
    problems = []
    for p in timing_params(spec, clk_hz):
        if p.name == "CLK_HZ":
            continue
        if p.value > MAX_TICKS:
            problems.append(f"{p.name}={p.value} exceeds the 32-bit parameter range at {clk_hz} Hz")
        if p.name.startswith("TICKS_") and p.value < MIN_TICKS:
            problems.append(f"{p.name}={p.value} is below {MIN_TICKS} cycles at {clk_hz} Hz")
    return problems


def param_dict(spec: DesignSpec, clk_hz: int) -> dict[str, int]:
    return {p.name: p.value for p in timing_params(spec, clk_hz)}
