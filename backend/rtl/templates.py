"""Deterministic, preverified cl_core generators for the supported circuit families.

These are used (a) when no LLM provider is configured, (b) as known-good
references in the test suite, and (c) never as a silent substitute for a failed
LLM generation -- the pipeline always records which generator produced a
revision.

Every generated core:
  * starts with the exact header from `core_interface(spec)` (parameters + ports),
  * uses the single clock `clk`; all timing goes through clock enables
    (`cl_tick_div`) or an explicit cycle counter -- never a derived clock,
  * takes its power-on state from declaration initializers (the 7-series GSR
    idiom); the only functional reset is the spec's `reset_input`,
  * has no initial blocks and no combinational always blocks (so no latches),
  * is lint-clean under `verilator -Wall` at both the real and the simulation
    parameter sets: unused inputs/parameters are explicitly sunk and outputs the
    behaviour does not drive are tied low.

Internal identifiers are written as `@name@` markers and resolved at the end so
that they can never collide with a port name chosen in the spec (e.g. an output
called `lights` or an input called `step`).

The behaviour implemented here is the normative one from docs/contracts.md
("Behavioural semantics the oracle must check"). Where the contract leaves a
detail open, the choice is documented at the generator that makes it.
"""

from __future__ import annotations

import re

from backend.rtl.wrapper import GENERATED_BANNER, core_interface, core_parameters, core_ports
from backend.specifications.ir import (
    BlinkerBehavior,
    CounterBehavior,
    DesignSpec,
    LedPatternBehavior,
    RateMode,
    TrafficLightBehavior,
    TrafficState,
)


class TemplateUnavailable(Exception):
    pass


GENERATOR_PREFIX = "template/"


def generator_name(spec: DesignSpec) -> str:
    """The `generator` label recorded for a template-generated revision."""
    return f"{GENERATOR_PREFIX}{spec.family}"


# --------------------------------------------------------------------------- shared helpers


def _ticks_name(mode: RateMode) -> str:
    return f"TICKS_{mode.name.upper()}"


def _state_ticks_name(state: TrafficState) -> str:
    return f"TICKS_STATE_{state.name.upper()}"


def _max_chain(names: list[str]) -> str:
    expr = names[0]
    for n in names[1:]:
        expr = f"(({n}) > ({expr}) ? ({n}) : ({expr}))"
    return expr


def _max_localparams(names: list[str], result: str = "TMAX") -> list[str]:
    """Running-maximum localparam chain; linear in len(names) (unlike _max_chain's nesting)."""
    if len(names) == 1:
        return [f"    localparam integer {result} = {names[0]};"]
    lines = [f"    localparam integer {result}_0 = {names[0]};"]
    for k, n in enumerate(names[1:], start=1):
        prev = f"{result}_{k - 1}"
        target = result if k == len(names) - 1 else f"{result}_{k}"
        lines.append(f"    localparam integer {target} = (({n}) > ({prev})) ? ({n}) : ({prev});")
    return lines


def _period_mux(modes: list[RateMode], tw: str = "TW") -> str:
    """Priority mux selecting the active TICKS_* value (first matching mode wins)."""
    lines = []
    for m in modes[:-1]:
        lvl = "1'b1" if m.when_level == 1 else "1'b0"
        lines.append(f"        ({m.when_input} == {lvl}) ? {_ticks_name(m)}[{tw}-1:0] :")
    lines.append(f"        {_ticks_name(modes[-1])}[{tw}-1:0];")
    return "\n".join(lines)


def _mode_inputs(modes: list[RateMode]) -> set[str]:
    return {m.when_input for m in modes if m.when_input}


def _bits(value: int, width: int) -> str:
    """Sized binary literal, MSB first (reads like the LED row, highest LED on the left)."""
    return f"{width}'b{value:0{width}b}"


def _concat_msb_first(names: list[str]) -> str:
    """{names[n-1], ..., names[0]} so that names[i] lands on bit i."""
    return "{" + ", ".join(reversed(names)) + "}"


_MARKER_RE = re.compile(r"@([a-z][a-z0-9_]*)@")


