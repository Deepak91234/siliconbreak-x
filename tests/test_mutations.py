"""Mutation tests: every applicable defect injected into the template designs must be rejected."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.rtl.templates import generate_core
from backend.rtl.wrapper import generate_wrapper
from backend.specifications.ir import DesignSpec
from backend.verification.mutations import MUTATIONS, Mutation, apply_mutation
from backend.verification.runner import run_simulation_suite

EXAMPLES = sorted(Path(__file__).resolve().parents[1].joinpath("examples").glob("*.json"))
SPECS = {p.stem: DesignSpec.model_validate(json.loads(p.read_text())) for p in EXAMPLES}
CONCURRENCY = 6


def _sources(spec: DesignSpec) -> dict[str, str]:
    return {"core": generate_core(spec), "wrapper": generate_wrapper(spec)}


def applicable(spec: DesignSpec) -> list[str]:
    src = _sources(spec)
    out = []
    for name, m in MUTATIONS.items():
        if not m.applies_to(spec.family):
            continue
        try:
            apply_mutation(name, src[m.target])
        except ValueError:
            continue
        out.append(name)
    return out


# --------------------------------------------------------------------------- pure (no Verilator)


def test_registry_shape() -> None:
    assert MUTATIONS
    for name, m in MUTATIONS.items():
        assert isinstance(m, Mutation) and m.name == name
        assert m.target in ("core", "wrapper")
        assert m.description
        assert isinstance(m.families, frozenset)
        assert m.families <= {"counter", "blinker", "led_pattern", "traffic_light"}


def test_unknown_and_inapplicable_raise() -> None:
    with pytest.raises(ValueError):
        apply_mutation("no_such_mutation", "module x; endmodule")
    for name in MUTATIONS:
        with pytest.raises(ValueError):
            apply_mutation(name, "module cl_core(input wire clk); endmodule\n")


def test_every_mutation_applies_somewhere_and_changes_text() -> None:
    used: set[str] = set()
    for spec in SPECS.values():
        src = _sources(spec)
        for name in applicable(spec):
            m = MUTATIONS[name]
            out = apply_mutation(name, src[m.target])
            assert out != src[m.target]
            used.add(name)
    unused = set(MUTATIONS) - used - {"ignore_enable"}  # no example spec has an enable input
    assert not unused, f"mutations that apply to no example design: {sorted(unused)}"


def test_no_equivalent_constant_edits() -> None:
    """Mutations of constant-tied signals would be equivalent mutants and must not apply."""
    core = ("wire hold_reset = 1'b0;\nwire run = 1'b1;\n"
            "cl_tick_div u (.clk(clk), .clear(hold_reset), .enable(run), .period(period), .tick(step));\n")
    for name in ("ignore_reset", "ignore_enable", "no_timer_restart"):
        with pytest.raises(ValueError):
            apply_mutation(name, core)


def test_ignore_enable_applies_to_enable_driven_run() -> None:
    out = apply_mutation("ignore_enable", "wire          run = en_sw;\n")
    assert "| 1'b1" in out and "en_sw" in out


# --------------------------------------------------------------------------- Verilator: mutants are rejected


async def _run_mutant(spec: DesignSpec, name: str, root: Path, sem: asyncio.Semaphore) -> tuple[str, dict]:
    m = MUTATIONS[name]
    src = _sources(spec)
    src[m.target] = apply_mutation(name, src[m.target])
    ws = root / name
    (ws / "rtl").mkdir(parents=True)
    (ws / "rtl" / "cl_core.v").write_text(src["core"])
    (ws / "rtl" / "basys3_top.v").write_text(src["wrapper"])
    async with sem:
        rep = await run_simulation_suite(spec, [ws / "rtl" / "basys3_top.v", ws / "rtl" / "cl_core.v"], ws,
                                         fail_fast=True, waveforms=False)
    return name, rep.model_dump()


@pytest.mark.verilator
@pytest.mark.parametrize("key", sorted(SPECS))
async def test_applicable_mutants_are_rejected(key: str, tmp_path: Path) -> None:
    spec = SPECS[key]
    names = applicable(spec)
    assert names, f"no mutation applies to {key}"
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(_run_mutant(spec, n, tmp_path, sem) for n in names))
    survivors = []
    for name, rep in results:
        assert rep["status"] in ("failed", "passed", "error"), (name, rep["status"])
        assert rep["status"] != "error", f"{name}: infrastructure error {rep['diagnostics']}"
        lint_failed = bool(rep["diagnostics"].get("lint_errors"))
        if rep["status"] == "passed" or not (rep["failed_checks"] >= 1 or lint_failed):
            survivors.append(name)
            continue
        assert rep["status"] == "failed"
        if not lint_failed:
            assert rep["diagnostics"]["failing_checks"], name
            assert all(f["summary"] for f in rep["diagnostics"]["failing_checks"])
    assert not survivors, f"{key}: mutants not detected by the oracle: {survivors}"
