"""USB enumeration on macOS through `ioreg` (no libusb, no device access).

Why ioreg: it reads the IOKit registry, so listing devices never opens or claims
anything. That keeps `HardwareManager.status(deep=False)` cheap and safe to poll
even while xvcd or openFPGALoader holds the FTDI JTAG interface (a claimed
device is still listed in the IOUSB plane).

The Basys 3's USB-JTAG/UART bridge is an FTDI FT2232H that enumerates as
VID 0x0403 / PID 0x6010 (boards/basys3/board.json). Many other FT2232H
boards share that VID/PID, so a match here only means "a candidate USB-JTAG
adapter is attached"; the FPGA itself is confirmed by a JTAG IDCODE read
(openfpgaloader.detect / hwserver.detect), never by USB alone.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field

from pydantic import BaseModel

from backend.core.process import run_command

IOREG_ARGV_TAIL = ("-p", "IOUSB", "-l", "-w0")

# "+-o EarPods@01100000  <class IOUSBHostDevice, id 0x100031b1a, registered, ...>"
_NODE_RE = re.compile(r"\+-o\s+(?P<name>.*?)\s+<class\s+(?P<cls>[^,>]+)")
# '  |       "idVendor" = 1452'
_PROP_RE = re.compile(r'^[\s|]*"(?P<key>[^"]+)"\s+=\s+(?P<value>.*?)\s*$')

_PRODUCT_KEYS = ("USB Product Name", "kUSBProductString")
_SERIAL_KEYS = ("USB Serial Number", "kUSBSerialNumberString")
_VENDOR_NAME_KEYS = ("USB Vendor Name", "kUSBVendorString")


class UsbDevice(BaseModel):
    vendor_id: int
    product_id: int
    product: str | None
    serial: str | None
    location: str | None  # IOKit locationID as 0x%08x (stable per physical port)

    @property
    def vid_pid(self) -> str:
        return f"{self.vendor_id:04x}:{self.product_id:04x}"


@dataclass
class UsbScan:
    ok: bool
    devices: list[UsbDevice] = field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0


def _ioreg_bin() -> str | None:
    if os.path.isfile("/usr/sbin/ioreg"):
        return "/usr/sbin/ioreg"
    return shutil.which("ioreg")


def _parse_value(raw: str) -> str | int | None:
    """Decode the scalar ioreg value forms we need: integers and quoted strings."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1]
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"0x[0-9a-fA-F]+", raw):
        return int(raw, 16)
    return None  # dicts, arrays, data blobs, Yes/No: not needed here


def parse_ioreg(text: str) -> list[UsbDevice]:
    """Parse `ioreg -p IOUSB -l -w0` output into the USB devices it lists.

    Every `+-o` line opens a registry node; the `"key" = value` lines that
    follow belong to it until the next node. Host controllers and the root
    have no idVendor/idProduct and are skipped. Unknown or malformed lines are
    ignored rather than raising, so a changed ioreg format degrades to "fewer
    devices found" (and the JTAG detect remains the source of truth).
    """
    nodes: list[dict[str, str | int | None]] = []
    current: dict[str, str | int | None] | None = None
    for line in text.splitlines():
        m = _NODE_RE.search(line)
        if m:
            current = {"__name": m["name"], "__class": m["cls"].strip()}
            nodes.append(current)
            continue
        if current is None:
            continue
        p = _PROP_RE.match(line)
        if p:
            current[p["key"]] = _parse_value(p["value"])

    devices: list[UsbDevice] = []
    seen: set[tuple] = set()
    for n in nodes:
        vid, pid = n.get("idVendor"), n.get("idProduct")
        if not isinstance(vid, int) or not isinstance(pid, int):
            continue
        product = next((n[k] for k in _PRODUCT_KEYS if isinstance(n.get(k), str)), None)
        if product is None:
            name = n.get("__name")
            product = str(name).split("@")[0] if name else None
        serial = next((n[k] for k in _SERIAL_KEYS if isinstance(n.get(k), str)), None)
        loc = n.get("locationID")
        location = f"0x{loc:08x}" if isinstance(loc, int) else None
        key = (vid, pid, serial, location)
        if key in seen:
            continue
        seen.add(key)
        devices.append(UsbDevice(vendor_id=vid, product_id=pid, product=product, serial=serial, location=location))
    return devices


def find_ftdi(devices: list[UsbDevice], vendor_id: int = 0x0403, product_id: int = 0x6010) -> list[UsbDevice]:
    """Candidate Basys 3 USB-JTAG adapters (FTDI FT2232H 0403:6010 by default)."""
    return [d for d in devices if d.vendor_id == vendor_id and d.product_id == product_id]


def signature(devices: list[UsbDevice]) -> tuple:
    """Hashable identity of a device set; used to invalidate a cached JTAG detect on re-plug."""
    return tuple(sorted((d.vendor_id, d.product_id, d.serial or "", d.location or "") for d in devices))


async def scan_usb(timeout_s: float = 10.0) -> UsbScan:
    """Enumerate USB devices with ioreg. Never opens a device."""
    ioreg = _ioreg_bin()
    if not ioreg:
        return UsbScan(ok=False, error="ioreg not found (macOS only)")
    r = await run_command([ioreg, *IOREG_ARGV_TAIL], timeout_s=timeout_s, max_lines=1_000_000)
    if not r.ok:
        return UsbScan(ok=False, error=(r.error or r.text[-300:] or f"ioreg exited with {r.exit_code}"),
                       duration_s=r.duration_s)
    return UsbScan(ok=True, devices=parse_ioreg(r.text), duration_s=r.duration_s)
