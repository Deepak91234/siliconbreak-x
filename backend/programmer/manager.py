"""HardwareManager: Basys 3 connectivity state, adapter choice, approved volatile programming.

State model (docs/api.md "Hardware state"):
  no_board      no FTDI 0403:6010 on USB and no XVC bridge holding one
  busy          a JTAG cable is visible but held by another program and no usable alternative path
  connected     the xc7a35t IDCODE was actually read by the last JTAG detect (same USB device set)
  unsupported   a JTAG chain answered with a device that is not the Basys 3's xc7a35t
  tool_missing  neither openFPGALoader nor the hw_server path is available
  error         the programmer tool failed
  unknown       a cable is visible but no JTAG detect has been run for it yet

`status(deep=False)` only reads the USB registry and the process table (no
JTAG traffic), so it is safe to poll. JTAG detection runs only in `detect()`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from backend.core.events import bus
from backend.programmer import hwserver, openfpgaloader as ofl, usb, xvc
from backend.programmer.openfpgaloader import DetectResult, ProgramResult
from backend.programmer.usb import UsbDevice
from backend.programmer.xvc import XvcStatus
from backend.specifications.board import load_board

log = logging.getLogger("siliconbreak.hardware")

HardwareState = Literal["connected", "no_board", "busy", "unsupported", "error", "tool_missing", "unknown"]
ApprovalVerifier = Callable[[str, str], Awaitable[bool]]


class HardwareStatus(BaseModel):
    state: HardwareState
    board: dict
    usb_devices: list[UsbDevice]
    xvc: XvcStatus
    adapters: dict
    preferred_adapter: str | None
    last_detect: DetectResult | None
    last_program: ProgramResult | None
    programming: bool
    checked_at: float
    message: str


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class HardwareManager:
    def __init__(self) -> None:
        self._program_lock = asyncio.Lock()
        self._detect_lock = asyncio.Lock()
        self._verifier: ApprovalVerifier | None = None
        self._xvcd_verifier: Callable[[str, int], Awaitable[bool]] | None = None
        self.last_detect: DetectResult | None = None
        self._detect_usb_sig: tuple | None = None
        self.last_program: ProgramResult | None = None
        self._ofl_version: str | None = None
        self._ofl_checked = False
        self._hw_avail: dict | None = None
        self._hw_avail_at = 0.0

    # ------------------------------------------------------------ configuration
    def set_approval_verifier(self, fn: ApprovalVerifier) -> None:
        """fn(approval_id, bitstream_sha256) -> True only for an approved, unused program approval of that file."""
        self._verifier = fn

    def set_xvcd_approval_verifier(self, fn: Callable[[str, int], Awaitable[bool]]) -> None:
        self._xvcd_verifier = fn

    # ------------------------------------------------------------ status
    async def _adapters(self, need_hw: bool) -> dict:
        if not self._ofl_checked:
            self._ofl_version = await ofl.version()
            self._ofl_checked = True
        hw = {"available": False, "reason": "not checked (only needed while xvcd holds the cable)"}
        if need_hw:
            if self._hw_avail is None or time.time() - self._hw_avail_at > 20:
                self._hw_avail = await hwserver.availability()
                self._hw_avail_at = time.time()
            hw = {k: v for k, v in self._hw_avail.items() if k != "settings_script"}
        return {
            "openfpgaloader": {"available": bool(ofl.binary()), "path": ofl.binary(), "version": self._ofl_version},
            "hw_server": hw,
        }

    async def status(self, deep: bool = False) -> HardwareStatus:
        if deep:
            await self.detect()
        board = load_board("basys3")
        scan, xv = await asyncio.gather(usb.scan_usb(), xvc.status())
        ftdi = usb.find_ftdi(scan.devices, board.usb_vendor_id, board.usb_product_id) if scan.ok else []
        adapters = await self._adapters(need_hw=bool(xv.xvcd_pids) or xv.launcher_running)
        preferred = self._preferred(ftdi, xv, adapters)
        sig = usb.signature(ftdi)
        last = self.last_detect if self._detect_usb_sig == sig else None

        if self.programming:
            state, msg = "connected", "programming in progress"
        elif not adapters["openfpgaloader"]["available"] and not adapters["hw_server"].get("available"):
            state, msg = "tool_missing", "install openFPGALoader (brew install openfpgaloader) or start vivado_container"
        elif xv.xvcd_pids:
            if not adapters["hw_server"].get("available"):
                state, msg = "busy", "xvcd holds the JTAG cable and the hw_server path is unavailable: " + adapters["hw_server"].get("reason", "")
            elif last and last.adapter == "hw_server":
                state, msg = last.state, last.message
            else:
                state, msg = "unknown", "xvcd holds the cable; run Detect to query the board through hw_server"
        elif not ftdi:
            state, msg = "no_board", "no Basys 3 (FTDI 0403:6010) on USB"
        elif last:
            state, msg = last.state, last.message
        else:
            state, msg = "unknown", f"FTDI cable visible ({ftdi[0].product or 'FT2232H'}); run Detect to confirm the FPGA"
        if not scan.ok:
            msg += f" (USB scan failed: {scan.error})"
        return HardwareStatus(
            state=state, board={"name": board.name, "part": board.part, "idcode": board.idcode},
            usb_devices=scan.devices if scan.ok else [], xvc=xv, adapters=adapters, preferred_adapter=preferred,
            last_detect=self.last_detect, last_program=self.last_program, programming=self.programming,
            checked_at=time.time(), message=msg,
        )

    @property
    def programming(self) -> bool:
        return self._program_lock.locked()

    @staticmethod
    def _preferred(ftdi: list[UsbDevice], xv: XvcStatus, adapters: dict) -> str | None:
        # While the cse100 launcher loop runs it (re)starts xvcd every ~2 s, and xvcd grabs the
        # cable as soon as the board appears: go through hw_server instead of fighting it.
        if xv.xvcd_pids or xv.launcher_running:
            if adapters["hw_server"].get("available"):
                return "hw_server"
            return None if xv.xvcd_pids else ("openfpgaloader" if adapters["openfpgaloader"]["available"] else None)
        if adapters["openfpgaloader"]["available"]:
            return "openfpgaloader"
        return None

    # ------------------------------------------------------------ detect
    async def detect(self) -> DetectResult:
        async with self._detect_lock:
            board = load_board("basys3")
            scan, xv = await asyncio.gather(usb.scan_usb(), xvc.status())
            ftdi = usb.find_ftdi(scan.devices, board.usb_vendor_id, board.usb_product_id) if scan.ok else []
            if xv.xvcd_pids or (xv.launcher_running and ftdi):
                avail = await hwserver.availability()
                if avail["available"]:
                    res = await hwserver.detect()
                elif not xv.xvcd_pids:
                    res = await ofl.detect()
                else:
                    res = DetectResult(adapter="openfpgaloader", ok=False, state="busy", idcode=None, device=None,
                                       message=f"xvcd (pid {xv.xvcd_pids[0]}) holds the JTAG cable and the hw_server "
                                               f"path is unavailable: {avail['reason']}", raw="", duration_s=0.0)
            else:
                res = await ofl.detect()
            self.last_detect = res
            self._detect_usb_sig = usb.signature(ftdi)
            return res

    # ------------------------------------------------------------ program
    async def program(self, bitstream: Path, sha256: str, *, approval_id: str) -> ProgramResult:
        bitstream = Path(bitstream)
        base = dict(bitstream_path=str(bitstream), bitstream_sha256=sha256)

        def refuse(msg: str, adapter: str = "none") -> ProgramResult:
            return ProgramResult(adapter=adapter, ok=False, state="refused", message=msg, raw="",
                                 duration_s=0.0, finished_at=time.time(), **base)

        if self._verifier is None:
            return refuse("no approval verifier configured; programming is disabled")
        if not approval_id or not await self._verifier(approval_id, sha256):
            return refuse("programming requires an approved request for exactly this bitstream")
        if not bitstream.is_file():
            return refuse(f"bitstream not found: {bitstream}")
        actual = _sha256(bitstream)
        if actual != sha256:
            return refuse(f"bitstream hash mismatch (expected {sha256[:12]}..., file is {actual[:12]}...)")
        if self._program_lock.locked():
            return refuse("another programming operation is in progress")
        async with self._program_lock:
            await bus.emit("hardware.updated", hardware=(await self.status()).model_dump())
            xv = await xvc.status()
            if xv.xvcd_pids or (xv.launcher_running and self.last_detect and self.last_detect.adapter == "hw_server"):
                avail = await hwserver.availability()
                if not avail["available"]:
                    res = ProgramResult(adapter="openfpgaloader", ok=False, state="busy",
                                        message=f"xvcd holds the JTAG cable and hw_server is unavailable: {avail['reason']}",
                                        raw="", duration_s=0.0, finished_at=time.time(), **base)
                else:
                    res = await hwserver.program(bitstream, sha256)
            else:
                res = await ofl.program(bitstream, sha256)
            self.last_program = res
            log.info("program %s via %s: ok=%s state=%s %s", bitstream.name, res.adapter, res.ok, res.state, res.message)
        await bus.emit("hardware.updated", hardware=(await self.status()).model_dump())
        return res

    # ------------------------------------------------------------ xvcd
    async def stop_xvcd(self, pid: int, *, approval_id: str) -> dict:
        if self._xvcd_verifier is not None and not await self._xvcd_verifier(approval_id, pid):
            return {"ok": False, "message": "stopping xvcd requires an approved request for this pid"}
        if not approval_id:
            return {"ok": False, "message": "stopping xvcd requires an approval"}
        res = await xvc.stop_xvcd(int(pid))
        await bus.emit("hardware.updated", hardware=(await self.status()).model_dump())
        return res

    # ------------------------------------------------------------ monitor
    async def monitor(self, interval_s: float = 3.0) -> None:
        last_key = None
        while True:
            try:
                st = await self.status(deep=False)
                key = (st.state, usb.signature(st.usb_devices), tuple(st.xvc.xvcd_pids), st.xvc.launcher_running,
                       st.programming, st.preferred_adapter)
                if key != last_key:
                    last_key = key
                    await bus.emit("hardware.updated", hardware=st.model_dump())
            except asyncio.CancelledError:
                raise
            except RuntimeError as e:  # transient probe failure (e.g. across sleep/wake): retry next tick
                log.warning("hardware monitor: %s; retrying in %.0f s", e, interval_s)
            except Exception:
                log.exception("hardware monitor iteration failed")
            await asyncio.sleep(interval_s)


hardware = HardwareManager()
