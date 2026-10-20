"""Derive verifiable requirements and camera observations from a DesignSpec.

This is the shared contract between the simulation oracle and the webcam
verifier. Both refer to requirement ids from here, so every report can say
exactly which requirements were verified in simulation, which were physically
observed, and which could not be observed by a camera at all.

Requirement ids are stable for a given spec (they are derived, not random).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .ir import (
    BlinkerBehavior,
    CounterBehavior,
    DesignSpec,
    LedPatternBehavior,
    RateMode,
    TrafficLightBehavior,
)

# A rate is camera-verifiable when each visible state lasts at least this many
# frames at the assumed camera frame rate (keeps well clear of aliasing).
CAMERA_ASSUMED_FPS = 30.0
CAMERA_MIN_FRAMES_PER_STATE = 4.0
MAX_CAMERA_EVENT_HZ = CAMERA_ASSUMED_FPS / CAMERA_MIN_FRAMES_PER_STATE  # 7.5 visible changes per second

Method = Literal["simulation", "physical"]


class Requirement(BaseModel):
    id: str
    text: str
    category: Literal["output", "rate", "sequence", "reset", "enable", "direction", "input", "wrap", "fsm", "pattern"]
    methods: list[Method]
    physical_note: Optional[str] = None  # why it can / cannot be observed by camera


class Condition(BaseModel):
    """Input state the user must hold during an observation phase."""

    input: str
    board_signal: str
    level: Literal[0, 1]
    instruction: str  # e.g. "Press and hold BTNU"


class Observation(BaseModel):
    """A camera measurement the vision module can perform."""

    id: str
    requirement_ids: list[str]
    kind: Literal["step_rate", "toggle_rate", "rate_ratio", "count_sequence", "state_durations", "static_pattern"]
    description: str
    leds: list[int]  # physical LED indices involved, core bit order
    conditions: list[Condition] = Field(default_factory=list)
    expected: dict = Field(default_factory=dict)
    min_duration_s: float = 0.0
    camera_verifiable: bool = True
    reason_not_verifiable: Optional[str] = None


def _mode_condition(spec: DesignSpec, m: RateMode) -> list[Condition]:
    if not m.when_input:
        return []
    b = spec.input(m.when_input)
    verb = "Press and hold" if b.port != "sw" else "Set"
    state = "" if b.port != "sw" else (" up (on)" if m.when_level else " down (off)")
    if b.port != "sw" and m.when_level == 0:
        verb = "Release"
    return [Condition(input=b.name, board_signal=b.board_signal, level=m.when_level,
                      instruction=f"{verb} {b.board_signal.upper() if b.port != 'sw' else b.board_signal}{state}")]


def _default_conditions(spec: DesignSpec, modes: list[RateMode]) -> list[Condition]:
    """Conditions for the default (last) mode: every earlier mode's input inactive."""
    conds: list[Condition] = []
    for m in modes[:-1]:
        b = spec.input(m.when_input)  # type: ignore[arg-type]
        lvl = 1 - m.when_level
        if b.port == "sw":
            text = f"Set {b.board_signal} {'up (on)' if lvl else 'down (off)'}"
        else:
            text = f"Keep {b.board_signal.upper()} {'pressed' if lvl else 'released'}"
        conds.append(Condition(input=b.name, board_signal=b.board_signal, level=lvl, instruction=text))
    return conds


def _visible_hz(spec: DesignSpec, hz: float) -> float:
    """Fastest visible LED change rate produced by one behaviour step rate."""
    if isinstance(spec.behavior, BlinkerBehavior):
        return 2 * hz  # toggles per second
    return hz  # LSB of a counter / one pattern step changes once per step


