"""Run a CircuitLoop Vivado build inside the existing Vivado container.

Flow: copy sources + XDC + Tcl into the shared workspace (bind mount) ->
`docker exec ... vivado -mode batch -source build.tcl` -> stream output ->
parse reports -> validate timing / DRC / critical warnings / bitstream header ->
copy artifacts back into the CircuitLoop run directory.

Only one CircuitLoop Vivado build may run at a time (in-process asyncio lock +
an exclusive fcntl lock file in the shared workspace, so two backend processes
also cannot overlap).
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from backend.core.config import Settings, get_settings
from backend.specifications.board import load_board
from backend.vivado import container as ctr
from backend.vivado import reports as rpt

TCL_TEMPLATE = Path(__file__).resolve().parent / "tcl" / "build.tcl"
BUILD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
FLOW_STAGES = ("read", "synth", "opt", "place", "route", "reports", "bitstream")

# Critical warnings that are acceptable (none so far). Everything else fails the build.
ALLOWED_CRITICAL_WARNINGS: tuple[str, ...] = ()

_proc_lock = asyncio.Lock()

StageCallback = Callable[[str, str], "Awaitable[None] | None"]
LineCallback = Callable[[str], "Awaitable[None] | None"]


class VivadoBusy(RuntimeError):
    pass


class VivadoUnavailable(RuntimeError):
    pass


@dataclass
class VivadoBuildResult:
    build_id: str
    ok: bool
    status: str  # passed | failed | cancelled | timeout | unavailable
    failed_stage: str | None = None
    message: str = ""
    container: str | None = None
    vivado_version: str | None = None
    duration_s: float = 0.0
    exit_code: int | None = None
    stage_times: dict[str, float] = field(default_factory=dict)
    timing: dict | None = None
    utilization: dict | None = None
    utilization_synth: dict | None = None
    drc: dict | None = None
    log_summary: dict | None = None
    tcl_metrics: dict | None = None
    bitstream_path: str | None = None
    bitstream_sha256: str | None = None
    bitstream: dict | None = None
    artifacts_dir: str | None = None
    workspace_dir_host: str | None = None
    checks: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise VivadoBusy("another CircuitLoop Vivado build holds the workspace lock")
        os.write(fd, f"{os.getpid()}\n".encode())
        self.fd = fd

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def is_busy() -> bool:
    return _proc_lock.locked()


def _tcl_list(items: list[str]) -> str:
    for i in items:
        if not re.match(r"^[A-Za-z0-9_./-]+$", i):
            raise ValueError(f"unsafe file name for Tcl: {i!r}")
    return "[list " + " ".join(items) + "]"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _maybe(cb, *args):
    if cb is None:
        return
    r = cb(*args)
    if asyncio.iscoroutine(r):
        await r


async def run_build(
    build_id: str,
    sources: list[Path],
    xdc: Path,
    artifacts_dir: Path,
    *,
    top: str = "basys3_top",
    on_line: LineCallback | None = None,
    on_stage: StageCallback | None = None,
    cancel: asyncio.Event | None = None,
    settings: Settings | None = None,
) -> VivadoBuildResult:
    s = settings or get_settings()
    board = load_board("basys3")
    if not BUILD_ID_RE.match(build_id):
        raise ValueError(f"invalid build id {build_id!r}")
    result = VivadoBuildResult(build_id=build_id, ok=False, status="failed")
    t0 = time.monotonic()

    if _proc_lock.locked():
        raise VivadoBusy("a Vivado build is already running in this CircuitLoop process")
    async with _proc_lock:
        flock = _FileLock(s.vivado_workspace_host / ".build.lock")
        flock.acquire()
        try:
            env = await ctr.detect(s)
            if not env.ready:
                result.status = "unavailable"
                result.message = "; ".join(env.problems) or "Vivado environment not ready"
                return result
            if env.batch_builds_running:
                raise VivadoBusy("a CircuitLoop batch build is already running inside the container")
            result.container = env.active_container
            result.vivado_version = env.vivado_version

            # ---- stage files into the shared workspace
            host_dir = s.vivado_workspace_host / "builds" / build_id
            cont_dir = f"{s.vivado_workspace_container}/builds/{build_id}"
            if host_dir.exists():
                shutil.rmtree(host_dir)
            (host_dir / "src").mkdir(parents=True)
            names = []
            for src in sources:
                dst = host_dir / "src" / Path(src).name
                if dst.exists():
                    raise ValueError(f"duplicate source file name {dst.name}")
                shutil.copy2(src, dst)
                names.append(f"src/{dst.name}")
            shutil.copy2(xdc, host_dir / "constraints.xdc")
            shutil.copy2(TCL_TEMPLATE, host_dir / "build.tcl")
            bit_name = f"{top}.bit"
            (host_dir / "config.tcl").write_text(
                "# generated by CircuitLoop\n"
                f"set CL_PART {board.part}\n"
                f"set CL_TOP {top}\n"
                f"set CL_SOURCES {_tcl_list(names)}\n"
                "set CL_XDC constraints.xdc\n"
                f"set CL_THREADS {int(s.vivado_max_threads)}\n"
                f"set CL_BIT_NAME {bit_name}\n"
            )
            result.workspace_dir_host = str(host_dir)

            # ---- run Vivado (batch, no GUI)
            tag = f"CLBUILD_{build_id}"
            script = (
                f"source {env.settings_script} >/dev/null 2>&1; "
                f"exec vivado -mode batch -nojournal -notrace -log vivado.log -source build.tcl -tclargs {tag}"
            )
            lines: list[str] = []
            stage_start: dict[str, float] = {}

            async def handle(line: str) -> None:
                lines.append(line)
                await _maybe(on_line, line)
                if line.startswith("CL_STAGE "):
                    parts = line.split()
                    if len(parts) >= 3:
                        stage, status = parts[1], parts[2]
                        if status == "running":
                            stage_start[stage] = time.monotonic()
                        elif stage in stage_start:
                            result.stage_times[stage] = round(time.monotonic() - stage_start[stage], 1)
                        await _maybe(on_stage, stage, status)

            await _maybe(on_line, f"CL_CMD docker exec -u user -w {cont_dir} {env.active_container} bash -c "
                                  f"'source {env.settings_script}; vivado -mode batch -nojournal -notrace -log vivado.log "
                                  f"-source build.tcl -tclargs {tag}'")
            proc = await ctr.container_exec(
                env.active_container, script, workdir=cont_dir, timeout_s=s.vivado_build_timeout_s,
                on_line=handle, cancel=cancel, settings=s,
            )
            result.exit_code = proc.exit_code
            if proc.cancelled or proc.timed_out:
                # Stop only our own Vivado process inside the container (matched by the unique tag).
                await ctr.container_exec(env.active_container, f"pkill -f -- '-tclargs {tag}' || true", timeout_s=30, settings=s)
                result.status = "cancelled" if proc.cancelled else "timeout"
                result.message = "build cancelled by user" if proc.cancelled else f"build exceeded {s.vivado_build_timeout_s:.0f} s"

            result.log_summary = rpt.collect_log_messages(lines)
            res_file = host_dir / "results.json"
            tcl_res = json.loads(res_file.read_text()) if res_file.is_file() else None
            if tcl_res:
                result.tcl_metrics = tcl_res.get("metrics")
                if tcl_res.get("status") != "passed":
                    result.failed_stage = tcl_res.get("failed_stage") or None
                    result.message = result.message or tcl_res.get("message", "")
            elif result.status == "failed":
                result.message = result.message or (proc.error or f"vivado exited with {proc.exit_code} without results.json")

            # ---- parse reports that exist (even for failed builds, for diagnosis)
            rep = host_dir / "reports"
            if (rep / "utilization_synth.rpt").is_file():
                result.utilization_synth = rpt.parse_utilization(rep / "utilization_synth.rpt").as_dict()
            if (rep / "utilization_impl.rpt").is_file():
                result.utilization = rpt.parse_utilization(rep / "utilization_impl.rpt").as_dict()
            timing = rpt.parse_timing_summary(rep / "timing_summary.rpt") if (rep / "timing_summary.rpt").is_file() else None
            drc = rpt.parse_drc(rep / "drc.rpt") if (rep / "drc.rpt").is_file() else None
            result.timing = timing.as_dict() if timing else None
            result.drc = drc.__dict__ if drc else None

            # ---- copy artifacts back into the run directory
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            for name in ("vivado.log", "results.json", "build.tcl", "config.tcl", "constraints.xdc"):
                if (host_dir / name).is_file():
                    shutil.copy2(host_dir / name, artifacts_dir / name)
            if rep.is_dir():
                shutil.copytree(rep, artifacts_dir / "reports", dirs_exist_ok=True)
            bit_host = host_dir / "bitstream" / bit_name
            if bit_host.is_file():
                (artifacts_dir / "bitstream").mkdir(exist_ok=True)
                bit_dst = artifacts_dir / "bitstream" / bit_name
                shutil.copy2(bit_host, bit_dst)
                result.bitstream_path = str(bit_dst)
                result.bitstream_sha256 = _sha256(bit_dst)
                result.bitstream = rpt.inspect_bitstream(bit_dst, board.part).__dict__
            result.artifacts_dir = str(artifacts_dir)

            # ---- acceptance checks (all must pass for a successful build)
            checks = result.checks
            flow_ok = proc.exit_code == 0 and tcl_res is not None and tcl_res.get("status") == "passed"
            checks.append({"name": "vivado_flow", "passed": flow_ok,
                           "detail": f"exit code {proc.exit_code}" + (f", failed in {result.failed_stage}" if result.failed_stage else "")})
            errors = result.log_summary["errors"]
            checks.append({"name": "no_errors", "passed": not errors, "detail": f"{len(errors)} ERROR messages"})
            crit = [c for c in result.log_summary["critical_warnings"] if not any(a in c for a in ALLOWED_CRITICAL_WARNINGS)]
            checks.append({"name": "no_critical_warnings", "passed": not crit,
                           "detail": f"{len(crit)} critical warnings" + (f": {crit[0][:200]}" if crit else "")})
            if timing:
                checks.append({"name": "timing_met", "passed": timing.timing_met,
                               "detail": f"WNS {timing.wns_ns} ns, WHS {timing.whs_ns} ns, constraints met: {timing.constraints_met}"})
                clk_ok = any(abs((c.get("period_ns") or 0) - board.clock_period_ns) < 1e-6 for c in timing.clocks)
                checks.append({"name": "clock_constrained", "passed": clk_ok,
                               "detail": ", ".join(f"{c['name']} {c['period_ns']} ns" for c in timing.clocks) or "no clocks found"})
            else:
                checks.append({"name": "timing_met", "passed": False, "detail": "no timing report"})
            if drc:
                checks.append({"name": "drc_clean", "passed": drc.errors == 0 and drc.critical_warnings == 0,
                               "detail": f"{drc.errors} errors, {drc.critical_warnings} critical warnings, {drc.warnings} warnings"})
            if result.utilization:
                over = [r for r in result.utilization["resources"] if r["used"] > r["available"]]
                checks.append({"name": "fits_device", "passed": not over,
                               "detail": "all resources within xc7a35t capacity" if not over else f"over capacity: {over}"})
            if result.tcl_metrics:
                unrouted = result.tcl_metrics.get("unrouted_nets")
                checks.append({"name": "fully_routed", "passed": unrouted == "0", "detail": f"{unrouted} unrouted nets"})
                noloc = result.tcl_metrics.get("ports_without_loc")
                checks.append({"name": "all_ports_placed", "passed": noloc == "0", "detail": f"{noloc} ports without a fixed LOC"})
            bit_ok = bool(result.bitstream and result.bitstream.get("valid"))
            checks.append({"name": "bitstream_valid", "passed": bit_ok,
                           "detail": (f"{result.bitstream['size_bytes']} bytes, part {result.bitstream['part']}, sha256 {result.bitstream_sha256[:16]}..."
                                      if bit_ok else (result.bitstream or {}).get("problem", "no bitstream produced"))})

            if result.status not in ("cancelled", "timeout"):
                result.ok = all(c["passed"] for c in checks)
                result.status = "passed" if result.ok else "failed"
                if not result.ok and not result.message:
                    result.message = "; ".join(f"{c['name']}: {c['detail']}" for c in checks if not c["passed"])
            result.duration_s = round(time.monotonic() - t0, 1)
            (artifacts_dir / "build_result.json").write_text(json.dumps(result.as_dict(), indent=2, default=str))
            return result
        finally:
            flock.release()
            result.duration_s = round(time.monotonic() - t0, 1)
