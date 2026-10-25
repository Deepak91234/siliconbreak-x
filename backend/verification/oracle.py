"""Independent simulation oracle: judges a simulation trace against the DesignSpec.

The oracle never looks at the RTL. Expected values, legal transitions and interval
lengths come from the behavioural reference models in `reference.py`, which derive
every cycle count from the spec's physical values (Hz, seconds, milliseconds) and the
simulation clock of the test plan, following docs/contracts.md ("Behavioural
semantics the oracle must check").

Each `CheckSpec` of a scenario (produced by `testgen.generate_test_plan`) is evaluated
into one `CheckResult` with a one-line human-readable summary, the expected and
observed values and the first failing cycle, so the repair prompt and the UI can
explain exactly what went wrong.

Cycle convention (shared with the harness): trace values at cycle k are sampled just
after rising edge k; cycle 0 holds the power-on values.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from backend.specifications.ir import DesignSpec

from . import reference as R
from .testgen import CheckSpec, Scenario, TestPlan
from .verilator import Trace

MAX_DETAIL_ITEMS = 12  # bound on lists stored in CheckResult.details


class CheckResult(BaseModel):
    id: str
    scenario_id: str
    requirement_ids: list[str]
    passed: bool
    summary: str  # one line, human readable
    expected: Any = None
    observed: Any = None
    first_failure_cycle: int | None = None
    details: dict = Field(default_factory=dict)


# --------------------------------------------------------------------------- evaluation context


class _Ctx:
    """Everything a check needs for one scenario: model, conditioned inputs and the output series."""

    def __init__(self, spec: DesignSpec, plan: TestPlan, scenario: Scenario, trace: Trace):
        self.spec = spec
        self.plan = plan
        self.scenario = scenario
        self.trace = trace
        self.clk_hz = plan.clk_hz
        self.model = R.model_for(spec, plan.clk_hz)
        self.tl = R.InputTimeline(spec, plan.clk_hz, scenario.events)
        self.end = min(int(scenario.end_cycle), int(trace.end_cycle)) if trace.end_cycle else int(scenario.end_cycle)
        led = list(trace.changes.get("led") or [])
        if not led or led[0][0] != 0:
            led.insert(0, (0, 0))
        self.led = led
        pts: list[tuple[int, int]] = []
        for c, v in led:
            cv = self.model.core_value(int(v))
            if not pts or pts[-1][1] != cv:
                pts.append((int(c), cv))
        self.pts = pts
        self.cycles = [c for c, _ in pts]

    # -- output series ---------------------------------------------------------
    def value_at(self, cycle: int) -> int:
        i = bisect.bisect_right(self.cycles, cycle) - 1
        return self.pts[max(i, 0)][1]

    def changes_in(self, a: int, b: int) -> list[tuple[int, int]]:
        """Output changes at cycles c with a < c <= b."""
        lo = bisect.bisect_right(self.cycles, a)
        hi = bisect.bisect_right(self.cycles, b)
        return self.pts[lo:hi]

    def excerpt(self, cycle: int | None, before: int = 4, after: int = 4) -> list[str]:
        """A few output samples around `cycle` for diagnostics."""
        if cycle is None:
            return []
        i = bisect.bisect_right(self.cycles, cycle) - 1
        lo, hi = max(0, i - before), min(len(self.pts), i + after + 1)
        return [f"cycle {c}: {self.model.fmt(v)}" for c, v in self.pts[lo:hi]]

    def fmt(self, v: int | None) -> str:
        return self.model.fmt(v)

    def result(self, chk: CheckSpec, passed: bool, summary: str, expected: Any = None, observed: Any = None,
               cycle: int | None = None, **details: Any) -> CheckResult:
        details = {k: (v[:MAX_DETAIL_ITEMS] if isinstance(v, list) else v) for k, v in details.items()}
        if not passed and cycle is not None:
            details.setdefault("trace_excerpt", self.excerpt(cycle))
        return CheckResult(id=chk.id, scenario_id=self.scenario.id, requirement_ids=list(chk.requirement_ids),
                           passed=passed, summary=summary, expected=expected, observed=observed,
                           first_failure_cycle=None if passed else cycle, details=details)

    # -- timing helpers ----------------------------------------------------------
    def quiet(self, a: int, b: int) -> bool:
        """No conditioned input change (with its latency window) touches cycles a..b."""
        return not self.tl.overlapping(a, b)

    def tol(self, expected: float) -> int:
        return int(R.tolerance(expected))


def _rng(chk: CheckSpec, ctx: _Ctx) -> tuple[int, int]:
    frm = int(chk.params.get("frm", 0))
    to = min(int(chk.params.get("to", ctx.end)), ctx.end)
    return frm, max(frm, to)


# --------------------------------------------------------------------------- generic checks


def _idle_off(ctx: _Ctx, chk: CheckSpec) -> CheckResult:
    mask = ctx.model.unused_led_mask()
    for c, led in ctx.led:
        if c > ctx.end:
            break
        if int(led) & mask:
            bits = [i for i in range(16) if (int(led) & mask) >> i & 1]
            return ctx.result(chk, False, f"unused LED(s) {bits} lit at cycle {c}; every LED not used by the "
                              "design must stay off", expected=0, observed=f"0x{int(led) & mask:04x}", cycle=c,
                              unused_mask=f"0x{mask:04x}")
    return ctx.result(chk, True, f"all unused LEDs (mask 0x{mask:04x}) stay off for cycles 0..{ctx.end}",
                      expected=0, observed=0)


def _expected_value(ctx: _Ctx, expect: Any) -> tuple[int, str]:
    m = ctx.model
    if expect == "initial":
        return m.initial_value(), "the power-on value"
    if expect == "reset":
        return m.reset_value(), "the reset value"
    if expect == "off":
        return 0, "all LEDs off"
    if expect == "state0":
        return m.initial_value(), "the first state's lights"
    return int(expect) & m.mask, "the expected pattern"


def _value_at(ctx: _Ctx, chk: CheckSpec) -> CheckResult:
    frm, to = _rng(chk, ctx)
    exp, what = _expected_value(ctx, chk.params.get("expect"))
    v0 = ctx.value_at(frm)
    bad: tuple[int, int] | None = None if v0 == exp else (frm, v0)
    if bad is None:
        for c, v in ctx.changes_in(frm, to):
            if v != exp:
                bad = (c, v)
                break
    if bad is None:
        return ctx.result(chk, True, f"output = {ctx.fmt(exp)} ({what}) throughout cycles {frm}..{to}",
                          expected=exp, observed=exp)
    c, v = bad
    return ctx.result(chk, False, f"output was {ctx.fmt(v)} at cycle {c}; expected {ctx.fmt(exp)} ({what}) "
                      f"throughout cycles {frm}..{to}", expected=exp, observed=v, cycle=c)


def _hold(ctx: _Ctx, chk: CheckSpec) -> CheckResult:
    frm, to = _rng(chk, ctx)
    v0 = ctx.value_at(frm)
    ch = ctx.changes_in(frm, to)
    if not ch:
        return ctx.result(chk, True, f"output holds {ctx.fmt(v0)} throughout cycles {frm}..{to}",
                          expected=v0, observed=v0)
    c, v = ch[0]
    return ctx.result(chk, False, f"output changed from {ctx.fmt(v0)} to {ctx.fmt(v)} at cycle {c}; it must hold "
                      f"its value during cycles {frm}..{to}", expected=v0, observed=v, cycle=c,
                      changes=len(ch))


# --------------------------------------------------------------------------- periodic families


def _can_step(ctx: _Ctx, value: int, levels: dict[str, int]) -> bool:
    m = ctx.model
    assert isinstance(m, R.PeriodicModel)
    hidden = m.resync(value)
    if not hidden:
        return True  # an illegal value is the sequence check's business; the rate is still required
    return any(v != value for h in hidden for v, _ in m.successors(h, levels))


def _gaps(ctx: _Ctx, frm: int, to: int) -> tuple[list[tuple[int, int, Any]], list[dict]]:
    """Steady-state intervals inside frm..to.

    Returns (intervals, violations): intervals are (start, end, mode) between two
    consecutive output changes with no input change nearby; violations are interval
    or open-gap (no step for too long) failures.
    """
    m = ctx.model
    assert isinstance(m, R.PeriodicModel)
    ch = ctx.changes_in(frm, to)
    intervals: list[tuple[int, int, Any]] = []
    violations: list[dict] = []

    def gap_limit(a: int, b: int, value: int) -> None:
        if not ctx.quiet(a, b):
            return
        levels = ctx.tl.levels(a)
        mode = m.active_mode(levels)
        if mode is None or not _can_step(ctx, value, levels):
            return
        T = m.period(mode)
        if b - a > T + ctx.tol(T):
            violations.append({"cycle": a + T + ctx.tol(T), "kind": "no_step", "from": a, "to": b,
                               "length": b - a, "expected": T, "mode": mode.name})

    # leading gap: from frm to the first change (or to the end of the window)
    gap_limit(frm, ch[0][0] if ch else to, ctx.value_at(frm))
    for (c0, _v0), (c1, _v1) in zip(ch, ch[1:]):
        if not ctx.quiet(c0, c1):
            continue
        mode = m.active_mode(ctx.tl.levels(c0))
        if mode is None:
            continue
        T = m.period(mode)
        intervals.append((c0, c1, mode))
        if not R.within_tolerance(c1 - c0, T):
            violations.append({"cycle": c1, "kind": "interval", "from": c0, "to": c1, "length": c1 - c0,
                               "expected": T, "mode": mode.name})
    if ch:
        gap_limit(ch[-1][0], to, ch[-1][1])
    return intervals, violations


def _mode_desc(ctx: _Ctx, mode_name: str) -> str:
    m = ctx.model
    assert isinstance(m, R.PeriodicModel)
    mode = m.mode_by_name(mode_name)
    per = "toggle" if ctx.spec.family == "blinker" else "step"
    return f"mode '{mode.name}' {mode.hz:g}/s at {ctx.clk_hz} Hz -> {m.period(mode)} cycles per {per}"


def _steady_rate(ctx: _Ctx, chk: CheckSpec) -> CheckResult:
    frm, to = _rng(chk, ctx)
    intervals, violations = _gaps(ctx, frm, to)
    lengths = [b - a for a, b, _ in intervals]
    modes = sorted({md.name for _, _, md in intervals})
    mean = round(sum(lengths) / len(lengths), 1) if lengths else None
    if not violations:
        if lengths:
            exp = sorted({ctx.model.period(md) for _, _, md in intervals})
            summary = (f"{len(lengths)} steady intervals in cycles {frm}..{to}, mean {mean} cycles "
                       f"(expected {exp[0] if len(exp) == 1 else exp} ± {ctx.tol(exp[0])}; "
                       + "; ".join(_mode_desc(ctx, n) for n in modes) + ")")
        else:
            summary = f"no steady-state interval to measure in cycles {frm}..{to} (no step expected)"
        return ctx.result(chk, True, summary, expected=None, observed=mean, intervals=len(lengths))
    v = violations[0]
    desc = _mode_desc(ctx, v["mode"])
    if v["kind"] == "interval":
        summary = (f"interval of {v['length']} cycles between output changes at cycles {v['from']} and {v['to']}; "
                   f"expected {v['expected']} ± {ctx.tol(v['expected'])} ({desc})")
    else:
        summary = (f"no output change for {v['length']} cycles (cycles {v['from']}..{v['to']}); expected a change "
                   f"every {v['expected']} ± {ctx.tol(v['expected'])} cycles ({desc})")
    return ctx.result(chk, False, summary, expected=v["expected"], observed=v["length"], cycle=v["cycle"],
                      violations=violations, intervals=len(lengths), mean_interval=mean)


def _ratio(ctx: _Ctx, chk: CheckSpec) -> CheckResult:
    m = ctx.model
    assert isinstance(m, R.PeriodicModel)
    fast = m.mode_by_name(chk.params["fast_mode"])
    slow = m.mode_by_name(chk.params["slow_mode"])
    Pf, Ps = m.period(fast), m.period(slow)

    def collect(windows: list, mode) -> list[int]:
        out = []
        for a, b in windows:
            iv, _ = _gaps(ctx, int(a), min(int(b), ctx.end))
            out += [e - s for s, e, md in iv if md.name == mode.name]
        return out

    fl, sl = collect(chk.params.get("fast_windows", []), fast), collect(chk.params.get("slow_windows", []), slow)
    expected = round(Ps / Pf, 4)
    if not fl or not sl:
        return ctx.result(chk, False, f"could not measure the '{fast.name}'/'{slow.name}' rate ratio (steady intervals: "
                          f"{len(fl)} fast, {len(sl)} slow); expected {expected}", expected=expected, observed=None,
                          cycle=int(chk.params.get("fast_windows", [[0]])[0][0]))
    mf, ms = sum(fl) / len(fl), sum(sl) / len(sl)
    measured = round(ms / mf, 4)
    rel = R.tolerance(Ps) / Ps + R.tolerance(Pf) / Pf
    ok = abs(measured - expected) <= expected * rel
    summary = (f"'{fast.name}' runs {measured}x as fast as '{slow.name}' (mean {mf:.1f} vs {ms:.1f} cycles); "
               f"expected {expected}x ({fast.hz:g}/s vs {slow.hz:g}/s)")
    return ctx.result(chk, ok, summary, expected=expected, observed=measured,
                      cycle=None if ok else int(chk.params["fast_windows"][0][0]),
                      fast_mean=round(mf, 2), slow_mean=round(ms, 2), physical_ratio=round(fast.hz / slow.hz, 4))


def _wrap(ctx: _Ctx, chk: CheckSpec) -> CheckResult:
    m = ctx.model
    assert isinstance(m, R.PeriodicModel)
    frm, to = _rng(chk, ctx)
    levels = ctx.tl.levels(frm)
    seq = [(frm, ctx.value_at(frm))] + ctx.changes_in(frm, to)
    if isinstance(m, R.CounterModel) and not m.b.wrap:
        limit = m.limit(levels)
        mode = m.active_mode(levels)
        T = m.period(mode) if mode else 0
        reach = next((c for c, v in seq if v == limit), None)
        if reach is None:
            return ctx.result(chk, False, f"the counter never reached its limit {ctx.fmt(limit)} in cycles {frm}..{to}",
                              expected=limit, observed=seq[-1][1], cycle=to)
        after = ctx.changes_in(reach, to)
        if after:
            c, v = after[0]
            return ctx.result(chk, False, f"the saturating counter left its limit {ctx.fmt(limit)} at cycle {c} "
                              f"(became {ctx.fmt(v)}); it must stay there", expected=limit, observed=v, cycle=c)
        if to - reach < T:
            return ctx.result(chk, False, f"the counter reached {ctx.fmt(limit)} only at cycle {reach}, too late to "
                              f"confirm saturation before cycle {to}", expected=limit, observed=limit, cycle=reach)
        return ctx.result(chk, True, f"the counter saturates at {ctx.fmt(limit)} (reached at cycle {reach}, held "
                          f"until {to})", expected=limit, observed=limit)
    wanted = m.wrap_transitions(levels)
    seen = {(a[1], b[1]): b[0] for a, b in zip(seq, seq[1:])}
    missing = [t for t in wanted if t not in seen]
    fmt_t = [f"{ctx.fmt(a)} -> {ctx.fmt(b)}" for a, b in wanted]
    if not missing:
        return ctx.result(chk, True, f"wrap-around observed: {', '.join(fmt_t)} (cycles {frm}..{to})",
                          expected=fmt_t, observed=[seen[t] for t in wanted])
    miss = [f"{ctx.fmt(a)} -> {ctx.fmt(b)}" for a, b in missing]
    return ctx.result(chk, False, f"wrap-around transition(s) {', '.join(miss)} never observed in cycles {frm}..{to} "
                      f"(last value {ctx.fmt(seq[-1][1])})", expected=fmt_t, observed=None, cycle=to,
                      values_seen=[ctx.fmt(v) for _, v in seq[-MAX_DETAIL_ITEMS:]])


def _sequence(ctx: _Ctx, chk: CheckSpec) -> CheckResult:
    m = ctx.model
    assert isinstance(m, R.PeriodicModel)
    frm, to = _rng(chk, ctx)
    names = m.legality_inputs()
    v0 = ctx.value_at(frm)
    hidden = set(m.initial_hidden()) if (frm == 0 and v0 == m.initial_value()) else set(m.resync(v0))
    if not hidden:
        return ctx.result(chk, False, f"output {ctx.fmt(v0)} at cycle {frm} is not a legal value ({m.rule()})",
                          expected=m.rule(), observed=v0, cycle=frm)
    prev = v0
    n = 0
    for c, v in ctx.changes_in(frm, to):
        envs = ctx.tl.envs(c, names) if names else [ctx.tl.levels(c)]
        succ = [s for h in hidden for s in m.successors_for(h, envs)]
        nxt = {h2 for val, h2 in succ if val == v}
        if not nxt:
            legal = sorted({val for val, _ in succ})
            exp = [ctx.fmt(x) for x in legal] if legal else "no change"
            env_txt = ", ".join(f"{k}={'/'.join(map(str, ctx.tl.options(k, c)))}" for k in names) or "no inputs"
            return ctx.result(chk, False, f"illegal transition {ctx.fmt(prev)} -> {ctx.fmt(v)} at cycle {c} "
                              f"(inputs {env_txt}); legal next value(s): {exp}; rule: {m.rule()}",
                              expected=exp, observed=ctx.fmt(v), cycle=c, previous=ctx.fmt(prev),
                              transitions_checked=n)
        hidden, prev = nxt, v
        n += 1
    return ctx.result(chk, True, f"{n} output transitions in cycles {frm}..{to} all follow the rule: {m.rule()}",
                      expected=m.rule(), observed=n)


def _first_change_after(ctx: _Ctx, chk: CheckSpec) -> CheckResult:
    m = ctx.model
    assert isinstance(m, R.PeriodicModel)
    anchor = int(chk.params["anchor"])
    levels = ctx.tl.levels(anchor)
    mode = m.active_mode(levels)
    what = f"release of {chk.params['input']}" if chk.params.get("input") else "power-on"
    if mode is None:
        return ctx.result(chk, False, f"the behaviour is paused at cycle {anchor}; cannot check the first step",
                          cycle=anchor)
    T = m.period(mode)
    tol = ctx.tol(T)
    nxt = ctx.changes_in(anchor, ctx.end)
    if not nxt:
        return ctx.result(chk, False, f"no output change after the {what} (cycle {anchor}); expected the first "
                          f"step {T} ± {tol} cycles later ({_mode_desc(ctx, mode.name)})", expected=T,
                          observed=None, cycle=min(ctx.end, anchor + T + tol))
    c, v = nxt[0]
    measured = c - anchor
    ok = R.within_tolerance(measured, T)
    summary = (f"first output change {measured} cycles after the {what} (cycle {anchor} -> {c}, value "
               f"{ctx.fmt(v)}); expected {T} ± {tol} ({_mode_desc(ctx, mode.name)})")
    return ctx.result(chk, ok, summary, expected=T, observed=measured, cycle=c)


# --------------------------------------------------------------------------- static led patterns


def _static_map(ctx: _Ctx, chk: CheckSpec) -> CheckResult:
    m = ctx.model
    assert isinstance(m, R.PatternModel)
    frm, to = _rng(chk, ctx)
    tl = ctx.tl
    pts = {frm} | {c for c, _ in ctx.changes_in(frm, to)}
    for a, b, _n in tl.windows():
        pts.update(x for x in (a, b + 1) if frm <= x <= to)
    for chs in tl.changes.values():
        pts.update(ch.eff_cycle for ch in chs if frm <= ch.eff_cycle <= to)
    en = m.b.enable_input
    checked = 0
    for c in sorted(pts):
        if en and 0 in tl.options(en, c):
            continue  # disabled (or possibly disabled): the output holds; checked by 'hold'
        v = ctx.value_at(c)
        opts = m.bit_options(tl, c)
        bad = [i for i in range(m.width) if (v >> i) & 1 not in opts[i]]
        checked += 1
        if bad:
            exp = m.mapping(tl.levels(c))
            srcs = list(m.b.source_inputs)
            return ctx.result(chk, False, f"output {ctx.fmt(v)} at cycle {c} does not match the inputs: bit(s) {bad} "
                              f"wrong; expected {ctx.fmt(exp)} ({m.rule()})", expected=exp, observed=v, cycle=c,
                              inputs={n: tl.level(n, c) for n in srcs})
    return ctx.result(chk, True, f"output follows the source inputs at all {checked} change points in cycles "
                      f"{frm}..{to} ({m.rule()})", observed=checked)


def _change_count(ctx: _Ctx, chk: CheckSpec) -> CheckResult:
    m = ctx.model
    assert isinstance(m, R.PatternModel)
    frm, to = _rng(chk, ctx)
    tl = ctx.tl
    nominal = [m.mapping(tl.levels(frm))]
    for ch in tl.effective_in(frm + 1, to):
        val = m.mapping(tl.levels(ch.eff_cycle))
        if val != nominal[-1]:
            nominal.append(val)
    expected = len(nominal) - 1
    obs = ctx.changes_in(frm, to)
    ok = len(obs) == expected
    summary = (f"output changed {len(obs)} time(s) in cycles {frm}..{to}; expected exactly {expected} "
               "(contact bounce shorter than the debounce time must be ignored)")
    cycle = None
    if not ok:
        cycle = obs[expected][0] if len(obs) > expected else to
    return ctx.result(chk, ok, summary, expected=expected, observed=len(obs), cycle=cycle,
                      change_cycles=[c for c, _ in obs])


# --------------------------------------------------------------------------- traffic light


def _fsm_segments(ctx: _Ctx, anchor: int, until: int) -> list[dict]:
    """Reference visits from `anchor` merged into visible segments (equal lights merge)."""
    m = ctx.model
    assert isinstance(m, R.TrafficModel)
    req = [ch.eff_cycle for ch in ctx.tl.rising_edges(m.b.request_input)] if m.has_request and m.b.request_input else []
    segs: list[dict] = []
    for v in m.timeline(anchor, until, req):
        if segs and segs[-1]["lights"] == v.lights:
            segs[-1]["end"] = v.end
            segs[-1]["visits"].append(v)
        else:
            segs.append({"start": v.start, "end": v.end, "lights": v.lights, "visits": [v]})
    return segs


def _seg_name(seg: dict) -> str:
    return "+".join(v.state for v in seg["visits"])


def _fsm_sequence(ctx: _Ctx, chk: CheckSpec) -> CheckResult:
    frm, to = _rng(chk, ctx)
    anchor = int(chk.params.get("anchor", frm))
    tol = lambda t: ctx.tol(max(1, t - anchor))  # noqa: E731 - boundary tolerance grows with time since anchor
    segs = _fsm_segments(ctx, anchor, to + tol(to) + 1)
    bounds = [(s["start"], s["lights"], _seg_name(s)) for s in segs[1:] if frm < s["start"] <= to + tol(s["start"])]
    seg0 = next((s for s in segs if s["start"] <= frm < s["end"]), segs[0])
    v0 = ctx.value_at(frm)
    expected_seq = [f"{_seg_name(s)}={ctx.fmt(s['lights'])}@{s['start']}" for s in segs if s["start"] <= to]
    near_start = any(abs(t - frm) <= tol(t) for t, _, _ in bounds)
    if v0 != seg0["lights"] and not near_start:
        return ctx.result(chk, False, f"at cycle {frm} the lights are {ctx.fmt(v0)}; expected {_seg_name(seg0)} "
                          f"{ctx.fmt(seg0['lights'])}", expected=seg0["lights"], observed=v0, cycle=frm,
                          expected_sequence=expected_seq)
    obs = ctx.changes_in(frm, to)
    j = 0
    cur_name, cur_lights = _seg_name(seg0), seg0["lights"]
    for t, lights, name in bounds:
        tt = tol(t)
        if j < len(obs) and obs[j][0] < t - tt:
            c, v = obs[j]
            return ctx.result(chk, False, f"lights changed to {ctx.fmt(v)} at cycle {c}; expected {cur_name} "
                              f"({ctx.fmt(cur_lights)}) until cycle {t} ± {tt}, then {name} ({ctx.fmt(lights)})",
                              expected=cur_lights, observed=v, cycle=c, expected_sequence=expected_seq)
        if j < len(obs) and abs(obs[j][0] - t) <= tt:
            c, v = obs[j]
            if v != lights:
                return ctx.result(chk, False, f"after {cur_name} the lights became {ctx.fmt(v)} at cycle {c}; "
                                  f"expected {name} ({ctx.fmt(lights)})", expected=lights, observed=v, cycle=c,
                                  expected_sequence=expected_seq)
            j += 1
        elif t + tt > to:
            continue  # boundary at the very end of the window: may or may not be visible
        else:
            v = ctx.value_at(t + tt)
            return ctx.result(chk, False, f"expected the change from {cur_name} to {name} ({ctx.fmt(lights)}) at "
                              f"cycle {t} ± {tt}; the lights were still {ctx.fmt(v)}", expected=lights,
                              observed=v, cycle=t + tt, expected_sequence=expected_seq)
        cur_name, cur_lights = name, lights
    if j < len(obs):
        c, v = obs[j]
        return ctx.result(chk, False, f"unexpected change of the lights to {ctx.fmt(v)} at cycle {c}; expected "
                          f"{cur_name} ({ctx.fmt(cur_lights)})", expected=cur_lights, observed=v, cycle=c,
                          expected_sequence=expected_seq)
    return ctx.result(chk, True, f"{len(bounds)} state transitions in cycles {frm}..{to} match the expected sequence "
                      + " -> ".join(_seg_name(s) for s in segs if s["start"] <= to),
                      expected=expected_seq, observed=len(obs))


def _fsm_duration(ctx: _Ctx, chk: CheckSpec) -> CheckResult:
    m = ctx.model
    assert isinstance(m, R.TrafficModel)
    frm, to = _rng(chk, ctx)
    anchor = int(chk.params.get("anchor", frm))
    state = str(chk.params["state"])
    D = m.duration(m.state_named(state))
    tol = lambda t: ctx.tol(max(1, t - anchor))  # noqa: E731
    segs = _fsm_segments(ctx, anchor, to + tol(to) + 1)
    obs = ctx.changes_in(frm, ctx.end)

    def find(t: int, lights: int) -> int | None:
        """Observed entry into `lights` closest to the nominal cycle t (within half the state's duration,
        so a wrong earlier duration does not hide this one)."""
        best = None
        for c, v in obs:
            if v == lights and abs(c - t) <= max(tol(t), D // 2) and (best is None or abs(c - t) < abs(best - t)):
                best = c
        return best

    measured: list[int] = []
    unobservable = 0
    for k, s in enumerate(segs):
        if len(s["visits"]) != 1 or s["visits"][0].state != state:
            if any(v.state == state for v in s["visits"]):
                unobservable += 1
            continue
        if s["start"] < frm or s["end"] > to:
            continue
        start = anchor if s["start"] == anchor else find(s["start"], s["lights"])
        if start is None:
            return ctx.result(chk, False, f"state {state} ({ctx.fmt(s['lights'])}) did not start near cycle "
                              f"{s['start']} ± {tol(s['start'])}", expected=D, observed=None, cycle=s["start"])
        end = next((c for c, _v in obs if c > start), None)
        if end is None or end > to:
            return ctx.result(chk, False, f"state {state} (started at cycle {start}) did not end near cycle "
                              f"{s['end']} ± {tol(s['end'])}; expected it to last {D} cycles "
                              f"({m.state_named(state).duration_s:g} s at {ctx.clk_hz} Hz)", expected=D,
                              observed=None, cycle=s["end"])
        dur = end - start
        measured.append(dur)
        if not R.within_tolerance(dur, D):
            return ctx.result(chk, False, f"state {state} lasted {dur} cycles (cycles {start}..{end}); expected {D} ± "
                              f"{ctx.tol(D)} ({m.state_named(state).duration_s:g} s at {ctx.clk_hz} Hz)",
                              expected=D, observed=dur, cycle=end, measured=measured)
    if not measured:
        why = "its lights equal a neighbouring state's" if unobservable else "no complete visit in the window"
        return ctx.result(chk, True, f"duration of state {state} not separately observable in cycles {frm}..{to} "
                          f"({why})", expected=D, observed=None)
    return ctx.result(chk, True, f"state {state} lasted {measured} cycles; expected {D} ± {ctx.tol(D)} "
                      f"({m.state_named(state).duration_s:g} s at {ctx.clk_hz} Hz)", expected=D, observed=measured)


# --------------------------------------------------------------------------- seven-segment display


def _pin_series(ctx: _Ctx, name: str, idle: int) -> list[tuple[int, int]]:
    ch = list(ctx.trace.changes.get(name) or [])
    if not ch or ch[0][0] != 0:
        ch.insert(0, (0, idle))
    return [(int(c), int(v)) for c, v in ch]


def _display(ctx: _Ctx, chk: CheckSpec) -> CheckResult:
    """Decode the multiplexed display from the seg/an pins and compare it with the spec's text.

    Every interval where exactly one anode is low is attributed to that digit. A
    digit with a character must show exactly its glyph whenever selected and be
    selected at least once per 1/60 s; a blank digit must never light a segment;
    two anodes low at once (ghosting) fails.
    """
    m = ctx.model
    assert isinstance(m, R.DisplayModel)
    frm, to = _rng(chk, ctx)
    an = _pin_series(ctx, "an", 0xF)
    seg = _pin_series(ctx, "seg", R.SEG_BLANK)
    cuts = sorted({c for c, _ in an} | {c for c, _ in seg} | {frm, to + 1})
    ia = isg = 0
    slots: dict[int, list[tuple[int, int, int]]] = {p: [] for p in range(4)}
    for a, b in zip(cuts, cuts[1:]):
        while ia + 1 < len(an) and an[ia + 1][0] <= a:
            ia += 1
        while isg + 1 < len(seg) and seg[isg + 1][0] <= a:
            isg += 1
        lo, hi = max(a, frm), min(b - 1, to)
        if lo > hi:
            continue
        av, sv = an[ia][1] & 0xF, seg[isg][1] & 0x7F
        active = [p for p in range(4) if not (av >> p) & 1]
        if len(active) > 1 and sv != R.SEG_BLANK:
            return ctx.result(chk, False, f"digits {['AN%d' % p for p in active]} were selected together at cycle {lo} "
                              "(ghosting); the display must drive one digit at a time", expected="one anode low",
                              observed=f"an=0b{av:04b}", cycle=lo)
        if len(active) == 1:
            slots[active[0]].append((lo, hi, sv))
    shown: dict[int, str] = {}
    for p in range(4):
        exp = m.expected_seg(p)
        lit = [(lo, hi, v) for lo, hi, v in slots[p] if v != R.SEG_BLANK]
        if exp is None:
            if lit:
                lo, _hi, v = lit[0]
                return ctx.result(chk, False, f"digit AN{p} should be blank but showed '{R.glyph_char(v)}' at cycle {lo}",
                                  expected="blank", observed=R.glyph_char(v), cycle=lo, seg=f"0x{v:02x}")
            shown[p] = " "
            continue
        if not slots[p]:
            return ctx.result(chk, False, f"digit AN{p} was never selected in cycles {frm}..{to}; expected "
                              f"'{R.glyph_char(exp)}'", expected=R.glyph_char(exp), observed="never driven", cycle=frm)
        wrong = [(lo, hi, v) for lo, hi, v in slots[p] if v != exp]
        if wrong:
            lo, _hi, v = wrong[0]
            return ctx.result(chk, False, f"digit AN{p} showed '{R.glyph_char(v)}' (seg=0x{v:02x}) at cycle {lo}; "
                              f"expected '{R.glyph_char(exp)}' (seg=0x{exp:02x})", expected=R.glyph_char(exp),
                              observed=R.glyph_char(v), cycle=lo)
        starts = [frm] + [lo for lo, _hi, _v in slots[p]] + [to]
        gap = max(b - a for a, b in zip(starts, starts[1:]))
        if gap > m.max_frame_cycles():
            return ctx.result(chk, False, f"digit AN{p} went {gap} cycles without being refreshed; at most "
                              f"{m.max_frame_cycles()} ({R.MIN_FRAME_HZ} Hz at {ctx.clk_hz} Hz) avoids visible flicker",
                              expected=m.max_frame_cycles(), observed=gap, cycle=frm)
        shown[p] = R.glyph_char(exp)
    text = "".join(shown[p] for p in (3, 2, 1, 0))
    return ctx.result(chk, True, f"the display shows '{text}' (AN3..AN0) throughout cycles {frm}..{to}, one digit "
                      f"at a time, each refreshed within {m.max_frame_cycles()} cycles", expected=m.text(), observed=text)


# --------------------------------------------------------------------------- entry point


_CHECKS: dict[str, Callable[[_Ctx, CheckSpec], CheckResult]] = {
    "idle_off": _idle_off,
    "value_at": _value_at,
    "hold": _hold,
    "steady_rate": _steady_rate,
    "ratio": _ratio,
    "wrap": _wrap,
    "sequence": _sequence,
    "first_change_after": _first_change_after,
    "static_map": _static_map,
    "change_count": _change_count,
    "fsm_sequence": _fsm_sequence,
    "fsm_duration": _fsm_duration,
    "display": _display,
}


def evaluate_scenario(spec: DesignSpec, plan: TestPlan, scenario: Scenario, trace: Trace) -> list[CheckResult]:
    """Evaluate every check of `scenario` against the simulated `trace` (expectations from the spec only)."""
    ctx = _Ctx(spec, plan, scenario, trace)
    out: list[CheckResult] = []
    if trace.end_cycle < scenario.end_cycle:
        out.append(CheckResult(id="trace_complete", scenario_id=scenario.id,
                               requirement_ids=list(scenario.requirement_ids), passed=False,
                               summary=f"simulation stopped at cycle {trace.end_cycle} before the scenario end "
                                       f"{scenario.end_cycle} (does the design call $finish/$stop?)",
                               expected=scenario.end_cycle, observed=trace.end_cycle,
                               first_failure_cycle=trace.end_cycle))
    for chk in scenario.checks:
        fn = _CHECKS.get(chk.kind)
        if fn is None:
            out.append(CheckResult(id=chk.id, scenario_id=scenario.id, requirement_ids=list(chk.requirement_ids),
                                   passed=False, summary=f"unknown check kind '{chk.kind}' (oracle bug)"))
            continue
        try:
            out.append(fn(ctx, chk))
        except Exception as e:  # an oracle bug must never look like a passing design
            out.append(CheckResult(id=chk.id, scenario_id=scenario.id, requirement_ids=list(chk.requirement_ids),
                                   passed=False, summary=f"oracle error in check '{chk.kind}': {type(e).__name__}: {e}",
                                   details={"oracle_error": True}))
    return out
