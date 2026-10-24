"""Formal property check with Yosys's built-in SAT solver.

SymbiYosys and external SMT solvers are not required: Yosys's `sat` command
(MiniSAT) runs a bounded check plus temporal induction over the case's trusted
property wrapper (immediate assert/assume statements read with
`read_verilog -formal`). Outcomes:

  proven          the induction step succeeded: the asserted properties hold in
                  every reachable state (under the wrapper's assumptions)
  counterexample  the base case found a violating trace (VCD + input table kept)
  bounded         no violation within max_steps, but induction did not close;
                  this is NOT a proof
  error / not_run tool or input problems

A proof covers only the wrapper's assertions and assumptions, never "the chip".
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from backend.core.process import LineCallback, run_command
from backend.security.cases import CaseManifest
from backend.security.hostsafety import scan_source
from backend.security.verify import CommandRecord, display_command

FormalStatus = Literal["proven", "counterexample", "bounded", "error", "not_run"]

_STEP_RE = re.compile(r"^\s*(?P<t>\d+)\s+\\(?P<sig>\S+)\s+(?P<dec>\S+)\s+(?P<hex>\S+)\s+(?P<bin>\S+)\s*$")


class FormalResult(BaseModel):
    label: str
    case_id: str
    status: FormalStatus
    message: str = ""
    engine: str = "yosys sat (MiniSAT): bounded check + temporal induction"
    properties: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    max_steps: int = 0
    induction_length: Optional[int] = None
    counterexample: list[dict] = Field(default_factory=list)  # [{step, signals: {name: value}}]
    vcd: Optional[str] = None
    log: Optional[str] = None
    command: Optional[CommandRecord] = None
    tool_version: Optional[str] = None
    duration_s: float = 0.0


def yosys_bin() -> str | None:
    return shutil.which("yosys") or next((p for p in ("/opt/homebrew/bin/yosys", "/usr/local/bin/yosys")
                                          if Path(p).is_file()), None)


async def yosys_version() -> str | None:
    b = yosys_bin()
    if not b:
        return None
    proc = await run_command([b, "-V"], timeout_s=20)
    return proc.output[0].strip() if proc.ok and proc.output else None


def _parse_cex(lines: list[str]) -> list[dict]:
    """Input table of the last trace Yosys printed (the failing base case)."""
    start = max((i for i, ln in enumerate(lines) if "Time Signal Name" in ln), default=-1)
    if start < 0:
        return []
    steps: dict[int, dict[str, str]] = {}
    for ln in lines[start + 1:]:
        m = _STEP_RE.match(ln)
        if m:
            steps.setdefault(int(m["t"]), {})[m["sig"]] = m["hex"]
        elif steps and ln.strip() and not set(ln.strip()) <= {"-", " "}:
            break
    return [{"step": t, "signals": sig} for t, sig in sorted(steps.items())]


async def check(case: CaseManifest, sources: list[Path], out_dir: Path, *, label: str,
                on_line: LineCallback | None = None) -> FormalResult:
    t0 = time.monotonic()
    res = FormalResult(label=label, case_id=case.case_id, status="not_run")
    if case.formal is None:
        res.message = "this case has no formal property wrapper"
        return res
    res.properties = list(case.formal.properties)
    res.max_steps = case.formal.max_steps
    res.assumptions = ["reset is asserted in the first cycle", "all registers start at zero before that reset"]
    yb = yosys_bin()
    if not yb:
        res.message = "yosys not found"
        return res
    res.tool_version = await yosys_version()
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    for s in sources:  # the verifier already scanned these; formal is also reachable on its own
        if issues := scan_source(s.name, s.read_text()):
            res.status = "error"
            res.message = f"host-safety scan rejected {s.name}: {issues[0].message}"
            return res
    wrapper = case.path(case.formal.wrapper)
    reads = "; ".join(f"read_verilog -formal {p}" for p in [*map(str, sources), str(wrapper)])
    script = (f"{reads}; prep -top {case.formal.top}; flatten; chformal -lower; opt_clean; "
              f"sat -tempinduct -prove-asserts -set-assumes -set-init-zero -seq 1 "
              f"-maxsteps {int(case.formal.max_steps)} -show-inputs -dump_vcd cex.vcd {case.formal.top}")
    argv = [yb, "-q", "-l", "formal.log", "-p", script]
    proc = await run_command(argv, cwd=out_dir, timeout_s=300, on_line=on_line)
    res.command = CommandRecord(step="formal", command=display_command(argv), exit_code=proc.exit_code,
                                duration_s=round(proc.duration_s, 3), timed_out=proc.timed_out, error=proc.error)
    log_path = out_dir / "formal.log"
    lines = log_path.read_text().splitlines() if log_path.is_file() else proc.output
    res.log = "formal.log" if log_path.is_file() else None
    text = "\n".join(lines)
    res.duration_s = round(time.monotonic() - t0, 3)
    m = re.findall(r"Trying induction with length (\d+)", text)
    if m:
        res.induction_length = int(m[-1])
    if proc.timed_out:
        res.status, res.message = "error", "yosys timed out"
    elif "Induction step proven: SUCCESS!" in text:
        res.status = "proven"
        res.message = f"properties proven by temporal induction (length {res.induction_length})"
    elif "model found for base case: FAIL!" in text or "failed with a counterexample" in text.lower():
        res.status = "counterexample"
        res.counterexample = _parse_cex(lines)
        res.message = f"counterexample found ({len(res.counterexample)} steps)"
        if (out_dir / "cex.vcd").is_file():
            res.vcd = "cex.vcd"
    elif "Reached maximum number of time steps" in text or "reached maximum" in text.lower():
        res.status = "bounded"
        res.message = f"no violation within {res.max_steps} steps; induction did not close (not a proof)"
    else:
        res.status = "error"
        err = next((ln for ln in lines if ln.startswith("ERROR")), None)
        res.message = err or (proc.error or f"yosys exited with {proc.exit_code}")
    return res
