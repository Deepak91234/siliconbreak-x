"""Basys 3 before/after builds for security cases that ship a trusted board wrapper.

A variant is the case's vulnerable source, or the patched copy from a repair
attempt that the trusted verifier accepted (an agent patch, or the human
reference fix, labelled as such). For each variant:

  board_sim  the full board top (trusted wrapper + SiliconBreak X library + the
             variant's RTL) runs the case's physical demo sequence in SiliconBreak X's
             generic Verilator harness, with debounce shortened by parameter. Each
             step's simulated LED pattern is compared with the pattern the contract
             requires. This is simulation, never a hardware observation.
  build      Vivado synth/impl/timing/bitstream through the existing batch flow in
             the user's container (backend/vivado/build.py), with pins from the
             trusted board definition (board_xdc).

Programming reuses the existing SHA-bound program_fpga approval and the
hardware manager, which re-verifies the approval and the file hash.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from backend.core.process import LineCallback
from backend.rtl.wrapper import board_xdc, library_paths
from backend.security.cases import CaseManifest, DemoStep, sha256_file
from backend.security.verify import display_command
from backend.specifications.board import load_board
from backend.verification import verilator as vl

SIM_DEBOUNCE = 4        # wrapper DEBOUNCE_CYCLES in simulation
POR_CYCLES = 40         # power-on reset (16) + input synchronizers, with margin
SETTLE = 24             # switches change, then wait for sync + debounce
HOLD = 40               # button held this long; LEDs sampled at the end of the hold
GAP = 24                # released before the next step

_PORTS = {"BTNU": "btnU", "BTNC": "btnC", "BTNL": "btnL", "BTNR": "btnR", "BTND": "btnD"}


def led_text(v: int) -> str:
    return f"0x{v:04X}"


def stimulus_for(steps: list[DemoStep]) -> tuple[str, list[tuple[DemoStep, int, int]]]:
    """Harness stimulus for the demo steps -> (text, [(step, press_cycle, sample_cycle)])."""
    lines = ["# SiliconBreak X board-level demo sequence (generated from the case manifest)"]
    t = POR_CYCLES
    sw = 0
    plan = []
    lines.append(f"{t} sw 0x0")
    for st in steps:
        for name, val in st.set.items():
            bit = int(name[2:])
            sw = (sw | (1 << bit)) if val else (sw & ~(1 << bit))
        lines.append(f"{t} sw {hex(sw)}")
        press = t + SETTLE
        sample = press + HOLD
        if st.press:
            lines.append(f"{press} {_PORTS[st.press]} 1")
            lines.append(f"{sample + 1} {_PORTS[st.press]} 0")
        plan.append((st, press, sample))
        t = sample + 1 + GAP
    lines.insert(1, f"end {t + 4}")
    return "\n".join(lines) + "\n", plan


def board_sources(case: CaseManifest, candidates: dict[str, Path], ws: Path) -> list[Path]:
    """Copy the trusted wrapper and the variant's RTL into ws (library files stay in place)."""
    src = ws / "src"
    if src.exists():
        shutil.rmtree(src)
    src.mkdir(parents=True)
    wrapper = src / "basys3_top.v"
    shutil.copyfile(case.path(case.board.wrapper), wrapper)
    out = [wrapper]
    for rel in case.editable:
        dst = src / Path(rel).name
        shutil.copyfile(candidates[rel], dst)
        out.append(dst)
    return out + library_paths()


async def board_sim(case: CaseManifest, candidates: dict[str, Path], out_dir: Path, *,
                    on_line: LineCallback | None = None) -> dict:
    res: dict = {"status": "error", "message": "", "steps": [], "commands": [], "lint_ok": None,
                 "note": "Verilator simulation of the full Basys 3 top (debounce shortened to "
                         f"{SIM_DEBOUNCE} cycles); not a hardware observation."}
    if case.board is None or not case.board.demo:
        res.update(status="not_run", message="this case has no board demo sequence")
        return res
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        srcs = board_sources(case, candidates, out_dir)
        params = {"DEBOUNCE_CYCLES": SIM_DEBOUNCE}
        lint = await vl.lint(srcs, out_dir, params=params, on_line=on_line)
        res["commands"].append({"step": "lint", "command": display_command(lint.process.argv), "exit_code": lint.process.exit_code})
        res["lint_ok"] = lint.ok
        if not lint.ok:
            res["message"] = "board-level lint failed: " + "; ".join(m.message for m in lint.errors)[:300]
            return res
        build = await vl.build_harness(srcs, out_dir, params, out_dir / "obj", trace=True, on_line=on_line)
        res["commands"].append({"step": "build", "command": display_command(build.process.argv), "exit_code": build.process.exit_code})
        if not build.ok or build.binary is None:
            res["message"] = "board-level harness build failed"
            return res
        text, plan = stimulus_for(case.board.demo)
        stim = out_dir / "demo_stimulus.txt"
        stim.write_text(text)
        end = max(s for _, _, s in plan) + GAP
        run = await vl.run_harness(build.binary, out_dir, stim, out_dir / "trace.txt",
                                   vcd=(out_dir / "board.vcd", 0, end), on_line=on_line)
        res["commands"].append({"step": "simulate", "command": display_command(run.process.argv), "exit_code": run.process.exit_code})
        if not run.ok:
            res["message"] = "board-level simulation failed"
            return res
        trace = vl.parse_trace(run.trace_path)
        all_ok = True
        for st, press, sample in plan:
            got = trace.value_at("led", sample)
            want = int(st.expect_leds, 16)
            ok = got == want
            all_ok &= ok
            res["steps"].append({"id": st.id, "title": st.title, "set": st.set, "press": st.press,
                                 "expected": led_text(want), "expected_text": st.expect_text,
                                 "simulated": led_text(got), "matches_contract": ok, "press_cycle": press,
                                 "sample_cycle": sample})
        res["status"] = "matches_contract" if all_ok else "differs_from_contract"
        n_bad = sum(not s["matches_contract"] for s in res["steps"])
        res["message"] = ("every demo step matches the contract" if all_ok else
                          f"{n_bad} demo step(s) differ from the contract (the LEDs would show it)")
        res["vcd"] = "board.vcd"
    except vl.VerilatorUnavailable as e:
        res.update(status="not_run", message=str(e))
    return res


def build_id_for(audit_id: str, variant: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", f"sb-{audit_id}-{variant}".lower())[:63]


def write_xdc(out_dir: Path) -> Path:
    xdc = out_dir / "constraints.xdc"
    xdc.write_text(board_xdc(load_board("basys3")))
    return xdc


def bitstream_info(res) -> dict:
    return {"status": res.status, "ok": res.ok, "message": res.message, "failed_stage": res.failed_stage,
            "timing": res.timing, "utilization": (res.utilization or {}).get("summary"),
            "bitstream_path": res.bitstream_path, "bitstream_sha256": res.bitstream_sha256,
            "bitstream_bytes": (res.bitstream or {}).get("size_bytes"), "checks": res.checks,
            "duration_s": res.duration_s, "vivado_version": res.vivado_version, "container": res.container}


def verify_bitstream_file(path: str | None, sha: str | None) -> bool:
    return bool(path and sha and Path(path).is_file() and sha256_file(Path(path)) == sha)
