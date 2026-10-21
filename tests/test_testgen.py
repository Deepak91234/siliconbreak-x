"""Spec-derived test plans (no Verilator needed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.specifications.derive import derive_requirements
from backend.specifications.ir import DesignSpec
from backend.specifications.timing import param_dict
from backend.verification import reference as R
from backend.verification.oracle import _CHECKS
from backend.verification.testgen import VCD_MAX_CYCLES, TestPlanError, generate_test_plan

EXAMPLES = sorted(Path(__file__).resolve().parents[1].joinpath("examples").glob("*.json"))
SPECS = {p.stem: DesignSpec.model_validate(json.loads(p.read_text())) for p in EXAMPLES}
SIM_CLK = 10_000


@pytest.mark.parametrize("key", sorted(SPECS))
@pytest.mark.parametrize("clk", [SIM_CLK, 20_000])
def test_plan_is_consistent(key: str, clk: int) -> None:
    spec = SPECS[key]
    plan = generate_test_plan(spec, clk)
    assert plan.clk_hz == clk and plan.spec_title == spec.title
    assert plan.params == param_dict(spec, clk)
    assert plan.scenarios
    ids = [s.id for s in plan.scenarios]
    assert len(ids) == len(set(ids))
    req_ids = {r.id for r in derive_requirements(spec)}
    for sc in plan.scenarios:
        assert sc.checks, sc.id
        assert set(sc.requirement_ids) <= req_ids
        assert all(0 <= e.cycle <= sc.end_cycle for e in sc.events)
        if sc.vcd_window is not None:
            a, b = sc.vcd_window
            assert 0 <= a <= b <= sc.end_cycle and b - a <= VCD_MAX_CYCLES
        cids = [c.id for c in sc.checks]
        assert len(cids) == len(set(cids))
        for c in sc.checks:
            assert c.kind in _CHECKS, c.kind
            assert c.requirement_ids and set(c.requirement_ids) <= req_ids
            for k in ("frm", "to", "anchor"):
                if k in c.params:
                    assert 0 <= int(c.params[k]) <= sc.end_cycle


@pytest.mark.parametrize("key", sorted(SPECS))
def test_every_simulation_requirement_is_checked(key: str) -> None:
    spec = SPECS[key]
    plan = generate_test_plan(spec, SIM_CLK)
    tagged = {r for s in plan.scenarios for c in s.checks for r in c.requirement_ids}
    missing = [r.id for r in derive_requirements(spec) if "simulation" in r.methods and r.id not in tagged]
    assert not missing


@pytest.mark.parametrize("key", sorted(SPECS))
def test_plan_is_deterministic(key: str) -> None:
    a = generate_test_plan(SPECS[key], SIM_CLK).model_dump()
    b = generate_test_plan(SPECS[key], SIM_CLK).model_dump()
    assert a == b


@pytest.mark.parametrize("key", sorted(SPECS))
def test_stimulus_conditions_as_planned(key: str) -> None:
    """The ideal conditioner sees bursts as no change and clean presses after the debounce time."""
    spec = SPECS[key]
    plan = generate_test_plan(spec, SIM_CLK)
    for sc in plan.scenarios:
        tl = R.InputTimeline(spec, SIM_CLK, sc.events)
        for name, changes in tl.changes.items():
            deb = tl.debounce[name]
            for ch in changes:
                assert ch.eff_cycle == ch.raw_cycle + 1 + deb
            levels = [ch.level for ch in changes]
            assert all(x != y for x, y in zip(levels, levels[1:])), (sc.id, name)


def test_periods_come_from_physical_values() -> None:
    """Expected cycle counts are recomputed from Hz / seconds / ms, not read from TICKS parameters."""
    assert R.cycles_per_step(10_000, 3) == 3333
    assert R.cycles_per_step(10_000, 1) == 10_000
    assert R.cycles_per_toggle(10_000, 1) == 5000
    assert R.cycles_for_duration(10_000, 4) == 40_000
    assert R.debounce_cycles(10_000, 10) == 100
    assert R.tolerance(10_000) == 20 and R.tolerance(100) == 4
    assert R.within_tolerance(10_020, 10_000) and not R.within_tolerance(10_021, 10_000)


def test_tolerance_matches_contract() -> None:
    for exp in (10, 1000, 2000, 5000, 3333, 40_000):
        assert R.tolerance(exp) == max(4, 0.002 * exp)


def test_invalid_clock_rejected() -> None:
    with pytest.raises(TestPlanError):
        generate_test_plan(SPECS[sorted(SPECS)[0]], 0)


def test_checks_do_not_depend_on_plan_params() -> None:
    """Checks are identical whatever TICKS values a (possibly buggy) parameter generator produced."""
    from backend.verification import testgen

    spec = SPECS["demo1_counter_spec"]
    good = generate_test_plan(spec, SIM_CLK)
    orig = testgen.param_dict
    try:
        testgen.param_dict = lambda s, c: {k: v * 2 for k, v in orig(s, c).items()}  # type: ignore[assignment]
        bad = generate_test_plan(spec, SIM_CLK)
    finally:
        testgen.param_dict = orig  # type: ignore[assignment]
    assert bad.params != good.params
    assert [s.model_dump() for s in bad.scenarios] == [s.model_dump() for s in good.scenarios]
