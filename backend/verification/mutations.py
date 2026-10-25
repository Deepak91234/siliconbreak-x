"""Deliberately defective RTL ("mutants") for oracle tests and the fault-injection demo mode.

Each mutation is a small, deterministic text edit of either the behaviour core
(`cl_core.v`, target "core") or the trusted board wrapper (`basys3_top.v`, target
"wrapper"). They are written against the shapes produced by the SiliconBreak X
generators; `apply_mutation` raises ValueError when the edit does not apply to the
given Verilog (pattern not found or no change), so callers can skip it.

A good oracle must reject every applicable mutant: at least one failing check or a
lint failure. None of these edits ever runs anything; they only rewrite text.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

ALL_FAMILIES: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Mutation:
    name: str
    description: str
    target: str  # "core" | "wrapper"
    families: frozenset[str] = field(default_factory=frozenset)  # empty = every family
    _apply: Callable[[str], str] | None = None

    def applies_to(self, family: str) -> bool:
        return not self.families or family in self.families

    def apply(self, verilog: str) -> str:
        assert self._apply is not None
        out = self._apply(verilog)
        if out == verilog:
            raise ValueError(f"mutation '{self.name}' does not change this RTL")
        return out


def _sub1(pattern: str, repl, text: str, what: str, flags: int = 0) -> str:
    out, n = re.subn(pattern, repl, text, count=1, flags=flags)
    if n == 0:
        raise ValueError(f"{what} not found")
    return out


def _output_port(verilog: str) -> tuple[str, int]:
    m = re.search(r"\boutput\s+(?:wire|reg)?\s*(?:\[(\d+)\s*:\s*0\])?\s*([A-Za-z_]\w*)", verilog)
    if not m:
        raise ValueError("no output port")
    return m.group(2), (int(m.group(1)) + 1) if m.group(1) else 1


def _output_assign(verilog: str) -> re.Match:
    name, _w = _output_port(verilog)
    m = None
    for m in re.finditer(r"\bassign\s+" + re.escape(name) + r"\s*=\s*([^;]+);", verilog):
        pass  # the last continuous assignment of the output port
    if m is None:
        raise ValueError(f"no 'assign {name} = ...;'")
    return m


# --------------------------------------------------------------------------- core mutations


def _stuck_output(v: str) -> str:
    name, w = _output_port(v)
    m = _output_assign(v)
    return v[: m.start()] + f"assign {name} = {{{w}{{1'b0}}}} & ({m.group(1).strip()});" + v[m.end():]


def _invert_output(v: str) -> str:
    name, _w = _output_port(v)
    m = _output_assign(v)
    return v[: m.start()] + f"assign {name} = ~({m.group(1).strip()});" + v[m.end():]


def _double_rate(v: str) -> str:
    return _sub1(r"\.period\(\s*period\s*\)", ".period(period >> 1)", v, "tick divider '.period(period)'")


def _wrong_direction(v: str) -> str:
    return _sub1(r"\?\s*\(\s*(\w+)\s*\+\s*([^()]+?)\s*\)\s*:\s*\(\s*(\w+)\s*-\s*([^()]+?)\s*\)",
                 lambda m: f"? ({m.group(1)} - {m.group(2)}) : ({m.group(3)} + {m.group(4)})",
                 v, "up/down step expression 'c ? (v + 1) : (v - 1)'")


def _step_by_two(v: str) -> str:
    # both the up and the down step, so a fixed-direction counter cannot hide the defect in a dead branch
    out, n = re.subn(r"(\w+\s*[+-]\s*)(\d+)'d1\b", lambda m: f"{m.group(1)}{m.group(2)}'d2", v)
    if n == 0:
        raise ValueError("'value +/- N'd1' step not found")
    return out


def _wrong_initial(v: str) -> str:
    def repl(m: re.Match) -> str:
        w, k = int(m.group(2)) + 1, int(m.group(4))
        return f"{m.group(1)}{w}'d{(k + 1) % (1 << w)};"
    return _sub1(r"(reg\s+\[(\d+):0\]\s+(value)\s*=\s*)\d+'d(\d+);", repl, v, "counter register initializer")


def _ignore_reset(v: str) -> str:
    return _sub1(r"(wire\s+hold_reset\s*=)\s*(?!1'b0\s*;)([^;\s][^;]*);", lambda m: f"{m.group(1)} ({m.group(2).strip()}) & 1'b0;",
                 v, "'hold_reset' driven by a reset input")


def _no_timer_restart(v: str) -> str:
    if not re.search(r"wire\s+hold_reset\s*=\s*(?!1'b0\s*;)[^;\s]", v):
        raise ValueError("no reset input drives 'hold_reset'")
    return _sub1(r"\.clear\(\s*hold_reset\s*\)", ".clear(1'b0)", v, "tick divider '.clear(hold_reset)'")


def _ignore_enable(v: str) -> str:
    return _sub1(r"(wire\s+run\s*=)\s*(?!1'b1\s*;)([^;\s][^;]*);", lambda m: f"{m.group(1)} ({m.group(2).strip()}) | 1'b1;",
                 v, "'run' driven by an enable input")


def _chase_no_wrap(v: str) -> str:
    return _sub1(r"\{(\w+)\[(\d+):0\],\s*\1\[(\d+)\]\}", lambda m: f"{{{m.group(1)}[{m.group(2)}:0], 1'b0}}",
                 v, "rotate-left '{x[n-2:0], x[n-1]}'")


def _mirror_bit_swap(v: str) -> str:
    def repl(m: re.Match) -> str:
        items = [x.strip() for x in m.group(2).split(",")]
        if len(items) < 2:
            raise ValueError("single source input")
        items[-1], items[-2] = items[-2], items[-1]
        return f"{m.group(1)}{{{', '.join(items)}}};"
    return _sub1(r"(wire\s+\[\d+:0\]\s+live\s*=\s*)\{([^}]*)\};", repl, v, "'live' source concatenation")


def _fsm_skip_state(v: str) -> str:
    if not re.search(r"\bST_2\b", v):
        raise ValueError("fewer than three states")
    return _sub1(r"(ST_0:\s*begin\s*state\s*<=\s*)ST_1(\s*;\s*lamps\s*<=\s*)L_1\s*;",
                 lambda m: f"{m.group(1)}ST_2{m.group(2)}L_2 & (L_1 | ~L_1);", v, "transition ST_0 -> ST_1")


def _fsm_ignore_request(v: str) -> str:
    return _sub1(r"if\s*\(\s*req_pending\s*\)\s*begin", "if (req_pending && 1'b0) begin", v, "request decision")


def _fsm_latch_stuck(v: str) -> str:
    return _sub1(r"(else\s+if\s*\(\s*take_req\s*\)\s*begin\s*req_pending\s*<=\s*)1'b0\s*;",
                 lambda m: f"{m.group(1)}1'b1;", v, "request latch clear")


def _fsm_reset_keeps_timer(v: str) -> str:
    return _sub1(r"(if\s*\(\s*hold_reset\s*\)\s*begin\s*state\s*<=\s*ST_0\s*;\s*)timer\s*<=\s*\{TW\{1'b0\}\}\s*;",
                 lambda m: m.group(1), v, "timer clear in the reset branch")


def _fsm_short_state(v: str) -> str:
    return _sub1(r"(TICKS_STATE_\w+)\[TW-1:0\]\s*-\s*1'b1", lambda m: f"({m.group(1)}[TW-1:0] >> 1) - 1'b1",
                 v, "state duration 'TICKS_STATE_X[TW-1:0] - 1'b1'")


# --------------------------------------------------------------------------- wrapper mutations


def _wrapper_no_debounce(v: str) -> str:
    # keep the DEBOUNCE_* parameters referenced so the defect survives lint and must be caught in simulation
    out, n = re.subn(r"\.STABLE_CYCLES\(\s*(DEBOUNCE_\w+)\s*\)", lambda m: f".STABLE_CYCLES({m.group(1)} - {m.group(1)} + 1)", v)
    if n == 0:
        raise ValueError("no conditioned inputs")
    return out


def _wrapper_led_stuck(v: str) -> str:
    drives = list(re.finditer(r"assign\s+led\[(\d+)\]\s*=\s*(\w+)\[(\d+)\]\s*;", v))
    if not drives:
        drives = list(re.finditer(r"assign\s+led\[(\d+)\]\s*=\s*(?!1'b)(\w+)\s*;", v))
    if not drives:
        raise ValueError("no LED driven by the core")
    m = drives[1] if len(drives) > 1 else drives[0]
    return v[: m.start()] + f"assign led[{m.group(1)}] = 1'b0 & {m.group(0)[m.group(0).index('=') + 1:].strip()}" + v[m.end():]


def _wrapper_idle_led_on(v: str) -> str:
    return _sub1(r"(assign\s+led\[\d+\]\s*=\s*)1'b0\s*;", lambda m: f"{m.group(1)}1'b1;", v, "unused LED")


def _wrapper_display_swap(v: str) -> str:
    """Swap the two rightmost digit fields fed to cl_sevenseg (AN1 <-> AN0)."""
    m = re.search(r"(_display_digits\s*=\s*\{)([^}]*)(\})", v)
    if not m:
        raise ValueError("no seven-segment display")
    parts = [p.strip() for p in m.group(2).split(",")]
    if len(parts) != 4 or parts[2] == parts[3]:
        raise ValueError("display digit fields cannot be swapped")
    parts[2], parts[3] = parts[3], parts[2]
    return v[: m.start()] + m.group(1) + ", ".join(parts) + m.group(3) + v[m.end():]


_PERIODIC = frozenset({"counter", "blinker", "led_pattern"})

MUTATIONS: dict[str, Mutation] = {m.name: m for m in [
    Mutation("stuck_output", "the behaviour output is stuck at 0", "core", ALL_FAMILIES, _stuck_output),
    Mutation("invert_output", "every output bit is inverted", "core", ALL_FAMILIES, _invert_output),
    Mutation("double_rate", "the step timer runs twice as fast as specified", "core", _PERIODIC, _double_rate),
    Mutation("wrong_direction", "the counter steps in the opposite direction", "core",
             frozenset({"counter"}), _wrong_direction),
    Mutation("step_by_two", "the counter steps by 2 instead of 1", "core", frozenset({"counter"}), _step_by_two),
    Mutation("wrong_initial_value", "the counter powers up one above its initial value", "core",
             frozenset({"counter"}), _wrong_initial),
    Mutation("ignore_reset", "the reset input has no effect", "core",
             frozenset({"counter", "led_pattern", "traffic_light"}), _ignore_reset),
    Mutation("no_timer_restart", "reset does not restart the step timer", "core",
             frozenset({"counter", "led_pattern"}), _no_timer_restart),
    Mutation("ignore_enable", "the enable input has no effect", "core", _PERIODIC, _ignore_enable),
    Mutation("chase_no_wrap", "the chase pattern falls off the top instead of wrapping", "core",
             frozenset({"led_pattern"}), _chase_no_wrap),
    Mutation("mirror_bit_swap", "the two lowest source inputs drive each other's LEDs", "core",
             frozenset({"led_pattern"}), _mirror_bit_swap),
    Mutation("fsm_skip_state", "the FSM skips the second state", "core", frozenset({"traffic_light"}), _fsm_skip_state),
    Mutation("fsm_ignore_request", "a latched request is never served", "core",
             frozenset({"traffic_light"}), _fsm_ignore_request),
    Mutation("fsm_request_latch_stuck", "the request latch is never cleared", "core",
             frozenset({"traffic_light"}), _fsm_latch_stuck),
    Mutation("fsm_reset_keeps_timer", "reset does not clear the state timer", "core",
             frozenset({"traffic_light"}), _fsm_reset_keeps_timer),
    Mutation("fsm_short_state", "the first state lasts half its duration", "core",
             frozenset({"traffic_light"}), _fsm_short_state),
    Mutation("wrapper_no_debounce", "the input conditioners do not debounce", "wrapper",
             ALL_FAMILIES, _wrapper_no_debounce),
    Mutation("wrapper_led_stuck", "one LED driven by the core is stuck off", "wrapper",
             ALL_FAMILIES, _wrapper_led_stuck),
    Mutation("wrapper_idle_led_on", "an unused LED is lit", "wrapper", ALL_FAMILIES, _wrapper_idle_led_on),
    Mutation("wrapper_display_swap", "two display digits are swapped", "wrapper",
             frozenset({"seven_segment"}), _wrapper_display_swap),
]}


def apply_mutation(name: str, verilog: str) -> str:
    """Apply mutation `name` to `verilog`; raises ValueError if unknown or not applicable."""
    m = MUTATIONS.get(name)
    if m is None:
        raise ValueError(f"unknown mutation '{name}'")
    return m.apply(verilog)