def _resolve_internal_names(text: str, spec: DesignSpec) -> str:
    """Replace `@name@` markers with identifiers that cannot collide with a port.

    Ports keep the exact names fixed by core_interface(spec); an internal signal
    whose preferred name is taken gets a deterministic `_i<n>` suffix instead.
    """
    taken = {p.name for p in core_ports(spec)}
    resolved: dict[str, str] = {}

    def sub(m: re.Match[str]) -> str:
        base = m.group(1)
        if base not in resolved:
            cand, n = base, 0
            while cand in taken:
                n += 1
                cand = f"{base}_i{n}"
            taken.add(cand)
            resolved[base] = cand
        return resolved[base]

    return _MARKER_RE.sub(sub, text)


def _module(spec: DesignSpec, body: str, used_inputs: set[str], used_params: set[str]) -> str:
    """Wrap a behaviour body into the complete cl_core source.

    Adds the banner, the exact interface header, ties off outputs the behaviour
    does not drive, and sinks every input/parameter the body does not use (so
    -Wall stays silent without weakening the lint policy for real problems).
    """
    b = spec.behavior
    tail: list[str] = []

    for o in spec.outputs:
        if o.name != b.output:
            tail.append(f"    assign {o.name} = {o.width}'d0;  // not driven by the {spec.family} behaviour")
    if tail:
        tail.append("")

    unused_inputs = [i.name for i in spec.inputs if i.name not in used_inputs]
    if unused_inputs:
        tail += [
            "    /* verilator lint_off UNUSEDSIGNAL */",
            "    wire _unused_inputs = &{1'b0, " + ", ".join(unused_inputs) + "};",
            "    /* verilator lint_on UNUSEDSIGNAL */",
            "",
        ]

    unused_params = [name for name, _value, _meaning in core_parameters(spec) if name not in used_params]
    if unused_params:
        tail.append("    /* verilator lint_off UNUSEDPARAM */")
        tail += [f"    localparam integer UNUSED_{name} = {name};" for name in unused_params]
        tail.append("    /* verilator lint_on UNUSEDPARAM */")

    text = (
        f"// {GENERATED_BANNER}\n"
        f"// Generator: {generator_name(spec)}\n"
        "`default_nettype none\n"
        f"{core_interface(spec)}\n"
        f"{body}\n"
        + "\n".join(tail)
        + "\nendmodule\n`default_nettype wire\n"
    )
    return _resolve_internal_names(text, spec)


def _rate_section(modes: list[RateMode], clear: str, enable: str, tick: str) -> str:
    """TMAX/TW, the active-period mux and a cl_tick_div producing one-cycle `tick` pulses."""
    ticks = [_ticks_name(m) for m in modes]
    return f"""    localparam integer TMAX = {_max_chain(ticks)};
    localparam integer TW   = $clog2(TMAX + 1);

    wire [TW-1:0] @period@ =
{_period_mux(modes)}
    wire          {tick};

    cl_tick_div #(.WIDTH(TW)) @u_tick@ (
        .clk(clk), .clear({clear}), .enable({enable}), .period(@period@), .tick({tick})
    );
"""


# --------------------------------------------------------------------------- counter


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
    inc = f"@value@ + {w}'d1" if b.wrap else f"(@value@ == {w}'d{maxv}) ? @value@ : @value@ + {w}'d1"
    dec = f"@value@ - {w}'d1" if b.wrap else f"(@value@ == {w}'d0) ? @value@ : @value@ - {w}'d1"

    body = f"""    localparam integer TMAX = {_max_chain(ticks)};
    localparam integer TW   = $clog2(TMAX + 1);

    wire          @counting_up@ = {up_expr};
    wire          @hold_reset@  = {reset_expr};
    wire          @run@         = {enable_expr};
    wire [TW-1:0] @period@ =
{_period_mux(b.rate_modes)}
    wire          @step@;

    cl_tick_div #(.WIDTH(TW)) @u_tick@ (
        .clk(clk), .clear(@hold_reset@), .enable(@run@), .period(@period@), .tick(@step@)
    );

    reg [{w - 1}:0] @value@ = {w}'d{b.initial_value};

    always @(posedge clk) begin
        if (@hold_reset@) begin
            @value@ <= {w}'d{b.reset_value};
        end else if (@step@) begin
            @value@ <= @counting_up@ ? ({inc}) : ({dec});
        end
    end

    assign {b.output} = @value@;
"""
    used_inputs = _mode_inputs(b.rate_modes) | {x for x in (b.reset_input, b.enable_input) if x}
    if b.direction_control is not None:
        used_inputs.add(b.direction_control.input)
    return _module(spec, body, used_inputs, set(ticks))


