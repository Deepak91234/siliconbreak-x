"""Opt-in: real Vivado build of Demo 1 in the existing container (pytest --run-vivado, ~2-3 min)."""

import json
import time
from pathlib import Path

import pytest

from backend.rtl.templates import generate_core
from backend.rtl.wrapper import generate_wrapper, generate_xdc, library_paths
from backend.specifications.ir import DesignSpec
from backend.vivado import build as vb
from backend.vivado import container as ctr

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.vivado
async def test_demo1_vivado_build(tmp_path):
    env = await ctr.detect()
    if not env.ready:
        pytest.skip("Vivado container not running: " + "; ".join(env.problems))
    spec = DesignSpec.model_validate(json.loads((ROOT / "examples" / "demo1_counter_spec.json").read_text()))
    rtl = tmp_path / "rtl"
    rtl.mkdir()
    (rtl / "basys3_top.v").write_text(generate_wrapper(spec))
    (rtl / "cl_core.v").write_text(generate_core(spec))
    (rtl / "constraints.xdc").write_text(generate_xdc(spec))
    stages = []
    res = await vb.run_build(f"pytest-{int(time.time())}", [rtl / "basys3_top.v", rtl / "cl_core.v"] + library_paths(),
                             rtl / "constraints.xdc", tmp_path / "vivado",
                             on_stage=lambda s, st: stages.append((s, st)))
    assert res.ok, res.message
    assert ("bitstream", "passed") in stages
    assert res.timing["timing_met"] and res.timing["clocks"][0]["period_ns"] == 10.0
    assert res.bitstream["valid"] and res.bitstream["part"] == "7a35tcpg236"
    assert all(c["passed"] for c in res.checks)
