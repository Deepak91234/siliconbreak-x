"""Phase 1 proof: spec -> trusted wrapper + template core -> Verilator lint + sim -> Vivado -> .bit.

Runs the real tools (no mocks). Usage:
    .venv/bin/python scripts/phase1_pipeline.py [examples/demo1_counter_spec.json]
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import get_settings  # noqa: E402
from backend.rtl.templates import generate_core  # noqa: E402
from backend.rtl.wrapper import generate_wrapper, generate_xdc, library_paths  # noqa: E402
from backend.specifications.ir import DesignSpec  # noqa: E402
from backend.specifications.timing import param_dict  # noqa: E402
from backend.verification import verilator as V  # noqa: E402
from backend.vivado import build as vb  # noqa: E402
from backend.vivado import container as ctr  # noqa: E402


async def main(spec_path: str) -> int:
    s = get_settings()
    spec = DesignSpec.model_validate(json.loads(Path(spec_path).read_text()))
    run_id = time.strftime("phase1-%Y%m%d-%H%M%S").lower()
    ws = (s.runs_dir / "phase1" / run_id).resolve()
    (ws / "rtl").mkdir(parents=True)
    (ws / "spec.json").write_text(spec.model_dump_json(indent=2))
    (ws / "rtl" / "basys3_top.v").write_text(generate_wrapper(spec))
    (ws / "rtl" / "cl_core.v").write_text(generate_core(spec))
    (ws / "rtl" / "constraints.xdc").write_text(generate_xdc(spec))
    srcs = [ws / "rtl" / "basys3_top.v", ws / "rtl" / "cl_core.v"] + library_paths()
    print(f"[phase1] workspace {ws}")

    env = await ctr.detect()
    print(f"[phase1] vivado env ready={env.ready} container={env.active_container} version={env.vivado_version} "
          f"gui={env.gui_session_running} hw_server={env.hw_server_running} problems={env.problems}")

    lint_real = await V.lint(srcs, ws, param_dict(spec, spec.clock_hz))
    sim_p = param_dict(spec, s.sim_clk_hz)
    lint_sim = await V.lint(srcs, ws, sim_p)
    print(f"[phase1] lint real-params ok={lint_real.ok} sim-params ok={lint_sim.ok} messages={len(lint_real.messages) + len(lint_sim.messages)}")
    if not (lint_real.ok and lint_sim.ok):
        for m in lint_real.messages + lint_sim.messages:
            print("   ", m.severity, m.code, m.file, m.line, m.message)
        return 1

    build = await V.build_harness(srcs, ws, sim_p, ws / "sim" / "obj")
    print(f"[phase1] harness build ok={build.ok} ({build.process.duration_s:.1f}s)")
    if not build.ok:
        print(build.process.text[-2000:])
        return 1
    clk = s.sim_clk_hz
    stim = ws / "sim" / "stim.txt"
    stim.write_text("\n".join([f"end {20 * clk}", f"{6 * clk} btnU 1", f"{9 * clk} btnU 0"]) + "\n")
    run = await V.run_harness(build.binary, ws, stim, ws / "sim" / "trace.txt", vcd=(ws / "sim" / "wave.vcd", 5 * clk, 7 * clk))
    tr = V.parse_trace(ws / "sim" / "trace.txt")
    led = tr.changes["led"]
    steps_ok = all(((b[1] - a[1]) & 0xF) == 1 for a, b in zip(led, led[1:]))
    iv = [b[0] - a[0] for a, b in zip(led, led[1:])]
    print(f"[phase1] sim ok={run.ok} changes={len(led)} all_steps_plus_one={steps_ok} intervals={iv}")

    if not env.ready:
        print("[phase1] Vivado not ready; stopping before implementation")
        return 2
    t0 = time.monotonic()

    async def on_stage(stage, status):
        print(f"[vivado] {stage} {status} (+{time.monotonic() - t0:.0f}s)", flush=True)

    res = await vb.run_build(run_id, srcs, ws / "rtl" / "constraints.xdc", ws / "vivado", on_stage=on_stage)
    print(f"[phase1] vivado status={res.status} ok={res.ok} duration={res.duration_s}s stage_times={res.stage_times}")
    for c in res.checks:
        print(f"   check {c['name']:<22} {'PASS' if c['passed'] else 'FAIL'}  {c['detail']}")
    if res.timing:
        print(f"   timing WNS={res.timing['wns_ns']} WHS={res.timing['whs_ns']} met={res.timing['timing_met']} clocks={res.timing['clocks']}")
    if res.utilization:
        print("   utilization", json.dumps(res.utilization["summary"]))
    print(f"   bitstream {res.bitstream_path} sha256={res.bitstream_sha256}")
    if not res.ok:
        print("   message:", res.message[:2000])
    return 0 if res.ok else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "examples/demo1_counter_spec.json")))