# --------------------------------------------------------------------------- blinker


def _blinker_core(spec: DesignSpec, b: BlinkerBehavior) -> str:
    """50% duty blinker: one phase bit toggled every TICKS_<MODE> cycles.

    Power-on phase is 0 (together: all off; alternate: bit0=0, bit1=1, ...).
    While enable_input is low every output bit is 0 and the toggle timer and
    phase are held cleared, so re-enabling restarts from the power-on state
    (a full half-period before the first toggle) -- deterministic and testable.
    """
    out = spec.output(b.output)
    w = out.width
    enable_expr = b.enable_input or "1'b1"

    if b.pattern == "alternate":
        odd = sum(1 << i for i in range(w) if i % 2 == 1)
        mask_decl = f"    localparam [{w - 1}:0] ODD_BITS = {_bits(odd, w)};  // odd bits are lit while the phase is 0\n"
        shown = f"({{{w}{{@phase@}}}} ^ ODD_BITS)"
    else:
        mask_decl = ""
        shown = f"{{{w}{{@phase@}}}}"

    body = f"""{mask_decl}    wire          @run@ = {enable_expr};

{_rate_section(b.rate_modes, clear="~@run@", enable="@run@", tick="@toggle@")}
    reg @phase@ = 1'b0;

    always @(posedge clk) begin
        if (!@run@) begin
            @phase@ <= 1'b0;
        end else if (@toggle@) begin
            @phase@ <= ~@phase@;
        end
    end

    assign {b.output} = @run@ ? {shown} : {w}'d0;
"""
    used_inputs = _mode_inputs(b.rate_modes) | ({b.enable_input} if b.enable_input else set())
    return _module(spec, body, used_inputs, {_ticks_name(m) for m in b.rate_modes})


# --------------------------------------------------------------------------- led_pattern


def _direction_expr(b: LedPatternBehavior) -> str:
    """Expression that is 1 while the pattern moves 'left' (towards higher LED indices).

    Contract: direction_control overrides `direction` while its input is active
    (high); 'up' is read as 'left' (higher index), 'down' as 'right'. While the
    input is low the static `direction` applies.
    """
    base = "1'b1" if b.direction == "left" else "1'b0"
    if b.direction_control is None:
        return base
    dc = b.direction_control
    active = "1'b1" if dc.high_means == "up" else "1'b0"
    return f"{dc.input} ? {active} : {base}"


def _static_pattern_body(spec: DesignSpec, b: LedPatternBehavior, w: int) -> tuple[str, set[str]]:
    """mirror_inputs / invert_inputs: out[i] = (~)source_inputs[i].

    The shown value is always registered (one cycle after the conditioned
    inputs, so the LED pins are driven straight from flip-flops). enable_input
    low holds it; reset_input high makes it follow the inputs again even while
    enable is low (a static pattern has no step timer, so 'restart the pattern'
    means 'return to the live mapping'). Power-on value = the mapping of
    all-low inputs, because the conditioned inputs power up 0.
    """
    if len(b.source_inputs) != w:
        raise TemplateUnavailable(
            f"{b.pattern} needs exactly one source input per output bit "
            f"({len(b.source_inputs)} sources for a {w}-bit output)"
        )
    live = _concat_msb_first(list(b.source_inputs))
    if b.pattern == "invert_inputs":
        live = f"~{live}"
    power_on = (1 << w) - 1 if b.pattern == "invert_inputs" else 0
    used = set(b.source_inputs) | {x for x in (b.enable_input, b.reset_input) if x}
    body = f"""    wire [{w - 1}:0] @live@       = {live};
    wire       @hold_reset@ = {b.reset_input or "1'b0"};
    wire       @run@        = {b.enable_input or "1'b1"};
    reg  [{w - 1}:0] @shown@      = {_bits(power_on, w)};

    always @(posedge clk) begin
        if (@hold_reset@ || @run@) begin
            @shown@ <= @live@;
        end
    end

    assign {b.output} = @shown@;
"""
    return body, used


