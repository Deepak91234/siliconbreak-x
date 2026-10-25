"""Alternative adapter: program through the existing Vivado hw_server in vivado_container.

Why it exists: while the user's start_container.sh loop runs, the macOS `xvcd`
bridge owns the FTDI JTAG interface (and is restarted within ~2 s if stopped),
so openFPGALoader cannot open the board. hw_server inside vivado_container
already reaches the board through that bridge
(auto-open-servers xilinx-xvc:host.docker.internal:2542), so SiliconBreak X
connects to it as an additional client from a short Vivado batch session.

STATUS: implemented but NOT yet exercised against a real board on this machine
(no board was attached during development). Tcl generation and output parsing
are unit-tested with representative sample output.

Constraints honoured:
  * never runs concurrently with a SiliconBreak X Vivado build (shares the build locks),
  * volatile programming only (program_hw_devices; no cfgmem / flash commands),
  * the bitstream is staged into the shared workspace, never written elsewhere.
"""

from __future__ import annotations

import re
import secrets
import shutil
import time
from pathlib import Path

from backend.core.config import get_settings
from backend.programmer.openfpgaloader import DetectResult, ProgramResult, idcode_matches
from backend.vivado import build as vb
from backend.vivado import container as ctr

HW_SERVER_URL = "localhost:3121"
_MARK = re.compile(r"^CL_HW\s+(\S+)\s*(.*)$")
_STARTUP_HIGH = re.compile(r"End of startup status:\s*HIGH", re.I)
# Real Vivado 2023.2 message when the XVC target exists but no board is behind it (captured on this Mac).
_NO_ACTIVE_TARGET = re.compile(r"Labtoolstcl 44-494|no active target available", re.I)

TCL = r"""# SiliconBreak X hw_server adapter (generated). Volatile programming only.
set cl_mode [lindex $argv 0]
set cl_bit  [lindex $argv 1]
proc out {k v} { puts "CL_HW $k $v"; flush stdout }
if {[catch {
    open_hw_manager
    if {[catch {connect_hw_server -url %(url)s -allow_non_jtag} e]} {
        if {[string match "*44-494*" $e]} { out state no_board; out message "hw_server has no active JTAG target: the XVC bridge (xvcd on macOS) is not serving a board - is the Basys 3 plugged in?"; exit 3 }
        error $e
    }
    # With no board, the XVC target exists but is inactive and Vivado raises
    # "There is no active target available" instead of returning an empty list.
    set targets {}
    if {[catch {refresh_hw_server} e]} { out refresh_note [string map {"\n" " | "} $e] }
    if {[catch {set targets [get_hw_targets -quiet *]} e]} { out targets_note [string map {"\n" " | "} $e]; set targets {} }
    out targets [llength $targets]
    if {[llength $targets] == 0} { out state no_board; out message "hw_server has no active JTAG target: the XVC bridge (xvcd on macOS) is not serving a board - is the Basys 3 plugged in?"; exit 3 }
    current_hw_target [lindex $targets 0]
    open_hw_target
    set devs [get_hw_devices -quiet]
    out devices [join $devs ,]
    set dev ""
    foreach d $devs { if {[string match -nocase "xc7a35t*" [get_property PART $d]] || [string match -nocase "xc7a35t*" $d]} { set dev $d; break } }
    if {$dev eq ""} { out state unsupported; out message "no xc7a35t on the JTAG chain"; exit 4 }
    current_hw_device $dev
    refresh_hw_device -update_hw_probes false $dev
    if {[catch {out idcode 0x[get_property IDCODE_HEX $dev]}]} { catch {out idcode_bin [get_property IDCODE $dev]} }
    out part [get_property PART $dev]
    if {$cl_mode eq "program"} {
        set_property PROGRAM.FILE $cl_bit $dev
        set_property PROBES.FILE {} $dev
        program_hw_devices $dev
        refresh_hw_device -update_hw_probes false $dev
        if {[catch {out done [get_property REGISTER.CONFIG_STATUS.BIT14_DONE_PIN $dev]}]} { out done unknown }
    }
    catch {close_hw_target}
    catch {disconnect_hw_server}
    catch {close_hw_manager}
} err]} {
    out state error
    out message [string map {"\n" " | "} $err]
    exit 2
}
out state ok
exit 0
"""


def tcl_script() -> str:
    return TCL % {"url": HW_SERVER_URL}


def parse_output(lines: list[str]) -> dict:
    """Collect CL_HW markers (last value wins) plus Vivado's own startup-status line."""
    out: dict = {}
    for line in lines:
        m = _MARK.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
        if _STARTUP_HIGH.search(line):
            out["startup_high"] = "1"
        if line.startswith("ERROR:"):
            out.setdefault("errors", []).append(line.strip())
    if "idcode" not in out and out.get("idcode_bin"):
        try:
            out["idcode"] = hex(int(out["idcode_bin"], 2))
        except ValueError:
            pass
    return out


