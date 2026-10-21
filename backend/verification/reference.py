"""Behavioural reference models for the independent simulation oracle.

Everything in this module is derived from the DesignSpec's physical values (Hz,
seconds, milliseconds, widths, directions, reset values) and the simulation clock,
following the normative semantics in docs/contracts.md ("Behavioural semantics the
oracle must check"). Nothing here reads the RTL, and cycle counts are recomputed
from the physical quantities instead of being read back from
backend.specifications.timing: if the trusted parameter generator or a core got the
arithmetic wrong, the two would disagree and the checks would catch it.

Time base (shared with the harness): cycle k is the k-th rising clock edge; a
stimulus event at cycle k is applied just before edge k; trace samples are taken
just after edge k; cycle 0 holds the power-on values.

The models here are deliberately *tolerant* where the contract is: they describe
which output values are legal and how long intervals must be, not the exact cycle
on which a particular implementation updates a register.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol

from backend.specifications.ir import (
    BlinkerBehavior,
    CounterBehavior,
    DesignSpec,
    LedPatternBehavior,
    RateMode,
    TrafficLightBehavior,
    TrafficState,
)

# Contract tolerances: an interval passes if |measured - expected| <= max(4 cycles, 0.2 %).
MIN_TOLERANCE_CYCLES = 4
RELATIVE_TOLERANCE = Fraction(2, 1000)
# Extra cycles allowed after the conditioned input changes before the core must react
# (synchronizer edge alignment + a couple of core registers).
LATENCY_CYCLES = 4


class StimulusLike(Protocol):
    cycle: int
    port: str
    value: int


# --------------------------------------------------------------------------- physical -> cycles


def _round_half_up(x: Fraction) -> int:
    return int(x + Fraction(1, 2))


def _q(v: float) -> Fraction:
    return Fraction(str(v))


def cycles_per_step(clk_hz: int, hz: float) -> int:
    """Cycles between behaviour steps at `hz` steps per second (counter / pattern)."""
    return max(1, _round_half_up(Fraction(clk_hz) / _q(hz)))


def cycles_per_toggle(clk_hz: int, hz: float) -> int:
    """Cycles between LED toggles of a `hz` Hz, 50 % duty blink."""
    return max(1, _round_half_up(Fraction(clk_hz) / (2 * _q(hz))))


def cycles_for_duration(clk_hz: int, seconds: float) -> int:
    return max(1, _round_half_up(Fraction(clk_hz) * _q(seconds)))


def debounce_cycles(clk_hz: int, ms: float) -> int:
    """Stable cycles the trusted input conditioner requires before passing a change."""
    if ms <= 0:
        return 0
    return _round_half_up(Fraction(clk_hz) * _q(ms) / 1000)


def tolerance(expected: float) -> float:
    return max(float(MIN_TOLERANCE_CYCLES), float(RELATIVE_TOLERANCE) * float(expected))


def within_tolerance(measured: float, expected: float) -> bool:
    return abs(measured - expected) <= tolerance(expected)


# --------------------------------------------------------------------------- input conditioning


@dataclass(frozen=True)
class EffChange:
    """One change of a conditioned (synchronized + debounced) input as the core sees it.

    raw_cycle: start of the stable raw run that caused it; eff_cycle: first sample
    at which the conditioned level is the new one. The latency window
    [raw_cycle, eff_cycle + LATENCY_CYCLES] is where either level may apply.
    """

    input: str
    raw_cycle: int
    eff_cycle: int
    level: int

    @property
    def window(self) -> tuple[int, int]:
        return (self.raw_cycle, self.eff_cycle + LATENCY_CYCLES)


def raw_input_levels(spec: DesignSpec, events: Iterable[StimulusLike]) -> dict[str, list[tuple[int, int]]]:
    """Per bound input, the raw pin level as [(cycle, level)] (first entry at cycle 0).

    Events at the same cycle are applied in order (the harness uses a stable sort),
    so the last one wins.
    """
    per_cycle: dict[int, dict[str, int]] = {}
    for ev in sorted(events, key=lambda e: e.cycle):
        for b in spec.inputs:
            if b.port != ev.port:
                continue
            lvl = (int(ev.value) >> int(b.index)) & 1 if b.port == "sw" else int(ev.value) & 1
            per_cycle.setdefault(int(ev.cycle), {})[b.name] = lvl
    out: dict[str, list[tuple[int, int]]] = {b.name: [(0, 0)] for b in spec.inputs}
    for cycle in sorted(per_cycle):
        for name, lvl in per_cycle[cycle].items():
            series = out[name]
            if series[-1][0] == cycle:
                series[-1] = (cycle, lvl)
                if len(series) >= 2 and series[-2][1] == lvl:
                    series.pop()
            elif series[-1][1] != lvl:
                series.append((cycle, lvl))
    return out


def condition_input(name: str, raw: Sequence[tuple[int, int]], debounce: int) -> list[EffChange]:
    """Ideal 2-FF synchronizer + debouncer: a new level is passed on only after the raw
    pin has held it for `debounce` consecutive cycles (bounces shorter than the
    debounce time have no effect). It reaches the core debounce + 1 cycles after the
    stable run starts; events at cycle 0 behave like events at cycle 1 (the
    synchronizer's first sampling edge).
    """
    need = max(debounce, 1)
    level = 0  # conditioned inputs power up low (INIT = 0)
    changes: list[EffChange] = []
    for k, (cycle, lvl) in enumerate(raw):
        start = max(int(cycle), 1)
        nxt = max(int(raw[k + 1][0]), 1) if k + 1 < len(raw) else None
        if lvl == level:
            continue
        if nxt is None or nxt - start >= need:
            changes.append(EffChange(name, start, start + 1 + debounce, int(lvl)))
            level = int(lvl)
    return changes


class InputTimeline:
    """Conditioned view of every bound input for one scenario's stimulus."""

    def __init__(self, spec: DesignSpec, clk_hz: int, events: Iterable[StimulusLike]):
        self.spec = spec
        self.debounce = {b.name: debounce_cycles(clk_hz, b.debounce_ms) for b in spec.inputs}
        self.raw = raw_input_levels(spec, events)
        self.changes: dict[str, list[EffChange]] = {
            name: condition_input(name, series, self.debounce[name]) for name, series in self.raw.items()
        }

    # -- levels ---------------------------------------------------------------
    def level(self, name: str, cycle: int) -> int:
        """Nominal conditioned level at `cycle` (ignores latency windows)."""
        lvl = 0
        for ch in self.changes.get(name, []):
            if ch.eff_cycle <= cycle:
                lvl = ch.level
            else:
                break
        return lvl

    def options(self, name: str, cycle: int) -> tuple[int, ...]:
        """Levels the core may legitimately be acting on at `cycle`."""
        opts = {self.level(name, cycle)}
        prev = 0
        for ch in self.changes.get(name, []):
            a, b = ch.window
            if a <= cycle <= b:
                opts.update((prev, ch.level))
            prev = ch.level
        return tuple(sorted(opts))

    def levels(self, cycle: int, names: Iterable[str] | None = None) -> dict[str, int]:
        names = list(names) if names is not None else list(self.changes)
        return {n: self.level(n, cycle) for n in names}

    def envs(self, cycle: int, names: Iterable[str]) -> list[dict[str, int]]:
        """Every combination of possible levels of `names` at `cycle`."""
        names = list(dict.fromkeys(names))
        opts = [self.options(n, cycle) for n in names]
        return [dict(zip(names, combo)) for combo in itertools.product(*opts)]

    # -- windows --------------------------------------------------------------
    def windows(self, names: Iterable[str] | None = None) -> list[tuple[int, int, str]]:
        names = list(names) if names is not None else list(self.changes)
        out = [(ch.window[0], ch.window[1], n) for n in names for ch in self.changes.get(n, [])]
        return sorted(out)

    def overlapping(self, a: int, b: int, names: Iterable[str] | None = None) -> list[tuple[int, int, str]]:
        return [w for w in self.windows(names) if w[0] <= b and w[1] >= a]

    def rising_edges(self, name: str) -> list[EffChange]:
        return [ch for ch in self.changes.get(name, []) if ch.level == 1]

    def effective_in(self, a: int, b: int, names: Iterable[str] | None = None) -> list[EffChange]:
        names = list(names) if names is not None else list(self.changes)
        return sorted((ch for n in names for ch in self.changes.get(n, []) if a <= ch.eff_cycle <= b),
                      key=lambda c: c.eff_cycle)


# --------------------------------------------------------------------------- behaviour models


class BehaviourModel:
    """Common view of a spec's output bus and inputs."""

    family: str = ""

    def __init__(self, spec: DesignSpec, clk_hz: int):
        self.spec = spec
        self.clk_hz = clk_hz
        self.b = spec.behavior
        self.out = spec.output(self.b.output)
        self.width = self.out.width
        self.mask = (1 << self.width) - 1

    # -- output decoding ---------------------------------------------------------
    def core_value(self, led: int) -> int:
        """Behaviour output value: core bit k is led[indices[k]]."""
        return sum(((led >> idx) & 1) << k for k, idx in enumerate(self.out.indices))

    def unused_led_mask(self) -> int:
        used = {i for o in self.spec.outputs for i in o.indices}
        return sum(1 << i for i in range(16) if i not in used)

    def fmt(self, value: int | None) -> str:
        if value is None:
            return "?"
        return f"{value} (0b{value:0{self.width}b})"

    # -- inputs --------------------------------------------------------------------
    def referenced_inputs(self) -> list[str]:
        from backend.specifications.ir import referenced_inputs

        return list(dict.fromkeys(referenced_inputs(self.b)))

    def legality_inputs(self) -> list[str]:
        """Inputs that change which output transitions are legal."""
        return []

    def initial_value(self) -> int:
        raise NotImplementedError

    def reset_value(self) -> int:
        return self.initial_value()


class PeriodicModel(BehaviourModel):
    """Families that advance one step per tick of the active rate mode."""

    @property
    def modes(self) -> list[RateMode]:
        return list(getattr(self.b, "rate_modes", []) or [])

    def period(self, mode: RateMode) -> int:
        return cycles_per_step(self.clk_hz, mode.hz)

    def selected_mode(self, levels: dict[str, int]) -> RateMode:
        """First mode whose condition holds; the last mode is the unconditional default."""
        for m in self.modes[:-1]:
            if levels.get(m.when_input or "", 0) == m.when_level:
                return m
        return self.modes[-1]

    def paused(self, levels: dict[str, int]) -> bool:
        reset = getattr(self.b, "reset_input", None)
        enable = getattr(self.b, "enable_input", None)
        if reset and levels.get(reset, 0) == 1:
            return True
        if enable and levels.get(enable, 0) == 0:
            return True
        return False

    def active_mode(self, levels: dict[str, int]) -> RateMode | None:
        return None if self.paused(levels) else self.selected_mode(levels)

    def mode_by_name(self, name: str) -> RateMode:
        for m in self.modes:
            if m.name == name:
                return m
        raise KeyError(name)

    # -- nondeterministic state tracking ---------------------------------------------
    def initial_hidden(self) -> set:
        return {self.initial_value()}

    def resync(self, value: int) -> set:
        return {value}

    def successors(self, hidden, env: dict[str, int]) -> list[tuple[int, object]]:
        raise NotImplementedError

    def successors_for(self, hidden, envs: list[dict[str, int]]) -> list[tuple[int, object]]:
        """Union of the legal (value, hidden) successors over every possible input env."""
        return [s for env in envs for s in self.successors(hidden, env)]

    def rule(self) -> str:
        return ""

    def wrap_transitions(self, levels: dict[str, int]) -> list[tuple[int, int]]:
        """Transitions that must be seen when the behaviour runs through its whole range."""
        return []

    def steps_to_wrap(self, levels: dict[str, int]) -> int:
        return 0


class CounterModel(PeriodicModel):
    family = "counter"
    b: CounterBehavior

    def initial_value(self) -> int:
        return self.b.initial_value & self.mask

    def reset_value(self) -> int:
        return self.b.reset_value & self.mask

    def legality_inputs(self) -> list[str]:
        names = [self.b.reset_input, self.b.enable_input]
        if self.b.direction_control:
            names.append(self.b.direction_control.input)
        return [n for n in names if n]

    def direction(self, levels: dict[str, int]) -> str:
        dc = self.b.direction_control
        if dc is None:
            return self.b.direction
        high = levels.get(dc.input, 0) == 1
        if high:
            return dc.high_means
        return "down" if dc.high_means == "up" else "up"

    def step(self, value: int, direction: str) -> int:
        if direction == "up":
            if value == self.mask:
                return 0 if self.b.wrap else value
            return value + 1
        if value == 0:
            return self.mask if self.b.wrap else 0
        return value - 1

    def successors(self, hidden, env):
        v = int(hidden)
        if self.b.reset_input and env.get(self.b.reset_input, 0) == 1:
            return [(self.reset_value(), self.reset_value())]
        if self.b.enable_input and env.get(self.b.enable_input, 0) == 0:
            return []
        nv = self.step(v, self.direction(env))
        return [(nv, nv)] if nv != v else []

    def rule(self) -> str:
        how = "modulo 2^%d" % self.width if self.b.wrap else "saturating at 0 / %d" % self.mask
        return f"each step changes the value by exactly 1 in the active direction ({how})"

    def wrap_transitions(self, levels):
        d = self.direction(levels)
        if self.b.wrap:
            return [(self.mask, 0)] if d == "up" else [(0, self.mask)]
        return []

    def limit(self, levels) -> int:
        return self.mask if self.direction(levels) == "up" else 0

    def steps_to_wrap(self, levels) -> int:
        v0 = self.initial_value()
        if self.direction(levels) == "up":
            return (self.mask - v0) + 1
        return v0 + 1

    def nominal_after(self, steps: int, levels: dict[str, int]) -> int:
        v = self.initial_value()
        for _ in range(steps):
            v = self.step(v, self.direction(levels))
        return v


class BlinkerModel(PeriodicModel):
    family = "blinker"
    b: BlinkerBehavior

    def period(self, mode: RateMode) -> int:
        return cycles_per_toggle(self.clk_hz, mode.hz)

    @property
    def phase_a(self) -> int:
        """Power-on pattern: all off (together) or bit0=0, bit1=1, ... (alternate)."""
        if self.b.pattern == "alternate":
            return sum(1 << i for i in range(self.width) if i % 2 == 1)
        return 0

    @property
    def phase_b(self) -> int:
        return self.phase_a ^ self.mask

    def initial_value(self) -> int:
        # The enable input powers up low, and a disabled blinker shows all LEDs off.
        return 0 if self.b.enable_input else self.phase_a

    def running_initial(self) -> int:
        return self.phase_a

    def legality_inputs(self) -> list[str]:
        return [self.b.enable_input] if self.b.enable_input else []

    def paused(self, levels):
        return bool(self.b.enable_input) and levels.get(self.b.enable_input, 0) == 0

    def successors(self, hidden, env):
        v = int(hidden)
        if self.b.enable_input and env.get(self.b.enable_input, 0) == 0:
            return [(0, 0)]
        if v == self.phase_a:
            return [(self.phase_b, self.phase_b)]
        if v == self.phase_b:
            return [(self.phase_a, self.phase_a)]
        return [(self.phase_a, self.phase_a), (self.phase_b, self.phase_b)]

    def rule(self) -> str:
        if self.b.pattern == "alternate":
            return (f"output alternates between {self.fmt(self.phase_a)} and {self.fmt(self.phase_b)} "
                    "(even bits opposite to odd bits); 0 while disabled")
        return f"all bits equal: output alternates between 0 and {self.fmt(self.mask)}; 0 while disabled"


class PatternModel(PeriodicModel):
    """chase / bounce / fill (stepped) and mirror_inputs / invert_inputs (static)."""

    family = "led_pattern"
    b: LedPatternBehavior

    @property
    def static(self) -> bool:
        return self.b.pattern in ("mirror_inputs", "invert_inputs")

    def initial_value(self) -> int:
        if self.static:
            return self.mapping({})
        if self.b.pattern == "fill":
            return 0
        return 1

    def legality_inputs(self) -> list[str]:
        names = [self.b.reset_input, self.b.enable_input]
        if self.b.direction_control and self.b.pattern in ("chase", "fill"):
            names.append(self.b.direction_control.input)
        return [n for n in names if n]

    def directions(self, env: dict[str, int]) -> str:
        """'left' (towards higher LED indices) or 'right'."""
        dc = self.b.direction_control
        if dc is not None and env.get(dc.input, 0) == 1:
            return "left" if dc.high_means == "up" else "right"
        return self.b.direction

    # -- static patterns ---------------------------------------------------------------
    def mapping(self, levels: dict[str, int]) -> int:
        v = 0
        for i, name in enumerate(self.b.source_inputs[: self.width]):
            bit = levels.get(name, 0)
            if self.b.pattern == "invert_inputs":
                bit ^= 1
            v |= bit << i
        return v

    def bit_options(self, timeline: InputTimeline, cycle: int) -> list[set[int]]:
        out = []
        for i in range(self.width):
            if i < len(self.b.source_inputs):
                opts = set(timeline.options(self.b.source_inputs[i], cycle))
                if self.b.pattern == "invert_inputs":
                    opts = {1 - o for o in opts}
            else:
                opts = {0}
            out.append(opts)
        return out

    # -- stepped patterns ----------------------------------------------------------------
    def bar(self, count: int, direction: str) -> int:
        ones = (1 << count) - 1
        return ones if direction == "left" else (ones << (self.width - count)) & self.mask

    def initial_hidden(self) -> set:
        if self.b.pattern == "bounce":
            return {(0, 1)}
        if self.b.pattern == "fill":
            return {0}
        return {self.initial_value()}

    def resync(self, value: int) -> set:
        w = self.width
        if self.b.pattern == "bounce":
            if value and value & (value - 1) == 0:
                p = value.bit_length() - 1
                return {(p, 1), (p, -1)}
            return set()
        if self.b.pattern == "fill":
            c = bin(value).count("1")
            if value in (self.bar(c, "left"), self.bar(c, "right")):
                return {c}
            return set()
        return {value} if value and value & (value - 1) == 0 and value <= (1 << w) - 1 else set()

    def successors(self, hidden, env):
        w = self.width
        if self.b.reset_input and env.get(self.b.reset_input, 0) == 1:
            init = self.initial_value()
            if self.b.pattern == "bounce":
                return [(init, (0, 1))]
            if self.b.pattern == "fill":
                return [(0, 0)]
            return [(init, init)]
        if self.b.enable_input and env.get(self.b.enable_input, 0) == 0:
            return []
        if self.b.pattern == "chase":
            v = int(hidden)
            if w == 1:
                return []
            if self.directions(env) == "left":
                nv = ((v << 1) | (v >> (w - 1))) & self.mask
            else:
                nv = ((v >> 1) | ((v & 1) << (w - 1))) & self.mask
            return [(nv, nv)]
        if self.b.pattern == "bounce":
            p, h = hidden
            if w == 1:
                return []
            if h > 0:
                np_, nh = (p + 1, 1) if p < w - 1 else (p - 1, -1)
            else:
                np_, nh = (p - 1, -1) if p > 0 else (p + 1, 1)
            return [(1 << np_, (np_, nh))]
        if self.b.pattern == "fill":
            c = int(hidden)
            nc = 0 if c == w else c + 1
            d = self.directions(env)
            out = [(self.bar(nc, d), nc)]
            # While the direction input is changing, a core may re-anchor the bar it shows.
            other = "right" if d == "left" else "left"
            out.append((self.bar(c, other), c))
            return out
        return []

    def successors_for(self, hidden, envs: list[dict[str, int]]) -> list[tuple[int, object]]:
        out = []
        for env in envs:
            for v, h in self.successors(hidden, env):
                if self.b.pattern == "fill" and h == hidden:
                    # Re-anchoring without a step is only legal while the direction is ambiguous.
                    if len({self.directions(e) for e in envs}) < 2:
                        continue
                out.append((v, h))
        return out

    def rule(self) -> str:
        p = self.b.pattern
        if p == "chase":
            return "exactly one LED lit, moving one position per step in the active direction and wrapping"
        if p == "bounce":
            return "exactly one LED lit, moving towards the top bit, then back to bit 0, one position per step"
        if p == "fill":
            return "bar graph: each step lights the next LED; the step after full clears it"
        return f"output bit i = {'NOT ' if p == 'invert_inputs' else ''}source input i"

    def wrap_transitions(self, levels):
        w = self.width
        if w < 2:
            return []
        top = 1 << (w - 1)
        if self.b.pattern == "chase":
            return [(top, 1)] if self.directions(levels) == "left" else [(1, top)]
        if self.b.pattern == "bounce":
            return [(top, top >> 1), (1, 2)]
        if self.b.pattern == "fill":
            return [(self.mask, 0)]
        return []

    def steps_to_wrap(self, levels) -> int:
        w = self.width
        if self.b.pattern == "chase":
            return w if self.directions(levels) == "left" else 1
        if self.b.pattern == "bounce":
            return 2 * (w - 1) + 1
        if self.b.pattern == "fill":
            return w + 1
        return 0

    def nominal_after(self, steps: int, levels: dict[str, int]) -> int:
        hidden = next(iter(self.initial_hidden()))
        v = self.initial_value()
        env = dict(levels)
        for _ in range(steps):
            succ = [s for s in self.successors(hidden, env) if self.b.pattern != "fill" or s[1] != hidden]
            if not succ:
                break
            v, hidden = succ[0]
        return v


@dataclass(frozen=True)
class Visit:
    start: int
    end: int  # exclusive: the next state starts here
    state: str
    lights: int
    request: bool = False


class TrafficModel(BehaviourModel):
    family = "traffic_light"
    b: TrafficLightBehavior

    @property
    def states(self) -> list[TrafficState]:
        return list(self.b.states)

    @property
    def has_request(self) -> bool:
        return bool(self.b.request_input and self.b.request_state and self.b.request_after)

    @property
    def after_index(self) -> int:
        names = [s.name for s in self.states]
        return names.index(self.b.request_after) if self.has_request and self.b.request_after in names else -1

    def duration(self, state: TrafficState) -> int:
        return cycles_for_duration(self.clk_hz, state.duration_s)

    def state_named(self, name: str) -> TrafficState:
        for s in self.states + ([self.b.request_state] if self.b.request_state else []):
            if s.name == name:
                return s
        raise KeyError(name)

    def lights(self, state: TrafficState) -> int:
        return state.lights & self.mask

    def initial_value(self) -> int:
        return self.lights(self.states[0])

    def cycle_length(self) -> int:
        return sum(self.duration(s) for s in self.states)

    def legality_inputs(self) -> list[str]:
        return [n for n in (self.b.reset_input, self.b.request_input) if n]

    def timeline(self, anchor: int, until: int, request_effs: Sequence[int]) -> list[Visit]:
        """Reference visits from `anchor` (states[0] entered, timer at 0, latch clear).

        A conditioned rising edge of request_input sets the latch (it must reach the
        core before the decision edge); when request_after ends with the latch set
        the FSM inserts request_state, clears the latch, then continues with the
        state after request_after.
        """
        reqs = sorted(r for r in request_effs if r >= anchor)
        visits: list[Visit] = []
        t, idx, latch, in_request = anchor, 0, False, False
        ri = 0
        guard = 0
        while t <= until and guard < 100_000:
            guard += 1
            state = self.b.request_state if in_request else self.states[idx]
            assert state is not None
            end = t + self.duration(state)
            visits.append(Visit(t, end, state.name, self.lights(state), in_request))
            while ri < len(reqs) and reqs[ri] < end - 1:
                latch = True
                ri += 1
            if in_request:
                in_request = False
                idx = (self.after_index + 1) % len(self.states)
            elif self.has_request and idx == self.after_index and latch:
                in_request, latch = True, False
            else:
                idx = (idx + 1) % len(self.states)
            t = end
        return visits


def model_for(spec: DesignSpec, clk_hz: int) -> BehaviourModel:
    b = spec.behavior
    if isinstance(b, CounterBehavior):
        return CounterModel(spec, clk_hz)
    if isinstance(b, BlinkerBehavior):
        return BlinkerModel(spec, clk_hz)
    if isinstance(b, LedPatternBehavior):
        return PatternModel(spec, clk_hz)
    if isinstance(b, TrafficLightBehavior):
        return TrafficModel(spec, clk_hz)
    raise ValueError(f"no reference model for family '{spec.family}'")