def _chase_update(w: int) -> str:
    return f"""        if (@hold_reset@) begin
            @lit@ <= {w}'d1;
        end else if (@step@) begin
            @lit@ <= @move_left@ ? {{@lit@[{w - 2}:0], @lit@[{w - 1}]}} : {{@lit@[0], @lit@[{w - 1}:1]}};
        end"""


def _bounce_update(w: int) -> str:
    return f"""        if (@hold_reset@) begin
            @lit@        <= {w}'d1;
            @heading_up@ <= 1'b1;
        end else if (@step@) begin
            if (@heading_up@) begin
                if (@lit@[{w - 1}]) begin
                    @heading_up@ <= 1'b0;
                    @lit@        <= {{1'b0, @lit@[{w - 1}:1]}};
                end else begin
                    @lit@        <= {{@lit@[{w - 2}:0], 1'b0}};
                end
            end else begin
                if (@lit@[0]) begin
                    @heading_up@ <= 1'b1;
                    @lit@        <= {{@lit@[{w - 2}:0], 1'b0}};
                end else begin
                    @lit@        <= {{1'b0, @lit@[{w - 1}:1]}};
                end
            end
        end"""


def _fill_section(w: int) -> str:
    """Bar graph: a count of lit LEDs (0..w) plus the bar registered from it.

    Keeping the count (instead of shifting the bar) means a direction change in
    the middle of a fill still shows a legal bar, just anchored on the other end.
    """
    kw = w.bit_length()
    arms = [f"                {kw}'d0: @bar@ <= {w}'d0;"]
    for k in range(1, w):
        low = (1 << k) - 1
        high = low << (w - k)
        arms.append(f"                {kw}'d{k}: @bar@ <= @move_left@ ? {_bits(low, w)} : {_bits(high, w)};")
    arms.append(f"                {kw}'d{w}: @bar@ <= {_bits((1 << w) - 1, w)};")
    arms.append(f"                default: @bar@ <= {w}'d0;")
    arms_text = "\n".join(arms)
    return f"""    reg  [{kw - 1}:0] @lit_count@  = {kw}'d0;
    reg  [{w - 1}:0] @bar@        = {w}'d0;
    wire [{kw - 1}:0] @count_next@ = (@lit_count@ == {kw}'d{w}) ? {kw}'d0 : @lit_count@ + {kw}'d1;

    always @(posedge clk) begin
        if (@hold_reset@) begin
            @lit_count@ <= {kw}'d0;
            @bar@       <= {w}'d0;
        end else if (@step@) begin
            @lit_count@ <= @count_next@;
            case (@count_next@)
{arms_text}
            endcase
        end
    end
"""


