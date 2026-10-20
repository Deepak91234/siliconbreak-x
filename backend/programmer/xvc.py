"""Detection of the macOS `xvcd` USB-to-XVC bridge and its launcher loop.

Facts (from ~/cse100/cse100-vivado-on-silicon-mac-main/scripts/start_container.sh):

  * The launcher starts `vivado_container`, then loops while the container runs:
    `if ! pgrep -x xvcd; then <scripts>/xvcd/bin/xvcd &; sleep 2; fi`.
  * xvcd claims the Basys 3's FTDI JTAG interface and serves it as a Xilinx
    Virtual Cable on TCP 2542. hw_server inside the container reaches the board
    only through it (auto-open-servers xilinx-xvc:host.docker.internal:2542).
  * While xvcd holds the interface, openFPGALoader cannot open the board
    (libftdi "unable to claim usb device").
  * Killing xvcd while the launcher loop runs is futile: the loop restarts it
    within ~2 s. With no board attached xvcd exits immediately and is restarted
    every ~2 s, so it can appear and disappear between polls.

So this module only *observes* (ps, lsof). The one mutating call, `stop_xvcd`,
sends SIGTERM to a single pid after re-verifying that its command line is the
xvcd binary, and refuses while the launcher loop would just restart it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic import BaseModel

from backend.core.process import run_command

XVC_PORT = 2542
XVCD_NAME = "xvcd"
LAUNCHER_NAME = "start_container.sh"
_SHELLS = {"zsh", "bash", "sh", "-zsh", "-bash", "-sh", "dash", "ksh"}


class XvcStatus(BaseModel):
    xvcd_pids: list[int]
    launcher_running: bool
    port_2542_listeners: list[dict]
    conflict: bool  # True = something holds (or will immediately re-grab) the FTDI JTAG interface
    note: str


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    command: str

    @property
    def argv0(self) -> str:
        parts = self.command.split()
        return parts[0] if parts else ""


def _bin(path: str, name: str) -> str | None:
    return path if os.path.isfile(path) else shutil.which(name)


# ------------------------------------------------------------------ pure parsing

def parse_ps(text: str) -> list[ProcInfo]:
    """Parse `ps -axo pid,ppid,command` output (header line optional)."""
    procs: list[ProcInfo] = []
    for line in text.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue  # header ("PID PPID COMMAND") or garbage
        procs.append(ProcInfo(pid=int(parts[0]), ppid=int(parts[1]), command=parts[2] if len(parts) > 2 else ""))
    return procs


def is_xvcd_command(command: str) -> bool:
    """True when the process's executable (argv[0]) is named exactly `xvcd`.

    Matches `/…/scripts/xvcd/bin/xvcd`, `./xvcd`, `xvcd -p 2542`; rejects
    `grep xvcd`, `vim xvcd.c`, `/…/xvcd/bin/xvcd-old`, `sleep 30` etc.
    """
    parts = command.split()
    return bool(parts) and PurePosixPath(parts[0]).name == XVCD_NAME


def is_launcher_command(command: str) -> bool:
    """True for the Vivado launcher loop: `[/bin/]zsh …/start_container.sh` or the script run directly."""
    parts = command.split()
    if not parts:
        return False
    if PurePosixPath(parts[0]).name == LAUNCHER_NAME:
        return True
    if PurePosixPath(parts[0]).name in _SHELLS:
        # first non-option argument is the script being run
        for a in parts[1:]:
            if a.startswith("-"):
                continue
            return PurePosixPath(a).name == LAUNCHER_NAME
    return False


def parse_lsof_listen(text: str) -> list[dict]:
    """Parse `lsof -nP -iTCP:<port> -sTCP:LISTEN` output.

    Columns: COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME [(LISTEN)].
    lsof escapes spaces in COMMAND (\\x20), so whitespace splitting is safe.
    """
    out: list[dict] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 9 or parts[0] == "COMMAND" or not parts[1].isdigit():
            continue
        name = parts[-2] if parts[-1].startswith("(") and len(parts) >= 10 else parts[-1]
        out.append({
            "command": parts[0],
            "pid": int(parts[1]),
            "user": parts[2],
            "type": parts[4],
            "name": name,
            "state": parts[-1].strip("()") if parts[-1].startswith("(") else None,
        })
    return out


def analyze(procs: list[ProcInfo], listeners: list[dict]) -> XvcStatus:
    """Combine process and socket observations into an XvcStatus with an explanatory note."""
    xvcd = [p for p in procs if is_xvcd_command(p.command)]
    launchers = [p for p in procs if is_launcher_command(p.command)]
    xvcd_pids = sorted(p.pid for p in xvcd)
    other_listeners = [l for l in listeners if l["pid"] not in xvcd_pids]
    launcher_running = bool(launchers)
    conflict = bool(xvcd_pids or launcher_running or listeners)

    notes: list[str] = []
    lpids = ", ".join(str(p.pid) for p in launchers)
    if xvcd_pids:
        pids = ", ".join(str(p) for p in xvcd_pids)
        notes.append(
            f"xvcd (pid {pids}) holds the Basys 3 USB-JTAG interface and serves it as a Xilinx Virtual Cable on "
            f"TCP {XVC_PORT}, so openFPGALoader cannot open the board."
        )
    if launcher_running:
        notes.append(
            f"The Vivado launcher loop ({LAUNCHER_NAME}, pid {lpids}) is running and restarts xvcd within ~2 s "
            "whenever it is not running, so stopping xvcd is futile while the launcher runs. The non-disruptive path "
            "is the hw_server adapter: program through hw_server inside vivado_container, which already reaches the "
            "board through xvcd."
        )
        if not xvcd_pids:
            notes.append("xvcd is not running at this instant (it exits at once when no board is attached and is "
                         "relaunched every ~2 s); it will claim the board as soon as one is plugged in.")
    elif xvcd_pids:
        notes.append("No launcher loop is running, so xvcd can be stopped (with your approval) to free the board for "
                     "openFPGALoader, or the hw_server adapter can be used if vivado_container is running.")
    if other_listeners:
        desc = ", ".join(f"{l['command']} (pid {l['pid']})" for l in other_listeners)
        notes.append(f"Another process listens on TCP {XVC_PORT} (XVC): {desc}; it may hold a JTAG cable.")
    if not notes:
        notes.append(f"No xvcd, launcher loop or TCP {XVC_PORT} listener: the USB-JTAG interface is free for openFPGALoader.")
    return XvcStatus(
        xvcd_pids=xvcd_pids,
        launcher_running=launcher_running,
        port_2542_listeners=listeners,
        conflict=conflict,
        note=" ".join(notes),
    )


# ------------------------------------------------------------------ live probes

async def list_processes(timeout_s: float = 10.0) -> list[ProcInfo]:
    ps = _bin("/bin/ps", "ps")
    if not ps:
        raise RuntimeError("ps not found")
    r = await run_command([ps, "-axo", "pid,ppid,command"], timeout_s=timeout_s)
    if not r.ok:
        raise RuntimeError(f"ps failed: {r.error or r.text[-200:]}")
    return parse_ps(r.text)


async def list_listeners(port: int = XVC_PORT, timeout_s: float = 10.0) -> list[dict]:
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise ValueError(f"invalid port {port!r}")
    lsof = _bin("/usr/sbin/lsof", "lsof")
    if not lsof:
        return []
    r = await run_command([lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], timeout_s=timeout_s)
    # lsof exits 1 with no output when nothing matches; that is "no listeners", not an error.
    if r.error or r.timed_out:
        return []
    return parse_lsof_listen(r.text)


async def status() -> XvcStatus:
    procs, listeners = await asyncio.gather(list_processes(), list_listeners())
    return analyze(procs, listeners)


async def _command_of(pid: int) -> str | None:
    ps = _bin("/bin/ps", "ps")
    if not ps:
        return None
    r = await run_command([ps, "-o", "command=", "-p", str(int(pid))], timeout_s=10)
    if not r.ok:
        return None
    text = r.text.strip()
    return text or None


async def _alive(pid: int) -> bool:
    """True while `pid` exists and is not a zombie (an exited child its parent has not reaped yet)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    ps = _bin("/bin/ps", "ps")
    if ps:
        r = await run_command([ps, "-o", "stat=", "-p", str(int(pid))], timeout_s=10)
        if r.exit_code is not None and r.exit_code != 0:
            return False
        if r.text.strip().startswith("Z"):
            return False
    return True


