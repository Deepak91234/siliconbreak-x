"""The trusted pin map must match the official Digilent Basys-3-Master.xdc exactly."""

import re

from backend.rtl.wrapper import generate_xdc
from backend.specifications.board import load_board
from backend.specifications.ir import CounterBehavior, DesignSpec, OutputBinding, RateMode

_LINE = re.compile(r"^#?\s*set_property\s+-dict\s+\{\s*PACKAGE_PIN\s+(\S+)\s+IOSTANDARD\s+(\S+)\s*\}\s+\[get_ports\s+\{?([A-Za-z]+)(?:\[(\d+)\])?\}?\]")


def _official_pins():
    board = load_board("basys3")
    pins = {}
    for line in board.source_xdc.read_text().splitlines():
        m = _LINE.match(line.strip())
        if m:
            pin, iostd, name, idx = m.groups()
            pins[(name, int(idx) if idx is not None else None)] = (pin, iostd)
    return pins


def test_master_xdc_checksum():
    assert load_board("basys3").verify_source_xdc()


def test_every_board_pin_matches_official_xdc():
    board = load_board("basys3")
    official = _official_pins()
    checked = 0
    for name, port in board.ports.items():
        for n, pin in enumerate(port.pins):
            key = (name, n if port.is_vector else None)
            assert key in official, f"{key} missing from official XDC"
            assert official[key] == (pin, board.iostandard), f"{key}: board.json {pin} vs official {official[key]}"
            checked += 1
    assert checked == 1 + 16 + 5 + 16 + 7 + 1 + 4


def test_generated_xdc_only_uses_trusted_pins():
    spec = DesignSpec(
        title="t",
        outputs=[OutputBinding(name="count", indices=[0, 1])],
        behavior=CounterBehavior(output="count", rate_modes=[RateMode(name="normal", hz=1)]),
    )
    xdc = generate_xdc(spec)
    official = _official_pins()
    allowed = {(p, s) for p, s in official.values()}
    found = re.findall(r"PACKAGE_PIN (\S+) IOSTANDARD (\S+)", xdc)
    assert len(found) == 50
    assert all(f in allowed for f in found)
    assert "create_clock -add -name sys_clk_pin -period 10.00" in xdc
    assert "set_property CONFIG_VOLTAGE 3.3 [current_design]" in xdc