async def availability() -> dict:
    """hw_server path needs vivado_container (the GUI container) running with hw_server."""
    s = get_settings()
    env = await ctr.detect(s)
    if not env.docker_daemon:
        return {"available": False, "reason": "Docker Desktop is not running"}
    if env.active_container != s.vivado_container:
        return {"available": False, "reason": f"{s.vivado_container} (the container with hw_server/XVC) is not running"}
    if not env.hw_server_running:
        return {"available": False, "reason": f"hw_server is not running inside {s.vivado_container}"}
    if not env.vivado_executable:
        return {"available": False, "reason": "Vivado is not executable in the container"}
    return {"available": True, "reason": f"hw_server in {s.vivado_container} via XVC", "settings_script": env.settings_script}


async def _run(mode: str, bitstream: Path | None, timeout_s: float) -> tuple[dict, str, float, str | None]:
    """Execute the Tcl session. Returns (markers, raw_text, duration, refusal_reason)."""
    s = get_settings()
    t0 = time.monotonic()
    avail = await availability()
    if not avail["available"]:
        return {}, "", 0.0, avail["reason"]
    if vb.is_busy():
        return {}, "", 0.0, "a SiliconBreak X Vivado build is running; try again when it finishes"
    lock = vb._FileLock(s.vivado_workspace_host / ".build.lock")
    try:
        lock.acquire()
    except vb.VivadoBusy as e:
        return {}, "", 0.0, str(e)
    try:
        tag = f"clhw{secrets.token_hex(4)}"
        host_dir = s.vivado_workspace_host / "hw" / tag
        host_dir.mkdir(parents=True)
        (host_dir / "hw.tcl").write_text(tcl_script())
        bit_arg = "none"
        if bitstream is not None:
            shutil.copy2(bitstream, host_dir / "design.bit")
            bit_arg = f"{s.vivado_workspace_container}/hw/{tag}/design.bit"
        cont_dir = f"{s.vivado_workspace_container}/hw/{tag}"
        script = (f"source {avail['settings_script']} >/dev/null 2>&1; "
                  f"exec vivado -mode batch -nojournal -notrace -log hw.log -source hw.tcl -tclargs {mode} {bit_arg}")
        r = await ctr.container_exec(s.vivado_container, script, workdir=cont_dir, timeout_s=timeout_s, settings=s)
        return parse_output(r.output), r.text, time.monotonic() - t0, None if (r.exit_code is not None or r.ok) else (r.error or "docker exec failed")
    finally:
        lock.release()


async def detect(timeout_s: float = 240.0) -> DetectResult:
    m, raw, dur, refused = await _run("detect", None, timeout_s)
    if refused:
        return DetectResult(adapter="hw_server", ok=False, state="error", idcode=None, device=None,
                            message=refused, raw=raw[-8000:], duration_s=round(dur, 2))
    st = m.get("state", "error")
    idcode = m.get("idcode")
    if st == "ok" and idcode_matches(idcode):
        state, msg = "connected", f"{m.get('part', 'xc7a35t')} via hw_server/XVC"
    elif st == "ok":
        state, msg = "unsupported", f"device idcode {idcode} is not the Basys 3's xc7a35t"
    elif st in ("no_board", "unsupported"):
        state, msg = st, m.get("message", st)
    elif _NO_ACTIVE_TARGET.search(m.get("message", "")):
        state, msg = "no_board", "hw_server has no active JTAG target (XVC bridge not serving a board)"
    else:
        state, msg = "error", m.get("message") or "; ".join(m.get("errors", [])) or "hw_server session failed"
    return DetectResult(adapter="hw_server", ok=state == "connected", state=state, idcode=idcode,
                        device=m.get("part"), message=msg, raw=raw[-8000:], duration_s=round(dur, 2))


async def program(bitstream: Path, sha256: str, timeout_s: float = 300.0) -> ProgramResult:
    m, raw, dur, refused = await _run("program", Path(bitstream), timeout_s)
    base = dict(adapter="hw_server", bitstream_path=str(bitstream), bitstream_sha256=sha256)
    if refused:
        return ProgramResult(**base, ok=False, state="refused", message=refused, raw=raw[-8000:],
                             duration_s=round(dur, 2), finished_at=time.time())
    done_ok = m.get("done") == "1" or (m.get("done") == "unknown" and m.get("startup_high") == "1")
    ok = m.get("state") == "ok" and idcode_matches(m.get("idcode")) and done_ok and not m.get("errors")
    if ok:
        msg = "programmed via hw_server; DONE pin high"
    elif m.get("state") == "no_board":
        msg = m.get("message", "no JTAG target")
    else:
        msg = m.get("message") or "; ".join(m.get("errors", [])) or f"programming not confirmed (done={m.get('done')})"
    state = "programmed" if ok else ("no_board" if m.get("state") == "no_board" else "failed")
    return ProgramResult(**base, ok=ok, state=state, message=msg, raw=raw[-8000:],
                         duration_s=round(dur, 2), finished_at=time.time())
