"""Board-capability and consistency validation for a DesignSpec.

`ir.py` enforces structure (types, ranges, references). This module answers the
next question: can CircuitLoop actually build, simulate and observe this design
on a Basys 3? Findings are graded:

  error    the pipeline must not continue (the design cannot be generated,
           simulated or built as specified)
  warning  it will build, but something is probably not what the user wants or
           cannot be verified (e.g. too fast for the camera)
  info     neutral facts worth showing (quantization, observation length)

Every condition that would make a preverified template generator raise
`TemplateUnavailable` is an error here, so a validated spec always has a
deterministic reference implementation.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Literal, Optional

from pydantic import BaseModel

from backend.specifications.derive import (
    CAMERA_ASSUMED_FPS,
    CAMERA_MIN_FRAMES_PER_STATE,
    MAX_CAMERA_EVENT_HZ,
    derive_observations,
)
from backend.specifications.ir import (
    BUTTON_PORTS,
    BlinkerBehavior,
    CounterBehavior,
    DesignSpec,
    LedPatternBehavior,
    RateMode,
    TrafficLightBehavior,
    referenced_inputs,
)
from backend.specifications.timing import timing_params, timing_problems

DEFAULT_SIM_CLK_HZ = 10_000
QUANTIZATION_INFO_THRESHOLD = 0.005  # relative step-period error worth mentioning
LONG_OBSERVATION_S = 120.0
MIN_CAMERA_STATE_S = CAMERA_MIN_FRAMES_PER_STATE / CAMERA_ASSUMED_FPS


class ValidationIssue(BaseModel):
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    path: Optional[str] = None


class ValidationReport(BaseModel):
    ok: bool
    issues: list[ValidationIssue]

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]


def _sim_clk_hz() -> int:
    try:
        from backend.core.config import get_settings

        return int(get_settings().sim_clk_hz)
    except Exception:  # configuration problems must not hide spec problems
        return DEFAULT_SIM_CLK_HZ


def validate_spec(spec: DesignSpec, sim_clk_hz: int | None = None) -> ValidationReport:
    """Validate `spec` against the Basys 3 and the CircuitLoop toolchain."""
    v = _Validator(spec, sim_clk_hz or _sim_clk_hz())
    v.run()
    return ValidationReport(ok=not any(i.severity == "error" for i in v.issues), issues=v.issues)


class _Validator:
    def __init__(self, spec: DesignSpec, sim_clk_hz: int) -> None:
        self.spec = spec
        self.sim_clk_hz = sim_clk_hz
        self.issues: list[ValidationIssue] = []

    def add(self, severity: str, code: str, message: str, path: str | None = None) -> None:
        self.issues.append(ValidationIssue(severity=severity, code=code, message=message, path=path))  # type: ignore[arg-type]

    # ------------------------------------------------------------------ driver
    def run(self) -> None:
        self.board_and_io()
        self.timing()
        b = self.spec.behavior
        if isinstance(b, CounterBehavior):
            self.counter(b)
        elif isinstance(b, BlinkerBehavior):
            self.blinker(b)
        elif isinstance(b, LedPatternBehavior):
            self.led_pattern(b)
        elif isinstance(b, TrafficLightBehavior):
            self.traffic_light(b)
        self.rate_modes()
        self.input_roles()
        self.camera()

    # ------------------------------------------------------------------ board / io
    def board_and_io(self) -> None:
        spec = self.spec
        leds = [i for o in spec.outputs for i in o.indices]
        if len(leds) > 16:
            self.add("error", "too-many-leds", f"the Basys 3 has 16 LEDs; the design drives {len(leds)}", "outputs")
        b = spec.behavior
        for k, o in enumerate(spec.outputs):
            if o.name != b.output:
                self.add("warning", "output-not-driven",
                         f"output '{o.name}' ({_leds(o.indices)}) is not driven by the {spec.family} behaviour "
                         "and will stay off", f"outputs[{k}]")
        used = set(referenced_inputs(b))
        for k, i in enumerate(spec.inputs):
            if i.name not in used:
                self.add("warning", "input-unused",
                         f"input '{i.name}' ({i.board_signal}) is not used by the behaviour", f"inputs[{k}]")
            if i.debounce_ms == 0:
                if i.port in BUTTON_PORTS:
                    self.add("warning", "button-no-debounce",
                             f"button {i.board_signal} ('{i.name}') has no debounce; mechanical bounce can register "
                             "as several presses (10 ms is typical)", f"inputs[{k}].debounce_ms")
                else:
                    self.add("info", "switch-no-debounce",
                             f"switch {i.board_signal} ('{i.name}') is not debounced; it may flicker for a few "
                             "milliseconds when moved", f"inputs[{k}].debounce_ms")

    # ------------------------------------------------------------------ timing
    def timing(self) -> None:
        spec = self.spec
        for p in timing_problems(spec, spec.clock_hz):
            self.add("error", "timing-real", f"at the 100 MHz board clock: {p}", "behavior")
        if self.sim_clk_hz != spec.clock_hz:
            for p in timing_problems(spec, self.sim_clk_hz):
                self.add("error", "timing-sim",
                         f"cannot be simulated at the accelerated {self.sim_clk_hz} Hz clock: {p}", "behavior")
        b = spec.behavior
        clk = Fraction(self.sim_clk_hz)
        for m in getattr(b, "rate_modes", []) or []:
            divisor = 2 if isinstance(b, BlinkerBehavior) else 1
            exact = clk / (Fraction(str(m.hz)) * divisor)
            ticks = next(p.value for p in timing_params(spec, self.sim_clk_hz) if p.name == f"TICKS_{m.name.upper()}")
            if exact > 0 and ticks > 0:
                err = abs(Fraction(ticks) - exact) / exact
                if err > QUANTIZATION_INFO_THRESHOLD:
                    self.add("info", "rate-quantization",
                             f"mode '{m.name}' ({m.hz:g}/s) is quantized to {ticks} cycles at the {self.sim_clk_hz} Hz "
                             f"simulation clock ({float(err) * 100:.2f}% period error in simulation only; "
                             "negligible at 100 MHz)", "behavior.rate_modes")

    # ------------------------------------------------------------------ families
    def counter(self, b: CounterBehavior) -> None:
        w = self.spec.output(b.output).width
        maxv = (1 << w) - 1
        for field in ("reset_value", "initial_value"):
            val = getattr(b, field)
            if not 0 <= val <= maxv:
                self.add("error", "value-range",
                         f"{field.replace('_', ' ')} {val} does not fit the {w}-bit output (0..{maxv})",
                         f"behavior.{field}")
        if b.reset_input is None and b.reset_value != 0:
            self.add("warning", "reset-value-unused", "reset_value is set but there is no reset_input", "behavior.reset_value")
        if b.direction_control is not None:
            self.add("info", "direction-control",
                     f"direction is set at run time by '{b.direction_control.input}'; the static direction "
                     f"'{b.direction}' is not used", "behavior.direction_control")
        if w == 1:
            self.add("info", "one-bit-counter", "a 1-bit counter simply toggles its LED", "behavior")

    def blinker(self, b: BlinkerBehavior) -> None:
        w = self.spec.output(b.output).width
        if b.pattern == "alternate" and w == 1:
            self.add("warning", "alternate-one-led",
                     "the 'alternate' pattern needs at least two LEDs; with one LED it is the same as 'together'",
                     "behavior.pattern")

    def led_pattern(self, b: LedPatternBehavior) -> None:
        w = self.spec.output(b.output).width
        static = b.pattern in ("mirror_inputs", "invert_inputs")
        if static:
            if not b.source_inputs:
                self.add("error", "pattern-needs-sources", f"pattern '{b.pattern}' needs source_inputs", "behavior.source_inputs")
            elif len(b.source_inputs) != w:
                self.add("error", "source-width-mismatch",
                         f"pattern '{b.pattern}' needs exactly one source input per LED: "
                         f"{len(b.source_inputs)} sources for a {w}-LED output", "behavior.source_inputs")
            if len(set(b.source_inputs)) != len(b.source_inputs):
                self.add("warning", "duplicate-sources", "the same input drives more than one LED", "behavior.source_inputs")
            if b.rate_modes:
                self.add("warning", "rate-ignored", f"rate_modes have no effect on '{b.pattern}'", "behavior.rate_modes")
            if b.direction_control is not None:
                self.add("warning", "direction-ignored", f"direction_control has no effect on '{b.pattern}'",
                         "behavior.direction_control")
        else:
            if not b.rate_modes:
                self.add("error", "pattern-needs-rate",
                         f"animated pattern '{b.pattern}' needs at least one rate mode (steps per second)",
                         "behavior.rate_modes")
            if b.source_inputs:
                self.add("warning", "sources-ignored", f"source_inputs have no effect on '{b.pattern}'",
                         "behavior.source_inputs")
            if w == 1 and b.pattern in ("chase", "bounce"):
                self.add("error", "pattern-needs-two-leds",
                         f"a '{b.pattern}' pattern needs at least 2 LEDs (a single LED would just stay on)", "outputs")
            elif w == 1:
                self.add("warning", "pattern-one-led",
                         f"a '{b.pattern}' pattern on a single LED just toggles it", "outputs")
            if b.pattern == "bounce" and (b.direction != "left" or b.direction_control is not None):
                self.add("warning", "direction-ignored",
                         "a bounce pattern always starts at the lowest LED moving up and reverses at each end; "
                         "direction settings do not apply", "behavior.direction")

    def traffic_light(self, b: TrafficLightBehavior) -> None:
        w = self.spec.output(b.output).width
        maxv = (1 << w) - 1
        states = list(b.states) + ([b.request_state] if b.request_state else [])
        names = [s.name for s in states]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            self.add("error", "duplicate-state", f"state names must be unique (including request_state): {dup}",
                     "behavior.states")
        for k, s in enumerate(b.states):
            if s.lights > maxv:
                self.add("error", "lights-width",
                         f"state '{s.name}' lights {s.lights:#x} do not fit the {w}-LED output (max {maxv:#x})",
                         f"behavior.states[{k}].lights")
            if s.lights == 0:
                self.add("info", "dark-state", f"state '{s.name}' turns every light off", f"behavior.states[{k}].lights")
        if b.request_state is not None and b.request_state.lights > maxv:
            self.add("error", "lights-width",
                     f"request state '{b.request_state.name}' lights {b.request_state.lights:#x} do not fit the "
                     f"{w}-LED output", "behavior.request_state.lights")
        for k in range(len(b.states)):
            a, c = b.states[k], b.states[(k + 1) % len(b.states)]
            if a.lights == c.lights and a.name != c.name:
                self.add("warning", "indistinguishable-states",
                         f"consecutive states '{a.name}' and '{c.name}' show the same lights; the camera cannot "
                         "see the transition between them", f"behavior.states[{k}]")
        if b.request_input:
            if b.request_state is None or b.request_after is None:
                self.add("error", "request-incomplete",
                         "request_input needs both request_state and request_after", "behavior.request_input")
        else:
            if b.request_state is not None:
                self.add("warning", "request-state-unreachable",
                         f"request state '{b.request_state.name}' is never entered without a request_input",
                         "behavior.request_state")
            if b.request_after is not None:
                self.add("warning", "request-after-unused", "request_after has no effect without request_input",
                         "behavior.request_after")
        if b.request_after is not None and b.request_after not in [s.name for s in b.states]:
            self.add("error", "request-after-unknown",
                     f"request_after '{b.request_after}' is not one of the states {[s.name for s in b.states]}",
                     "behavior.request_after")

    # ------------------------------------------------------------------ cross-cutting
    def rate_modes(self) -> None:
        b = self.spec.behavior
        modes: list[RateMode] = getattr(b, "rate_modes", []) or []
        seen: dict[tuple[str, int], str] = {}
        for k, m in enumerate(modes[:-1]):
            key = (m.when_input or "", m.when_level)
            if key in seen:
                self.add("warning", "unreachable-mode",
                         f"mode '{m.name}' can never be active: mode '{seen[key]}' has the same condition and comes first",
                         f"behavior.rate_modes[{k}]")
            else:
                seen[key] = m.name
        hz = [m.hz for m in modes]
        if len(hz) >= 2 and len(set(hz)) == 1:
            self.add("warning", "identical-rates", "all rate modes have the same rate, so switching modes has no "
                     "visible effect", "behavior.rate_modes")

    def input_roles(self) -> None:
        b = self.spec.behavior
        roles: dict[str, list[str]] = {}
        for field in ("reset_input", "enable_input", "request_input"):
            v = getattr(b, field, None)
            if v:
                roles.setdefault(v, []).append(field.replace("_input", ""))
        dc = getattr(b, "direction_control", None)
        if dc is not None:
            roles.setdefault(dc.input, []).append("direction")
        for m in getattr(b, "rate_modes", []) or []:
            if m.when_input:
                roles.setdefault(m.when_input, []).append(f"speed mode '{m.name}'")
        for name, r in roles.items():
            if len(r) > 1:
                self.add("warning", "input-multiple-roles",
                         f"input '{name}' has several roles ({', '.join(r)}); they will interact", "behavior")

    def camera(self) -> None:
        b = self.spec.behavior
        for m in getattr(b, "rate_modes", []) or []:
            if isinstance(b, LedPatternBehavior) and b.pattern in ("mirror_inputs", "invert_inputs"):
                continue
            visible = 2 * m.hz if isinstance(b, BlinkerBehavior) else m.hz
            if visible > MAX_CAMERA_EVENT_HZ:
                self.add("warning", "camera-too-fast",
                         f"mode '{m.name}' changes the LEDs {visible:g} times per second; the webcam check can resolve "
                         f"at most {MAX_CAMERA_EVENT_HZ:g} changes/s at {CAMERA_ASSUMED_FPS:g} fps, so this rate is "
                         "verified in simulation only", "behavior.rate_modes")
        if isinstance(b, TrafficLightBehavior):
            states = list(b.states) + ([b.request_state] if b.request_state else [])
            for s in states:
                if s.duration_s < MIN_CAMERA_STATE_S:
                    self.add("warning", "camera-state-too-short",
                             f"state '{s.name}' lasts {s.duration_s:g} s, shorter than the {MIN_CAMERA_STATE_S:.2f} s "
                             "the webcam needs to measure it reliably", "behavior.states")
        try:
            obs = derive_observations(self.spec)
        except Exception:  # derive relies on consistency errors reported above
            return
        for o in obs:
            if o.camera_verifiable and o.min_duration_s > LONG_OBSERVATION_S:
                self.add("info", "long-observation",
                         f"camera observation {o.id} needs about {o.min_duration_s:g} s of video", "behavior")


def _leds(indices: list[int]) -> str:
    return ", ".join(f"LED{i}" for i in indices)
