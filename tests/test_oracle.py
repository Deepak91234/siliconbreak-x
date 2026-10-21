"""Simulation oracle: unit tests on hand-built traces + full Verilator runs of the template designs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.rtl.templates import generate_core
from backend.rtl.wrapper import generate_wrapper
from backend.specifications.ir import DesignSpec
from backend.verification import verilator as V
from backend.verification.oracle import CheckResult, evaluate_scenario
from backend.verification.runner import SimulationReport, run_simulation_suite
from backend.verification.testgen import CheckSpec, TestPlan, generate_test_plan
from backend.verification.waveform import vcd_to_json

EXAMPLES = sorted(Path(__file__).resolve().parents[1].joinpath("examples").glob("*.json"))
SPECS = {p.stem: DesignSpec.model_validate(json.loads(p.read_text())) for p in EXAMPLES}
CLK = 10_000


# --------------------------------------------------------------------------- hand-built traces (test doubles)


def led_of(spec: DesignSpec, value: int) -> int:
    out = spec.output(spec.behavior.output)
    return sum(((value >> k) & 1) << idx for k, idx in enumerate(out.indices))


def make_trace(spec: DesignSpec, points: list[tuple[int, int]], end: int, extra_led: int = 0) -> V.Trace:
    """Trace whose behaviour output takes value v from cycle c for each (c, v)."""
    changes = [(c, led_of(spec, v) | extra_led) for c, v in points]
    return V.Trace(end_cycle=end, changes={"led": changes})


def plan_and_scenario(key: str, sid: str) -> tuple[DesignSpec, TestPlan, object]:
    spec = SPECS[key]
    plan = generate_test_plan(spec, CLK)
    sc = next(s for s in plan.scenarios if s.id == sid)
    return spec, plan, sc


def by_kind(results: list[CheckResult]) -> dict[str, list[CheckResult]]:
    out: dict[str, list[CheckResult]] = {}
    for r in results:
        out.setdefault(r.id.split("_", 1)[1] if "_" in r.id else r.id, []).append(r)
    return out


def counter_points(start: int, period: int, n: int, first: int = 0, step: int = 1, width: int = 4,
                   offset: int = 1) -> list[tuple[int, int]]:
    pts = [(0, start)]
    v = start
    for k in range(1, n + 1):
        v = (v + step) % (1 << width)
        pts.append((first + k * period + offset, v))
    return pts


def test_ideal_counter_startup_passes() -> None:
    spec, plan, sc = plan_and_scenario("demo1_counter_spec", "startup")
    assert not sc.events  # power-on in the default mode, no stimulus
    T = round(CLK / 1)  # 'normal' mode: 1 step/s, computed here independently
    res = evaluate_scenario(spec, plan, sc, make_trace(spec, counter_points(0, T, 4), sc.end_cycle))
    assert res and all(r.passed for r in res), [r.summary for r in res if not r.passed]
    kinds = {r.id.split("_", 1)[1] for r in res}
    assert {"value_at", "first_change_after", "steady_rate", "sequence", "idle_off"} <= kinds


def test_counter_wrong_rate_fails() -> None:
    spec, plan, sc = plan_and_scenario("demo1_counter_spec", "startup")
    T = CLK
    res = by_kind(evaluate_scenario(spec, plan, sc, make_trace(spec, counter_points(0, T + 60, 4), sc.end_cycle)))
    rate = res["steady_rate"][0]
    assert not rate.passed and rate.expected == T and rate.first_failure_cycle is not None
    assert "10000 ± 20" in rate.summary
    assert not res["first_change_after"][0].passed
    assert res["sequence"][0].passed  # the step rule itself is fine


def test_counter_illegal_step_fails_with_cycle() -> None:
    spec, plan, sc = plan_and_scenario("demo1_counter_spec", "startup")
    pts = counter_points(0, CLK, 4)
    pts[3] = (pts[3][0], pts[3][1] + 1)  # 0,1,2,4,... skips 3
    res = by_kind(evaluate_scenario(spec, plan, sc, make_trace(spec, pts, sc.end_cycle)))
    seq = res["sequence"][0]
    assert not seq.passed and seq.first_failure_cycle == pts[3][0]
    assert "illegal transition" in seq.summary and seq.details.get("trace_excerpt")


def test_counter_wrong_power_on_and_lit_unused_led_fail() -> None:
    spec, plan, sc = plan_and_scenario("demo1_counter_spec", "startup")
    res = by_kind(evaluate_scenario(spec, plan, sc, make_trace(spec, counter_points(5, CLK, 4), sc.end_cycle)))
    assert not res["value_at"][0].passed and res["value_at"][0].first_failure_cycle == 0
    res = by_kind(evaluate_scenario(spec, plan, sc, make_trace(spec, counter_points(0, CLK, 4), sc.end_cycle,
                                                                extra_led=1 << 15)))
    assert not res["idle_off"][0].passed
    assert all(r.passed for k, rs in res.items() if k != "idle_off" for r in rs)


def traffic_points(spec: DesignSpec, end: int, stretch: dict[str, int] | None = None,
                   order: list[int] | None = None) -> list[tuple[int, int]]:
    states = spec.behavior.states
    order = order or list(range(len(states)))
    pts, t = [], 0
    while t <= end:
        for i in order:
            st = states[i]
            pts.append((t, st.lights))
            t += round(CLK * st.duration_s) + (stretch or {}).get(st.name, 0)
    return pts


def test_ideal_traffic_cycle_passes() -> None:
    spec, plan, sc = plan_and_scenario("traffic_light_spec", "fsm_cycle")
    res = evaluate_scenario(spec, plan, sc, make_trace(spec, traffic_points(spec, sc.end_cycle), sc.end_cycle))
    assert all(r.passed for r in res), [r.summary for r in res if not r.passed]
    durations = [r for r in res if r.id.endswith("fsm_duration")]
    assert len(durations) == 3 and all(r.observed for r in durations)


def test_traffic_wrong_duration_and_order_fail() -> None:
    spec, plan, sc = plan_and_scenario("traffic_light_spec", "fsm_cycle")
    res = evaluate_scenario(spec, plan, sc, make_trace(spec, traffic_points(spec, sc.end_cycle, {"yellow": 300}),
                                                       sc.end_cycle))
    failed = {r.id.split("_", 1)[1] for r in res if not r.passed}
    assert "fsm_sequence" in failed
    assert any(not r.passed and "yellow lasted" in r.summary for r in res)
    res = evaluate_scenario(spec, plan, sc, make_trace(spec, traffic_points(spec, sc.end_cycle, order=[0, 2, 1]),
                                                       sc.end_cycle))
    seq = next(r for r in res if r.id.endswith("fsm_sequence"))
    assert not seq.passed and seq.first_failure_cycle == 40_000


def mirror_points(spec: DesignSpec, sc, delay: int, swap: bool = False) -> list[tuple[int, int]]:
    deb = round(CLK * 10 / 1000)
    pts = [(0, 0)]
    for e in sorted(sc.events, key=lambda e: e.cycle):
        assert e.port == "sw"
        v = e.value & 0xFF
        if swap:
            v = (v & ~0b11) | ((v & 1) << 1) | ((v >> 1) & 1)
        pts.append((e.cycle + 1 + deb + delay, v))
    return pts


def test_mirror_static_map() -> None:
    spec, plan, sc = plan_and_scenario("led_mirror_spec", "static_walk")
    ok = evaluate_scenario(spec, plan, sc, make_trace(spec, mirror_points(spec, sc, 1), sc.end_cycle))
    assert all(r.passed for r in ok), [r.summary for r in ok if not r.passed]
    late = evaluate_scenario(spec, plan, sc, make_trace(spec, mirror_points(spec, sc, 40), sc.end_cycle))
    assert any(not r.passed and r.id.endswith("static_map") for r in late)
    swapped = evaluate_scenario(spec, plan, sc, make_trace(spec, mirror_points(spec, sc, 1, swap=True), sc.end_cycle))
    assert any(not r.passed and r.id.endswith("static_map") for r in swapped)


def test_short_trace_and_unknown_check_fail_safe() -> None:
    spec, plan, sc = plan_and_scenario("demo1_counter_spec", "startup")
    short = make_trace(spec, counter_points(0, CLK, 2), sc.end_cycle // 2)
    res = evaluate_scenario(spec, plan, sc, short)
    assert res[0].id == "trace_complete" and not res[0].passed
    weird = sc.model_copy(update={"checks": [CheckSpec(id="c99_bogus", kind="bogus", requirement_ids=["R-DIR"])]})
    res = evaluate_scenario(spec, plan, weird, make_trace(spec, counter_points(0, CLK, 4), sc.end_cycle))
    assert len(res) == 1 and not res[0].passed and "unknown check" in res[0].summary


def test_expectations_ignore_plan_params() -> None:
    """Corrupting the TICKS parameters in the plan must not change any expectation."""
    spec, plan, sc = plan_and_scenario("demo1_counter_spec", "startup")
    trace = make_trace(spec, counter_points(0, CLK, 4), sc.end_cycle)
    bad_plan = plan.model_copy(update={"params": {k: v * 3 for k, v in plan.params.items()}})
    a = [r.model_dump() for r in evaluate_scenario(spec, plan, sc, trace)]
    b = [r.model_dump() for r in evaluate_scenario(spec, bad_plan, sc, trace)]
    assert a == b and all(r["passed"] for r in a)


async def test_unavailable_without_verilator(monkeypatch, tmp_path: Path) -> None:
    def missing() -> str:
        raise V.VerilatorUnavailable("verilator not found")

    monkeypatch.setattr(V, "verilator_bin", missing)
    spec = SPECS["demo1_counter_spec"]
    rep = await run_simulation_suite(spec, [tmp_path / "basys3_top.v", tmp_path / "cl_core.v"], tmp_path)
    assert rep.status == "unavailable" and rep.passed_checks == 0 and rep.scenarios == []


# --------------------------------------------------------------------------- Verilator: template designs pass


def write_design(spec: DesignSpec, ws: Path, core: str | None = None) -> list[Path]:
    (ws / "rtl").mkdir(parents=True, exist_ok=True)
    (ws / "rtl" / "cl_core.v").write_text(core if core is not None else generate_core(spec))
    (ws / "rtl" / "basys3_top.v").write_text(generate_wrapper(spec))
    return [ws / "rtl" / "basys3_top.v", ws / "rtl" / "cl_core.v"]


@pytest.mark.verilator
@pytest.mark.parametrize("key", sorted(SPECS))
async def test_template_design_passes_oracle(key: str, tmp_path: Path) -> None:
    spec = SPECS[key]
    events: list[tuple[str, dict]] = []

    async def emit(kind: str, data: dict) -> None:
        events.append((kind, data))

    rep = await run_simulation_suite(spec, write_design(spec, tmp_path), tmp_path, emit=emit)
    assert isinstance(rep, SimulationReport)
    d = rep.model_dump()
    failing = [f["summary"] for f in d["diagnostics"].get("failing_checks", [])]
    assert rep.status == "passed", failing or d["diagnostics"]
    assert rep.failed_checks == 0 and rep.passed_checks > 0
    assert set(d["lint"]) == {"real", "sim"} and all(v["ok"] for v in d["lint"].values())
    assert d["diagnostics"]["lint_errors"] == [] and d["diagnostics"]["failing_checks"] == []
    assert d["plan"]["clk_hz"] == CLK
    assert [s["id"] for s in d["scenarios"]] == [s["id"] for s in d["plan"]["scenarios"]]
    for s in d["scenarios"]:
        assert s["passed"] and s["checks"] and all(c["passed"] and c["summary"] for c in s["checks"])
        assert (tmp_path / s["trace_file"]).is_file()
        if s["vcd_window"]:
            assert s["vcd_file"] and (tmp_path / s["vcd_file"]).is_file()
    assert sum(len(s["checks"]) for s in d["scenarios"]) == rep.passed_checks
    statuses = {r["id"]: r["sim_status"] for r in d["requirements"]}
    assert "failed" not in statuses.values() and "passed" in statuses.values()
    scen_events = [e for k, e in events if k == "scenario"]
    assert [e["id"] for e in scen_events] == [s["id"] for s in d["scenarios"]]
    assert all(e["passed"] for e in scen_events)
    assert any(k == "log" and "line" in e for k, e in events)
    json.dumps(d, default=str)  # the orchestrator stores the report as JSON

    with_vcd = next(s for s in d["scenarios"] if s["vcd_file"])
    wf = vcd_to_json(tmp_path / with_vcd["vcd_file"], signals=["TOP.led"])
    assert wf["time_unit"] == "half_cycle" and wf["signals"][0]["name"] == "TOP.led"


@pytest.mark.verilator
async def test_lint_failure_is_reported(tmp_path: Path) -> None:
    spec = SPECS["demo1_counter_spec"]
    broken = generate_core(spec).replace("endmodule", "assign oops = ;\nendmodule", 1)
    rep = await run_simulation_suite(spec, write_design(spec, tmp_path, core=broken), tmp_path)
    assert rep.status == "failed"
    assert rep.diagnostics["lint_errors"] and rep.scenarios == []
    assert not all(v["ok"] for v in rep.lint.values())