def derive_requirements(spec: DesignSpec) -> list[Requirement]:
    b = spec.behavior
    out = spec.output(b.output)
    reqs: list[Requirement] = []
    leds = ", ".join(f"LED{i}" for i in out.indices)

    def add(rid, text, cat, methods, note=None):
        reqs.append(Requirement(id=rid, text=text, category=cat, methods=methods, physical_note=note))

    unused = [i for i in range(16) if i not in {x for o in spec.outputs for x in o.indices}]
    if unused:
        add("R-IDLE", f"LEDs not used by the design ({len(unused)} LEDs) stay off", "output", ["simulation", "physical"])

    for i in spec.inputs:
        if i.debounce_ms > 0:
            add(f"R-DEB-{i.name}", f"{i.board_signal} is synchronized and debounced ({i.debounce_ms:g} ms); "
                "bounces shorter than the debounce time have no effect", "input", ["simulation"],
                "contact bounce lasts microseconds-milliseconds; a 30 fps camera cannot observe it")

    if isinstance(b, CounterBehavior):
        direction = "up" if b.direction == "up" else "down"
        if b.direction_control:
            add("R-DIR", f"counts up when {b.direction_control.input} is "
                f"{'high' if b.direction_control.high_means == 'up' else 'low'}, otherwise down", "direction",
                ["simulation", "physical"])
        else:
            add("R-DIR", f"{out.width}-bit counter on {leds} counts {direction} by exactly 1 per step", "sequence",
                ["simulation", "physical"])
        for m in b.rate_modes:
            cond = f" while {m.when_input} is {'high' if m.when_level else 'low'}" if m.when_input else ""
            vis = _visible_hz(spec, m.hz) <= MAX_CAMERA_EVENT_HZ
            add(f"R-RATE-{m.name}", f"steps at {m.hz:g} per second{cond}", "rate",
                ["simulation", "physical"] if vis else ["simulation"],
                None if vis else f"{m.hz:g} steps/s exceeds the camera limit of {MAX_CAMERA_EVENT_HZ:g} changes/s")
        if len(b.rate_modes) >= 2:
            fast, slow = b.rate_modes[0], b.rate_modes[-1]
            add("R-RATIO", f"mode '{fast.name}' is {fast.hz / slow.hz:g}x the speed of mode '{slow.name}'", "rate",
                ["simulation", "physical"])
        if b.wrap:
            add("R-WRAP", f"wraps around ({'15' if out.width == 4 else 2**out.width - 1} -> 0 when counting up, 0 -> max when counting down)",
                "wrap", ["simulation", "physical"])
        else:
            add("R-WRAP", "saturates at the end of its range instead of wrapping", "wrap", ["simulation", "physical"])
        add("R-INIT", f"starts at {b.initial_value} after configuration", "sequence", ["simulation"],
            "the first frames after programming are usually missed by the camera")
        if b.reset_input:
            add("R-RESET", f"while {b.reset_input} is held the value is {b.reset_value}; counting resumes after release",
                "reset", ["simulation", "physical"])
        if b.enable_input:
            add("R-ENABLE", f"pauses while {b.enable_input} is low", "enable", ["simulation", "physical"])

    elif isinstance(b, BlinkerBehavior):
        for m in b.rate_modes:
            cond = f" while {m.when_input} is {'high' if m.when_level else 'low'}" if m.when_input else ""
            vis = _visible_hz(spec, m.hz) <= MAX_CAMERA_EVENT_HZ
            add(f"R-RATE-{m.name}", f"{leds} blink at {m.hz:g} Hz with 50% duty{cond}", "rate",
                ["simulation", "physical"] if vis else ["simulation"],
                None if vis else f"{2 * m.hz:g} toggles/s exceeds the camera limit of {MAX_CAMERA_EVENT_HZ:g}")
        add("R-PATTERN", "all LEDs blink together" if b.pattern == "together" else "even and odd LEDs alternate",
            "pattern", ["simulation", "physical"])
        if b.enable_input:
            add("R-ENABLE", f"LEDs stay off while {b.enable_input} is low", "enable", ["simulation", "physical"])

    elif isinstance(b, LedPatternBehavior):
        if b.pattern in ("mirror_inputs", "invert_inputs"):
            add("R-PATTERN", f"{leds} {'follow' if b.pattern == 'mirror_inputs' else 'show the inverse of'} "
                f"{', '.join(b.source_inputs)}", "pattern", ["simulation", "physical"])
        else:
            add("R-PATTERN", f"{b.pattern} pattern moving {b.direction} on {leds}", "pattern", ["simulation", "physical"])
            for m in b.rate_modes:
                cond = f" while {m.when_input} is {'high' if m.when_level else 'low'}" if m.when_input else ""
                vis = _visible_hz(spec, m.hz) <= MAX_CAMERA_EVENT_HZ
                add(f"R-RATE-{m.name}", f"advances {m.hz:g} steps per second{cond}", "rate",
                    ["simulation", "physical"] if vis else ["simulation"])
            if b.direction_control:
                add("R-DIR", f"direction set by {b.direction_control.input}", "direction", ["simulation", "physical"])
        if b.enable_input:
            add("R-ENABLE", f"pattern holds while {b.enable_input} is low", "enable", ["simulation", "physical"])
        if b.reset_input:
            add("R-RESET", f"{b.reset_input} restarts the pattern", "reset", ["simulation", "physical"])

    elif isinstance(b, TrafficLightBehavior):
        seq = " -> ".join(f"{s.name} ({s.duration_s:g} s)" for s in b.states)
        add("R-FSM-SEQ", f"cycles {seq} and repeats", "fsm", ["simulation", "physical"])
        add("R-FSM-OUT", "each state drives exactly its specified light pattern", "output", ["simulation", "physical"])
        for s in b.states:
            add(f"R-FSM-T-{s.name}", f"state {s.name} lasts {s.duration_s:g} s", "rate", ["simulation", "physical"])
        if b.request_input and b.request_state:
            add("R-FSM-REQ", f"a press of {b.request_input} is latched and inserts state {b.request_state.name} "
                f"after {b.request_after}", "fsm", ["simulation", "physical"])
        if b.reset_input:
            add("R-RESET", f"{b.reset_input} returns the FSM to {b.states[0].name}", "reset", ["simulation", "physical"])
    return reqs


