"""Rule-based intent interpretation used when no LLM provider is configured.

This is deliberately modest: it recognises the four supported families in
common phrasings (the demo sentences, number words, en-dash ranges, "N Hz",
"hold BTNU for 3x", "use BTNC to reset", ...) and a handful of modifications to
an existing design. Everything it infers that the user did not say explicitly is
recorded as an assumption. When it cannot interpret a request it says so and
points out that no LLM is configured, instead of guessing.

The output is a plain dataclass; `backend.agent.intent` wraps it into the
public IntentResult (interpreter "offline-rules").
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from backend.specifications.ir import (
    BlinkerBehavior,
    CounterBehavior,
    DesignSpec,
    LedPatternBehavior,
    TrafficLightBehavior,
)

NO_LLM = ("No LLM is configured (set ANTHROPIC_API_KEY to enable the AI interpreter), so CircuitLoop is using "
          "its offline rules")
OFFLINE_EXAMPLES = ("Examples the offline rules understand: \"Build a 4-bit counter on LEDs 0-3 that counts once "
                    "per second and 3 times per second while I hold BTNU\", \"Blink LED0 at 2 Hz\", \"LEDs 0-7 follow "
                    "switches 0-7\", \"Knight Rider on LEDs 0-7\", \"A traffic light on LEDs 0-2\", and changes such as "
                    "\"count backward and use BTNC to reset it\".")

DEFAULT_DEBOUNCE_MS = 10.0
DEFAULT_PATTERN_HZ = 4.0
DEFAULT_TRAFFIC = (("green", 4.0), ("yellow", 1.0), ("red", 3.0))
DEFAULT_WALK_S = 3.0


@dataclass
class OfflineResult:
    kind: str
    spec: DesignSpec | None = None
    message: str = ""
    change_summary: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    unsupported_reasons: list[str] = field(default_factory=list)


class _Clarify(Exception):
    """The request is understood but ambiguous or contradictory."""


class _Unsupported(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


# =============================================================================== normalisation

_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "hundred": 100,
}


def normalize(text: str) -> str:
    t = text.lower()
    for dash in ("–", "—", "−", "‐", "‑"):
        t = t.replace(dash, "-")
    t = t.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", lambda m: str(_NUMBER_WORDS[m.group(1)]), t)
    t = re.sub(r"\bonce\b", "1 time", t)
    t = re.sub(r"\btwice\b", "2 times", t)
    t = re.sub(r"\bthrice\b", "3 times", t)
    t = re.sub(r"\bhalf (?:a )?second\b", "0.5 seconds", t)
    t = re.sub(r"\bevery second\b", "every 1 second", t)
    t = re.sub(r"\ba second\b", "per second", t)
    t = re.sub(r"\bdouble (?:the )?speed\b", "2 times faster", t)
    t = re.sub(r"\bhalf (?:the )?speed\b|\bhalf as fast\b", "0.5 times faster", t)
    t = re.sub(r"(\d)\s*-\s*bit\b", r"\1-bit", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _clauses(t: str) -> list[str]:
    # A dot ends a sentence unless it is a decimal point ("0.5 Hz"); "otherwise" stays
    # in its clause because it marks the default rate.
    parts = re.split(r"\.(?!\d)|[;!?,]| and | but | whereas |(?=\botherwise\b)", t)
    return [p.strip() for p in parts if p and p.strip()]


# =============================================================================== extraction helpers

_BTN_RE = re.compile(
    r"\b(?:btn ?([cudlr])|bt([cudlr])|button ?([cudlr])"
    r"|(center|centre|middle|up|top|down|bottom|left|right) (?:push ?)?button)\b")
_BTN_MAP = {"c": "btnC", "center": "btnC", "centre": "btnC", "middle": "btnC", "u": "btnU", "up": "btnU",
            "top": "btnU", "d": "btnD", "down": "btnD", "bottom": "btnD", "l": "btnL", "left": "btnL",
            "r": "btnR", "right": "btnR"}
_SW_RANGE_RE = re.compile(r"\b(?:switches|sws?|switch) ?(\d+) ?(?:-|to|through|thru) ?(?:sw ?|switch ?)?(\d+)\b")
_SW_RE = re.compile(r"\b(?:sw|switch) ?(\d+)\b")
_LED_RANGE_RE = re.compile(r"\b(?:leds?|ld) ?(\d+) ?(?:-|to|through|thru|\.\.) ?(?:leds? ?|ld ?)?(\d+)\b")
_LED_LIST_RE = re.compile(r"\bleds? ((?:\d+ ?, ?)+(?:and )?\d+)\b")
_LED_FIRST_RE = re.compile(r"\b(?:first|lowest|rightmost|bottom|right-most) (\d+) leds\b")
_LED_ALL_RE = re.compile(r"\ball (?:16 |of the |the )?leds\b")
_LED_ONE_RE = re.compile(r"\b(?:led|ld) ?(\d+)\b")
_LED_COUNT_RE = re.compile(r"\b(\d+) leds\b")
_WIDTH_RE = re.compile(r"\b(\d+)-bit\b")

_RESET_RE = re.compile(r"\breset|\brestart|\bclear(?:s|ed)?\b|\bback to (?:0|zero|the (?:start|beginning))")
_ENABLE_RE = re.compile(r"\benabl|\bpause|\bfreez|\bstop(?:s|ped)?\b|\bonly (?:count|run|blink|move|step)s?\b|\bhalt")
_PAUSE_RE = re.compile(r"\bpause|\bfreez|\bstop(?:s|ped)?\b|\bhalt")
_DIR_RE = re.compile(r"\bdirection|\brevers|\bbackwards?\b|\bcount(?:s|ing)? down\b|\bup/down\b|\bdown/up\b")
_REQUEST_RE = re.compile(r"pedestrian|\bwalk|crosswalk|crossing|\brequest")
_SPEED_RE = re.compile(r"\bfast|\bslow|\bspeed|\bquick|\brate\b")
_HOLD_RE = re.compile(r"\b(?:hold|holding|held|press|pressing|pressed|push|pushing|pushed|keep)\b")
_RELEASE_RE = re.compile(r"\b(?:release|released|not (?:pressed|held|holding|pressing))\b")


def _buttons(t: str) -> list[str]:
    out = []
    for m in _BTN_RE.finditer(t):
        key = next(g for g in m.groups() if g)
        port = _BTN_MAP[key]
        if port not in out:
            out.append(port)
    return out


def _switches(t: str) -> list[int]:
    out: list[int] = []
    for m in _SW_RANGE_RE.finditer(t):
        a, b = int(m.group(1)), int(m.group(2))
        for i in range(min(a, b), max(a, b) + 1):
            if i not in out:
                out.append(i)
    t2 = _SW_RANGE_RE.sub(" ", t)
    for m in _SW_RE.finditer(t2):
        i = int(m.group(1))
        if i not in out:
            out.append(i)
    return out


def _strip_refs(t: str) -> str:
    """Remove button/switch/LED references so their words ('right', 'up', 'down') are not misread."""
    t = _BTN_RE.sub(" ", t)
    t = _SW_RANGE_RE.sub(" ", t)
    t = _SW_RE.sub(" ", t)
    t = _LED_RANGE_RE.sub(" ", t)
    return t


def _leds(t: str) -> list[int] | None:
    m = _LED_RANGE_RE.search(t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return list(range(min(a, b), max(a, b) + 1))
    m = _LED_LIST_RE.search(t)
    if m:
        vals = [int(x) for x in re.findall(r"\d+", m.group(1))]
        return list(dict.fromkeys(vals))
    m = _LED_FIRST_RE.search(t)
    if m:
        return list(range(int(m.group(1))))
    if _LED_ALL_RE.search(t):
        return list(range(16))
    m = _LED_COUNT_RE.search(t)
    if m:
        return list(range(int(m.group(1))))
    ones = [int(x) for x in _LED_ONE_RE.findall(t)]
    if ones:
        return list(dict.fromkeys(ones))
    return None


def _num(s: str) -> float:
    v = float(s)
    return int(v) if v.is_integer() else v


def _rate(clause: str) -> tuple[float | None, float | None, str | None]:
    """(absolute hz, multiplier, assumption) found in one clause."""
    m = re.search(r"(\d+(?:\.\d+)?) ?khz\b", clause)
    if m:
        return float(m.group(1)) * 1000, None, None
    m = re.search(r"(\d+(?:\.\d+)?) ?(?:hz|hertz)\b", clause)
    if m:
        return float(m.group(1)), None, None
    m = re.search(r"(\d+(?:\.\d+)?) (?:[a-z]+ )?(?:per|each) second\b", clause)
    if m:
        return float(m.group(1)), None, None
    m = re.search(r"every (\d+(?:\.\d+)?) ?(ms|milliseconds?|s|secs?|seconds?)\b", clause)
    if m:
        period = float(m.group(1)) / (1000.0 if m.group(2).startswith("m") else 1.0)
        if period > 0:
            return 1.0 / period, None, f"'every {m.group(1)} {m.group(2)}' read as {1.0 / period:g} steps per second"
    m = re.search(r"(\d+(?:\.\d+)?) ?(?:x|times) (?:faster|quicker|as fast|the speed|speed|more quickly)", clause)
    if m:
        return None, float(m.group(1)), None
    m = re.search(r"(\d+(?:\.\d+)?) ?x speed\b", clause)
    if m:
        return None, float(m.group(1)), None
    m = re.search(r"(\d+(?:\.\d+)?) times slower\b", clause)
    if m and float(m.group(1)) > 0:
        return None, 1.0 / float(m.group(1)), None
    if re.search(r"\bslow(?:ly)?\b", clause) and not re.search(r"\bslower\b", clause):
        return 1.0, None, "'slowly' interpreted as 1 step per second"
    return None, None, None


@dataclass
class _Ref:
    port: str          # btnC.. or "sw"
    index: int | None  # switch number
    level: int         # active level named in the clause

    @property
    def signal(self) -> str:
        return f"sw[{self.index}]" if self.port == "sw" else self.port

    @property
    def is_button(self) -> bool:
        return self.port != "sw"


def _refs(clause: str) -> list[_Ref]:
    refs: list[_Ref] = []
    for port in _buttons(clause):
        level = 0 if _RELEASE_RE.search(clause) else 1
        refs.append(_Ref(port, None, level))
    for idx in _switches(clause):
        level = 1
        m = re.search(rf"\b(?:sw|switch) ?{idx}\b (?:is |was |set |turned |flipped |goes )?(up|on|high|1|down|off|low|0)\b",
                      clause)
        if m:
            level = 1 if m.group(1) in ("up", "on", "high", "1") else 0
        elif re.search(r"\b(?:off|down|low)\b", _strip_refs(clause)) and not re.search(r"\b(?:on|up|high)\b",
                                                                                      _strip_refs(clause)):
            level = 0
        refs.append(_Ref("sw", idx, level))
    return refs


# =============================================================================== unsupported / question

_UNSUPPORTED = [
    (r"\bvga\b|\bhdmi\b|\bvideo\b|\bpixels?\b", "Video output (VGA/HDMI) is not supported; CircuitLoop designs drive "
     "only the 16 on-board LEDs."),
    (r"\buart\b|\bserial (?:port|link|interface|output|communication)|\brs-?232\b|\bbaud\b|\busb (?:serial|keyboard|"
     r"mouse|device|communication)", "UART/serial/USB communication is not supported."),
    (r"seven[- ]?segment|\b7[- ]?seg(?:ment)?\b|\bsseg\b|\bhex display\b|\bdigit display\b",
     "The seven-segment display is not part of the current specification format; only the 16 LEDs can be driven."),
    (r"\bpmod\b|\bheader pins?\b|\bja\b|\bjb\b|\bjc\b|\bjxadc\b", "PMOD/expansion headers are not supported."),
    (r"\baudio\b|\bsound\b|\bspeaker\b|\bbuzzer\b|\bbeep|\bmusic\b|\bmelod", "Audio output is not supported."),
    (r"\bpwm\b|\bbrightness\b|\bdim(?:ming|mer)?\b|\bfade", "LED brightness control (PWM) is not one of the four "
     "supported families."),
    (r"\bsensor|\bmotor|\bservo|\bkeyboard|\bps/?2\b|\bmouse\b|\blcd\b|\boled\b|\bbreadboard|\bethernet|\bwi-?fi\b"
     r"|\bbluetooth|\bexternal (?:led|button|switch|part|component|circuit|device|board)",
     "External parts and interfaces are not supported; only the on-board buttons, switches and LEDs are."),
    (r"\b(?:cpu|processor|risc-?v|microcontroller|alu|fft|fifo|ram|memory|calculator|adder)\b",
     "General-purpose logic (processors, memories, arithmetic units) is outside the four supported families: "
     "counter, blinker, LED pattern and traffic light."),
]


def _unsupported_reasons(t: str) -> list[str]:
    reasons = [msg for pat, msg in _UNSUPPORTED if re.search(pat, t)]
    for m in re.finditer(r"(\d+(?:\.\d+)?) ?(mhz|ghz)\b", t):
        mhz = float(m.group(1)) * (1000 if m.group(2) == "ghz" else 1)
        if mhz > 100:
            reasons.append(f"A {m.group(1)} {m.group(2).upper().replace('HZ', 'Hz')} clock is not possible: designs run "
                           "from the Basys 3's single 100 MHz oscillator and clocks above 100 MHz are not supported.")
        elif mhz != 100:
            reasons.append("Other clock frequencies are not supported; every design runs from the 100 MHz board clock "
                           "(slower behaviour is made with clock enables).")
    for m in re.finditer(r"\b(\d+) leds\b", t):
        if int(m.group(1)) > 16:
            reasons.append(f"The Basys 3 has 16 LEDs (LED0-LED15); {m.group(1)} LEDs are not available.")
    for m in re.finditer(r"\b(?:led|ld) ?(\d+)\b", t):
        if int(m.group(1)) > 15:
            reasons.append(f"LED{m.group(1)} does not exist; the Basys 3 LEDs are LED0-LED15.")
    for m in _WIDTH_RE.finditer(t):
        if int(m.group(1)) > 16:
            reasons.append(f"A {m.group(1)}-bit value needs more than the 16 available LEDs.")
    for idx in _switches(t):
        if idx > 15:
            reasons.append(f"SW{idx} does not exist; the Basys 3 switches are SW0-SW15.")
    return list(dict.fromkeys(reasons))


_FAMILY_PATTERNS = [
    ("traffic_light", None, r"traffic[- ]?lights?|stop ?lights?|traffic signal|crosswalk light"),
    ("led_pattern", "bounce", r"knight ?rider|\bbounc|back[- ]and[- ]forth|ping[- ]?pong|\bcylon\b|\blarson\b|\bscanner\b"),
    ("led_pattern", "chase", r"\bchas(?:e|er|es|ing)\b|running lights?|\bmarquee\b|\brotat(?:e|es|ing) (?:a |the )?(?:single )?(?:lit )?led"),
    ("led_pattern", "fill", r"bar[- ]?graph|\bfill(?:s|ing)?\b|progress bar"),
    ("counter", None, r"\bcount(?:er|s|ing)?\b|\bincrement|\bdecrement"),
    ("led_pattern", "mirror", r"(?:follow|mirror|reflect|show|display|copy|track|match|indicate)\w* (?:the )?(?:state of |position of |positions of )?(?:the )?(?:switch|sw ?\d)"
     r"|(?:switch|switches|sw ?\d+)\b[^.]*\b(?:control|drive|turn|light|toggle)s?\b[^.]*\bleds?\b"
     r"|leds? (?:\d+ ?(?:-|to) ?\d+ )?(?:follow|mirror|copy|track|match|show)"),
    ("blinker", None, r"\bblink|\bflash|\btoggl"),
]


def _detect_family(t: str) -> tuple[str, str | None] | None:
    for family, variant, pat in _FAMILY_PATTERNS:
        if re.search(pat, t):
            return family, variant
    return None


_QUESTION_START = re.compile(r"^(?:what|why|how|which|when|where|who|does|do|is|are|can|could|would|will|should|"
                             r"explain|tell me|describe)\b")
_DESIGN_VERBS = re.compile(r"\b(?:build|make|create|design|implement|generate|change|modify|add|use|set|turn|"
                           r"switch to|swap|reverse|replace|remove|let|program)\b")
_MODIFY_CUES = re.compile(r"\b(?:change|modify|instead|now|make it|make the|also|add|remove|use|switch to|so (?:that )?"
                          r"it|keep|reverse|backwards?|faster|slower|speed (?:it )?up|slow (?:it )?down|swap|rather|"
                          r"set the|let it|count(?:s|ing)? (?:down|up)|decrement|increment)\b")
_NEW_CUES = re.compile(r"\b(?:new|another|from scratch|start over|instead of the)\b")


# =============================================================================== describe

def describe_spec(spec: DesignSpec) -> str:
    """Plain-language one-paragraph description derived only from the spec."""
    b = spec.behavior
    out = spec.output(b.output)
    leds = _led_span(out.indices)

    def cond(m) -> str:
        if not m.when_input:
            return ""
        i = spec.input(m.when_input)
        if i.port == "sw":
            return f" while {i.board_signal.upper()} is {'up' if m.when_level else 'down'}"
        return f" while {i.port.upper()} is {'held' if m.when_level else 'released'}"

    def modes_text(unit: str) -> str:
        parts = []
        for m in b.rate_modes:
            if m.when_input:
                parts.append(f"{m.hz:g} {unit}{cond(m)}")
            elif len(b.rate_modes) > 1:
                parts.append(f"{m.hz:g} {unit} otherwise")
            else:
                parts.append(f"{m.hz:g} {unit}")
        return "; ".join(parts)

    def name_of(inp: str) -> str:
        i = spec.input(inp)
        return i.board_signal.upper() if i.port == "sw" else i.port.upper()

    s: list[str] = []
    if isinstance(b, CounterBehavior):
        s.append(f"{out.width}-bit binary {'down' if b.direction == 'down' else 'up'}-counter on {leds} "
                 f"(LSB on LED{out.indices[0]}), starting at {b.initial_value}.")
        s.append(f"Steps: {modes_text('per second')}.")
        if b.direction_control:
            s.append(f"{name_of(b.direction_control.input)} high makes it count {b.direction_control.high_means}.")
        if b.reset_input:
            s.append(f"Holding {name_of(b.reset_input)} resets it to {b.reset_value}.")
        if b.enable_input:
            s.append(f"It pauses while {name_of(b.enable_input)} is low.")
        s.append("It wraps around at the end of its range." if b.wrap else "It saturates at the end of its range.")
    elif isinstance(b, BlinkerBehavior):
        s.append(f"{leds} blink {'together' if b.pattern == 'together' else 'alternately'} with 50% duty at "
                 f"{modes_text('Hz')}.")
        if b.enable_input:
            s.append(f"The LEDs stay off while {name_of(b.enable_input)} is low.")
    elif isinstance(b, LedPatternBehavior):
        if b.pattern in ("mirror_inputs", "invert_inputs"):
            srcs = ", ".join(name_of(x) for x in b.source_inputs)
            s.append(f"{leds} {'follow' if b.pattern == 'mirror_inputs' else 'show the inverse of'} {srcs}.")
        else:
            what = {"chase": "A single lit LED chases", "bounce": "A single lit LED bounces back and forth",
                    "fill": "A bar graph fills"}[b.pattern]
            s.append(f"{what} along {leds}" + ("" if b.pattern == "bounce" else
                     f" towards {'higher' if b.direction == 'left' else 'lower'} LED numbers") +
                     f" at {modes_text('steps per second')}.")
            if b.direction_control:
                s.append(f"{name_of(b.direction_control.input)} high sets the direction "
                         f"{'towards higher LEDs' if b.direction_control.high_means == 'up' else 'towards LED0'}.")
        if b.reset_input:
            s.append(f"{name_of(b.reset_input)} restarts the pattern.")
        if b.enable_input:
            s.append(f"The pattern holds while {name_of(b.enable_input)} is low.")
    elif isinstance(b, TrafficLightBehavior):
        seq = ", ".join(f"{st.name} {st.duration_s:g} s" for st in b.states)
        s.append(f"Traffic light on {leds}: {seq}, repeating.")
        if b.request_input and b.request_state:
            s.append(f"A press of {name_of(b.request_input)} inserts '{b.request_state.name}' "
                     f"({b.request_state.duration_s:g} s) after '{b.request_after}'.")
        if b.reset_input:
            s.append(f"{name_of(b.reset_input)} restarts at {b.states[0].name}.")
    return " ".join(s)


def _led_span(indices: list[int]) -> str:
    if len(indices) == 1:
        return f"LED{indices[0]}"
    if indices == list(range(indices[0], indices[0] + len(indices))):
        return f"LED{indices[0]}-LED{indices[-1]}"
    return ", ".join(f"LED{i}" for i in indices)


# =============================================================================== builders


class _Inputs:
    """Accumulates InputBinding dicts, refusing to bind one board input to two roles."""

    def __init__(self, existing: list[dict] | None = None) -> None:
        self.items: list[dict] = copy.deepcopy(existing or [])

    def by_signal(self, ref: _Ref) -> dict | None:
        for i in self.items:
            if i["port"] == ref.port and (ref.port != "sw" or i.get("index") == ref.index):
                return i
        return None

    def add(self, ref: _Ref, base_name: str, description: str) -> str:
        found = self.by_signal(ref)
        if found is not None:
            return found["name"]
        names = {i["name"] for i in self.items}
        name, n = base_name, 1
        while name in names:
            n += 1
            name = f"{base_name}{n}"
        item: dict[str, Any] = {"name": name, "port": ref.port, "debounce_ms": DEFAULT_DEBOUNCE_MS,
                                "description": description}
        if ref.port == "sw":
            item["index"] = ref.index
        self.items.append(item)
        return name


def _role_name(ref: _Ref, role: str) -> str:
    return f"{role}_{'btn' if ref.is_button else 'sw'}"


def _signal_text(ref: _Ref) -> str:
    return ref.signal.upper() if ref.is_button else ref.signal


@dataclass
class _Parsed:
    default_hz: float | None = None
    cond_modes: list[tuple[_Ref, float | None, float | None]] = field(default_factory=list)  # (ref, hz, mult)
    reset: _Ref | None = None
    enable: _Ref | None = None
    direction_ref: tuple[_Ref, str] | None = None  # (ref, meaning when active: 'down'/'reverse'/...)
    request: _Ref | None = None
    reset_value: int | None = None
    assumptions: list[str] = field(default_factory=list)
    global_multiplier: float | None = None


def _parse_clauses(t: str, family: str) -> _Parsed:
    """Assign rates and input roles clause by clause."""
    p = _Parsed()
    pending: _Ref | None = None
    for clause in _clauses(t):
        refs = _refs(clause)
        hz, mult, note = _rate(clause)
        plain = _strip_refs(clause)
        role_reset = bool(_RESET_RE.search(plain))
        role_request = family == "traffic_light" and bool(_REQUEST_RE.search(plain))
        role_dir = bool(_DIR_RE.search(plain))
        role_enable = bool(_ENABLE_RE.search(plain))
        m = re.search(r"reset(?:s)? (?:it |the \w+ )?(?:to|at) (\d+)", plain)
        if m:
            p.reset_value = int(m.group(1))
        if not refs:
            if (hz is not None or mult is not None) and pending is not None:
                refs = [pending]
                pending = None
            else:
                if hz is not None:
                    if p.default_hz is None or re.search(r"\bnormal|\bdefault|\botherwise|\busual", clause):
                        p.default_hz = hz
                    if note:
                        p.assumptions.append(note)
                elif mult is not None:
                    p.global_multiplier = mult
                continue
        for ref in refs:
            if role_reset:
                if p.reset is not None and p.reset.signal != ref.signal:
                    raise _Clarify("two different reset inputs were mentioned")
                p.reset = ref
            elif role_request:
                p.request = ref
            elif role_dir and family in ("counter", "led_pattern"):
                p.direction_ref = (ref, "reverse")
            elif role_enable and family != "traffic_light":
                pause = bool(_PAUSE_RE.search(plain))
                active_runs = (ref.level == 0) if pause else (ref.level == 1)
                if not active_runs:
                    raise _Clarify(
                        f"{_signal_text(ref)} would have to stop the design while it is "
                        f"{'pressed' if ref.is_button else 'up'}; enable inputs in CircuitLoop run the design while "
                        "they are high. Could you use a switch that is up to run instead?")
                p.enable = ref
            elif hz is not None or mult is not None:
                p.cond_modes.append((ref, hz, mult))
                if note:
                    p.assumptions.append(note)
            elif _HOLD_RE.search(clause) or _SPEED_RE.search(plain) or re.search(r"\b(?:when|while|if)\b", clause):
                pending = ref
    return p


def _rate_modes(p: _Parsed, inputs: _Inputs, default_hz: float, unit: str) -> list[dict]:
    modes: list[dict] = []
    used_names: set[str] = set()
    for ref, hz, mult in p.cond_modes:
        value = hz if hz is not None else default_hz * float(mult or 1)
        if value <= 0 or value > 1000:
            raise _Unsupported([f"{value:g} {unit} is outside the supported range (0-1000 per second)."])
        name = "fast" if value >= default_hz else "slow"
        n = 1
        while name in used_names or name == "normal":
            n += 1
            name = f"{'fast' if value >= default_hz else 'slow'}{n}"
        used_names.add(name)
        inp = inputs.add(ref, _role_name(ref, "speed"),
                         f"{'Hold' if ref.is_button else 'Set'} for {value:g} {unit}")
        modes.append({"name": name, "hz": _num(str(round(value, 6))), "when_input": inp, "when_level": ref.level})
    modes.append({"name": "normal", "hz": _num(str(round(default_hz, 6)))})
    return modes


def _default_rate(p: _Parsed, fallback: float, unit: str, assumptions: list[str]) -> float:
    hz = p.default_hz
    if hz is None:
        hz = fallback
        assumptions.append(f"no rate given; using {fallback:g} {unit}")
    if p.global_multiplier:
        assumptions.append(f"'{p.global_multiplier:g}x' without a button or switch condition was ignored")
    if hz <= 0 or hz > 1000:
        raise _Unsupported([f"{hz:g} {unit} is outside the supported range (up to 1000 per second)."])
    return hz


def _apply_common_inputs(p: _Parsed, inputs: _Inputs, behavior: dict, family: str) -> None:
    if p.reset is not None:
        behavior["reset_input"] = inputs.add(p.reset, _role_name(p.reset, "reset"),
                                             "Hold to reset" if p.reset.is_button else "Up to reset")
    if p.enable is not None and family != "traffic_light":
        behavior["enable_input"] = inputs.add(p.enable, _role_name(p.enable, "enable"),
                                              "Runs while high")


def _check_roles(inputs: _Inputs, behavior: dict) -> None:
    roles: dict[str, list[str]] = {}
    for f in ("reset_input", "enable_input", "request_input"):
        if behavior.get(f):
            roles.setdefault(behavior[f], []).append(f)
    if behavior.get("direction_control"):
        roles.setdefault(behavior["direction_control"]["input"], []).append("direction")
    for m in behavior.get("rate_modes", []):
        if m.get("when_input"):
            roles.setdefault(m["when_input"], []).append("speed")
    for name, r in roles.items():
        if len(r) > 1:
            raise _Clarify(f"the same board input ('{name}') was asked to do several things ({', '.join(r)})")


def _indices_for(t: str, width_default: int, assumptions: list[str], what: str) -> list[int]:
    leds = _leds(t)
    wm = _WIDTH_RE.search(t)
    width = int(wm.group(1)) if wm else None
    if leds is not None and width is not None and len(leds) != width:
        raise _Clarify(f"a {width}-bit {what} needs {width} LEDs, but {len(leds)} LEDs were named ({_led_span(leds)})")
    if leds is None:
        n = width or width_default
        leds = list(range(n))
        assumptions.append(f"shown on {_led_span(leds)}" + (" (LSB on LED0)" if what == "counter" else ""))
    if any(i > 15 for i in leds) or len(leds) > 16:
        raise _Unsupported(["The Basys 3 has 16 LEDs (LED0-LED15)."])
    return leds


def _build_counter(t: str, p: _Parsed) -> dict:
    assumptions: list[str] = []
    leds = _indices_for(t, 4, assumptions, "counter")
    w = len(leds)
    inputs = _Inputs()
    default_hz = _default_rate(p, 1.0, "steps per second", assumptions)
    behavior: dict[str, Any] = {"family": "counter", "output": "count"}
    plain = _strip_refs(t)
    down = bool(re.search(r"\bcount(?:s|ing)? (?:down|backwards?)\b|\bdecrement|\bbackwards?\b|\bdown[- ]?counter|"
                          r"\bcount(?:s|ing)? in reverse\b", plain))
    if p.direction_ref is not None:
        ref, _ = p.direction_ref
        inp = inputs.add(ref, _role_name(ref, "dir"), "Selects the count direction")
        # "count down while SW0 is up": the input active level selects the opposite of the base direction.
        behavior["direction_control"] = {"input": inp, "high_means": "down" if ref.level == 1 else "up"}
        behavior["direction"] = "up"
        assumptions.append(f"{_signal_text(ref)} {'high' if ref.level == 1 else 'low'} counts down, otherwise up")
    else:
        behavior["direction"] = "down" if down else "up"
    behavior["rate_modes"] = _rate_modes(p, inputs, default_hz, "steps per second")
    _apply_common_inputs(p, inputs, behavior, "counter")
    if p.reset is not None:
        behavior["reset_value"] = p.reset_value or 0
    m = re.search(r"\bstart(?:s|ing)? (?:at|from) (\d+)", t)
    behavior["initial_value"] = int(m.group(1)) if m else 0
    if not m:
        assumptions.append("the counter starts at 0 after configuration")
    behavior["wrap"] = not re.search(r"saturat|stop(?:s)? at (?:the )?(?:max|top|end|\d+)|(?:don't|do not|never) wrap|"
                                     r"no wrap", t)
    _check_roles(inputs, behavior)
    if inputs.items:
        assumptions.append("buttons and switches are debounced for 10 ms")
    modes = behavior["rate_modes"]
    title = (f"{w}-bit {'variable-speed ' if len(modes) > 1 else ''}"
             f"{'down-' if behavior['direction'] == 'down' and 'direction_control' not in behavior else ''}counter"
             f"{' with reset' if behavior.get('reset_input') else ''}")
    return {"title": title, "inputs": inputs.items,
            "outputs": [{"name": "count", "port": "led", "indices": leds, "description":
                         f"Counter value, LSB on LED{leds[0]}"}],
            "behavior": behavior, "assumptions": assumptions}


def _build_blinker(t: str, p: _Parsed) -> dict:
    assumptions: list[str] = []
    leds = _leds(t)
    if leds is None:
        leds = [0]
        assumptions.append("blinking LED0")
    if any(i > 15 for i in leds):
        raise _Unsupported(["The Basys 3 has 16 LEDs (LED0-LED15)."])
    inputs = _Inputs()
    default_hz = _default_rate(p, 1.0, "Hz", assumptions)
    alternate = bool(re.search(r"\balternat|\bopposite|\bin turn\b|\bwig-?wag", t)) and len(leds) > 1
    behavior: dict[str, Any] = {"family": "blinker", "output": "leds",
                                "pattern": "alternate" if alternate else "together"}
    behavior["rate_modes"] = _rate_modes(p, inputs, default_hz, "Hz")
    if p.reset is not None:
        raise _Clarify("a blinker has no reset input in CircuitLoop; did you mean an enable switch?")
    _apply_common_inputs(p, inputs, behavior, "blinker")
    _check_roles(inputs, behavior)
    if inputs.items:
        assumptions.append("buttons and switches are debounced for 10 ms")
    assumptions.append("the LEDs start off after configuration; 50% duty cycle")
    n = len(leds)
    title = (f"{'LED%d' % leds[0] if n == 1 else f'{n}-LED'} blinker"
             + (" with fast mode" if len(behavior["rate_modes"]) > 1 else ""))
    return {"title": title, "inputs": inputs.items,
            "outputs": [{"name": "leds", "port": "led", "indices": leds,
                         "description": "Blink together" if not alternate else "Even and odd LEDs alternate"}],
            "behavior": behavior, "assumptions": assumptions}


def _build_mirror(t: str, p: _Parsed) -> dict:
    assumptions: list[str] = []
    switches = _switches(t)
    leds = _leds(_SW_RANGE_RE.sub(" ", _SW_RE.sub(" ", t)))
    if not switches and leds is None:
        raise _Clarify("which switches should drive which LEDs?")
    if not switches:
        switches = list(leds or [])
        assumptions.append("LEDi follows SWi")
    if leds is None:
        leds = list(switches)
        assumptions.append(f"switch SWi drives LEDi ({_led_span(leds)})")
    if len(leds) != len(switches):
        raise _Clarify(f"{len(switches)} switches were named but {len(leds)} LEDs; each LED needs exactly one switch")
    if any(i > 15 for i in leds + switches):
        raise _Unsupported(["The Basys 3 has 16 LEDs and 16 switches (0-15)."])
    invert = bool(re.search(r"\binvert|\binverse|\bopposite|\bnegat|\bcomplement|\bnot\b", t))
    inputs = _Inputs()
    sources = [inputs.add(_Ref("sw", i, 1), f"s{i}", "") for i in switches]
    behavior: dict[str, Any] = {"family": "led_pattern", "output": "leds",
                                "pattern": "invert_inputs" if invert else "mirror_inputs", "source_inputs": sources}
    _apply_common_inputs(p, inputs, behavior, "led_pattern")
    assumptions.append("switches are debounced for 10 ms, so LEDs follow within about 10 ms")
    return {"title": f"Switches {'inverted' if invert else 'mirrored'} on LEDs", "inputs": inputs.items,
            "outputs": [{"name": "leds", "port": "led", "indices": leds,
                         "description": f"LEDi {'shows the inverse of' if invert else 'follows'} its switch"}],
            "behavior": behavior, "assumptions": assumptions}


def _build_moving(t: str, p: _Parsed, pattern: str) -> dict:
    assumptions: list[str] = []
    leds = _leds(t)
    if leds is None:
        leds = list(range(8))
        assumptions.append("using LED0-LED7")
    if any(i > 15 for i in leds):
        raise _Unsupported(["The Basys 3 has 16 LEDs (LED0-LED15)."])
    if len(leds) < 2 and pattern in ("chase", "bounce"):
        raise _Clarify(f"a {pattern} pattern needs at least 2 LEDs")
    inputs = _Inputs()
    default_hz = _default_rate(p, DEFAULT_PATTERN_HZ, "steps per second", assumptions)
    plain = _strip_refs(t)
    right = bool(re.search(r"\bright(?:wards?)?\b|towards? led ?0|\bdownwards?\b|\bdescending\b", plain))
    behavior: dict[str, Any] = {"family": "led_pattern", "output": "leds", "pattern": pattern,
                                "direction": "right" if right and pattern != "bounce" else "left"}
    if pattern != "bounce":
        assumptions.append("'left' means towards higher LED numbers" if not right else
                           "moving towards LED0 (direction 'right')")
    if p.direction_ref is not None and pattern != "bounce":
        ref, _ = p.direction_ref
        inp = inputs.add(ref, _role_name(ref, "dir"), "Reverses the direction")
        base_left = behavior["direction"] == "left"
        reverse = "down" if base_left else "up"
        behavior["direction_control"] = {"input": inp, "high_means": reverse}
        assumptions.append(f"{_signal_text(ref)} high reverses the direction")
    behavior["rate_modes"] = _rate_modes(p, inputs, default_hz, "steps per second")
    _apply_common_inputs(p, inputs, behavior, "led_pattern")
    _check_roles(inputs, behavior)
    if inputs.items:
        assumptions.append("buttons and switches are debounced for 10 ms")
    n = len(leds)
    title = {"chase": f"{n}-LED chase", "bounce": f"{n}-LED Knight Rider bounce", "fill": f"{n}-LED bar-graph fill"}[pattern]
    return {"title": title, "inputs": inputs.items,
            "outputs": [{"name": "leds", "port": "led", "indices": leds, "description": f"{pattern} pattern"}],
            "behavior": behavior, "assumptions": assumptions}


_COLOR_ALIASES = {"amber": "yellow", "orange": "yellow"}


def _build_traffic(t: str, p: _Parsed) -> dict:
    assumptions: list[str] = []
    color_led: dict[str, int] = {}
    for m in re.finditer(r"\b(red|yellow|amber|orange|green)(?: light)? (?:on |at |is |= ?)?(?:led|ld) ?(\d+)\b", t):
        color_led[_COLOR_ALIASES.get(m.group(1), m.group(1))] = int(m.group(2))
    for m in re.finditer(r"\b(?:led|ld) ?(\d+) (?:is |for |as |= ?|shows )?(?:the )?(red|yellow|amber|orange|green)\b", t):
        color_led[_COLOR_ALIASES.get(m.group(2), m.group(2))] = int(m.group(1))
    leds = _leds(t)
    if color_led and (leds is None or len(leds) < 3):
        leds = sorted(set(color_led.values()) | set(leds or []))
    if leds is None:
        leds = [0, 1, 2]
        assumptions.append("lights on LED0-LED2")
    if len(leds) < 3:
        raise _Clarify("a traffic light needs three LEDs (green, yellow and red)")
    if any(i > 15 for i in leds):
        raise _Unsupported(["The Basys 3 has 16 LEDs (LED0-LED15)."])
    if not color_led:
        color_led = {"green": leds[0], "yellow": leds[1], "red": leds[2]}
        assumptions.append(f"LED{leds[0]} green, LED{leds[1]} yellow, LED{leds[2]} red")
    if set(color_led) != {"green", "yellow", "red"} or any(v not in leds for v in color_led.values()):
        raise _Clarify("please name the LED for each of green, yellow and red")
    durations = dict(DEFAULT_TRAFFIC)
    given = set()
    for m in re.finditer(r"\b(red|yellow|amber|orange|green|walk)(?: light| phase| state)?(?: for| lasts?| lasting| of| "
                         r"stays? on for| on for| should last|:|=)? ?(\d+(?:\.\d+)?) ?(?:seconds?|secs?|s)\b", t):
        durations[_COLOR_ALIASES.get(m.group(1), m.group(1))] = float(m.group(2))
        given.add(_COLOR_ALIASES.get(m.group(1), m.group(1)))
    for m in re.finditer(r"\b(\d+(?:\.\d+)?) ?(?:seconds?|secs?|s) (?:of |on |for )?(?:the )?(red|yellow|amber|orange|green|walk)\b", t):
        durations[_COLOR_ALIASES.get(m.group(2), m.group(2))] = float(m.group(1))
        given.add(_COLOR_ALIASES.get(m.group(2), m.group(2)))
    missing = [c for c, _ in DEFAULT_TRAFFIC if c not in given]
    if missing:
        assumptions.append("durations " + ", ".join(f"{c} {durations[c]:g} s" for c in missing)
                           + (" (not specified)" if given else " (defaults)"))
    for c, d in durations.items():
        if not 0 < d <= 20:
            raise _Unsupported([f"state durations must be between 0 and 20 s ({c}: {d:g} s)"])
    bit = {c: leds.index(i) for c, i in color_led.items()}
    states = [{"name": c, "duration_s": _num(str(durations[c])), "lights": 1 << bit[c]} for c in ("green", "yellow", "red")]
    inputs = _Inputs()
    behavior: dict[str, Any] = {"family": "traffic_light", "output": "lights", "states": states}
    if p.request is not None:
        walk_led = next((i for i in leds if i not in color_led.values()), None)
        if walk_led is None:
            nxt = max(leds) + 1
            if nxt > 15:
                raise _Clarify("there is no free LED for the walk light")
            leds = leds + [nxt]
            walk_led = nxt
            assumptions.append(f"the walk light is LED{nxt}")
        walk_bit = leds.index(walk_led)
        behavior["request_input"] = inputs.add(p.request, "walk_btn" if p.request.is_button else "walk_sw",
                                               "Pedestrian crossing request")
        behavior["request_after"] = "red"
        behavior["request_state"] = {"name": "walk", "duration_s": _num(str(durations.get("walk", DEFAULT_WALK_S))),
                                     "lights": (1 << bit["red"]) | (1 << walk_bit)}
        assumptions.append("a request is remembered until served; the walk phase follows red and keeps the car light red")
    if p.reset is not None:
        behavior["reset_input"] = inputs.add(p.reset, _role_name(p.reset, "reset"), "Restart at green")
    _check_roles(inputs, behavior)
    if inputs.items:
        assumptions.append("buttons are debounced for 10 ms")
    assumptions.append("the light starts green after configuration")
    names = {v: k for k, v in color_led.items()}
    desc = ", ".join(f"bit{k} {names.get(i, 'walk')}" for k, i in enumerate(leds))
    return {"title": "Traffic light" + (" with pedestrian request" if p.request else ""), "inputs": inputs.items,
            "outputs": [{"name": "lights", "port": "led", "indices": leds, "description": desc}],
            "behavior": behavior, "assumptions": assumptions}


def _finish(data: dict) -> DesignSpec:
    data = dict(data)
    assumptions = list(dict.fromkeys(data.pop("assumptions", [])))
    spec = DesignSpec.model_validate({**data, "summary": "", "assumptions": assumptions})
    return spec.model_copy(update={"summary": describe_spec(spec)})


# =============================================================================== modification


def _modify(t: str, current: DesignSpec) -> tuple[DesignSpec, list[str]]:
    data = current.model_dump()
    b = data["behavior"]
    family = b["family"]
    p = _parse_clauses(t, family)
    inputs = _Inputs(data["inputs"])
    assumptions: list[str] = []
    changed = False
    plain = _strip_refs(t)

    # direction
    if family == "counter":
        if re.search(r"\bcount(?:s|ing)? (?:down|backwards?)\b|\bbackwards?\b|\bdecrement|\bin reverse\b|"
                     r"\breverse (?:the )?(?:count|direction)", plain) and p.direction_ref is None:
            b["direction"] = "down"
            changed = True
        elif re.search(r"\bcount(?:s|ing)? (?:up|forwards?)\b|\bincrement", plain) and p.direction_ref is None:
            b["direction"] = "up"
            changed = True
    elif family == "led_pattern" and b["pattern"] in ("chase", "fill") and p.direction_ref is None:
        if re.search(r"\breverse|\bother direction|\bopposite direction|\bbackwards?\b", plain):
            b["direction"] = "right" if b["direction"] == "left" else "left"
            changed = True
        elif re.search(r"\bright(?:wards?)?\b|towards? led ?0", plain):
            b["direction"], changed = "right", True
        elif re.search(r"\bleft(?:wards?)?\b|towards? (?:the )?higher", plain):
            b["direction"], changed = "left", True

    if p.direction_ref is not None and family in ("counter", "led_pattern"):
        ref, _ = p.direction_ref
        inp = inputs.add(ref, _role_name(ref, "dir"), "Selects the direction")
        if family == "counter":
            b["direction_control"] = {"input": inp, "high_means": "down" if ref.level == 1 else "up"}
            b["direction"] = "up"
        else:
            b["direction_control"] = {"input": inp, "high_means": "down" if b["direction"] == "left" else "up"}
        changed = True

    # reset / enable / request
    if p.reset is not None:
        if family == "blinker":
            raise _Clarify("a blinker has no reset input in CircuitLoop")
        existing = inputs.by_signal(p.reset)
        b["reset_input"] = inputs.add(p.reset, _role_name(p.reset, "reset"),
                                      "Hold to reset the count to 0" if family == "counter" else "Hold to restart")
        if existing is None:
            assumptions.append(f"{_signal_text(p.reset)} is debounced for 10 ms")
        if family == "counter":
            b["reset_value"] = p.reset_value if p.reset_value is not None else 0
            assumptions.append(f"while {_signal_text(p.reset)} is held the count stays at {b['reset_value']}; "
                               "counting resumes one full period after release")
        changed = True
    if p.enable is not None and family != "traffic_light":
        b["enable_input"] = inputs.add(p.enable, _role_name(p.enable, "enable"), "Runs while high")
        changed = True
    if p.request is not None and family == "traffic_light" and not b.get("request_input"):
        raise _Clarify("adding a pedestrian request to an existing traffic light is not supported by the offline "
                       "rules; describe the complete traffic light instead")

    # rates
    modes = b.get("rate_modes") or []
    if modes and (p.default_hz is not None or p.cond_modes or p.global_multiplier):
        default = modes[-1]
        if p.default_hz is not None:
            default["hz"] = _num(str(p.default_hz))
            changed = True
        for ref, hz, mult in p.cond_modes:
            inp = inputs.add(ref, _role_name(ref, "speed"), "Speed control")
            value = hz if hz is not None else float(default["hz"]) * float(mult or 1)
            existing_mode = next((m for m in modes[:-1] if m.get("when_input") == inp), None)
            if existing_mode is not None:
                existing_mode["hz"] = _num(str(round(value, 6)))
                existing_mode["when_level"] = ref.level
            else:
                names = {m["name"] for m in modes}
                name = "fast" if value >= float(default["hz"]) else "slow"
                n = 1
                while name in names:
                    n += 1
                    name = f"{'fast' if value >= float(default['hz']) else 'slow'}{n}"
                modes.insert(len(modes) - 1, {"name": name, "hz": _num(str(round(value, 6))),
                                              "when_input": inp, "when_level": ref.level})
            changed = True
        if p.global_multiplier and not p.cond_modes and p.default_hz is None:
            for m in modes:
                m["hz"] = _num(str(round(float(m["hz"]) * p.global_multiplier, 6)))
            changed = True
    elif modes and re.search(r"\bfaster\b|\bspeed (?:it )?up\b|\bslower\b|\bslow (?:it )?down\b", plain) \
            and not p.cond_modes:
        factor = 2.0 if re.search(r"\bfaster\b|\bspeed (?:it )?up\b", plain) else 0.5
        for m in modes:
            m["hz"] = _num(str(round(float(m["hz"]) * factor, 6)))
        assumptions.append(f"'{'faster' if factor > 1 else 'slower'}' without a number read as "
                           f"{'twice' if factor > 1 else 'half'} the speed")
        changed = True

    # blinker pattern
    if family == "blinker":
        if re.search(r"\balternat|\bopposite", plain):
            b["pattern"], changed = "alternate", True
        elif re.search(r"\btogether|\bin sync|\bsame time", plain):
            b["pattern"], changed = "together", True

    # traffic durations
    if family == "traffic_light":
        names = {s["name"] for s in b["states"]} | ({b["request_state"]["name"]} if b.get("request_state") else set())
        for m in re.finditer(r"\b([a-z][a-z0-9_]*)(?: light| phase| state)?(?: for| lasts?| last| to| of| should last|"
                             r" stays? on for| on for|:|=)? ?(\d+(?:\.\d+)?) ?(?:seconds?|secs?|s)\b", t):
            name = _COLOR_ALIASES.get(m.group(1), m.group(1))
            if name in names:
                for s in b["states"] + ([b["request_state"]] if b.get("request_state") else []):
                    if s["name"] == name:
                        s["duration_s"] = _num(m.group(2))
                        changed = True

    # counter width / LEDs
    leds = _leds(t)
    wm = _WIDTH_RE.search(t)
    if family in ("counter", "blinker") or (family == "led_pattern" and b["pattern"] in ("chase", "bounce", "fill")):
        out = next(o for o in data["outputs"] if o["name"] == b["output"])
        new_leds = None
        if leds is not None and re.search(r"\b(?:use|on|move|shift|instead|to)\b", t):
            new_leds = leds
        elif wm is not None and family == "counter":
            w = int(wm.group(1))
            if w > 16:
                raise _Unsupported(["The Basys 3 has 16 LEDs (LED0-LED15)."])
            start = out["indices"][0]
            new_leds = list(range(start, start + w)) if start + w <= 16 else list(range(w))
        if new_leds is not None and new_leds != out["indices"]:
            out["indices"] = new_leds
            changed = True

    if family == "counter" and b.get("reset_input") and b.get("reset_value", 0) and p.reset_value is not None:
        b["reset_value"] = p.reset_value

    if not changed:
        raise _Clarify("the offline rules did not recognise a change they can apply to the current design")

    data["inputs"] = inputs.items
    _check_roles(inputs, b)
    data["behavior"] = b
    data["assumptions"] = list(dict.fromkeys(list(current.assumptions) + assumptions))
    new = DesignSpec.model_validate(data)
    new = new.model_copy(update={"title": _retitle(new, current), "summary": describe_spec(new)})
    return new, assumptions


def _retitle(new: DesignSpec, old: DesignSpec) -> str:
    b = new.behavior
    if isinstance(b, CounterBehavior):
        w = new.output(b.output).width
        return (f"{w}-bit {'variable-speed ' if len(b.rate_modes) > 1 else ''}"
                f"{'down-' if b.direction == 'down' and not b.direction_control else ''}counter"
                f"{' with reset' if b.reset_input else ''}")
    return old.title


# =============================================================================== entry point


def interpret_offline(text: str, current: DesignSpec | None) -> OfflineResult:
    t = normalize(text)
    if not t:
        return OfflineResult(kind="clarify", message="The request is empty. Describe the circuit you want.")
    reasons = _unsupported_reasons(t)
    if reasons:
        return OfflineResult(kind="unsupported", unsupported_reasons=reasons,
                             message="This request needs hardware or features CircuitLoop cannot build yet.")

    fam = _detect_family(t)
    is_question = (text.strip().endswith("?") or bool(_QUESTION_START.match(t))) and not _DESIGN_VERBS.search(t)
    if is_question and (fam is None or not re.search(r"\b(?:build|make|create|design)\b", t)):
        if current is not None and re.search(r"\b(?:current|this|my|the) (?:design|circuit|counter|blinker|pattern|"
                                             r"traffic light|spec)|\bwhat does it do\b", t):
            return OfflineResult(kind="question", message=f"The current design: {describe_spec(current)}")
        return OfflineResult(kind="clarify", message=f"{NO_LLM}, which cannot answer free-form questions. "
                             + OFFLINE_EXAMPLES)

    explicit_new = bool(_NEW_CUES.search(t) or re.search(r"\b(?:build|create|design|implement|generate)\b|"
                                                       r"\bmake (?:a|an|me)\b", t))
    wants_modify = current is not None and not explicit_new and (
        fam is None or fam[0] == current.family or bool(_MODIFY_CUES.search(t)))
    try:
        if wants_modify:
            assert current is not None
            new, assumptions = _modify(t, current)
            return OfflineResult(kind="modify", spec=new, assumptions=assumptions,
                                 message="Modified the current design with the offline rules (no LLM configured): "
                                 + new.summary)
        if fam is None:
            if current is None and _MODIFY_CUES.search(t):
                return OfflineResult(kind="clarify", message="There is no current design to change yet. Describe the "
                                     "complete circuit first. " + OFFLINE_EXAMPLES)
            return OfflineResult(kind="clarify", message=f"{NO_LLM} and they could not interpret this request. "
                                 + OFFLINE_EXAMPLES)
        family, variant = fam
        p = _parse_clauses(t, family)
        if family == "counter":
            data = _build_counter(t, p)
        elif family == "blinker":
            data = _build_blinker(t, p)
        elif family == "traffic_light":
            data = _build_traffic(t, p)
        elif variant == "mirror":
            data = _build_mirror(t, p)
        else:
            data = _build_moving(t, p, variant or "chase")
        assumptions = list(data.get("assumptions", []))
        spec = _finish(data)
        return OfflineResult(kind="new_design", spec=spec, assumptions=assumptions,
                             message=f"Interpreted with the offline rules (no LLM configured): {spec.summary}")
    except _Unsupported as e:
        return OfflineResult(kind="unsupported", unsupported_reasons=e.reasons,
                             message="This request is outside what CircuitLoop can build.")
    except _Clarify as e:
        return OfflineResult(kind="clarify", message=f"{NO_LLM}: {e}")
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:5])
        return OfflineResult(kind="clarify", message=f"{NO_LLM}, and the specification they produced is invalid "
                             f"({errs}). Please rephrase the request.")
