"""Native macOS programming through openFPGALoader (verified flags for v1.1.1).

    detect:   openFPGALoader -b basys3 --detect
    program:  openFPGALoader -b basys3 --write-sram <file.bit>     (volatile SRAM only)

`--write-flash` / `-f` is never used: CircuitLoop only performs volatile
configuration, so power-cycling the board restores whatever was in flash.

Success requires exit status 0 AND no error markers in the output; the board is
"connected" only when a JTAG IDCODE matching the xc7a35t was actually read.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from backend.core.config import get_settings
from backend.core.process import LineCallback, run_command
from backend.specifications.board import load_board

DetectState = Literal["connected", "no_board", "busy", "unsupported", "error", "tool_missing"]

# Real output on this Mac with no board attached (openFPGALoader v1.1.1):
#   unable to open ftdi device: -3 (device not found)
#   JTAG init failed with: unable to open ftdi device
_NO_BOARD = re.compile(r"device not found|no device found|unable to find (?:the )?device|no ftdi device|"
                       r"cable not found|usb_open|libusb_open.*NO_DEVICE", re.I)
# libftdi -5 "unable to claim usb device" / -4 "unable to open device" / resource busy: something else holds it
_BUSY = re.compile(r"unable to claim|resource busy|LIBUSB_ERROR_BUSY|LIBUSB_ERROR_ACCESS|-4 \(unable to open device\)|"
                   r"default FTDI driver is in use|access denied", re.I)
_UNSUPPORTED = re.compile(r"unknown device|not supported|unsupported|no such board|idcode mismatch|"
                          r"wrong device|device does not match", re.I)
_ERROR_MARK = re.compile(r"\berror\b|failed|fail:|abort", re.I)
_IDCODE = re.compile(r"idcode\s*[:=]?\s*(0x[0-9a-fA-F]+)", re.I)
_MODEL = re.compile(r"^\s*model\s*[:=]?\s*(\S+)", re.I | re.M)
_FAMILY = re.compile(r"^\s*family\s*[:=]?\s*(.+?)\s*$", re.I | re.M)
_SRAM_DONE = re.compile(r"Load SRAM.*100(?:\.0+)?%|^\s*Done\s*$", re.I | re.M)

IDCODE_MASK = 0x0FFFFFFF  # the top nibble is the silicon revision


class DetectResult(BaseModel):
    adapter: Literal["openfpgaloader", "hw_server"]
    ok: bool
    state: DetectState
    idcode: str | None
    device: str | None
    message: str
    raw: str
    duration_s: float


class ProgramResult(BaseModel):
    adapter: str
    ok: bool
    state: Literal["programmed", "failed", "busy", "no_board", "refused"]
    bitstream_path: str
    bitstream_sha256: str
    message: str
    raw: str
    duration_s: float
    finished_at: float


def binary() -> str | None:
    return get_settings().openfpgaloader_bin


def idcode_matches(idcode: str | None, expected: str | None = None) -> bool:
    if not idcode:
        return False
    want = int(expected or load_board("basys3").idcode, 16)
    try:
        return (int(idcode, 16) & IDCODE_MASK) == (want & IDCODE_MASK)
    except ValueError:
        return False


def classify(text: str, exit_code: int | None) -> tuple[DetectState, str]:
    """Map raw tool output to a state. Order matters: busy/no-board are checked before generic errors."""
    if _BUSY.search(text):
        return "busy", "the FTDI JTAG interface is in use by another program (e.g. xvcd or Vivado hw_server)"
    if _NO_BOARD.search(text):
        return "no_board", "no Basys 3 / FTDI JTAG cable found on USB"
    ids = _IDCODE.findall(text)
    if ids:
        if any(idcode_matches(i) for i in ids):
            if exit_code == 0:
                return "connected", "xc7a35t detected"
            return "error", f"device found but openFPGALoader exited with {exit_code}"
        return "unsupported", f"JTAG device(s) {', '.join(ids)} do not match the Basys 3's xc7a35t"
    if _UNSUPPORTED.search(text):
        return "unsupported", "openFPGALoader reports an unsupported device or board"
    if exit_code == 0:
        return "error", "openFPGALoader succeeded but printed no IDCODE"
    return "error", f"openFPGALoader failed (exit {exit_code})"


def _summary_line(text: str) -> str:
    for line in text.splitlines():
        if _ERROR_MARK.search(line) or _NO_BOARD.search(line) or _BUSY.search(line):
            return line.strip()[:300]
    return ""


async def version() -> str | None:
    b = binary()
    if not b:
        return None
    r = await run_command([b, "--Version"], timeout_s=15)
    first = next((ln.strip() for ln in r.output if ln.strip()), None)
    return first


async def detect(timeout_s: float = 30.0) -> DetectResult:
    t0 = time.monotonic()
    b = binary()
    board = load_board("basys3")
    if not b:
        return DetectResult(adapter="openfpgaloader", ok=False, state="tool_missing", idcode=None, device=None,
                            message="openFPGALoader is not installed (brew install openfpgaloader)", raw="",
                            duration_s=0.0)
    r = await run_command([b, "-b", board.openfpgaloader_board, "--detect"], timeout_s=timeout_s)
    raw = r.text
    if r.timed_out:
        state, msg = "error", f"openFPGALoader --detect timed out after {timeout_s:.0f} s"
    elif r.error:
        state, msg = "tool_missing", r.error
    else:
        state, msg = classify(raw, r.exit_code)
        detail = _summary_line(raw)
        if detail and state != "connected":
            msg = f"{msg}: {detail}"
    ids = _IDCODE.findall(raw)
    model = _MODEL.search(raw)
    family = _FAMILY.search(raw)
    device = model.group(1) if model else None
    if device and family:
        device = f"{device} ({family.group(1)})"
    return DetectResult(adapter="openfpgaloader", ok=state == "connected", state=state,
                        idcode=ids[0] if ids else None, device=device, message=msg, raw=raw[-8000:],
                        duration_s=round(time.monotonic() - t0, 2))


async def program(bitstream: Path, sha256: str, on_line: LineCallback | None = None,
                  timeout_s: float = 180.0) -> ProgramResult:
    """Volatile SRAM configuration. The caller has already verified approval and the file hash."""
    t0 = time.monotonic()
    b = binary()
    board = load_board("basys3")
    base = dict(adapter="openfpgaloader", bitstream_path=str(bitstream), bitstream_sha256=sha256)
    if not b:
        return ProgramResult(**base, ok=False, state="refused", message="openFPGALoader is not installed", raw="",
                             duration_s=0.0, finished_at=time.time())
    bitstream = Path(bitstream).resolve()
    if bitstream.suffix != ".bit" or not bitstream.is_file():
        return ProgramResult(**base, ok=False, state="refused", message=f"not a .bit file: {bitstream}", raw="",
                             duration_s=0.0, finished_at=time.time())
    r = await run_command([b, "-b", board.openfpgaloader_board, "--write-sram", str(bitstream)],
                          timeout_s=timeout_s, on_line=on_line)
    raw = r.text
    if r.timed_out:
        state, ok, msg = "failed", False, f"programming timed out after {timeout_s:.0f} s"
    elif r.error:
        state, ok, msg = "refused", False, r.error
    else:
        cls, cmsg = classify(raw, r.exit_code)
        errors = [ln for ln in raw.splitlines() if _ERROR_MARK.search(ln)]
        if r.exit_code == 0 and not errors and cls not in ("busy", "no_board", "unsupported"):
            ok = True
            state = "programmed"
            msg = "bitstream loaded into SRAM" + (" (100% transferred)" if _SRAM_DONE.search(raw) else "")
        else:
            ok = False
            state = {"busy": "busy", "no_board": "no_board"}.get(cls, "failed")
            msg = (errors[0].strip()[:300] if errors else cmsg) + f" (exit {r.exit_code})"
    return ProgramResult(**base, ok=ok, state=state, message=msg, raw=raw[-8000:],
                         duration_s=round(time.monotonic() - t0, 2), finished_at=time.time())
