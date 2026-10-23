"""Operating guide for a design on the physical Basys 3.

Built from three trusted sources only:
  * the validated DesignSpec (which board inputs/outputs exist and what they do),
  * the trusted board map (boards/basys3/board.json: labels and package pins),
  * the structured simulation test plan (scenario ids that exercised each step),
and, when available, the simulation report (whether those requirements passed).

Steps are derived from the behaviour (rate modes, reset, enable, direction,
FSM states, request latch), not from pin names: e.g. a counter with a fast mode
yields "watch the default rate", "hold <button> for the fast rate", "release".
The same structure drives the 2D and 3D board views in the dashboard.
"""

from __future__ import annotations

from typing import Any

from .board import load_board
from .derive import MAX_CAMERA_EVENT_HZ, derive_observations
from .ir import (
    BlinkerBehavior,
    CounterBehavior,
    DesignSpec,
    InputBinding,
    LedPatternBehavior,
    RateMode,
    TrafficLightBehavior,
)

BUTTON_LABEL = {"btnC": "BTNC", "btnU": "BTNU", "btnL": "BTNL", "btnR": "BTNR", "btnD": "BTND"}


def component_id(port: str, index: int | None = None) -> str:
    """Stable component ids shared with the frontend board model: sw0..sw15, led0..led15, btnU..., seg, an."""
    if port in ("sw", "led"):
        return f"{port}{index}"
    return port


def _label(port: str, index: int | None = None) -> str:
    if port == "sw":
        return f"SW{index}"
    if port == "led":
        return f"LD{index}"
    return BUTTON_LABEL.get(port, port)


def _pin(port: str, index: int | None) -> str | None:
    board = load_board("basys3")
    p = board.ports.get(port)
    if p is None:
        return None
    return p.pins[index or 0] if p.pins else None


def _input_label(b: InputBinding) -> str:
    return _label(b.port, b.index)


def _leds(indices: list[int]) -> str:
    s = sorted(indices)
    if len(s) > 1 and s == list(range(s[0], s[-1] + 1)):
        return f"LD{s[0]}–LD{s[-1]}"
    return ", ".join(f"LD{i}" for i in s)


def _hz(v: float) -> str:
    return f"{v:g}"


