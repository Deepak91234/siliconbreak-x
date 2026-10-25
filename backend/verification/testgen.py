"""Spec-derived simulation test plans: stimulus scenarios + oracle checks.

The plan is generated from the DesignSpec alone (never from the RTL), using the
behavioural reference models in `reference.py`. Every check is tagged with
requirement ids from `derive_requirements(spec)` so the report can show a
simulation status per requirement.

Scenario timing is expressed in periods of the behaviour (at the accelerated
simulation clock) so a whole suite stays at a few hundred thousand cycles. Input
changes are placed half-way between nominal steps so that they never coincide
with a step, and every bounce burst is made of pulses well below the debounce time
so the ideal conditioner rejects all of them.

`TestPlan.params` are the -G overrides for the DUT (param_dict at the sim clock);
the checks never read expected cycle counts from them -- the oracle recomputes
periods from the physical values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from backend.specifications.derive import derive_requirements
from backend.specifications.ir import (
    BlinkerBehavior,
    CounterBehavior,
    DesignSpec,
    LedPatternBehavior,
    RateMode,
    SevenSegmentBehavior,
    TrafficLightBehavior,
)
from backend.specifications.timing import param_dict

from . import reference as R

PortName = Literal["sw", "btnC", "btnU", "btnL", "btnR", "btnD"]

VCD_MAX_CYCLES = 16_000          # bound on each scenario's VCD window
MAX_SCENARIO_CYCLES = 4_000_000  # scenarios longer than this are skipped (reported in notes)
MIN_BOUNCE_DEBOUNCE = 3          # below this many debounce cycles a "shorter than debounce" pulse is meaningless

# Deterministic bounce shapes as fractions of the debounce time (all < 0.5).
_ON_FRACTIONS = (0.35, 0.2, 0.45, 0.25, 0.4, 0.3)
_OFF_FRACTIONS = (0.25, 0.4, 0.15, 0.35, 0.2, 0.3)


class StimulusEvent(BaseModel):
    cycle: int = Field(ge=0)
    port: PortName
    value: int = Field(ge=0, le=0xFFFF)


class CheckSpec(BaseModel):
    id: str
    kind: str
    requirement_ids: list[str]
    params: dict = Field(default_factory=dict)


class Scenario(BaseModel):
    id: str
    title: str
    description: str
    requirement_ids: list[str]
    events: list[StimulusEvent]
    end_cycle: int
    vcd_window: tuple[int, int] | None
    checks: list[CheckSpec]


class TestPlan(BaseModel):
    __test__: ClassVar[bool] = False  # not a pytest test class

    spec_title: str
    clk_hz: int
    params: dict[str, int]
    scenarios: list[Scenario]
    notes: list[str] = Field(default_factory=list)


class TestPlanError(ValueError):
    """The spec cannot be turned into a consistent test plan (a CircuitLoop bug or unsupported spec)."""

    __test__ = False  # not a pytest test class


# --------------------------------------------------------------------------- scenario builder


@dataclass(frozen=True)
class _Change:
    raw: int
    eff: int
    end: int  # last cycle of the latency window


class _Scn:
    """Accumulates stimulus and checks for one scenario."""

    def __init__(self, gen: "_Generator", sid: str, title: str, description: str):
        self.g = gen
        self.sid, self.title, self.description = sid, title, description
        self.raw = {b.name: 0 for b in gen.spec.inputs}
        self.sw_bus = 0
        self.events: list[StimulusEvent] = []
        self.checks: list[CheckSpec] = []
        self.expected_changes: list[tuple[str, int, int]] = []  # (input, eff, level)
        self.focus_window: tuple[int, int] | None = None

    # -- stimulus --------------------------------------------------------------
    def _drive(self, name: str, level: int, at: int) -> None:
        b = self.g.spec.input(name)
        self.raw[name] = level
        if b.port == "sw":
            assert b.index is not None
            self.sw_bus = (self.sw_bus & ~(1 << b.index)) | (level << b.index)
            self.events.append(StimulusEvent(cycle=at, port="sw", value=self.sw_bus))
        else:
            self.events.append(StimulusEvent(cycle=at, port=b.port, value=level))

    def set(self, name: str, level: int, at: int) -> _Change:
        """A clean (bounce-free) change that the conditioner passes after its debounce time."""
        at = max(1, int(at))
        self._drive(name, level, at)
        eff = at + 1 + self.g.deb[name]
        self.expected_changes.append((name, eff, level))
        return _Change(at, eff, eff + R.LATENCY_CYCLES)

    def apply(self, levels: dict[str, int], at: int = 1) -> int:
        """Drive every input whose raw level differs; returns the end of the last latency window (0 if none)."""
        end = 0
        for name, lvl in levels.items():
            if self.raw[name] != lvl:
                end = max(end, self.set(name, lvl, at).end)
        return end

    def _pulses(self, name: str, n: int, dense: bool = False) -> list[tuple[int, int]]:
        s = self.g.deb[name]
        out = []
        for k in range(n):
            on = 0.45 if dense else _ON_FRACTIONS[k % len(_ON_FRACTIONS)]
            off = 0.15 if dense else _OFF_FRACTIONS[k % len(_OFF_FRACTIONS)]
            out.append((max(2, min(s - 1, round(on * s))), max(2, min(s - 1, round(off * s)))))
        return out

    def burst(self, name: str, start: int, pulses: int = 5, dense: bool = False) -> int:
        """Pulses away from the current level, each shorter than the debounce time; returns the end cycle."""
        base = self.raw[name]
        t = max(1, int(start))
        for on, off in self._pulses(name, pulses, dense):
            self._drive(name, 1 - base, t)
            t += on
            self._drive(name, base, t)
            t += off
        return t

    def burst_length(self, name: str, pulses: int = 5, dense: bool = False) -> int:
        return sum(on + off for on, off in self._pulses(name, pulses, dense))

    def bouncy_set(self, name: str, level: int, start: int, pulses: int = 4) -> _Change:
        """A contact-bounce burst followed by a stable change to `level` (must act exactly once)."""
        base = self.raw[name]
        t = max(1, int(start))
        for on, off in self._pulses(name, pulses):
            self._drive(name, level, t)
            t += on
            self._drive(name, base, t)
            t += off
        return self.set(name, level, t)

    # -- checks ----------------------------------------------------------------
    def check(self, kind: str, tags: list[str], **params) -> None:
        tags = self.g.tags(*tags)
        n = len(self.checks) + 1
        params = {k: (int(v) if isinstance(v, float) and k in ("frm", "to", "anchor") else v) for k, v in params.items()}
        self.checks.append(CheckSpec(id=f"c{n:02d}_{kind}", kind=kind, requirement_ids=tags, params=params))

    def idle(self) -> None:
        if "R-IDLE" in self.g.req_ids:
            self.check("idle_off", ["R-IDLE"])

    def focus(self, a: int, b: int) -> None:
        a = max(0, int(a))
        self.focus_window = (a, max(a, min(int(b), a + VCD_MAX_CYCLES)))

    # -- finish ----------------------------------------------------------------
    def build(self, end_cycle: int) -> Scenario | None:
        end_cycle = int(end_cycle)
        if end_cycle > MAX_SCENARIO_CYCLES:
            self.g.notes.append(f"scenario '{self.sid}' skipped: {end_cycle} cycles exceeds {MAX_SCENARIO_CYCLES}")
            return None
        # Self-check: the reference conditioner must see exactly the changes this builder intended.
        tl = R.InputTimeline(self.g.spec, self.g.clk_hz, self.events)
        seen = sorted((ch.input, ch.eff_cycle, ch.level) for chs in tl.changes.values() for ch in chs)
        if seen != sorted(self.expected_changes):
            raise TestPlanError(f"scenario {self.sid}: stimulus does not condition as intended "
                                f"(intended {sorted(self.expected_changes)}, reference {seen})")
        for c in self.checks:
            for key in ("frm", "to", "anchor"):
                if key in c.params and not (0 <= int(c.params[key]) <= end_cycle):
                    raise TestPlanError(f"scenario {self.sid} check {c.id}: {key}={c.params[key]} outside 0..{end_cycle}")
        window = None
        if self.focus_window is not None:
            a, b = self.focus_window
            window = (min(a, end_cycle), min(b, end_cycle))
        reqs = list(dict.fromkeys(r for c in self.checks for r in c.requirement_ids))
        return Scenario(id=self.sid, title=self.title, description=self.description, requirement_ids=reqs,
                        events=self.events, end_cycle=end_cycle, vcd_window=window, checks=self.checks)


# --------------------------------------------------------------------------- generator


class _Generator:
    def __init__(self, spec: DesignSpec, clk_hz: int):
        self.spec = spec
        self.clk_hz = clk_hz
        self.model = R.model_for(spec, clk_hz)
        self.req_ids = {r.id for r in derive_requirements(spec)}
        self.deb = {b.name: R.debounce_cycles(clk_hz, b.debounce_ms) for b in spec.inputs}
        self.notes: list[str] = []
        self.neutral = self._neutral_levels()

    def tags(self, *ids: str) -> list[str]:
        return [i for i in dict.fromkeys(ids) if i in self.req_ids]

    # -- input levels ------------------------------------------------------------
    def _neutral_levels(self) -> dict[str, int]:
        """Levels for 'normal operation': enabled, not reset, default rate mode, default direction."""
        levels = {b.name: 0 for b in self.spec.inputs}
        b = self.spec.behavior
        for m in (getattr(b, "rate_modes", None) or [])[:-1]:
            if m.when_input:
                levels[m.when_input] = 1 - m.when_level
        enable = getattr(b, "enable_input", None)
        if enable:
            levels[enable] = 1
        return levels

    def activate(self, mode: RateMode) -> dict[str, int] | None:
        """Neutral levels changed so that `mode` is the active rate mode (None if unreachable)."""
        m_model = self.model
        assert isinstance(m_model, R.PeriodicModel)
        levels = dict(self.neutral)
        modes = m_model.modes
        k = modes.index(mode)
        if mode.when_input:
            levels[mode.when_input] = mode.when_level
        for j in range(k):
            other = modes[j]
            if other.when_input and other.when_input != mode.when_input:
                levels[other.when_input] = 1 - other.when_level
        if m_model.selected_mode(levels) is not mode or m_model.paused(levels):
            return None
        return levels

    def bounce_ok(self, name: str) -> bool:
        return self.deb[name] >= MIN_BOUNCE_DEBOUNCE

    def role(self, name: str) -> str:
        b = self.spec.behavior
        if getattr(b, "reset_input", None) == name:
            return "reset"
        if getattr(b, "enable_input", None) == name:
            return "enable"
        if getattr(b, "request_input", None) == name:
            return "request"
        for m in getattr(b, "rate_modes", None) or []:
            if m.when_input == name:
                return "mode"
        dc = getattr(b, "direction_control", None)
        if dc is not None and dc.input == name:
            return "direction"
        if name in (getattr(b, "source_inputs", None) or []):
            return "source"
        return "other"

    # -- entry -------------------------------------------------------------------
    def plan(self) -> list[Scenario]:
        b = self.spec.behavior
        if isinstance(b, TrafficLightBehavior):
            raw = self._traffic()
        elif isinstance(b, LedPatternBehavior) and b.pattern in ("mirror_inputs", "invert_inputs"):
            raw = self._static()
        elif isinstance(b, SevenSegmentBehavior):
            raw = self._display()
        elif isinstance(b, (CounterBehavior, BlinkerBehavior, LedPatternBehavior)):
            if not getattr(b, "rate_modes", None):
                raise TestPlanError(f"{self.spec.family} pattern '{getattr(b, 'pattern', '')}' needs a rate mode")
            raw = self._periodic()
        else:  # pragma: no cover - the IR has no other families
            raise TestPlanError(f"no test generator for family '{self.spec.family}'")
        scenarios = [s for s in raw if s is not None]
        ids = [s.id for s in scenarios]
        if len(set(ids)) != len(ids):
            raise TestPlanError(f"duplicate scenario ids: {ids}")
        return scenarios

    def _display(self) -> list[Scenario | None]:
        return _display_scenario(self)

    # ======================================================================= periodic families

    @property
    def _pm(self) -> R.PeriodicModel:
        assert isinstance(self.model, R.PeriodicModel)
        return self.model

    def _P(self, m: RateMode) -> int:
        return self._pm.period(m)

    def _seq_tags(self) -> list[str]:
        return ["R-DIR"] if self.spec.family == "counter" else ["R-PATTERN"]

    def _init_tags(self) -> list[str]:
        return ["R-INIT"] if self.spec.family == "counter" else ["R-PATTERN"]

    def _wrap_tags(self) -> list[str]:
        return ["R-WRAP"] if self.spec.family == "counter" else ["R-PATTERN"]

    @staticmethod
    def _rate(m: RateMode) -> str:
        return f"R-RATE-{m.name}"

    def _periodic(self) -> list[Scenario | None]:
        b = self.spec.behavior
        modes = self._pm.modes
        out: list[Scenario | None] = [self._p_startup()]
        for m in modes[:-1]:
            out.append(self._p_mode(m))
        if len(modes) >= 2:
            out.append(self._p_speed_switch())
        if not isinstance(b, BlinkerBehavior):
            out.append(self._p_wrap())
        if getattr(b, "reset_input", None):
            out.append(self._p_reset())
        if getattr(b, "enable_input", None):
            out.append(self._p_enable())
        dc = getattr(b, "direction_control", None)
        if dc is not None and (isinstance(b, CounterBehavior) or getattr(b, "pattern", "") in ("chase", "fill")):
            out.append(self._p_direction())
        for inp in self.spec.inputs:
            if self.bounce_ok(inp.name):
                out.append(self._p_debounce(inp.name))
        return out

    def _settle_neutral(self, s: _Scn) -> int:
        return s.apply(self.neutral, at=1)

    def _p_startup(self) -> Scenario | None:
        d = self._pm.modes[-1]
        T = self._P(d)
        s = _Scn(self, "startup", f"Power-on value and default rate ('{d.name}', {d.hz:g}/s)",
                 "Configure the design and run it in its default mode: checks the power-on value, the "
                 "first step one period later, the step rule and the steady step rate.")
        settle = self._settle_neutral(s)
        end = settle + int(4.5 * T) + 1
        if settle == 0:
            s.check("value_at", self._init_tags(), frm=0, to=max(0, T - math.ceil(R.tolerance(T)) - 1), expect="initial")
            s.check("first_change_after", self._init_tags() + [self._rate(d)], anchor=0)
        else:
            first_eff = min(eff for _n, eff, _l in s.expected_changes)
            s.check("value_at", self._init_tags(), frm=0, to=first_eff - 1, expect="initial")
        s.check("steady_rate", [self._rate(d)], frm=settle, to=end)
        s.check("sequence", self._seq_tags(), frm=0, to=end)
        s.idle()
        s.focus(0, end)
        return s.build(end)

    def _p_mode(self, m: RateMode) -> Scenario | None:
        levels = self.activate(m)
        if levels is None:
            self.notes.append(f"rate mode '{m.name}' cannot be selected on its own; not simulated separately")
            return None
        T, Td = self._P(m), self._P(self._pm.modes[-1])
        conds = ", ".join(f"{n}={v}" for n, v in levels.items() if v != 0) or "all inputs low"
        s = _Scn(self, f"mode_{m.name}", f"Rate mode '{m.name}' ({m.hz:g}/s)",
                 f"Hold {conds} from power-on so mode '{m.name}' is active; checks its steady rate and the step rule.")
        settle = s.apply(levels, at=1)
        end = settle + max(T, Td) + int(5.5 * T) + 1
        s.check("steady_rate", [self._rate(m)], frm=settle, to=end)
        s.check("sequence", self._seq_tags(), frm=0, to=end)
        s.idle()
        s.focus(0, end)
        return s.build(end)

    def _p_speed_switch(self) -> Scenario | None:
        modes = self._pm.modes
        fast, d = modes[0], modes[-1]
        lv = self.activate(fast)
        if lv is None:
            return None
        Tf, Td = self._P(fast), self._P(d)
        s = _Scn(self, "speed_switch", f"Switch '{d.name}' -> '{fast.name}' -> '{d.name}'",
                 f"Run in '{d.name}', select '{fast.name}' for several steps, then return. Each steady segment "
                 "must run at its own rate; the step rule holds across the switches.")
        settle = self._settle_neutral(s)
        t1 = settle + int(3.5 * Td)
        end1 = max(s.set(n, v, t1).end for n, v in lv.items() if s.raw[n] != v)
        t2 = end1 + int(6.5 * Tf) + max(0, Td - Tf)
        end2 = max(s.set(n, v, t2).end for n, v in self.neutral.items() if s.raw[n] != v)
        end = end2 + int(3.5 * Td) + 1
        s.check("steady_rate", [self._rate(d)], frm=settle, to=t1 - 1)
        s.check("steady_rate", [self._rate(fast)], frm=end1, to=t2 - 1)
        s.check("steady_rate", [self._rate(d)], frm=end2, to=end)
        if "R-RATIO" in self.req_ids:
            s.check("ratio", ["R-RATIO"], fast_mode=fast.name, slow_mode=d.name,
                    fast_windows=[[end1, t2 - 1]], slow_windows=[[settle, t1 - 1], [end2, end]])
        s.check("sequence", self._seq_tags(), frm=0, to=end)
        s.idle()
        s.focus(t1 - min(Td, VCD_MAX_CYCLES // 4), t1 + VCD_MAX_CYCLES)
        return s.build(end)

    def _p_wrap(self) -> Scenario | None:
        modes = self._pm.modes
        fast = max(modes, key=lambda m: m.hz)
        levels = self.neutral if fast is modes[-1] else self.activate(fast)
        if levels is None:
            fast, levels = modes[-1], self.neutral
        transitions = self._pm.wrap_transitions(levels)
        saturating = isinstance(self.spec.behavior, CounterBehavior) and not self.spec.behavior.wrap
        if not transitions and not saturating:
            return None
        T, Td = self._P(fast), self._P(modes[-1])
        steps = self._pm.steps_to_wrap(levels)
        what = "saturation at the end of the range" if saturating else "the wrap-around"
        s = _Scn(self, "wrap", f"Full range in mode '{fast.name}' ({what})",
                 f"Run {steps} or more steps at the fastest rate so the behaviour passes {what}.")
        settle = s.apply(levels, at=1)
        end = settle + max(T, Td) + int((steps + (3.5 if saturating else 2.5)) * T) + 1
        s.check("wrap", self._wrap_tags(), frm=settle, to=end)
        if not saturating:
            s.check("steady_rate", [self._rate(fast)], frm=settle, to=end)
        s.check("sequence", self._seq_tags() + self._wrap_tags(), frm=0, to=end)
        s.idle()
        s.focus(max(0, end - VCD_MAX_CYCLES), end)
        sc = s.build(end)
        if sc is None:
            self.notes.append("the full range needs too many steps at the simulation clock; wrap not simulated")
        return sc

    def _steps_until_not(self, value: int, levels: dict[str, int]) -> int:
        nominal = getattr(self._pm, "nominal_after")
        for k in range(2, 8):
            if nominal(k, levels) != value:
                return k
        return 2

    def _p_reset(self) -> Scenario | None:
        b = self.spec.behavior
        name = b.reset_input
        d = self._pm.modes[-1]
        T = self._P(d)
        S = self.deb[name]
        s = _Scn(self, "reset", f"Reset with {name}",
                 f"Run, press and hold {name} for two periods, release. While held the output is the reset "
                 "value; the first step after release comes one full period later.")
        settle = self._settle_neutral(s)
        k = self._steps_until_not(self._pm.reset_value(), self.neutral)
        ch_p = s.set(name, 1, settle + int((k + 0.5) * T) - 1 - S)
        ch_r = s.set(name, 0, ch_p.raw + 2 * T)
        end = ch_r.end + int(4.5 * T) + 1
        s.check("steady_rate", [self._rate(d)], frm=settle, to=ch_p.raw - 1)
        s.check("value_at", ["R-RESET"], frm=ch_p.end, to=ch_r.eff, expect="reset")
        s.check("first_change_after", ["R-RESET"], anchor=ch_r.eff, input=name)
        s.check("steady_rate", [self._rate(d)], frm=ch_r.end, to=end)
        s.check("sequence", self._seq_tags() + ["R-RESET"], frm=0, to=end)
        s.idle()
        s.focus(ch_r.eff - VCD_MAX_CYCLES // 8, ch_r.eff + T + VCD_MAX_CYCLES // 8)
        return s.build(end)

    def _p_enable(self) -> Scenario | None:
        b = self.spec.behavior
        name = b.enable_input
        d = self._pm.modes[-1]
        T = self._P(d)
        S = self.deb[name]
        blinker = isinstance(b, BlinkerBehavior)
        s = _Scn(self, "enable", f"Enable input {name}",
                 f"Run with {name} high, drop it for 2.5 periods, raise it again. While low the "
                 + ("LEDs are off." if blinker else "output holds its value."))
        settle = self._settle_neutral(s)
        ch_off = s.set(name, 0, settle + int(3.5 * T) - 1 - S)
        ch_on = s.set(name, 1, ch_off.raw + int(2.5 * T))
        end = ch_on.end + int(4.5 * T) + 1
        s.check("steady_rate", [self._rate(d)], frm=settle, to=ch_off.raw - 1)
        if blinker:
            s.check("value_at", ["R-ENABLE"], frm=ch_off.end, to=ch_on.eff, expect="off")
        else:
            s.check("hold", ["R-ENABLE"], frm=ch_off.end, to=ch_on.eff)
        s.check("steady_rate", [self._rate(d)], frm=ch_on.end, to=end)
        s.check("sequence", self._seq_tags() + ["R-ENABLE"], frm=0, to=end)
        s.idle()
        s.focus(ch_off.raw - VCD_MAX_CYCLES // 4, ch_off.raw + VCD_MAX_CYCLES)
        return s.build(end)

    def _p_direction(self) -> Scenario | None:
        dc = self.spec.behavior.direction_control
        name = dc.input
        d = self._pm.modes[-1]
        T = self._P(d)
        s = _Scn(self, "direction", f"Direction control {name}",
                 f"Run, set {name} high for several steps, then low again; each segment must step in its direction.")
        settle = self._settle_neutral(s)
        ch1 = s.set(name, 1, settle + int(3.5 * T))
        ch2 = s.set(name, 0, ch1.end + int(4.5 * T))
        end = ch2.end + int(3.5 * T) + 1
        s.check("steady_rate", [self._rate(d)], frm=settle, to=ch1.raw - 1)
        s.check("steady_rate", [self._rate(d)], frm=ch1.end, to=ch2.raw - 1)
        s.check("steady_rate", [self._rate(d)], frm=ch2.end, to=end)
        s.check("sequence", self._seq_tags() + ["R-DIR"], frm=0, to=end)
        s.idle()
        s.focus(ch1.raw - VCD_MAX_CYCLES // 2, ch1.raw + VCD_MAX_CYCLES // 2)
        return s.build(end)

    def _p_debounce(self, name: str) -> Scenario | None:
        b = self.spec.behavior
        role = self.role(name)
        inp = self.spec.input(name)
        deb_tag = f"R-DEB-{name}"
        d = self._pm.modes[-1]
        T = self._P(d)
        S = self.deb[name]
        s = _Scn(self, f"debounce_{name}", f"Bounce rejection on {inp.board_signal} ({name})",
                 f"Bursts of pulses shorter than the {inp.debounce_ms:g} ms debounce time must have no effect; "
                 "a press or release with contact bounce must act exactly once.")
        settle = self._settle_neutral(s)
        seq = self._seq_tags() + [deb_tag]

        if role == "mode":
            mode = next(m for m in self._pm.modes[:-1] if m.when_input == name)
            Tm = self._P(mode)
            t = s.burst(name, settle + int(2.7 * T))
            ch_p = s.bouncy_set(name, mode.when_level, max(t + 2 * S, settle + int(4.6 * T)))
            ch_r = s.bouncy_set(name, 1 - mode.when_level, ch_p.end + int(6.5 * Tm) + max(0, T - Tm))
            end = ch_r.end + int(3.5 * T) + 1
            s.check("steady_rate", [self._rate(d), deb_tag], frm=settle, to=ch_p.raw - 1)
            s.check("steady_rate", [self._rate(mode), deb_tag], frm=ch_p.end, to=ch_r.raw - 1)
            s.check("steady_rate", [self._rate(d), deb_tag], frm=ch_r.end, to=end)
            s.check("sequence", seq, frm=0, to=end)
            s.focus(settle + int(2.7 * T) - S, ch_p.end + 2 * S)
        elif role == "reset" and not isinstance(b, BlinkerBehavior):
            k = self._steps_until_not(self._pm.reset_value(), self.neutral)
            t = s.burst(name, settle + int((k + 0.3) * T))
            start = max(t + 2 * S, settle + int((k + 1.3) * T))
            ch_p = s.bouncy_set(name, 1, start)
            ch_r = s.bouncy_set(name, 0, ch_p.end + 2 * T)
            end = ch_r.end + int(4.5 * T) + 1
            s.check("steady_rate", [self._rate(d), deb_tag], frm=settle, to=ch_p.raw - 1)
            s.check("value_at", ["R-RESET", deb_tag], frm=ch_p.end, to=ch_r.eff, expect="reset")
            s.check("first_change_after", ["R-RESET", deb_tag], anchor=ch_r.eff, input=name)
            s.check("steady_rate", [self._rate(d), deb_tag], frm=ch_r.end, to=end)
            s.check("sequence", seq + ["R-RESET"], frm=0, to=end)
            s.focus(settle + int((k + 0.3) * T) - S, ch_p.end + 2 * S)
        elif role == "enable":
            t = s.burst(name, settle + int(2.7 * T))
            ch_off = s.bouncy_set(name, 0, max(t + 2 * S, settle + int(4.6 * T)))
            ch_on = s.bouncy_set(name, 1, ch_off.end + int(2.5 * T))
            end = ch_on.end + int(4.5 * T) + 1
            s.check("steady_rate", [self._rate(d), deb_tag], frm=settle, to=ch_off.raw - 1)
            if isinstance(b, BlinkerBehavior):
                s.check("value_at", ["R-ENABLE", deb_tag], frm=ch_off.end, to=ch_on.eff, expect="off")
            else:
                s.check("hold", ["R-ENABLE", deb_tag], frm=ch_off.end, to=ch_on.eff)
            s.check("steady_rate", [self._rate(d), deb_tag], frm=ch_on.end, to=end)
            s.check("sequence", seq, frm=0, to=end)
            s.focus(settle + int(2.7 * T) - S, settle + int(2.7 * T) + VCD_MAX_CYCLES // 2)
        elif role == "direction":
            n_dense = 6
            length = s.burst_length(name, n_dense, dense=True)
            t = s.burst(name, settle + 2 * T - length // 2, pulses=n_dense, dense=True)
            ch1 = s.bouncy_set(name, 1, max(t + 2 * S, settle + int(4.6 * T)))
            ch2 = s.bouncy_set(name, 0, ch1.end + int(4.5 * T))
            end = ch2.end + int(3.5 * T) + 1
            s.check("steady_rate", [self._rate(d), deb_tag], frm=settle, to=ch1.raw - 1)
            s.check("steady_rate", [self._rate(d), deb_tag], frm=ch1.end, to=ch2.raw - 1)
            s.check("steady_rate", [self._rate(d), deb_tag], frm=ch2.end, to=end)
            s.check("sequence", seq + ["R-DIR"], frm=0, to=end)
            s.focus(settle + 2 * T - length, settle + 2 * T + length)
        else:
            t0 = settle + int(2.7 * T)
            s.burst(name, t0)
            end = settle + int(5.5 * T) + S + 1
            s.check("steady_rate", [self._rate(d), deb_tag], frm=settle, to=end)
            s.check("sequence", seq, frm=0, to=end)
            s.focus(t0 - S, t0 + 4 * S)
        s.idle()
        return s.build(end)

    # ======================================================================= static led patterns

    def _static(self) -> list[Scenario | None]:
        m = self.model
        assert isinstance(m, R.PatternModel)
        b = self.spec.behavior
        assert isinstance(b, LedPatternBehavior)
        srcs = list(b.source_inputs[: m.width])
        if not srcs:
            raise TestPlanError("mirror/invert pattern without source inputs")
        smax = max([self.deb[n] for n in srcs] + [0])
        hold = max(4 * smax + 4 * R.LATENCY_CYCLES, 64)
        out: list[Scenario | None] = []

        def patterns() -> list[dict[str, int]]:
            pats = [{n: int(i == k) for i, n in enumerate(srcs)} for k in range(len(srcs))]
            pats.append({n: 1 for n in srcs})
            pats.append({n: i % 2 for i, n in enumerate(srcs)})
            pats.append({n: 1 - i % 2 for i, n in enumerate(srcs)})
            pats.append({n: 0 for n in srcs})
            return pats

        # -- walk through input combinations
        s = _Scn(self, "static_walk", "Outputs follow the source inputs",
                 "Walk a single high input across every source, then all high, alternating patterns and all low; "
                 "outside each change's debounce latency the output must equal the mapping.")
        settle = self._settle_neutral(s)
        t = settle + hold
        first_raw = t
        for p in patterns():
            s.apply(p, at=t)
            t += hold
        end = t + hold
        s.check("value_at", ["R-PATTERN"], frm=0, to=first_raw, expect="initial")
        s.check("static_map", ["R-PATTERN"], frm=settle, to=end)
        s.idle()
        s.focus(0, end)
        out.append(s.build(end))

        if b.enable_input:
            en = b.enable_input
            s = _Scn(self, "enable", f"Enable input {en} holds the output",
                     f"Set a pattern, drop {en}, change the sources (output must hold), raise {en} (output follows).")
            settle = self._settle_neutral(s)
            p1 = {n: i % 2 for i, n in enumerate(srcs)}
            p2 = {n: 1 - i % 2 for i, n in enumerate(srcs)}
            s.apply(p1, at=settle + hold)
            ch_off = s.set(en, 0, settle + 2 * hold)
            s.apply(p2, at=ch_off.end + hold)
            ch_on = s.set(en, 1, ch_off.end + 2 * hold)
            end = ch_on.end + 2 * hold
            s.check("static_map", ["R-PATTERN"], frm=settle, to=ch_off.raw - 1)
            s.check("hold", ["R-ENABLE"], frm=ch_off.end, to=ch_on.eff)
            s.check("value_at", ["R-ENABLE"], frm=ch_off.end, to=ch_on.eff, expect=m.mapping(p1))
            s.check("static_map", ["R-PATTERN", "R-ENABLE"], frm=ch_on.end, to=end)
            s.idle()
            s.focus(0, end)
            out.append(s.build(end))
        if b.reset_input:
            self.notes.append("reset_input on a mirror/invert pattern has no contract-defined observable effect; "
                              "R-RESET is not simulated")

        for name in [i.name for i in self.spec.inputs]:
            if not self.bounce_ok(name):
                continue
            role = self.role(name)
            if role not in ("source", "other"):
                continue
            S = self.deb[name]
            tag = f"R-DEB-{name}"
            s = _Scn(self, f"debounce_{name}", f"Bounce rejection on {self.spec.input(name).board_signal} ({name})",
                     "A burst of pulses shorter than the debounce time must not change the output; a bouncy press "
                     "and a bouncy release must each change it exactly once.")
            settle = self._settle_neutral(s)
            t0 = settle + hold
            t = s.burst(name, t0)
            if role == "source":
                ch_p = s.bouncy_set(name, 1, t + hold)
                ch_r = s.bouncy_set(name, 0, ch_p.end + hold)
                end = ch_r.end + hold
                s.check("hold", [tag], frm=settle, to=ch_p.raw - 1)
                s.check("change_count", [tag], frm=ch_p.raw - 2 * S, to=ch_p.end + hold // 2)
                s.check("change_count", [tag], frm=ch_r.raw - 2 * S, to=end)
            else:
                end = t + hold
                s.check("hold", [tag], frm=settle, to=end)
            s.check("static_map", ["R-PATTERN", tag], frm=settle, to=end)
            s.idle()
            s.focus(t0 - S, end)
            out.append(s.build(end))
        return out

    # ======================================================================= traffic light

    def _traffic(self) -> list[Scenario | None]:
        m = self.model
        assert isinstance(m, R.TrafficModel)
        b = self.spec.behavior
        assert isinstance(b, TrafficLightBehavior)
        states = m.states
        D = [m.duration(st) for st in states]
        L = m.cycle_length()
        D0 = D[0]
        req = m.has_request
        Dreq = m.duration(b.request_state) if req and b.request_state else 0
        out: list[Scenario | None] = []

        def t_state(k: int, rnd: int = 0) -> int:
            return rnd * L + sum(D[:k])

        def durations(s: _Scn, frm: int, to: int, anchor: int, names: list[str], extra: list[str] | None = None):
            for nm in names:
                tag = f"R-FSM-T-{nm}"
                s.check("fsm_duration", [tag] + (extra or []), frm=frm, to=to, anchor=anchor, state=nm)

        # -- plain cycle
        s = _Scn(self, "fsm_cycle", "State sequence and durations",
                 "Run two full cycles from power-on: every state in order with its light pattern and duration.")
        self._settle_neutral(s)
        end = 2 * L + D0 // 2
        s.check("value_at", ["R-FSM-OUT", "R-FSM-SEQ"], frm=0, to=max(0, D0 - math.ceil(R.tolerance(D0)) - 1), expect="state0")
        s.check("fsm_sequence", ["R-FSM-SEQ", "R-FSM-OUT"], frm=0, to=end, anchor=0)
        durations(s, 0, end, 0, [st.name for st in states])
        s.idle()
        s.focus(D0 - VCD_MAX_CYCLES // 2, D0 + VCD_MAX_CYCLES // 2)
        out.append(s.build(end))

        if req and b.request_input:
            rq = b.request_input
            S = self.deb[rq]
            press = 2 * S + 10
            after = m.after_index
            # -- one request
            s = _Scn(self, "fsm_request", f"Request with {rq}",
                     f"A short press of {rq} in the first state is latched; the request state "
                     f"{b.request_state.name} follows {b.request_after}; the next cycle has no request.")
            self._settle_neutral(s)
            s.set(rq, 1, D0 // 2)
            s.set(rq, 0, D0 // 2 + press)
            end = 2 * L + Dreq + D0 // 2
            s.check("fsm_sequence", ["R-FSM-REQ", "R-FSM-SEQ", "R-FSM-OUT"], frm=0, to=end, anchor=0)
            s.check("fsm_duration", ["R-FSM-REQ"], frm=0, to=end, anchor=0, state=b.request_state.name)
            s.idle()
            req_start = sum(D[: after + 1])
            s.focus(req_start - VCD_MAX_CYCLES // 2, req_start + VCD_MAX_CYCLES // 2)
            out.append(s.build(end))

            # -- latch semantics: two presses -> one request; a press during the request state -> next round
            if D0 >= 8 * S + 4 * press:
                s = _Scn(self, "fsm_request_latch", f"Request latch on {rq}",
                         "Two presses before the request is served give exactly one request phase; a press during "
                         "the request phase is remembered for the next cycle.")
                self._settle_neutral(s)
                for at in (D0 // 4, (3 * D0) // 4 - press):
                    s.set(rq, 1, at)
                    s.set(rq, 0, at + press)
                req_start = sum(D[: after + 1])
                at3 = req_start + max(Dreq // 2 - press, S)
                s.set(rq, 1, at3)
                s.set(rq, 0, at3 + press)
                end = 2 * (L + Dreq) + D0 // 2
                s.check("fsm_sequence", ["R-FSM-REQ", "R-FSM-SEQ"], frm=0, to=end, anchor=0)
                s.idle()
                s.focus(req_start - VCD_MAX_CYCLES // 4, req_start + 3 * VCD_MAX_CYCLES // 4)
                out.append(s.build(end))

        if b.reset_input:
            rs = b.reset_input
            S = self.deb[rs]
            s = _Scn(self, "fsm_reset", f"Reset with {rs}",
                     f"Press {rs} in the middle of a later state" + (" with a request pending" if req else "")
                     + ": the first state is shown while held, its full duration follows the release"
                     + (", and the pending request is cleared." if req else "."))
            self._settle_neutral(s)
            with_req = False
            j = 1
            if req and b.request_input:
                rq = b.request_input
                Sq = self.deb[rq]
                pressq = 2 * Sq + 10
                if m.after_index >= 1:
                    j = 1
                    with_req = True
                elif D0 >= 16 * max(S, Sq) + 4 * pressq:
                    j = 0
                    with_req = True
                if with_req:
                    s.set(rq, 1, D0 // 4 if j == 0 else D0 // 2)
                    s.set(rq, 0, (D0 // 4 if j == 0 else D0 // 2) + pressq)
            target = (3 * D0) // 4 if j == 0 else t_state(1) + D[1] // 2
            ch_p = s.set(rs, 1, target - 1 - S)
            ch_r = s.set(rs, 0, ch_p.raw + max(D0 // 2, 4 * S))
            end = ch_r.eff + L + D0 // 2
            s.check("fsm_sequence", ["R-FSM-SEQ"], frm=0, to=ch_p.raw - 1, anchor=0)
            s.check("value_at", ["R-RESET"], frm=ch_p.end, to=ch_r.eff, expect="state0")
            s.check("fsm_sequence", ["R-RESET", "R-FSM-SEQ"] + (["R-FSM-REQ"] if with_req else []),
                    frm=ch_r.eff, to=end, anchor=ch_r.eff)
            durations(s, ch_r.eff, end, ch_r.eff, [states[0].name], ["R-RESET"])
            s.idle()
            s.focus(ch_r.eff - VCD_MAX_CYCLES // 8, ch_r.eff + VCD_MAX_CYCLES)
            out.append(s.build(end))

        for inp in self.spec.inputs:
            name = inp.name
            if not self.bounce_ok(name):
                continue
            role = self.role(name)
            S = self.deb[name]
            tag = f"R-DEB-{name}"
            s = _Scn(self, f"debounce_{name}", f"Bounce rejection on {inp.board_signal} ({name})",
                     f"Bursts of pulses shorter than the {inp.debounce_ms:g} ms debounce time must have no effect; "
                     "a press with contact bounce must act exactly once.")
            self._settle_neutral(s)
            if role == "request" and req:
                t0 = D0 // 2 - s.burst_length(name) // 2
                s.burst(name, t0)
                ch = s.bouncy_set(name, 1, L + D0 // 3)
                s.set(name, 0, ch.end + 2 * S + 10)
                end = 2 * L + Dreq + D0 // 2
                s.check("fsm_sequence", [tag, "R-FSM-REQ", "R-FSM-SEQ"], frm=0, to=end, anchor=0)
                s.focus(t0 - S, t0 + VCD_MAX_CYCLES)
            elif role == "reset":
                t0 = t_state(1) + D[1] // 2 - s.burst_length(name) // 2
                s.burst(name, t0)
                ch_p = s.bouncy_set(name, 1, L + t_state(1) + D[1] // 2)
                ch_r = s.bouncy_set(name, 0, ch_p.end + max(D0 // 2, 8 * S))
                end = ch_r.eff + L + D0 // 2
                s.check("fsm_sequence", [tag, "R-FSM-SEQ"], frm=0, to=ch_p.raw - 1, anchor=0)
                s.check("value_at", ["R-RESET", tag], frm=ch_p.end, to=ch_r.eff, expect="state0")
                s.check("fsm_sequence", [tag, "R-RESET", "R-FSM-SEQ"], frm=ch_r.eff, to=end, anchor=ch_r.eff)
                durations(s, ch_r.eff, end, ch_r.eff, [states[0].name], ["R-RESET", tag])
                s.focus(t0 - S, t0 + 4 * S)
            else:
                t0 = t_state(1) + D[1] // 2
                s.burst(name, t0)
                end = L + D0 // 2
                s.check("fsm_sequence", [tag, "R-FSM-SEQ"], frm=0, to=end, anchor=0)
                s.focus(t0 - S, t0 + 4 * S)
            s.idle()
            out.append(s.build(end))
        return out


def _display_scenario(gen: "_Generator") -> list[Scenario | None]:
    m = gen.model
    assert isinstance(m, R.DisplayModel)
    s = _Scn(gen, "display_text", f"Display shows '{m.text()}'",
             "No inputs: decode the multiplexed seven-segment pins (seg/an) and check every digit shows its "
             "character, blank digits stay dark, one digit is driven at a time, and each digit is refreshed "
             f"at least {R.MIN_FRAME_HZ} times per second.")
    end = max(4 * m.max_frame_cycles(), m.startup_cycles() + 3 * m.max_frame_cycles())
    s.check("display", ["R-DISPLAY"], frm=m.startup_cycles(), to=end)
    s.idle()
    s.focus(0, end)
    return [s.build(end)]


def generate_test_plan(spec: DesignSpec, clk_hz: int) -> TestPlan:
    """Stimulus + checks for `spec` simulated at `clk_hz` (independent of any RTL)."""
    if clk_hz <= 0:
        raise TestPlanError("clock frequency must be positive")
    gen = _Generator(spec, clk_hz)
    scenarios = gen.plan()
    return TestPlan(spec_title=spec.title, clk_hz=clk_hz, params=param_dict(spec, clk_hz),
                    scenarios=scenarios, notes=gen.notes)
