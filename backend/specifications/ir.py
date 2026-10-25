"""CircuitLoop hardware specification intermediate representation (IR).

The IR is the single contract between every pipeline stage:

    natural language --(intent parser)--> DesignSpec
    DesignSpec --(capability validation)--> ValidationReport
    DesignSpec --(trusted generator)--> board wrapper + XDC + core port header
    DesignSpec --(RTL generator / LLM)--> cl_core body
    DesignSpec --(test generator, independent of RTL)--> stimulus + oracle checks
    DesignSpec --(derive)--> requirements + camera observations

Design rules enforced here:
  * Physical pins never appear in the IR. Inputs/outputs reference trusted board
    port names (btnU, sw[3], led[0]...) and boards/basys3/board.json maps those
    to package pins.
  * Every timing value is a physical quantity (Hz or seconds). The trusted
    generator converts them into integer cycle counts (TICKS_* parameters) for
    a given clock, so the same RTL can be simulated with an accelerated clock.
  * Logical names are restricted identifiers so they can be used verbatim as
    Verilog port names without injection risk.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

SPEC_VERSION = "1"

BUTTON_PORTS = ("btnC", "btnU", "btnL", "btnR", "btnD")
INPUT_PORTS = BUTTON_PORTS + ("sw",)
OUTPUT_PORTS = ("led", "display")  # display = the 4-digit seven-segment display
DISPLAY_DIGITS = 4  # Basys 3: AN3 (leftmost) .. AN0 (rightmost)
DIGIT_BITS = 5      # per display digit on the core port: {lit, hex[3:0]}
DISPLAY_CHARS = "0123456789abcdef "  # what the seven_segment behaviour can show (hex digits + blank)

_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
_RESERVED = {
    # Verilog / SystemVerilog keywords that fit the identifier pattern
    "always", "and", "assign", "begin", "buf", "case", "casex", "casez", "cmos",
    "deassign", "default", "defparam", "disable", "edge", "else", "end", "endcase",
    "endfunction", "endgenerate", "endmodule", "endprimitive", "endspecify", "endtable",
    "endtask", "event", "for", "force", "forever", "fork", "function", "generate",
    "genvar", "highz0", "highz1", "if", "ifnone", "initial", "inout", "input", "integer",
    "join", "large", "localparam", "macromodule", "medium", "module", "nand", "negedge",
    "nmos", "nor", "not", "notif0", "notif1", "or", "output", "parameter", "pmos",
    "posedge", "primitive", "pull0", "pull1", "pulldown", "pullup", "rcmos", "real",
    "realtime", "reg", "release", "repeat", "rnmos", "rpmos", "rtran", "rtranif0",
    "rtranif1", "scalared", "signed", "small", "specify", "specparam", "strong0",
    "strong1", "supply0", "supply1", "table", "task", "time", "tran", "tranif0",
    "tranif1", "tri", "tri0", "tri1", "triand", "trior", "trireg", "unsigned", "vectored",
    "wait", "wand", "weak0", "weak1", "while", "wire", "wor", "xnor", "xor", "logic",
    "bit", "byte", "int", "shortint", "longint", "struct", "union", "enum", "typedef",
    "class", "interface", "package", "import", "export", "return", "break", "continue",
    "do", "final", "unique", "priority", "property", "sequence", "assert", "assume",
    "cover", "string", "void", "static", "automatic", "const", "ref", "var", "type",
    # Names owned by the trusted wrapper / board shell
    "clk", "sw", "led", "seg", "dp", "an", "btnc", "btnu", "btnl", "btnr", "btnd",
}

def check_identifier(value: str) -> str:
    if not _IDENT_RE.match(value):
        raise ValueError(
            f"'{value}' is not a valid CircuitLoop identifier "
            "(lowercase letter first, then lowercase letters/digits/underscore, max 31 chars)"
        )
    if value in _RESERVED:
        raise ValueError(f"'{value}' is a reserved name")
    return value


Identifier = Annotated[str, AfterValidator(check_identifier)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- I/O


class InputBinding(_Model):
    """A board input as seen by the generated core.

    The trusted wrapper synchronizes (2-FF) and, when debounce_ms > 0, debounces
    the raw pin before it reaches the core. The core therefore receives a clean,
    active-high level named `name`.
    """

    name: Identifier
    port: Literal["btnC", "btnU", "btnL", "btnR", "btnD", "sw"]
    index: Optional[int] = Field(default=None, description="Switch number 0-15; must be None for buttons")
    debounce_ms: float = Field(default=10.0, ge=0.0, le=50.0)
    description: str = ""

    @model_validator(mode="after")
    def _check_index(self) -> "InputBinding":
        if self.port == "sw":
            if self.index is None or not (0 <= self.index <= 15):
                raise ValueError("switch inputs need index 0-15")
        elif self.index is not None:
            raise ValueError(f"{self.port} is a single button; index must be omitted")
        return self

    @property
    def board_signal(self) -> str:
        return f"sw[{self.index}]" if self.port == "sw" else self.port


class OutputBinding(_Model):
    """A core output bus.

    port="led":     core bit i drives led[indices[i]].
    port="display": the seven-segment display. `indices` are digit positions
                    (0 = rightmost digit AN0 ... 3 = leftmost AN3). Core bits
                    [5k+4:5k] describe digit indices[k] as {lit, hex[3:0]}; the
                    trusted wrapper multiplexes and decodes them onto seg/an.
    """

    name: Identifier
    port: Literal["led", "display"] = "led"
    indices: list[int] = Field(min_length=1, max_length=16)
    description: str = ""

    @model_validator(mode="after")
    def _check_indices(self) -> "OutputBinding":
        v = self.indices
        if len(set(v)) != len(v):
            raise ValueError("indices must be unique within an output")
        if self.port == "led" and any(i < 0 or i > 15 for i in v):
            raise ValueError("LED indices must be 0-15")
        if self.port == "display" and any(i < 0 or i >= DISPLAY_DIGITS for i in v):
            raise ValueError(f"display digit positions must be 0-{DISPLAY_DIGITS - 1} (0 = rightmost)")
        return self

    @property
    def width(self) -> int:
        return len(self.indices) * (DIGIT_BITS if self.port == "display" else 1)

    @property
    def leds(self) -> list[int]:
        """Physical LEDs this output drives (none for the display)."""
        return list(self.indices) if self.port == "led" else []


# --------------------------------------------------------------------------- behaviour


class RateMode(_Model):
    """One speed of a periodic behaviour.

    Modes are evaluated in order; the first mode whose condition holds is
    active. The last mode must be unconditional (the default speed).
    `hz` is the number of behaviour steps per second (counter increments,
    pattern steps, or LED toggles -- see each family).
    """

    name: Identifier
    hz: float = Field(gt=0.0, le=1000.0)
    when_input: Optional[Identifier] = None
    when_level: Literal[0, 1] = 1


class DirectionControl(_Model):
    input: Identifier
    high_means: Literal["up", "down"] = "up"


class CounterBehavior(_Model):
    """Binary counter shown on LEDs.

    Each tick of the active rate mode adds +1 (up) or -1 (down) modulo
    2**width (or saturates when wrap=False). While reset_input is high the
    value is held at reset_value. While enable_input is low the counter pauses.
    """

    family: Literal["counter"] = "counter"
    output: Identifier
    direction: Literal["up", "down"] = "up"
    direction_control: Optional[DirectionControl] = None
    rate_modes: list[RateMode] = Field(min_length=1, max_length=4)
    reset_input: Optional[Identifier] = None
    reset_value: int = 0
    initial_value: int = 0
    enable_input: Optional[Identifier] = None
    wrap: bool = True


class BlinkerBehavior(_Model):
    """LEDs blinking with a 50% duty cycle.

    RateMode.hz is the blink frequency (full on+off cycles per second); the LEDs
    toggle at 2*hz. pattern='together' drives every bit identically,
    'alternate' drives even bits opposite to odd bits.
    """

    family: Literal["blinker"] = "blinker"
    output: Identifier
    pattern: Literal["together", "alternate"] = "together"
    rate_modes: list[RateMode] = Field(min_length=1, max_length=4)
    enable_input: Optional[Identifier] = None


class LedPatternBehavior(_Model):
    """Switch/button controlled LED patterns.

    mirror_inputs / invert_inputs: output bit i follows (or inverts) source_inputs[i]
    chase:  a single lit LED moving one position per step, wrapping around
    bounce: a single lit LED moving back and forth (Knight Rider)
    fill:   a bar graph that fills one LED per step, then clears
    `direction` 'left' means towards higher LED indices.
    """

    family: Literal["led_pattern"] = "led_pattern"
    output: Identifier
    pattern: Literal["mirror_inputs", "invert_inputs", "chase", "bounce", "fill"]
    source_inputs: list[Identifier] = Field(default_factory=list)
    direction: Literal["left", "right"] = "left"
    direction_control: Optional[DirectionControl] = None
    rate_modes: list[RateMode] = Field(default_factory=list, max_length=4)
    enable_input: Optional[Identifier] = None
    reset_input: Optional[Identifier] = None


class TrafficState(_Model):
    name: Identifier
    duration_s: float = Field(gt=0.0, le=20.0)
    lights: int = Field(ge=0, description="Bit pattern driven on the output while in this state")


class TrafficLightBehavior(_Model):
    """Cyclic finite-state machine (traffic light).

    States are visited in list order and wrap to the first state. If
    request_input is set, a press is latched; when the state named
    request_after ends with a request pending, the FSM enters request_state
    (which is otherwise skipped) and clears the latch.
    reset_input returns to the first state with its timer restarted.
    """

    family: Literal["traffic_light"] = "traffic_light"
    output: Identifier
    states: list[TrafficState] = Field(min_length=2, max_length=8)
    request_input: Optional[Identifier] = None
    request_after: Optional[Identifier] = None
    request_state: Optional[TrafficState] = None
    reset_input: Optional[Identifier] = None


def check_display_text(value: str) -> str:
    if not 1 <= len(value) <= DISPLAY_DIGITS:
        raise ValueError(f"display text must be 1-{DISPLAY_DIGITS} characters")
    bad = sorted({c for c in value.lower() if c not in DISPLAY_CHARS})
    if bad:
        raise ValueError(f"the seven-segment display can show hex digits 0-9, A-F and blanks; not {bad}")
    return value


class SevenSegmentBehavior(_Model):
    """Static text on the seven-segment display.

    `text` is shown right-aligned on the display output's digit positions,
    leftmost character on the highest position; positions without a character
    (and spaces) are blank. Letters a-f use the conventional A, b, C, d, E, F
    glyphs. The decimal point stays off.
    """

    family: Literal["seven_segment"] = "seven_segment"
    output: Identifier
    text: Annotated[str, AfterValidator(check_display_text)]

    def digit_map(self, positions: list[int]) -> dict[int, Optional[int]]:
        """Display position -> hex value shown there (None = blank), right-aligned."""
        order = sorted(positions, reverse=True)  # leftmost first
        chars = list(self.text.rjust(len(order)))
        out: dict[int, Optional[int]] = {}
        for pos, ch in zip(order, chars):
            out[pos] = None if ch == " " else int(ch, 16)
        return out


Behavior = Annotated[
    Union[CounterBehavior, BlinkerBehavior, LedPatternBehavior, TrafficLightBehavior, SevenSegmentBehavior],
    Field(discriminator="family"),
]

FAMILIES = ("counter", "blinker", "led_pattern", "traffic_light", "seven_segment")


# --------------------------------------------------------------------------- spec


class DesignSpec(_Model):
    spec_version: Literal["1"] = SPEC_VERSION
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=2000)
    board: Literal["basys3"] = "basys3"
    clock_hz: Literal[100_000_000] = 100_000_000
    inputs: list[InputBinding] = Field(default_factory=list, max_length=21)
    outputs: list[OutputBinding] = Field(min_length=1, max_length=4)
    behavior: Behavior
    assumptions: list[str] = Field(default_factory=list)

    @property
    def family(self) -> str:
        return self.behavior.family

    def input(self, name: str) -> InputBinding:
        for i in self.inputs:
            if i.name == name:
                return i
        raise KeyError(name)

    def output(self, name: str) -> OutputBinding:
        for o in self.outputs:
            if o.name == name:
                return o
        raise KeyError(name)

    @property
    def used_leds(self) -> set[int]:
        return {i for o in self.outputs for i in o.leds}

    @property
    def display_output(self) -> Optional[OutputBinding]:
        return next((o for o in self.outputs if o.port == "display"), None)

    @model_validator(mode="after")
    def _structural_checks(self) -> "DesignSpec":
        """Structural consistency only; board capability checks live in validate.py."""
        names = [b.name for b in self.inputs] + [b.name for b in self.outputs]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"duplicate signal names: {sorted(dup)}")
        sigs = [b.board_signal for b in self.inputs]
        dup = {s for s in sigs if sigs.count(s) > 1}
        if dup:
            raise ValueError(f"board inputs bound more than once: {sorted(dup)}")
        leds = [i for o in self.outputs for i in o.leds]
        dup = {i for i in leds if leds.count(i) > 1}
        if dup:
            raise ValueError(f"LEDs driven by more than one output: {sorted(dup)}")
        if sum(1 for o in self.outputs if o.port == "display") > 1:
            raise ValueError("there is one seven-segment display; at most one output may use port 'display'")

        input_names = {b.name for b in self.inputs}
        output_names = {b.name for b in self.outputs}
        for ref in referenced_inputs(self.behavior):
            if ref not in input_names:
                raise ValueError(f"behaviour references unknown input '{ref}'")
        if self.behavior.output not in output_names:
            raise ValueError(f"behaviour drives unknown output '{self.behavior.output}'")
        driven = self.output(self.behavior.output)
        if isinstance(self.behavior, SevenSegmentBehavior):
            if driven.port != "display":
                raise ValueError("the seven_segment behaviour must drive an output with port 'display'")
            if len(self.behavior.text) > len(driven.indices):
                raise ValueError(f"display text '{self.behavior.text}' has {len(self.behavior.text)} characters but "
                                 f"the display output has only {len(driven.indices)} digit positions")
        elif driven.port != "led":
            raise ValueError(f"the {self.behavior.family} behaviour drives LEDs; output '{driven.name}' is the display")

        modes = getattr(self.behavior, "rate_modes", [])
        if modes:
            mode_names = [m.name for m in modes]
            if len(set(mode_names)) != len(mode_names):
                raise ValueError("rate mode names must be unique")
            if modes[-1].when_input is not None:
                raise ValueError("the last rate mode must be unconditional (the default speed)")
            if any(m.when_input is None for m in modes[:-1]):
                raise ValueError("only the last rate mode may be unconditional")
        return self


def referenced_inputs(b: BaseModel) -> list[str]:
    refs: list[str] = []
    for field in ("reset_input", "enable_input", "request_input"):
        v = getattr(b, field, None)
        if v:
            refs.append(v)
    dc = getattr(b, "direction_control", None)
    if dc is not None:
        refs.append(dc.input)
    for m in getattr(b, "rate_modes", []) or []:
        if m.when_input:
            refs.append(m.when_input)
    refs.extend(getattr(b, "source_inputs", []) or [])
    return refs