class _Builder:
    def __init__(self, spec: DesignSpec, plan: dict | None, report: dict | None):
        self.spec = spec
        self.b = spec.behavior
        self.out = spec.output(self.b.output)
        self.plan = plan or {}
        self.report = report or {}
        self.steps: list[dict] = []
        self.functions: dict[str, str] = {}

    # ------------------------------------------------------------- helpers
    def scenario_for(self, *req_ids: str) -> str | None:
        for sc in self.plan.get("scenarios", []) or []:
            if any(r in (sc.get("requirement_ids") or []) for r in req_ids):
                return sc.get("id")
        return None

    def sim_status(self, req_ids: list[str]) -> str:
        """passed only if every listed requirement passed in simulation; not_tested without a report."""
        table = {r.get("id"): r.get("sim_status") for r in self.report.get("requirements", []) or []}
        if not table:
            return "not_tested"
        vals = [table.get(r) for r in req_ids if r in table]
        if not vals:
            return "not_tested"
        if all(v == "passed" for v in vals):
            return "passed"
        if any(v == "failed" for v in vals):
            return "failed"
        return "not_tested"

    def add(self, title: str, instruction: str, *, actions: list[dict], observe: list[int], expected: str,
            confirm: str, req_ids: list[str], observation_id: str | None = None, hold: bool = False) -> None:
        self.steps.append({
            "id": f"s{len(self.steps) + 1}",
            "title": title,
            "instruction": instruction,
            "actions": actions,
            "observe": [component_id("led", i) for i in observe],
            "expected": expected,
            "confirm": confirm,
            "requirement_ids": req_ids,
            "scenario_id": self.scenario_for(*req_ids),
            "observation_id": observation_id,
            "sim_status": self.sim_status(req_ids),
            "hold": hold,
        })

    def act(self, b: InputBinding, action: str) -> dict:
        return {"component": component_id(b.port, b.index), "label": _input_label(b), "action": action,
                "signal": b.name}

    def mode_cond(self, m: RateMode) -> tuple[InputBinding, str]:
        b = self.spec.input(m.when_input)  # type: ignore[arg-type]
        if b.port == "sw":
            return b, "set_on" if m.when_level else "set_off"
        return b, "hold" if m.when_level else "release"

    def verb(self, b: InputBinding, action: str) -> str:
        lab = _input_label(b)
        return {"hold": f"Press and hold {lab}", "release": f"Release {lab}", "press": f"Press {lab}",
                "set_on": f"Slide {lab} up (on)", "set_off": f"Slide {lab} down (off)"}[action]

    # ------------------------------------------------------------- families
    def counter(self, b: CounterBehavior) -> None:
        leds = self.out.indices
        w = self.out.width
        direction = "up" if b.direction == "up" else "down"
        default = b.rate_modes[-1]
        self.functions.update({component_id("led", i): f"counter bit {k}" + (" (LSB)" if k == 0 else " (MSB)" if k == w - 1 else "")
                               for k, i in enumerate(leds)})
        idle = list(b.rate_modes[:-1])
        inactive = {"hold": "release", "set_on": "set_off", "release": "hold", "set_off": "set_on"}
        prep = []
        for m in idle:  # put every speed-mode input in its inactive state for the default rate
            inp, action = self.mode_cond(m)
            prep.append(self.act(inp, inactive[action]))
        wrap = (f"after {2 ** w - 1} it wraps to 0" if direction == "up" else f"after 0 it wraps to {2 ** w - 1}") if b.wrap \
            else ("it stops at the top" if direction == "up" else "it stops at 0")
        prep_text = "; ".join(self.verb(self.spec.input(a["signal"]), a["action"]) for a in prep)
        self.add(
            "Watch the counter",
            f"With the design programmed{' (' + prep_text + ')' if prep else ''}, "
            f"watch {_leds(leds)} (LD{leds[0]} is the least significant bit).",
            actions=prep, observe=leds,
            expected=f"The {w}-bit value on {_leds(leds)} counts {direction} by 1 every {1 / default.hz:g} s "
                     f"({_hz(default.hz)} step/s); {wrap}.",
            confirm=f"LD{leds[0]} changes on every step; it should toggle every {1 / default.hz:g} s.",
            req_ids=[f"R-RATE-{default.name}", "R-DIR", "R-WRAP"], observation_id=f"O-RATE-{default.name}")
        for m in idle:
            inp, action = self.mode_cond(m)
            self.functions[component_id(inp.port, inp.index)] = f"{'hold' if inp.port != 'sw' else 'switch on'} for mode '{m.name}' ({_hz(m.hz)} steps/s)"
            ratio = m.hz / default.hz
            self.add(
                f"{self.verb(inp, action)} for the '{m.name}' speed",
                f"{self.verb(inp, action)} and keep watching {_leds(leds)}.",
                actions=[self.act(inp, action)], observe=leds,
                expected=f"The count keeps going {direction} but now {_hz(m.hz)} steps/s"
                         + (f" ({ratio:g}× the normal speed)" if ratio != 1 else "") + ".",
                confirm=f"LD{leds[0]} now toggles every {1 / m.hz:.2f} s instead of every {1 / default.hz:g} s.",
                req_ids=[f"R-RATE-{m.name}"] + (["R-RATIO"] if len(b.rate_modes) >= 2 else []),
                observation_id=f"O-RATE-{m.name}", hold=action == "hold")
            back = {"hold": "release", "set_on": "set_off"}.get(action)
            if back:
                self.add(f"{self.verb(inp, back)}", f"{self.verb(inp, back)}.", actions=[self.act(inp, back)], observe=leds,
                         expected=f"The count returns to {_hz(default.hz)} step/s.",
                         confirm=f"LD{leds[0]} toggles every {1 / default.hz:g} s again.",
                         req_ids=[f"R-RATE-{default.name}"])
        if b.reset_input:
            inp = self.spec.input(b.reset_input)
            self.functions[component_id(inp.port, inp.index)] = f"reset: hold the value at {b.reset_value}"
            self.add(
                f"Reset with {_input_label(inp)}",
                f"Press and hold {_input_label(inp)} for a moment, then release it.",
                actions=[self.act(inp, "hold"), self.act(inp, "release")], observe=leds,
                expected=f"While held, {_leds(leds)} show {b.reset_value:0{w}b} (value {b.reset_value}). "
                         f"After release the count resumes from {b.reset_value}, first step about {1 / default.hz:g} s later.",
                confirm="The LEDs freeze at the reset value for as long as the button is held.",
                req_ids=["R-RESET"])
        if b.enable_input:
            inp = self.spec.input(b.enable_input)
            self.functions[component_id(inp.port, inp.index)] = "enable: counting pauses while off"
            self.add(f"Pause with {_input_label(inp)}", f"{self.verb(inp, 'set_off' if inp.port == 'sw' else 'release')}.",
                     actions=[self.act(inp, "set_off" if inp.port == "sw" else "release")], observe=leds,
                     expected="The count stops changing until the input is active again.",
                     confirm="The LEDs hold their value.", req_ids=["R-ENABLE"])
        if b.direction_control:
            inp = self.spec.input(b.direction_control.input)
            self.functions[component_id(inp.port, inp.index)] = f"direction: {b.direction_control.high_means} when on/pressed"
            self.add(f"Change direction with {_input_label(inp)}", f"Toggle {_input_label(inp)}.",
                     actions=[self.act(inp, "set_on" if inp.port == "sw" else "hold")], observe=leds,
                     expected=f"The count goes {b.direction_control.high_means} while the input is active.",
                     confirm="The binary value now moves the other way.", req_ids=["R-DIR"])

    def blinker(self, b: BlinkerBehavior) -> None:
        leds = self.out.indices
        default = b.rate_modes[-1]
        for i in leds:
            self.functions[component_id("led", i)] = f"blinks ({b.pattern})"
        pattern = "all together" if b.pattern == "together" else "alternating (even LEDs opposite odd LEDs)"
        self.add("Watch the LEDs blink", f"Watch {_leds(leds)}.", actions=[], observe=leds,
                 expected=f"{_leds(leds)} blink {pattern} at {_hz(default.hz)} Hz (on {1 / (2 * default.hz):g} s, off {1 / (2 * default.hz):g} s).",
                 confirm="Count the blinks for 10 s: about " f"{default.hz * 10:g}.",
                 req_ids=[f"R-RATE-{default.name}", "R-PATTERN"], observation_id=f"O-RATE-{default.name}")
        for m in b.rate_modes[:-1]:
            inp, action = self.mode_cond(m)
            self.functions[component_id(inp.port, inp.index)] = f"mode '{m.name}': {_hz(m.hz)} Hz blink"
            self.add(f"{self.verb(inp, action)} for {_hz(m.hz)} Hz", f"{self.verb(inp, action)}.",
                     actions=[self.act(inp, action)], observe=leds, expected=f"The blink rate changes to {_hz(m.hz)} Hz.",
                     confirm=f"Each on/off phase now lasts {1 / (2 * m.hz):.2f} s.", req_ids=[f"R-RATE-{m.name}"],
                     observation_id=f"O-RATE-{m.name}", hold=action == "hold")
        if b.enable_input:
            inp = self.spec.input(b.enable_input)
            self.functions[component_id(inp.port, inp.index)] = "enable: LEDs off while inactive"
            self.add(f"Turn blinking off with {_input_label(inp)}", f"{self.verb(inp, 'set_off' if inp.port == 'sw' else 'release')}.",
                     actions=[self.act(inp, "set_off" if inp.port == "sw" else "release")], observe=leds,
                     expected="All the design's LEDs stay off.", confirm="No blinking while disabled.", req_ids=["R-ENABLE"])

    def led_pattern(self, b: LedPatternBehavior) -> None:
        leds = self.out.indices
        if b.pattern in ("mirror_inputs", "invert_inputs"):
            srcs = [self.spec.input(n) for n in b.source_inputs]
            for k, (s, i) in enumerate(zip(srcs, leds)):
                self.functions[component_id(s.port, s.index)] = f"drives LD{i}" + (" (inverted)" if b.pattern == "invert_inputs" else "")
                self.functions[component_id("led", i)] = f"{'follows' if b.pattern == 'mirror_inputs' else 'inverse of'} {_input_label(s)}"
            first = srcs[0]
            self.add("Flip the switches", f"Slide {', '.join(_input_label(s) for s in srcs)} up and down one at a time.",
                     actions=[self.act(first, "set_on" if first.port == "sw" else "hold")], observe=leds,
                     expected=("Each LED lights exactly when its input is on." if b.pattern == "mirror_inputs"
                               else "Each LED is lit when its input is off, and goes dark when it is on."),
                     confirm=f"{_input_label(first)} controls LD{leds[0]}.", req_ids=["R-PATTERN"], observation_id="O-STATIC")
        else:
            default = b.rate_modes[-1]
            desc = {"chase": "a single lit LED moves one position per step and wraps around",
                    "bounce": "a single lit LED sweeps back and forth",
                    "fill": "a bar fills one LED per step, then clears"}[b.pattern]
            for i in leds:
                self.functions[component_id("led", i)] = f"{b.pattern} pattern"
            self.add(f"Watch the {b.pattern} pattern", f"Watch {_leds(leds)}.", actions=[], observe=leds,
                     expected=f"{desc[0].upper() + desc[1:]}, {_hz(default.hz)} steps/s"
                              + (f", moving {'towards LD' + str(max(leds)) if b.direction == 'left' else 'towards LD' + str(min(leds))}" if b.pattern != "bounce" else "") + ".",
                     confirm=f"Each step lasts {1 / default.hz:g} s.", req_ids=["R-PATTERN", f"R-RATE-{default.name}"],
                     observation_id=f"O-RATE-{default.name}")
            for m in b.rate_modes[:-1]:
                inp, action = self.mode_cond(m)
                self.functions[component_id(inp.port, inp.index)] = f"mode '{m.name}': {_hz(m.hz)} steps/s"
                self.add(f"{self.verb(inp, action)} for {_hz(m.hz)} steps/s", f"{self.verb(inp, action)}.",
                         actions=[self.act(inp, action)], observe=leds, expected=f"The pattern now steps {_hz(m.hz)} times per second.",
                         confirm=f"Each step lasts {1 / m.hz:.2f} s.", req_ids=[f"R-RATE-{m.name}"],
                         observation_id=f"O-RATE-{m.name}", hold=action == "hold")
            if b.direction_control:
                inp = self.spec.input(b.direction_control.input)
                self.functions[component_id(inp.port, inp.index)] = "direction control"
                self.add(f"Reverse with {_input_label(inp)}", f"Toggle {_input_label(inp)}.",
                         actions=[self.act(inp, "set_on" if inp.port == "sw" else "hold")], observe=leds,
                         expected="The pattern moves the other way while the input is active.", confirm="Direction flips.",
                         req_ids=["R-DIR"])
        if b.reset_input:
            inp = self.spec.input(b.reset_input)
            self.functions[component_id(inp.port, inp.index)] = "reset: restart the pattern"
            self.add(f"Restart with {_input_label(inp)}", f"Press {_input_label(inp)}.",
                     actions=[self.act(inp, "hold"), self.act(inp, "release")], observe=leds,
                     expected="The pattern returns to its starting position.", confirm="Only the first LED is lit / the bar is empty.",
                     req_ids=["R-RESET"])
        if b.enable_input:
            inp = self.spec.input(b.enable_input)
            self.functions[component_id(inp.port, inp.index)] = "enable: pattern holds while inactive"
            self.add(f"Freeze with {_input_label(inp)}", f"{self.verb(inp, 'set_off' if inp.port == 'sw' else 'release')}.",
                     actions=[self.act(inp, "set_off" if inp.port == "sw" else "release")], observe=leds,
                     expected="The pattern stops moving.", confirm="LEDs hold their state.", req_ids=["R-ENABLE"])

    def traffic(self, b: TrafficLightBehavior) -> None:
        leds = self.out.indices

        def lit(pattern: int) -> str:
            on = [f"LD{leds[k]}" for k in range(len(leds)) if pattern >> k & 1]
            return " + ".join(on) if on else "all off"

        for k, i in enumerate(leds):
            names = [s.name for s in b.states if s.lights >> k & 1]
            self.functions[component_id("led", i)] = "lit in " + (", ".join(names) if names else "no regular state")
        seq = "; ".join(f"{s.name}: {lit(s.lights)} for {s.duration_s:g} s" for s in b.states)
        self.add("Watch the light sequence", f"Watch {_leds(leds)} for one full cycle "
                 f"({sum(s.duration_s for s in b.states):g} s).", actions=[], observe=leds,
                 expected=f"{seq}; then it repeats.", confirm="Time each state with a stopwatch.",
                 req_ids=["R-FSM-SEQ", "R-FSM-OUT"], observation_id="O-FSM")
        if b.request_input and b.request_state and b.request_after:
            inp = self.spec.input(b.request_input)
            rs = b.request_state
            self.functions[component_id(inp.port, inp.index)] = f"request '{rs.name}' after {b.request_after}"
            self.add(f"Request '{rs.name}' with {_input_label(inp)}", f"Briefly press {_input_label(inp)} while the light is not in '{b.request_after}'.",
                     actions=[self.act(inp, "press")], observe=leds,
                     expected=f"When '{b.request_after}' ends, the FSM goes to '{rs.name}' ({lit(rs.lights)}) for {rs.duration_s:g} s, then continues.",
                     confirm="The extra state appears once per press.", req_ids=["R-FSM-REQ"])
        if b.reset_input:
            inp = self.spec.input(b.reset_input)
            self.functions[component_id(inp.port, inp.index)] = f"reset to {b.states[0].name}"
            self.add(f"Reset with {_input_label(inp)}", f"Press and release {_input_label(inp)}.",
                     actions=[self.act(inp, "hold"), self.act(inp, "release")], observe=leds,
                     expected=f"The light returns to '{b.states[0].name}' ({lit(b.states[0].lights)}) and its timer restarts.",
                     confirm=f"'{b.states[0].name}' lasts a full {b.states[0].duration_s:g} s after release.", req_ids=["R-RESET"])

    # ------------------------------------------------------------- output
    def build(self) -> dict[str, Any]:
        b = self.b
        if isinstance(b, CounterBehavior):
            self.counter(b)
        elif isinstance(b, BlinkerBehavior):
            self.blinker(b)
        elif isinstance(b, LedPatternBehavior):
            self.led_pattern(b)
        elif isinstance(b, TrafficLightBehavior):
            self.traffic(b)

        components = []
        for inp in self.spec.inputs:
            cid = component_id(inp.port, inp.index)
            components.append({
                "id": cid, "kind": "switch" if inp.port == "sw" else "button", "label": _input_label(inp),
                "pin": _pin(inp.port, inp.index), "role": "input", "signal": inp.name,
                "function": self.functions.get(cid, inp.description or "input"),
                "debounce_ms": inp.debounce_ms,
            })
        for o in self.spec.outputs:
            for k, i in enumerate(o.indices):
                cid = component_id("led", i)
                components.append({
                    "id": cid, "kind": "led", "label": _label("led", i), "pin": _pin("led", i), "role": "output",
                    "signal": f"{o.name}[{k}]" if o.width > 1 else o.name,
                    "function": self.functions.get(cid, o.description or "output"),
                })
        obs = {o.id: o for o in derive_observations(self.spec)}
        for st in self.steps:
            o = obs.get(st["observation_id"] or "")
            st["camera_verifiable"] = bool(o and o.camera_verifiable)
        return {
            "board": "basys3",
            "title": self.spec.title,
            "summary": self.spec.summary,
            "family": self.spec.family,
            "components": components,
            "unused_note": "Every LED not listed is held off by the trusted wrapper; the seven-segment display is blanked.",
            "steps": self.steps,
            "derived_from": {
                "spec": True,
                "test_scenarios": sorted({s["scenario_id"] for s in self.steps if s["scenario_id"]}),
                "simulation_report": bool(self.report),
            },
            "camera_limit_hz": MAX_CAMERA_EVENT_HZ,
        }


def build_guide(spec: DesignSpec, plan: dict | None = None, report: dict | None = None) -> dict[str, Any]:
    return _Builder(spec, plan, report).build()


def board_definition() -> dict[str, Any]:
    """Trusted board facts for the dashboard (labels and pins), straight from board.json."""
    board = load_board("basys3")
    return {
        "id": board.id, "name": board.name, "part": board.part, "idcode": board.idcode,
        "clock": {"port": board.clock_port, "hz": board.clock_hz},
        "iostandard": board.iostandard,
        "ports": {name: {"direction": p.direction, "kind": p.kind, "pins": list(p.pins), "active": p.active}
                  for name, p in board.ports.items()},
    }