async def stop_xvcd(pid: int, *, refuse_while_launcher: bool = True, wait_s: float = 3.0) -> dict:
    """Send SIGTERM to `pid` only if it is verifiably the xvcd binary.

    Approval is the caller's job (HardwareManager.stop_xvcd). Here we re-verify
    what the pid *is* right before signalling, because the approval was given
    for a pid observed earlier and pids are reused.
    """
    base = {"ok": False, "pid": pid, "signal": None, "exited": False, "command": None}
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return {**base, "message": f"refused: {pid!r} is not a valid process id"}
    if pid == os.getpid():
        return {**base, "message": "refused: that is the CircuitLoop backend itself"}

    procs = await list_processes()
    target = next((p for p in procs if p.pid == pid), None)
    if target is None:
        return {**base, "message": f"refused: no process with pid {pid} is running"}
    base["command"] = target.command
    if not is_xvcd_command(target.command):
        return {**base, "message": f"refused: pid {pid} is not xvcd (command: {target.command[:200]!r})"}

    if refuse_while_launcher:
        launchers = [p for p in procs if is_launcher_command(p.command)]
        if launchers:
            return {**base, "message": (
                f"refused: the Vivado launcher loop ({LAUNCHER_NAME}, pid {launchers[0].pid}) would restart xvcd within "
                "~2 s, so stopping it is futile and would only interrupt the Vivado GUI's hardware connection. "
                "Use the hw_server adapter instead (CircuitLoop selects it automatically while xvcd runs)."
            )}

    # Re-verify immediately before signalling (narrows the pid-reuse window to microseconds).
    again = await _command_of(pid)
    if again is None or again != target.command or not is_xvcd_command(again):
        return {**base, "message": f"refused: pid {pid} changed or exited during verification"}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {**base, "ok": True, "exited": True, "message": f"xvcd (pid {pid}) had already exited"}
    except PermissionError as e:
        return {**base, "message": f"not permitted to signal pid {pid}: {e}"}
    base["signal"] = "SIGTERM"

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not await _alive(pid):
            return {**base, "ok": True, "exited": True, "message": f"sent SIGTERM to xvcd (pid {pid}); it exited"}
        await asyncio.sleep(0.05)
    return {**base, "ok": False, "exited": False,
            "message": f"sent SIGTERM to xvcd (pid {pid}) but it is still running after {wait_s:.0f} s"}