def _led_pattern_core(spec: DesignSpec, b: LedPatternBehavior) -> str:
    """mirror/invert (static) and chase/bounce/fill (stepped by the active rate mode).

    Stepped patterns: enable_input low holds the pattern and pauses the step
    timer; reset_input high shows the initial pattern and keeps the timer
    cleared, so the first step after release comes one full period later.
    chase: one-hot, initially bit 0, rotating one position per step.
    bounce: one-hot, initially bit 0, moving up to the top bit and back; the end
      bits are lit for one step each. The contract fixes the start (bit 0,
      moving left), so `direction` / direction_control do not apply to bounce.
    fill: initially empty; each step lights the next LED (left: bit 0 first,
      right: top bit first); the step after the bar is full clears it.
    """
    out = spec.output(b.output)
    w = out.width

    if b.pattern in ("mirror_inputs", "invert_inputs"):
        body, used = _static_pattern_body(spec, b, w)
        return _module(spec, body, used, set())

    if not b.rate_modes:
        raise TemplateUnavailable(f"pattern '{b.pattern}' needs at least one rate mode for its step rate")
    if w == 1 and b.pattern in ("chase", "bounce"):
        # A one-hot dot on a single LED never moves: refuse rather than emit a constant.
        raise TemplateUnavailable(f"'{b.pattern}' needs at least 2 LEDs (a single LED would just stay on)")

    uses_direction = b.pattern == "chase" or (b.pattern == "fill" and w > 1)
    used = _mode_inputs(b.rate_modes) | {x for x in (b.reset_input, b.enable_input) if x}
    if uses_direction and b.direction_control is not None:
        used.add(b.direction_control.input)

    head = f"""    wire          @hold_reset@ = {b.reset_input or "1'b0"};
    wire          @run@        = {b.enable_input or "1'b1"};
"""
    if uses_direction:
        head += f"    wire          @move_left@  = {_direction_expr(b)};\n"
    head += "\n" + _rate_section(b.rate_modes, clear="@hold_reset@", enable="@run@", tick="@step@") + "\n"

    if b.pattern == "chase":
        body = head + f"""    reg [{w - 1}:0] @lit@ = {w}'d1;

    always @(posedge clk) begin
{_chase_update(w)}
    end

    assign {b.output} = @lit@;
"""
    elif b.pattern == "bounce":
        body = head + f"""    reg [{w - 1}:0] @lit@        = {w}'d1;
    reg       @heading_up@ = 1'b1;  // 1 = moving towards higher LED indices

    always @(posedge clk) begin
{_bounce_update(w)}
    end

    assign {b.output} = @lit@;
"""
    elif b.pattern == "fill":
        body = head + _fill_section(w) + f"\n    assign {b.output} = @bar@;\n"
    else:  # pragma: no cover - the IR Literal forbids anything else
        raise TemplateUnavailable(f"unknown led_pattern '{b.pattern}'")
    return _module(spec, body, used, {_ticks_name(m) for m in b.rate_modes})


# --------------------------------------------------------------------------- traffic_light


