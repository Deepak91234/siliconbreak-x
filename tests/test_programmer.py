"""Programmer subsystem: output classification, refusal paths, xvcd safety, live no-board detection."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys

import pytest

from backend.programmer import hwserver, manager as mgr, openfpgaloader as ofl, usb, xvc

# Captured verbatim on this Mac (openFPGALoader v1.1.1, Basys 3 not connected).
REAL_NO_BOARD = """unable to open ftdi device: -3 (device not found)
JTAG init failed with: unable to open ftdi device
"""

# SYNTHETIC examples in openFPGALoader's documented output format (no board was available to capture them).
SYNTH_DETECT_OK = """Jtag frequency : requested 6.00MHz   -> real 6.00MHz
index 0:
	idcode 0x362d093
	manufacturer xilinx
	family artix a7 35t
	model  xc7a35
	irlength 6
"""
SYNTH_BUSY = "unable to open ftdi device: -5 (unable to claim usb device. Make sure the default FTDI driver is not in use)\nJTAG init failed with: unable to open ftdi device\n"
SYNTH_OTHER_FPGA = "index 0:\n\tidcode 0x13631093\n\tmanufacturer xilinx\n\tfamily artix a7 100t\n\tmodel  xc7a100\n"
SYNTH_PROGRAM_OK = "Jtag frequency : requested 10.00MHz  -> real 10.00MHz\nOpen file DONE\nParse file DONE\nload program\nLoad SRAM: [==================================================] 100.00%\nDone\n"


def test_classify_real_no_board():
    assert ofl.classify(REAL_NO_BOARD, 1)[0] == "no_board"


@pytest.mark.parametrize("text,code,state", [
    (SYNTH_DETECT_OK, 0, "connected"),
    (SYNTH_DETECT_OK.replace("0x362d093", "0x1362d093"), 0, "connected"),  # different silicon revision nibble
    (SYNTH_BUSY, 1, "busy"),
    (SYNTH_OTHER_FPGA, 0, "unsupported"),
    ("", 1, "error"),
    (SYNTH_DETECT_OK, 1, "error"),
])
def test_classify(text, code, state):
    assert ofl.classify(text, code)[0] == state


def test_idcode_mask():
    assert ofl.idcode_matches("0x0362D093") and ofl.idcode_matches("0x4362d093")
    assert not ofl.idcode_matches("0x0362d094") and not ofl.idcode_matches(None)


def test_ioreg_parser_finds_ftdi():
    text = """+-o Root  <class IORegistryEntry, id 0x100000100, retain 31>
  +-o AppleT8112USBXHCI@00000000  <class AppleT8112USBXHCI, id 0x1000002a5>
    +-o Digilent USB Device@00100000  <class IOUSBHostDevice, id 0x100031b1b, registered>
      |   "idProduct" = 24592
      |   "idVendor" = 1027
      |   "USB Product Name" = "Digilent USB Device"
      |   "USB Serial Number" = "210183A12345"
      |   "locationID" = 1048576
"""  # SYNTHETIC ioreg excerpt (0x6010 = 24592, 0x0403 = 1027)
    devs = usb.parse_ioreg(text)
    assert len(devs) == 1 and devs[0].vid_pid == "0403:6010" and devs[0].serial == "210183A12345"
    assert usb.find_ftdi(devs) == devs


def test_hwserver_parse_and_tcl():
    tcl = hwserver.tcl_script()
    assert "program_hw_devices" in tcl and "localhost:3121" in tcl
    assert "write_cfgmem" not in tcl and "program_hw_cfgmem" not in tcl  # volatile only
    lines = ["CL_HW targets 1", "CL_HW devices xc7a35t_0", "CL_HW idcode 0x0362D093", "CL_HW part xc7a35t",
             "INFO: [Labtools 27-3164] End of startup status: HIGH", "CL_HW done 1", "CL_HW state ok"]  # SYNTHETIC
    m = hwserver.parse_output(lines)
    assert m["state"] == "ok" and ofl.idcode_matches(m["idcode"]) and m["done"] == "1" and m["startup_high"] == "1"


@pytest.fixture
def bitfile(tmp_path):
    p = tmp_path / "design.bit"
    p.write_bytes(b"\x00\x09\x0f\xf0" + b"x" * 64)
    return p, hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def no_tools(monkeypatch):
    """Fail loudly if any refusal path touches a programmer tool."""
    async def boom(*a, **k):
        raise AssertionError("programmer tool must not be invoked")
    monkeypatch.setattr(ofl, "program", boom)
    monkeypatch.setattr(hwserver, "program", boom)


async def test_program_refused_without_verifier(bitfile, no_tools):
    p, sha = bitfile
    r = await mgr.HardwareManager().program(p, sha, approval_id="a_1")
    assert r.state == "refused" and not r.ok


async def test_program_refused_when_not_approved(bitfile, no_tools):
    p, sha = bitfile
    hm = mgr.HardwareManager()

    async def deny(aid, s):
        return False
    hm.set_approval_verifier(deny)
    r = await hm.program(p, sha, approval_id="a_1")
    assert r.state == "refused" and "approved" in r.message


async def test_program_refused_on_hash_mismatch(bitfile, no_tools):
    p, sha = bitfile
    hm = mgr.HardwareManager()

    async def allow(aid, s):
        return True
    hm.set_approval_verifier(allow)
    r = await hm.program(p, "0" * 64, approval_id="a_1")
    assert r.state == "refused" and "mismatch" in r.message


async def test_stop_xvcd_refuses_non_xvcd_process():
    child = subprocess.Popen(["/bin/sleep", "30"])
    try:
        res = await xvc.stop_xvcd(child.pid, refuse_while_launcher=False)
        assert not res["ok"] and "not xvcd" in res["message"]
        assert child.poll() is None, "the sleep process must still be running"
    finally:
        child.kill()
        child.wait()


async def test_stop_xvcd_refuses_self_and_init():
    assert not (await xvc.stop_xvcd(1))["ok"]
    import os
    assert not (await xvc.stop_xvcd(os.getpid()))["ok"]


@pytest.mark.skipif(sys.platform != "darwin", reason="ioreg is macOS-only")
async def test_live_status_is_honest():
    scan = await usb.scan_usb()
    assert scan.ok
    if usb.find_ftdi(scan.devices):
        pytest.skip("an FTDI 0403:6010 device is attached; the no-board assertion does not apply")
    hm = mgr.HardwareManager()
    st = await hm.status()
    assert st.state in ("no_board", "tool_missing")
    if ofl.binary():
        d = await ofl.detect()
        assert d.state == "no_board" and not d.ok


def test_hwserver_no_active_target_is_no_board():
    # Captured verbatim from vivado_container on this Mac (Vivado 2023.2, no board, xvcd not serving).
    lines = ["CL_HW state error",
             'CL_HW message ERROR: [Labtoolstcl 44-494] There is no active target available for server at localhost:3121. |  '
             'Targets(s) ", jsn-XVC-host.docker.internal:2542" may be locked by another hw_server. | ']
    m = hwserver.parse_output(lines)
    assert hwserver._NO_ACTIVE_TARGET.search(m["message"])