def derive_observations(spec: DesignSpec) -> list[Observation]:
    b = spec.behavior
    out = spec.output(b.output)
    obs: list[Observation] = []

    def visible(hz: float) -> tuple[bool, str | None]:
        v = _visible_hz(spec, hz)
        if v > MAX_CAMERA_EVENT_HZ:
            return False, f"{v:g} visible changes/s exceeds {MAX_CAMERA_EVENT_HZ:g}/s at {CAMERA_ASSUMED_FPS:g} fps"
        return True, None

    modes = getattr(b, "rate_modes", []) or []
    if isinstance(b, (CounterBehavior, BlinkerBehavior)) or (
        isinstance(b, LedPatternBehavior) and b.pattern not in ("mirror_inputs", "invert_inputs")
    ):
        kind = "toggle_rate" if isinstance(b, BlinkerBehavior) else "step_rate"
        for idx, m in enumerate(modes):
            ok, why = visible(m.hz)
            conds = _mode_condition(spec, m) if m.when_input else _default_conditions(spec, modes)
            steps_needed = 6
            obs.append(Observation(
                id=f"O-RATE-{m.name}",
                requirement_ids=[f"R-RATE-{m.name}"] + (["R-DIR", "R-WRAP"] if isinstance(b, CounterBehavior) else []),
                kind=kind,
                description=f"measure the {'blink' if kind == 'toggle_rate' else 'step'} rate in mode '{m.name}'",
                leds=list(out.indices),
                conditions=conds,
                expected={"hz": m.hz, "visible_changes_per_s": _visible_hz(spec, m.hz), "mode": m.name,
                          "family": spec.family, "width": out.width,
                          "direction": getattr(b, "direction", None), "wrap": getattr(b, "wrap", None)},
                min_duration_s=round(steps_needed / m.hz + 1.0, 1) if kind == "step_rate" else round(steps_needed / (2 * m.hz) + 1.0, 1),
                camera_verifiable=ok,
                reason_not_verifiable=why,
            ))
        if len(modes) >= 2:
            fast, slow = modes[0], modes[-1]
            both_ok = visible(fast.hz)[0] and visible(slow.hz)[0]
            obs.append(Observation(
                id="O-RATIO",
                requirement_ids=["R-RATIO"] if isinstance(b, CounterBehavior) else [f"R-RATE-{fast.name}", f"R-RATE-{slow.name}"],
                kind="rate_ratio",
                description=f"compare the '{fast.name}' rate against the '{slow.name}' rate",
                leds=list(out.indices),
                expected={"ratio": fast.hz / slow.hz, "numerator_observation": f"O-RATE-{fast.name}",
                          "denominator_observation": f"O-RATE-{slow.name}"},
                camera_verifiable=both_ok,
                reason_not_verifiable=None if both_ok else "one of the rates is too fast for the camera",
            ))
    if isinstance(b, TrafficLightBehavior):
        obs.append(Observation(
            id="O-FSM",
            requirement_ids=["R-FSM-SEQ", "R-FSM-OUT"] + [f"R-FSM-T-{s.name}" for s in b.states],
            kind="state_durations",
            description="record the light sequence and the time spent in each state",
            leds=list(out.indices),
            expected={"states": [{"name": s.name, "duration_s": s.duration_s, "lights": s.lights} for s in b.states]},
            min_duration_s=round(sum(s.duration_s for s in b.states) * 2 + 1.0, 1),
        ))
    if isinstance(b, LedPatternBehavior) and b.pattern in ("mirror_inputs", "invert_inputs"):
        obs.append(Observation(
            id="O-STATIC",
            requirement_ids=["R-PATTERN"],
            kind="static_pattern",
            description="toggle the source inputs and confirm the LEDs follow",
            leds=list(out.indices),
            expected={"pattern": b.pattern, "sources": [spec.input(n).board_signal for n in b.source_inputs]},
            min_duration_s=5.0,
        ))
    return obs