def _traffic_light_core(spec: DesignSpec, b: TrafficLightBehavior) -> str:
    """Cyclic FSM with an exact per-state cycle timer and an optional request latch.

    Each state lasts exactly TICKS_STATE_<S> cycles (timer counts 0..T-1).
    The output is the registered `lights` of the current state, updated on the
    same edge as the state. A rising edge of request_input sets a latch; when
    request_after ends with the latch set the FSM enters request_state, clears
    the latch, and afterwards continues with the state that follows
    request_after. A press on the very cycle the latch is consumed is kept
    (latched for the next round). reset_input holds states[0] with the timer
    and the latch cleared.
    """
    out = spec.output(b.output)
    w = out.width
    states = list(b.states)

    names = [s.name for s in states] + ([b.request_state.name] if b.request_state else [])
    if len(set(names)) != len(names):
        raise TemplateUnavailable("traffic-light state names must be unique (including request_state)")
    for s in states + ([b.request_state] if b.request_state else []):
        if s.lights >= (1 << w):
            raise TemplateUnavailable(f"state '{s.name}' lights {s.lights:#x} do not fit the {w}-bit output")

    with_request = b.request_input is not None
    after_idx = -1  # index of request_after in states; -1 = no request handling
    if with_request:
        if b.request_state is None or b.request_after is None:
            raise TemplateUnavailable("request_input needs both request_state and request_after")
        state_names = [s.name for s in states]
        if b.request_after not in state_names:
            raise TemplateUnavailable(f"request_after '{b.request_after}' is not one of the states")
        after_idx = state_names.index(b.request_after)

    fsm = states + ([b.request_state] if with_request and b.request_state else [])
    n = len(fsm)
    sw = max(1, (n - 1).bit_length())
    req_idx = len(states)  # FSM index of the request state (only present when with_request)
    ticks = [_state_ticks_name(s) for s in fsm]

    def st(i: int) -> str:
        return "ST_REQ" if with_request and i == req_idx else f"ST_{i}"

    def lt(i: int) -> str:
        return "L_REQ" if with_request and i == req_idx else f"L_{i}"

    decls = _max_localparams(ticks) + ["    localparam integer TW   = $clog2(TMAX + 1);"]
    for i, s in enumerate(fsm):
        tag = " (request state)" if with_request and i == req_idx else ""
        decls.append(f"    localparam [{sw - 1}:0] {st(i)} = {sw}'d{i};  // {s.name}{tag}")
    for i, s in enumerate(fsm):
        decls.append(f"    localparam [{w - 1}:0] {lt(i)} = {_bits(s.lights, w)};  // {s.name} lights")

    last_lines = []
    for i, s in enumerate(fsm[:-1]):
        last_lines.append(f"        (@state@ == {st(i)}) ? {_state_ticks_name(s)}[TW-1:0] - 1'b1 :")
    last_lines.append(f"        {_state_ticks_name(fsm[-1])}[TW-1:0] - 1'b1;")

    def go(i: int, indent: str) -> str:
        return f"{indent}@state@  <= {st(i)};\n{indent}@lamps@  <= {lt(i)};"

    arms: list[str] = []
    for i in range(len(states)):
        nxt = (i + 1) % len(states)
        if i == after_idx:
            arms.append(
                f"                {st(i)}: begin\n"
                f"                    if (@req_pending@) begin\n{go(req_idx, ' ' * 24)}\n"
                f"                    end else begin\n{go(nxt, ' ' * 24)}\n"
                f"                    end\n"
                f"                end"
            )
        else:
            arms.append(f"                {st(i)}: begin\n{go(nxt, ' ' * 20)}\n                end")
    if with_request:
        resume = (after_idx + 1) % len(states)
        arms.append(f"                ST_REQ: begin\n{go(resume, ' ' * 20)}\n                end")
    arms.append(f"                default: begin\n{go(0, ' ' * 20)}\n                end")
    arms_text = "\n".join(arms)

    request_logic = ""
    if with_request:
        request_logic = f"""
    // Request latch: a rising edge of the conditioned input is remembered until it is served.
    wire          @req_press@;
    reg           @req_pending@ = 1'b0;
    wire          @take_req@    = @state_done@ && (@state@ == {st(after_idx)}) && @req_pending@;

    cl_edge_rise @u_req_edge@ (.clk(clk), .d({b.request_input}), .pulse(@req_press@));

    always @(posedge clk) begin
        if (@hold_reset@) begin
            @req_pending@ <= 1'b0;
        end else if (@req_press@) begin
            @req_pending@ <= 1'b1;
        end else if (@take_req@) begin
            @req_pending@ <= 1'b0;
        end
    end
"""

    last_text = "\n".join(last_lines)
    body = "\n".join(decls) + f"""

    wire          @hold_reset@ = {b.reset_input or "1'b0"};
    reg  [{sw - 1}:0]  @state@      = ST_0;
    reg  [TW-1:0] @timer@      = {{TW{{1'b0}}}};
    reg  [{w - 1}:0]  @lamps@      = L_0;

    // Last timer value of the current state: a state lasts exactly TICKS_STATE_<S> cycles.
    wire [TW-1:0] @state_last@ =
{last_text}
    wire          @state_done@ = (@timer@ == @state_last@);
{request_logic}
    always @(posedge clk) begin
        if (@hold_reset@) begin
            @state@  <= ST_0;
            @timer@  <= {{TW{{1'b0}}}};
            @lamps@  <= L_0;
        end else if (@state_done@) begin
            @timer@ <= {{TW{{1'b0}}}};
            case (@state@)
{arms_text}
            endcase
        end else begin
            @timer@ <= @timer@ + 1'b1;
        end
    end

    assign {b.output} = @lamps@;
"""
    used_inputs = {x for x in (b.reset_input, b.request_input) if x}
    return _module(spec, body, used_inputs, set(ticks))


# --------------------------------------------------------------------------- entry point


def generate_core(spec: DesignSpec) -> str:
    b = spec.behavior
    if isinstance(b, CounterBehavior):
        return _counter_core(spec, b)
    if isinstance(b, BlinkerBehavior):
        return _blinker_core(spec, b)
    if isinstance(b, LedPatternBehavior):
        return _led_pattern_core(spec, b)
    if isinstance(b, TrafficLightBehavior):
        return _traffic_light_core(spec, b)
    raise TemplateUnavailable(f"no template generator for family '{spec.family}'")
